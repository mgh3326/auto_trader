"""ROB-1271 (J6C) — Upbit shadow / Binance Futures Demo boundary regressions.

Negative-only regression suite for the two crypto lanes this job owns:

  * ``crypto.upbit.shadow``       — synthetic ``SHADOW_ONLY``; broker mutation
    is structurally rejected, and the lane surface never reaches Upbit's
    private account/order/credential module.
  * ``crypto.binance.futures_demo`` — ``DISABLED_NO_STRATEGY``; the default
    plugin is ``NullStrategy``, no recurring registration reaches the loop or
    the futures execution client, and the 1x / one-way / reduceOnly / leg-cap
    invariants stay exactly where they are.

Plus the two cross-lane facts ROB-1271 pins so nobody "tidies" them away:

  * the Spot/Futures ``cancel_order`` per-call ``confirm`` **asymmetry**, and
  * finding F-1 — both Demo runbooks claim their smoke CLI is the *only* path
    producing real Demo orders, and neither claim holds: Spot is reached by a
    schedule-bearing TaskIQ task, and Futures has a second operator CLI. The
    scheduler half of the futures sentence *is* accurate, and the tests keep the
    two halves apart (see ``docs/contracts/rob-1271-upbit-futures-boundary.md``).

Every assertion here is offline: expectations are written as literals in this
file (never read back out of the constant under test), no test opens a socket,
touches a database, or constructs a scheduler. Source-reachability facts are
established by parsing files with :mod:`ast`, not by importing them.
"""

from __future__ import annotations

import ast
import functools
import inspect
import re
from dataclasses import fields, replace
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from app.schemas.execution_contracts import LaneStatus, SchedulerOwner
from app.services import mock_lane_registry as registry
from app.services.brokers.binance.demo_strategy_loop.kill_switch import (
    LOCKED_LIMITS,
    KillSwitchLimitsNotLocked,
    StrategyLoopKillSwitchLimits,
    assert_kill_switch_limits_locked,
)
from app.services.brokers.binance.demo_strategy_loop.sizing import (
    LEG_NOTIONAL_CAP_MAX_USDT,
    LEG_NOTIONAL_CAP_MIN_USDT,
)
from app.services.brokers.binance.demo_strategy_loop.strategy import NullStrategy
from app.services.brokers.binance.futures_demo.errors import (
    BinanceFuturesDemoHedgeModeBlocked,
    BinanceFuturesDemoLeverageMismatch,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
    FuturesDemoDryRunResult,
)
from app.services.brokers.binance.futures_demo.host_allowlist import (
    FUTURES_DEMO_HOSTS,
)
from app.services.mock_integration.lineage import (
    DecisionIntentDraft,
    ExecutionPlanDraft,
    LineageEnvelope,
    MockLineageFactory,
)

REPO_ROOT = Path(__file__).resolve().parents[2]

UPBIT_SHADOW = "crypto.upbit.shadow"
FUTURES_DEMO = "crypto.binance.futures_demo"

_SPOT_EXECUTION_CLIENT = "app.services.brokers.binance.spot_demo.execution_client"
_FUTURES_EXECUTION_CLIENT = "app.services.brokers.binance.futures_demo.execution_client"
_STRATEGY_LOOP_PACKAGE = "app.services.brokers.binance.demo_strategy_loop"
_UPBIT_PRIVATE_WEBSOCKET = "app.services.upbit_websocket"

# Lifecycle-recovery surfaces named by contract §5.2 (C3-1 … C3-6).
_RECONCILE_JOB = "app.jobs.binance_demo_root_reservation_reconciliation"
_RECONCILE_TASK = "app.tasks.binance_demo_root_reservation_reconcile_tasks"
_STRATEGY_LOOP_EXECUTION = f"{_STRATEGY_LOOP_PACKAGE}.execution"
_DEMO_LEDGER_SERVICE = "app.services.brokers.binance.demo.ledger.service"
_DEMO_LEDGER_MODEL = "app.models.binance_demo_order_ledger"

# Scheduler entry points: TaskIQ task modules, Prefect flow modules, and the
# job modules they delegate to. A recurring registration can only originate
# from one of these directories in this repository.
_SCHEDULER_ENTRYPOINT_DIRS = ("app/tasks", "app/flows")


# ---------------------------------------------------------------------------
# Static first-party import graph (no runtime import of the scanned modules).
# ---------------------------------------------------------------------------
def _module_file(module: str) -> Path | None:
    parts = module.split(".")
    module_file = REPO_ROOT.joinpath(*parts).with_suffix(".py")
    if module_file.is_file():
        return module_file
    package_file = REPO_ROOT.joinpath(*parts, "__init__.py")
    if package_file.is_file():
        return package_file
    return None


def _module_name(path: Path) -> str:
    parts = list(path.relative_to(REPO_ROOT).parts)
    if parts[-1] == "__init__.py":
        parts = parts[:-1]
    else:
        parts[-1] = parts[-1].removesuffix(".py")
    return ".".join(parts)


def _parse(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


@functools.cache
def _direct_imports(module: str) -> frozenset[str]:
    """Absolute first-party module names imported by ``module``.

    ``from X import Y`` contributes both ``X`` and ``X.Y`` so a submodule
    import is visible even when ``Y`` is only a name inside ``X``.
    """
    path = _module_file(module)
    if path is None:
        return frozenset()
    package = module if path.name == "__init__.py" else module.rsplit(".", 1)[0]
    found: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            found.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.level:
                base = package.split(".")
                if node.level > 1:
                    base = base[: len(base) - (node.level - 1)]
                target = ".".join(base + ([node.module] if node.module else []))
            else:
                target = node.module or ""
            if not target:
                continue
            found.add(target)
            found.update(f"{target}.{alias.name}" for alias in node.names)
    return frozenset(
        name
        for name in found
        if name.split(".")[0] in {"app", "scripts", "research"}
        and _module_file(name) is not None
    )


@functools.cache
def _reachable_modules(root: str) -> frozenset[str]:
    seen: set[str] = set()
    pending = [root]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        pending.extend(name for name in _direct_imports(current) if name not in seen)
    return frozenset(seen)


def _scheduler_entrypoint_modules() -> tuple[str, ...]:
    # ``rglob`` rather than ``glob``: both directories are flat today, so this
    # changes no current reach set, but a task added under a future
    # ``app/tasks/<subpackage>/`` would otherwise be invisible to every
    # reachability assertion in this file.
    modules: list[str] = []
    for directory in _SCHEDULER_ENTRYPOINT_DIRS:
        for path in sorted((REPO_ROOT / directory).rglob("*.py")):
            if path.name == "__init__.py":
                continue
            modules.append(_module_name(path))
    return tuple(modules)


def _entrypoints_reaching(target: str) -> frozenset[str]:
    return frozenset(
        module
        for module in _scheduler_entrypoint_modules()
        if target in _reachable_modules(module)
    )


def _broker_task_keywords(module: str, function_name: str) -> frozenset[str]:
    """Keyword names on the ``@broker.task(...)`` decorator of ``function_name``."""
    path = _module_file(module)
    assert path is not None, f"{module} has no source file"
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if node.name != function_name:
            continue
        for decorator in node.decorator_list:
            if not isinstance(decorator, ast.Call):
                continue
            func = decorator.func
            if isinstance(func, ast.Attribute) and func.attr == "task":
                return frozenset(
                    keyword.arg
                    for keyword in decorator.keywords
                    if keyword.arg is not None
                )
    raise AssertionError(f"no @broker.task decorator found on {module}:{function_name}")


def _called_attribute_names(modules: tuple[str, ...]) -> frozenset[str]:
    names: set[str] = set()
    for module in modules:
        path = _module_file(module)
        assert path is not None, f"{module} has no source file"
        for node in ast.walk(_parse(path)):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                names.add(node.func.attr)
    return frozenset(names)


def _function_node(
    module: str, function_name: str
) -> ast.AsyncFunctionDef | ast.FunctionDef:
    path = _module_file(module)
    assert path is not None, f"{module} has no source file"
    for node in ast.walk(_parse(path)):
        if (
            isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
            and node.name == function_name
        ):
            return node
    raise AssertionError(f"{module} defines no {function_name}")


def _function_body_source(module: str, function_name: str) -> str:
    """Function body rendered back to source, **docstring dropped**.

    Prose must never be able to satisfy a source pin, so the leading string
    expression is stripped before the body is unparsed.
    """
    body = _function_node(module, function_name).body
    if (
        body
        and isinstance(body[0], ast.Expr)
        and isinstance(body[0].value, ast.Constant)
        and isinstance(body[0].value.value, str)
    ):
        body = body[1:]
    return "\n".join(ast.unparse(statement) for statement in body)


def _class_node(module: str, class_name: str) -> ast.ClassDef:
    path = _module_file(module)
    assert path is not None, f"{module} has no source file"
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.ClassDef) and node.name == class_name:
            return node
    raise AssertionError(f"{module} defines no class {class_name}")


def _method_names(module: str, class_name: str, prefix: str) -> frozenset[str]:
    return frozenset(
        statement.name
        for statement in _class_node(module, class_name).body
        if isinstance(statement, ast.AsyncFunctionDef | ast.FunctionDef)
        and statement.name.startswith(prefix)
    )


def _method_parameter_names(
    module: str, class_name: str, method_name: str
) -> frozenset[str]:
    for statement in _class_node(module, class_name).body:
        if (
            isinstance(statement, ast.AsyncFunctionDef | ast.FunctionDef)
            and statement.name == method_name
        ):
            args = statement.args
            collected = {
                argument.arg
                for argument in (*args.posonlyargs, *args.args, *args.kwonlyargs)
            }
            if args.vararg is not None:
                collected.add(args.vararg.arg)
            if args.kwarg is not None:
                collected.add(args.kwarg.arg)
            return frozenset(collected)
    raise AssertionError(f"{module}:{class_name} defines no {method_name}")


def _mapped_column_names(module: str, class_name: str) -> frozenset[str]:
    """Annotated ``mapped_column`` attribute names declared on ``class_name``."""
    names: set[str] = set()
    for statement in _class_node(module, class_name).body:
        if not isinstance(statement, ast.AnnAssign):
            continue
        if not isinstance(statement.target, ast.Name):
            continue
        value = statement.value
        if (
            isinstance(value, ast.Call)
            and isinstance(value.func, ast.Name)
            and value.func.id == "mapped_column"
        ):
            names.add(statement.target.id)
    return frozenset(names)


def _literal_reason_values(module: str) -> frozenset[str]:
    """String values of every literal ``"reason": "..."`` dict entry in ``module``."""
    path = _module_file(module)
    assert path is not None, f"{module} has no source file"
    values: set[str] = set()
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Dict):
            continue
        for key, value in zip(node.keys, node.values, strict=True):
            if (
                isinstance(key, ast.Constant)
                and key.value == "reason"
                and isinstance(value, ast.Constant)
                and isinstance(value.value, str)
            ):
                values.add(value.value)
    return frozenset(values)


# ---------------------------------------------------------------------------
# Lineage fixtures (mirrors the tests/test_mock_lane_registry.py idiom).
# ---------------------------------------------------------------------------
def _by_id() -> dict[str, registry.LaneRegistryEntry]:
    return {entry.lane_id: entry for entry in registry.CANONICAL_LANE_REGISTRY}


def _shadow_envelope() -> LineageEnvelope:
    entry = _by_id()[UPBIT_SHADOW]
    factory = MockLineageFactory()
    intent = factory.create_decision_intent(
        DecisionIntentDraft(
            policy_version="rob-1271-test-policy",
            policy_version_hash="rob-1271-test-policy-hash",
            decision_timestamp=datetime(2026, 8, 17, 0, 0, tzinfo=UTC),
            market_data_cutoff=datetime(2026, 8, 16, 23, 59, tzinfo=UTC),
            symbol="KRW-BTC",
            side="buy",
            target_notional=Decimal("1"),
            target_notional_currency=entry.quote_currency,
            limit_policy={"order_type": "limit"},
            expiry_policy={"kind": "day"},
            rationale="rob-1271 shadow boundary regression",
        )
    )
    return factory.create_plan_envelope(
        intent,
        ExecutionPlanDraft(
            lane_id=UPBIT_SHADOW,
            broker=entry.broker,
            account_profile=entry.account_profile,
            account_mode=entry.account_mode.value,
            normalized_symbol="KRW-BTC",
            quantity=Decimal("1"),
            limit_price=Decimal("1"),
            quote_currency=entry.quote_currency,
            tick_rounding={"increment": "1"},
            session="regular",
            time_in_force="day",
            min_order_validation={"quote_required": True},
            risk_caps={"max_notional": "1"},
        ),
    )


def _policy_bound_shadow_snapshot(
    envelope: LineageEnvelope,
) -> tuple[registry.LaneRegistryEntry, ...]:
    """Registry snapshot whose shadow row carries a policy binding.

    The canonical shadow row has ``policy_binding=None`` and still lists
    ``MissingBinding.POLICY``, so ``guarded_broker_io`` fails at
    ``lane_binding_incomplete`` long before the shadow-specific gate. Granting
    the strongest upstream weakening that the registry's own startup validation
    still accepts lets the chain actually *reach*
    ``assert_mock_only_endpoint`` — which is the guard under test.
    """
    shadow = _by_id()[UPBIT_SHADOW]
    bound = replace(
        shadow,
        policy_binding=registry.PolicyBinding(
            envelope.decision_intent.policy_version,
            envelope.decision_intent.policy_version_hash,
        ),
        missing_bindings=tuple(
            binding
            for binding in shadow.missing_bindings
            if binding is not registry.MissingBinding.POLICY
        ),
    )
    return tuple(
        bound if entry.lane_id == UPBIT_SHADOW else entry
        for entry in registry.CANONICAL_LANE_REGISTRY
    )


# ===========================================================================
# U — crypto.upbit.shadow stays synthetic SHADOW_ONLY (ROB-1271 output 1)
# ===========================================================================
def test_upbit_shadow_row_is_frozen_on_all_four_axes() -> None:
    """Literal 4-axis pin. ``scheduler_owner`` is absent, not ``DISABLED``."""
    shadow = registry.get_lane_registry_entry(UPBIT_SHADOW)

    assert shadow.role is registry.RegistryRole.SHADOW_ONLY
    assert shadow.lane_status is LaneStatus.SHADOW_ONLY
    assert shadow.activation_status is registry.ActivationStatus.DISABLED
    # Owner absent. This is NOT a spelling of SchedulerOwner.DISABLED: the two
    # values carry different meanings and must never collapse into one branch.
    assert shadow.scheduler_owner is None
    assert shadow.scheduler_owner is not SchedulerOwner.DISABLED
    assert registry.MissingBinding.OWNER in shadow.missing_bindings

    assert shadow.writer is False
    assert shadow.auto_order_enabled is False
    assert shadow.quote_currency == "KRW"


def test_upbit_shadow_is_synthetic_and_never_labelled_broker_paper_or_native() -> None:
    """Mutant 2 — a synthetic shadow row may not be presented as broker paper."""
    shadow = registry.get_lane_registry_entry(UPBIT_SHADOW)

    assert shadow.account_mode is registry.AccountMode.SHADOW
    assert shadow.account_mode is not registry.AccountMode.PAPER
    assert shadow.account_mode is not registry.AccountMode.DEMO
    assert shadow.account_mode is not registry.AccountMode.MOCK
    assert shadow.endpoint_class is registry.EndpointClass.SHADOW
    assert shadow.lane_type is registry.AccountMode.SHADOW

    # A synthetic lane owns no broker host and no credential namespace at all.
    assert shadow.allowed_hosts == ()
    assert shadow.credential_namespace is None
    assert shadow.physical_account_id is None
    assert shadow.identity_status == "UNKNOWN"


@pytest.mark.parametrize(
    "endpoint_url",
    [
        "https://api.upbit.com",
        "https://api.upbit.com/v1/orders",
        "https://demo-api.binance.com",
        "https://paper-api.alpaca.markets",
        "https://mockapi.kiwoom.com",
    ],
)
def test_upbit_shadow_rejects_every_endpoint_it_is_offered(endpoint_url: str) -> None:
    """Rejection is a property of the lane being SHADOW, not of the host string."""
    shadow = registry.get_lane_registry_entry(UPBIT_SHADOW)

    with pytest.raises(registry.LaneGuardError) as excinfo:
        registry.assert_mock_only_endpoint(shadow, endpoint_url)

    assert excinfo.value.code == "shadow_broker_io_forbidden"
    assert excinfo.value.lane_id == UPBIT_SHADOW


@pytest.mark.asyncio
async def test_upbit_shadow_chain_reaches_and_dies_at_the_shadow_guard() -> None:
    """The guard is on the live chain, not merely defined and exported.

    A direct ``assert_mock_only_endpoint`` call proves the function rejects; it
    does not prove ``guarded_broker_io`` ever consults it. This drives the real
    chain with a snapshot engineered to survive every *earlier* guard, so the
    kill can only come from the shadow gate itself.
    """
    envelope = _shadow_envelope()
    snapshot = _policy_bound_shadow_snapshot(envelope)
    broker_calls = 0

    async def broker_io() -> None:
        nonlocal broker_calls
        broker_calls += 1

    with pytest.raises(registry.LaneGuardError) as excinfo:
        await registry.guarded_broker_io(
            envelope,
            endpoint_url="https://api.upbit.com",
            credential_namespace="UPBIT_*",
            broker_io=broker_io,
            registry=snapshot,
        )

    assert excinfo.value.code == "shadow_broker_io_forbidden"
    assert broker_calls == 0


@pytest.mark.asyncio
async def test_upbit_shadow_chain_under_the_canonical_registry_dies_even_earlier() -> (
    None
):
    """Honest record of which guard actually fires with the signed registry.

    Under ``CANONICAL_LANE_REGISTRY`` the shadow row has no policy binding, so
    the chain stops at ``lane_binding_incomplete`` and never reaches the
    shadow-specific gate. Both facts are pinned so a future reader cannot
    mistake the previous test's fixture for canonical behaviour.
    """
    envelope = _shadow_envelope()
    broker_calls = 0

    async def broker_io() -> None:
        nonlocal broker_calls
        broker_calls += 1

    with pytest.raises(registry.LaneGuardError) as excinfo:
        await registry.guarded_broker_io(
            envelope,
            endpoint_url="https://api.upbit.com",
            credential_namespace="UPBIT_*",
            broker_io=broker_io,
        )

    assert excinfo.value.code == "lane_binding_incomplete"
    assert broker_calls == 0


def test_upbit_shadow_lane_cannot_be_activated() -> None:
    with pytest.raises(registry.ActivationTransitionBlocked) as excinfo:
        registry.transition_activation(
            UPBIT_SHADOW,
            registry.ActivationStatus.DISABLED,
            registry.ActivationStatus.ENABLED,
        )

    assert excinfo.value.code == "lane_signed_restriction_violation"


def test_upbit_shadow_surface_never_reaches_upbit_private_credential_modules() -> None:
    """Mutant 1 — transitive reachability, not just a direct-import check."""
    reachable = _reachable_modules("app.services.mock_lane_registry")

    assert _UPBIT_PRIVATE_WEBSOCKET not in reachable
    assert not [module for module in reachable if "upbit" in module]
    # The private module really is the credentialled one, so the exclusion above
    # is meaningful rather than vacuous.
    private_source = (REPO_ROOT / "app/services/upbit_websocket.py").read_text(
        encoding="utf-8"
    )
    assert "settings.upbit_access_key" in private_source
    assert "settings.upbit_secret_key" in private_source
    assert "wss://api.upbit.com/websocket/v1/private" in private_source


def test_registry_module_imports_no_broker_transport_or_credential_module() -> None:
    forbidden = {
        "jwt",
        "websockets",
        "httpx",
        "app.services.upbit_websocket",
        "app.services.brokers.upbit.client",
        "app.services.brokers.upbit.orders",
        "app.core.config",
    }
    path = Path(registry.__file__)
    imported: set[str] = set()
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imported.add(node.module)

    assert imported.isdisjoint(forbidden)


# ===========================================================================
# F — crypto.binance.futures_demo stays DISABLED_NO_STRATEGY (output 3)
# ===========================================================================
def test_futures_demo_row_is_frozen_on_all_four_axes() -> None:
    """Literal 4-axis pin. Here ``scheduler_owner`` is an explicit ``DISABLED``."""
    futures = registry.get_lane_registry_entry(FUTURES_DEMO)

    assert futures.role is None
    assert futures.role_pending_reason is None
    assert futures.lane_status is LaneStatus.DISABLED_NO_STRATEGY
    assert futures.activation_status is registry.ActivationStatus.DISABLED
    # Explicit disabled owner — the opposite spelling from the shadow row above.
    assert futures.scheduler_owner is SchedulerOwner.DISABLED
    assert futures.scheduler_owner is not None

    assert futures.writer is False
    assert futures.auto_order_enabled is False
    assert futures.quote_currency == "USDT"
    assert futures.allowed_hosts == ("demo-fapi.binance.com",)


def test_absent_and_disabled_scheduler_owners_stay_distinct_values() -> None:
    """Mutant guard against collapsing two different meanings into one branch."""
    shadow = registry.get_lane_registry_entry(UPBIT_SHADOW)
    futures = registry.get_lane_registry_entry(FUTURES_DEMO)

    assert shadow.scheduler_owner is None
    assert futures.scheduler_owner is SchedulerOwner.DISABLED
    assert shadow.scheduler_owner != futures.scheduler_owner


def test_futures_demo_lane_cannot_be_activated() -> None:
    with pytest.raises(registry.ActivationTransitionBlocked) as excinfo:
        registry.transition_activation(
            FUTURES_DEMO,
            registry.ActivationStatus.DISABLED,
            registry.ActivationStatus.ENABLED,
        )

    assert excinfo.value.code == "lane_signed_restriction_violation"


def test_default_strategy_plugin_is_null_strategy_and_never_emits_a_signal() -> None:
    """Mutant 3a — the shipped default must stay the always-``None`` plugin."""
    plugin = NullStrategy()

    assert plugin.strategy_id == "null"
    assert plugin.evaluate({}, decision_ts=0) is None
    assert plugin.evaluate({"XRPUSDT": ()}, decision_ts=1_755_000_000_000) is None


def test_the_strategy_loop_cli_wires_run_tick_to_null_strategy() -> None:
    """The default is what the only entry point actually passes, not just a doc."""
    cli_path = REPO_ROOT / "scripts/binance_demo_strategy_loop.py"
    strategy_arguments: list[str] = []
    for node in ast.walk(_parse(cli_path)):
        if not isinstance(node, ast.Call):
            continue
        for keyword in node.keywords:
            if keyword.arg == "strategy":
                strategy_arguments.append(ast.unparse(keyword.value))

    assert strategy_arguments == ["NullStrategy()"]


def test_no_scheduler_entrypoint_reaches_the_demo_strategy_loop() -> None:
    """Mutant 3b — the loop stays CLI-only; no TaskIQ/Prefect module imports it."""
    for module in _scheduler_entrypoint_modules():
        reachable = _reachable_modules(module)
        assert not [
            name for name in reachable if name.startswith(_STRATEGY_LOOP_PACKAGE)
        ], f"{module} reaches the demo strategy loop"


def test_only_the_scheduleless_reconcile_task_reaches_the_futures_demo_client() -> None:
    """Mutant 3b — futures has exactly one scheduler-side reach, and it is inert."""
    assert _entrypoints_reaching(_FUTURES_EXECUTION_CLIENT) == frozenset(
        {"app.tasks.binance_demo_root_reservation_reconcile_tasks"}
    )

    keywords = _broker_task_keywords(
        "app.tasks.binance_demo_root_reservation_reconcile_tasks",
        "binance_demo_root_reservation_reconcile",
    )
    assert keywords == frozenset({"task_name"})
    assert "schedule" not in keywords


def test_the_reconcile_chain_calls_no_order_producing_client_method() -> None:
    """The one futures-reaching task reads broker truth; it never mutates."""
    chain = (
        "app.tasks.binance_demo_root_reservation_reconcile_tasks",
        "app.jobs.binance_demo_root_reservation_reconciliation",
    )
    called = _called_attribute_names(chain)

    assert "submit_order" not in called
    assert "cancel_order" not in called
    assert "order_test" not in called
    assert "set_leverage" not in called
    # The read-only lookups it does use, pinned so the absence above is not
    # an artefact of the chain having stopped touching the clients entirely.
    assert "get_order_status" in called
    assert "get_order" in called


@pytest.mark.asyncio
@pytest.mark.parametrize("leverage", [0, 2, 3, 5, 10, 125])
async def test_futures_leverage_is_pinned_to_1x_before_any_http(leverage: int) -> None:
    """Mutant 4 — 1x is refused at the adapter boundary, pre-signing."""
    client = BinanceFuturesDemoExecutionClient(
        api_key="rob1271-test-key",
        api_secret="rob1271-test-secret",
    )
    try:
        with pytest.raises(BinanceFuturesDemoLeverageMismatch):
            await client.set_leverage(symbol="XRPUSDT", leverage=leverage)
    finally:
        await client.aclose()


def test_futures_one_way_mode_is_structural_and_hedge_mode_is_blocked() -> None:
    """Mutant 4 — one-way only: no positionSide parameter exists to set."""
    submit_parameters = set(
        inspect.signature(BinanceFuturesDemoExecutionClient.submit_order).parameters
    )
    assert "position_side" not in submit_parameters
    assert "positionSide" not in submit_parameters

    execution_source = (
        REPO_ROOT / "app/services/brokers/binance/demo_strategy_loop/execution.py"
    ).read_text(encoding="utf-8")
    assert "BinanceFuturesDemoHedgeModeBlocked" in execution_source
    assert "if mode_result.is_hedge_mode:" in execution_source
    assert issubclass(BinanceFuturesDemoHedgeModeBlocked, Exception)


@pytest.mark.asyncio
async def test_futures_reduce_only_defaults_off_and_submit_defaults_to_dry_run() -> (
    None
):
    """Mutant 4 — reduceOnly stays an explicit opt-in; submit stays gated."""
    submit_parameters = inspect.signature(
        BinanceFuturesDemoExecutionClient.submit_order
    ).parameters
    assert submit_parameters["reduce_only"].default is False
    assert submit_parameters["confirm"].default is False

    client = BinanceFuturesDemoExecutionClient(
        api_key="rob1271-test-key",
        api_secret="rob1271-test-secret",
    )
    try:
        result = await client.submit_order(
            symbol="XRPUSDT",
            side="BUY",
            order_type="MARKET",
            qty=Decimal("1"),
        )
    finally:
        await client.aclose()

    assert isinstance(result, FuturesDemoDryRunResult)
    assert result.reduce_only is False


def test_the_strategy_loop_close_leg_always_sets_reduce_only_true() -> None:
    """Mutant 4 — the close path may never submit without reduceOnly."""
    execution_source = (
        REPO_ROOT / "app/services/brokers/binance/demo_strategy_loop/execution.py"
    ).read_text(encoding="utf-8")

    assert "reduce_only=True" in execution_source
    assert "_close_with_reduce_only" in execution_source


def test_leg_notional_cap_constants_are_locked_literals() -> None:
    """Mutant 4 — the [6, 10] USDT leg cap is not an operator-tunable dial."""
    assert LEG_NOTIONAL_CAP_MIN_USDT == Decimal("6")
    assert LEG_NOTIONAL_CAP_MAX_USDT == Decimal("10")


def test_kill_switch_locked_limits_are_pinned_to_literal_1_and_2() -> None:
    """Mutant 4 — every kill-switch cap is pinned to a literal written here.

    ``assert_kill_switch_limits_locked`` compares its argument against
    ``LOCKED_LIMITS`` itself, so exercising only the guard has **zero**
    discriminating power over the cap values: widening
    ``LOCKED_LIMITS.max_concurrent_positions`` to 5 moves the expectation with
    the constant and the guard keeps passing. That self-reference is the trap
    the J6B round-1 review named, and it is why the expectations below are
    literals rather than reads of the module under test.
    """
    assert LOCKED_LIMITS.max_concurrent_positions == 1
    assert LOCKED_LIMITS.max_consecutive_stop_losses_per_utc_day == 2

    # Full literal reconstruction: any field drifting in either direction fails
    # here, not just the two named above.
    assert LOCKED_LIMITS == StrategyLoopKillSwitchLimits(
        max_concurrent_positions=1,
        max_consecutive_stop_losses_per_utc_day=2,
    )
    # A *third* field carrying a permissive default would satisfy every
    # assertion above (it would default on both sides of the comparison), so
    # the field set is pinned as a literal too.
    assert {field.name for field in fields(StrategyLoopKillSwitchLimits)} == {
        "max_concurrent_positions",
        "max_consecutive_stop_losses_per_utc_day",
    }


def test_kill_switch_guard_accepts_exactly_the_literal_locked_pair() -> None:
    """The guard must accept ``(1, 2)`` spelled out, not merely accept itself."""
    assert_kill_switch_limits_locked(
        StrategyLoopKillSwitchLimits(
            max_concurrent_positions=1,
            max_consecutive_stop_losses_per_utc_day=2,
        )
    )
    assert isinstance(LOCKED_LIMITS, StrategyLoopKillSwitchLimits)


@pytest.mark.parametrize(
    ("max_concurrent_positions", "max_consecutive_stop_losses_per_utc_day"),
    [
        # Each row widens exactly one cap, with both values written as literals
        # so the deviation cannot be derived from the constant under test.
        (2, 2),
        (5, 2),
        (1, 3),
        (1, 9),
    ],
)
def test_kill_switch_guard_rejects_each_widened_cap_individually(
    max_concurrent_positions: int,
    max_consecutive_stop_losses_per_utc_day: int,
) -> None:
    """Mutant 4 — widening either cap alone is refused, field by field."""
    widened = StrategyLoopKillSwitchLimits(
        max_concurrent_positions=max_concurrent_positions,
        max_consecutive_stop_losses_per_utc_day=max_consecutive_stop_losses_per_utc_day,
    )

    with pytest.raises(KillSwitchLimitsNotLocked):
        assert_kill_switch_limits_locked(widened)


def test_futures_demo_host_allowlist_stays_a_single_demo_host() -> None:
    assert FUTURES_DEMO_HOSTS == frozenset({"demo-fapi.binance.com"})


# ===========================================================================
# L — lane-native lifecycle-recovery evidence (contract §83 correction 3)
# ===========================================================================
@pytest.mark.parametrize("lane_id", [UPBIT_SHADOW, FUTURES_DEMO])
def test_auto_ready_blocked_by_lifecycle_is_unreachable_for_both_lanes(
    lane_id: str,
) -> None:
    """Neither lane can *compute* ``AUTO_READY_BLOCKED_BY_LIFECYCLE``.

    The signed status allowlist admits exactly ``SHADOW_ONLY`` for the Upbit row
    and exactly ``DISABLED_NO_STRATEGY`` for the futures row. Both frozen values
    are strictly more restrictive than the lifecycle-blocked status, so claiming
    the latter would assert a state no code path can produce. The contract
    records the unmet lifecycle prerequisites instead of relabelling the lane.
    """
    rows = tuple(
        replace(
            entry,
            lane_status=LaneStatus.AUTO_READY_BLOCKED_BY_LIFECYCLE,
            activation_reason="rob-1271 lifecycle-status probe",
        )
        if entry.lane_id == lane_id
        else entry
        for entry in registry.CANONICAL_LANE_REGISTRY
    )

    with pytest.raises(registry.RegistryStartupError) as excinfo:
        registry.assert_registry_startup(rows, require_canonical=True)

    assert "lane_signed_restriction_violation" in {
        issue.code for issue in excinfo.value.issues
    }


def test_neither_lane_calls_the_j3a_release_if_matches_contract() -> None:
    """C3-5 evidence — the exact release condition is absent from both lanes.

    ``mock_integration.coordination.release_if_matches`` is the J3A durable-claim
    release contract. No module in either lane's own surface calls it: the
    futures lane releases through its own
    ``BinanceDemoLedgerService.record_cancelled``, and the Upbit lane has no
    durable-claim surface at all. Recorded, not repaired.

    This is deliberately a call-site check rather than a module-reachability
    check: the strategy loop's forecast/journal dependency drags the KIS mock
    runner (and therefore the coordination module) into its import graph, which
    says nothing about whether this lane uses the release contract.
    """
    lane_sources = [
        *(REPO_ROOT / "app/services/brokers/binance/futures_demo").glob("*.py"),
        *(REPO_ROOT / "app/services/brokers/binance/demo_strategy_loop").glob("*.py"),
        REPO_ROOT / "app/jobs/binance_demo_root_reservation_reconciliation.py",
        REPO_ROOT / "app/tasks/binance_demo_root_reservation_reconcile_tasks.py",
        REPO_ROOT / "app/services/mock_lane_registry.py",
    ]
    offenders = [
        path.relative_to(REPO_ROOT).as_posix()
        for path in lane_sources
        if "release_if_matches" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []
    # The contract really does exist elsewhere, so the absence above is a fact
    # about these lanes rather than about the repository.
    coordination_source = (
        REPO_ROOT / "app/services/mock_integration/coordination.py"
    ).read_text(encoding="utf-8")
    assert "async def release_if_matches(" in coordination_source


def test_c3_1_futures_recovery_ownership_is_split_across_two_modules() -> None:
    """C3-1 — "exactly one recovery owner" is UNMET, and the failure is pinned.

    Two modules own different recovery phases and no contract names one: the
    reconciliation job owns pre-acknowledgement roots, the strategy loop owns
    the in-tick close. §5.2 reports that as a failed prerequisite instead of
    picking a winner — so the *failure* needs a regression too. If a later
    change consolidates ownership into a single module, this goes red and the
    contract row must be revisited rather than silently rotting.
    """
    assert (
        _function_node(_RECONCILE_JOB, "reconcile_binance_demo_root_reservations").name
        == "reconcile_binance_demo_root_reservations"
    )
    assert (
        _function_node(_STRATEGY_LOOP_EXECUTION, "_close_with_reduce_only").name
        == "_close_with_reduce_only"
    )

    # Neither phase owner delegates to the other, which is precisely why a
    # single owner cannot be named.
    assert _STRATEGY_LOOP_EXECUTION not in _reachable_modules(_RECONCILE_JOB)
    assert _RECONCILE_JOB not in _reachable_modules(_STRATEGY_LOOP_EXECUTION)


def test_c3_2_futures_restart_trigger_rediscovers_only_pre_ack_roots() -> None:
    """C3-2 — the durable-claim rediscovery predicate, pinned to its literals."""
    predicate = _function_body_source(_RECONCILE_JOB, "_candidate_where_clauses")

    assert "planned_at <= stale_before" in predicate
    assert "lifecycle_state == 'planned'" in predicate
    assert "'previewed', 'validated'" in predicate
    assert "broker_order_id.is_(None)" in predicate
    # Acknowledged roots carry broker evidence and are deliberately out of the
    # restart sweep's scope; widening it to them would need a contract change.
    assert "'submitted'" not in predicate
    assert "'filled'" not in predicate
    assert "'anomaly'" not in predicate


def test_c3_3_futures_authoritative_readback_is_get_order() -> None:
    """C3-3 — the readback operation is named, not merely implied by a call set."""
    lookup = _function_body_source(_RECONCILE_JOB, "_lookup_order")

    assert "product == 'spot'" in lookup
    assert "client.get_order(symbol=symbol, client_order_id=cid)" in lookup
    assert "client.get_order_status(symbol=symbol, client_order_id=cid)" in lookup
    # The operation the futures branch dispatches actually exists on the client.
    assert hasattr(BinanceFuturesDemoExecutionClient, "get_order")


def test_c3_4_futures_lane_evidence_has_no_reject_expiry_or_partial_fill_writer() -> (
    None
):
    """C3-4 — 4 of 7 kinds present, partial fill degraded, reject/expiry absent."""
    record_methods = _method_names(
        _DEMO_LEDGER_SERVICE, "BinanceDemoLedgerService", "record_"
    )

    assert record_methods == frozenset(
        {
            "record_planned",
            "record_previewed",
            "record_validated",
            "record_submitted",
            "record_filled",
            "record_closed",
            "record_cancelled",
            "record_reconciled",
            "record_anomaly",
        }
    )
    # reject and expiry have no dedicated write point; both collapse into the
    # cancelled / anomaly branches, which also absorb unrelated causes.
    assert "record_rejected" not in record_methods
    assert "record_expired" not in record_methods

    # Partial fill is degraded rather than recorded: the fill writer takes no
    # quantity argument, and the ledger has no column to hold one.
    assert _method_parameter_names(
        _DEMO_LEDGER_SERVICE, "BinanceDemoLedgerService", "record_filled"
    ) == frozenset({"self", "client_order_id", "now", "extra_metadata_merge"})
    columns = _mapped_column_names(_DEMO_LEDGER_MODEL, "BinanceDemoOrderLedger")
    assert "executed_qty" not in columns
    assert "remaining_qty" not in columns
    assert "qty" in columns  # planned quantity, set at insert and never split


def test_c3_6_futures_blocked_state_reasons_are_pinned_as_a_closed_set() -> None:
    """C3-6 — the operator-visible ``kept`` reasons are a closed literal set.

    Pinned as an equality rather than a membership sweep so that adding an
    eleventh reason fails here, instead of leaving the contract's enumeration
    stale. That is not hypothetical: the round-1 contract enumerated eight, and
    this assertion is what measured the real count at ten.
    """
    assert _literal_reason_values(_RECONCILE_JOB) == frozenset(
        {
            "client_unavailable",
            "venue_host_mismatch",
            "credential_fingerprint_missing",
            "client_credential_fingerprint_unavailable",
            "credential_fingerprint_mismatch",
            "broker_lookup_failed",
            "malformed_broker_truth",
            "broker_identity_mismatch",
            "broker_exposure_not_disproven",
            "broker_lookup_retention_exceeded",
        }
    )
    # The lifecycle-state half of the blocked surface: anomaly has a writer.
    assert "record_anomaly" in _method_names(
        _DEMO_LEDGER_SERVICE, "BinanceDemoLedgerService", "record_"
    )


# ===========================================================================
# A — Spot/Futures cancel-confirm asymmetry (output 3, mutant 5)
# ===========================================================================
def _spot_cancel_signature() -> inspect.Signature:
    # Imported lazily so this module's import graph keeps the Spot execution
    # client out of the Upbit/Futures sections above.
    from app.services.brokers.binance.spot_demo.execution_client import (
        BinanceSpotDemoExecutionClient,
    )

    return inspect.signature(BinanceSpotDemoExecutionClient.cancel_order)


def test_spot_cancel_has_a_per_call_confirm_gate() -> None:
    parameters = _spot_cancel_signature().parameters

    assert "confirm" in parameters
    assert parameters["confirm"].default is False


def test_futures_cancel_has_no_confirm_gate() -> None:
    parameters = inspect.signature(
        BinanceFuturesDemoExecutionClient.cancel_order
    ).parameters

    assert "confirm" not in parameters
    assert set(parameters) == {"self", "symbol", "client_order_id"}


def test_the_cancel_confirm_asymmetry_is_declared_intent_not_an_oversight() -> None:
    """Mutant 5 — ROB-1271 records the asymmetry; it does not resolve it.

    Unifying either direction (adding ``confirm`` to the futures cancel, or
    dropping it from the spot cancel) is out of scope for this job and must
    fail here rather than land silently.
    """
    raw_doc = inspect.getdoc(BinanceFuturesDemoExecutionClient.cancel_order) or ""
    futures_doc = " ".join(raw_doc.split())

    assert "There is no dry-run gate on cancel" in futures_doc
    assert "the operator has already committed to running against the broker" in (
        futures_doc
    )


def test_both_submit_paths_keep_the_confirm_gate_they_do_share() -> None:
    """The asymmetry is cancel-only; submit is gated on both lanes."""
    from app.services.brokers.binance.spot_demo.execution_client import (
        BinanceSpotDemoExecutionClient,
    )

    spot_submit = inspect.signature(BinanceSpotDemoExecutionClient.submit_order)
    futures_submit = inspect.signature(BinanceFuturesDemoExecutionClient.submit_order)

    assert spot_submit.parameters["confirm"].default is False
    assert futures_submit.parameters["confirm"].default is False


# ===========================================================================
# S — Spot Demo TaskIQ reach chain vs. runbook (ROB-1271 output 2)
# ===========================================================================
def test_the_spot_demo_client_is_reached_by_exactly_two_scheduler_entrypoints() -> None:
    """Pins the real reach set; Futures has one, Spot has two."""
    assert _entrypoints_reaching(_SPOT_EXECUTION_CLIENT) == frozenset(
        {
            "app.tasks.binance_demo_root_reservation_reconcile_tasks",
            "app.tasks.paper_cohort_tasks",
        }
    )


def test_the_paper_cohort_task_carries_a_schedule_unlike_the_reconcile_task() -> None:
    """One of the two Spot reaches is schedule-bearing; the other is not."""
    cohort_keywords = _broker_task_keywords(
        "app.tasks.paper_cohort_tasks", "run_paper_cohorts"
    )
    reconcile_keywords = _broker_task_keywords(
        "app.tasks.binance_demo_root_reservation_reconcile_tasks",
        "binance_demo_root_reservation_reconcile",
    )

    assert "schedule" in cohort_keywords
    assert "schedule" not in reconcile_keywords


def test_spot_demo_runbook_no_scheduler_claim_is_contradicted_by_repo_fact() -> None:
    """ROB-1271 finding F-1 — pinned, not fixed.

    ``docs/runbooks/binance-spot-demo-smoke.md`` states that no TaskIQ wiring
    touches the Spot Demo execution client and that the smoke CLI is the only
    path producing real Demo orders. Both clauses became inaccurate when the
    ROB-845/849 paper-cohort lane landed: a schedule-bearing TaskIQ task
    transitively reaches the client through the frozen paper adapter, whose
    ``submit`` calls the executor with ``confirm=True``.

    J6C owns neither the runbook nor ``app/**``, so this divergence is recorded
    rather than repaired. Resolving it (rewording the doc, or removing the
    reach) must update this test in the same change.
    """
    runbook = (REPO_ROOT / "docs/runbooks/binance-spot-demo-smoke.md").read_text(
        encoding="utf-8"
    )
    assert "No scheduler / TaskIQ / Prefect / cron / Hermes wiring touches the" in (
        runbook
    )
    assert "Spot Demo execution client. The smoke CLI is the **only** path" in runbook

    assert "app.tasks.paper_cohort_tasks" in _entrypoints_reaching(
        _SPOT_EXECUTION_CLIENT
    )
    adapter_source = (
        REPO_ROOT / "app/services/brokers/binance/paper_adapter.py"
    ).read_text(encoding="utf-8")
    assert "confirm=True" in adapter_source


def test_the_futures_runbook_scheduler_clause_matches_repo_fact() -> None:
    """The futures runbook's *first* clause is accurate on the TaskIQ facts.

    A TaskIQ module does reach the futures client, but it is scheduleless and
    calls no order-producing method. That is the half of the sentence the repo
    supports; the "smoke CLI is the only path" half is handled by the test
    below, which contradicts it.
    """
    runbook = (REPO_ROOT / "docs/runbooks/binance-futures-demo-smoke.md").read_text(
        encoding="utf-8"
    )
    assert "No scheduler / TaskIQ / Prefect / cron / Hermes wiring touches the" in (
        runbook
    )

    reaching = _entrypoints_reaching(_FUTURES_EXECUTION_CLIENT)
    assert reaching == frozenset({_RECONCILE_TASK})
    called = _called_attribute_names((_RECONCILE_TASK, _RECONCILE_JOB))
    assert called.isdisjoint({"submit_order", "cancel_order", "order_test"})


def test_futures_runbook_only_path_claim_is_contradicted_by_a_second_cli() -> None:
    """ROB-1271 finding F-1, futures half — pinned, not fixed.

    The futures runbook claims the smoke CLI is the **only** path producing real
    Demo futures orders. A second operator CLI also produces them:
    ``scripts/binance_demo_strategy_loop.py`` passes ``confirm=`` and
    ``signal_override=`` straight into ``run_tick`` (ROB-993 ``--paper-signal``
    is the end-to-end Demo round trip), and ``run_tick`` only short-circuits to
    a dry run when ``confirm`` is false.

    Unlike the scheduler clause this is *not* a scheduler reach — both paths are
    operator-invoked foreground processes — which is why the first clause of the
    sentence survives and this one does not. J6C owns neither
    ``docs/runbooks/**`` nor ``app/**``, so the divergence is recorded and
    pinned; resolving it in either direction must update this test in the same
    change.
    """
    runbook = (REPO_ROOT / "docs/runbooks/binance-futures-demo-smoke.md").read_text(
        encoding="utf-8"
    )
    assert (
        "Futures Demo execution client. The smoke CLI is the **only** path" in runbook
    )

    strategy_loop_cli = REPO_ROOT / "scripts/binance_demo_strategy_loop.py"
    smoke_cli = REPO_ROOT / "scripts/binance_futures_demo_smoke.py"
    assert strategy_loop_cli != smoke_cli
    assert smoke_cli.is_file()  # the CLI the runbook sentence is about

    run_tick_keywords: set[str] = set()
    for node in ast.walk(_parse(strategy_loop_cli)):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "run_tick"
        ):
            run_tick_keywords.update(
                keyword.arg for keyword in node.keywords if keyword.arg is not None
            )
    assert {"confirm", "signal_override"} <= run_tick_keywords

    # ``confirm`` is the real gate on the other side of that call, so the reach
    # above is an order-producing path rather than a permanently inert one.
    orchestrator_source = (
        REPO_ROOT / "app/services/brokers/binance/demo_strategy_loop/orchestrator.py"
    ).read_text(encoding="utf-8")
    assert "if not confirm:" in orchestrator_source
    assert 'blocked_reason="dry_run"' in orchestrator_source

    # Bounding the finding: the second producer is default-disabled by env and
    # its shipped plugin never emits a signal, so only an explicitly injected
    # ``--paper-signal`` reaches the submit path.
    cli_source = strategy_loop_cli.read_text(encoding="utf-8")
    assert "BINANCE_DEMO_STRATEGY_LOOP_ENABLED" in cli_source
    assert "--paper-signal" in cli_source


# ===========================================================================
# M — the contract's own assertion↔regression map cannot rot
# ===========================================================================
def test_the_contract_assertion_map_matches_this_modules_tests_exactly() -> None:
    """§8 of the contract maps every safety assertion to its regression.

    A map is only worth having if it cannot drift. Two ways it could: cite a
    test that was renamed or deleted (the map claims enforcement that no longer
    exists), or omit a test that was added (the contract understates what is
    enforced, and the next reader cannot tell record-only prose from a pinned
    invariant). Both are failures of the same kind the map exists to prevent, so
    the citation set and the test set are asserted **equal**, not merely
    overlapping.
    """
    contract = (
        REPO_ROOT / "docs/contracts/rob-1271-upbit-futures-boundary.md"
    ).read_text(encoding="utf-8")
    cited = set(re.findall(r"`(test_[a-z0-9_]+)`", contract))

    defined = {
        node.name
        for node in ast.parse(Path(__file__).read_text(encoding="utf-8")).body
        if isinstance(node, ast.AsyncFunctionDef | ast.FunctionDef)
        and node.name.startswith("test_")
    }

    assert cited == defined
    # Non-vacuity: an empty regex match on both sides would satisfy equality.
    assert len(defined) >= 40
