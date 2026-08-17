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
    futures sentence's scheduler half is **also** inaccurate as written — a
    TaskIQ module does reach the client — and what survives is only the narrower
    fact that the reach is scheduleless and calls no order-producing method. The
    tests keep the two halves apart at that strength and no stronger (see
    ``docs/contracts/rob-1271-upbit-futures-boundary.md`` §4.1).

Every assertion here is offline: expectations are written as literals in this
file (never read back out of the constant under test), no test opens a socket,
touches a database, or constructs a scheduler. Source-reachability facts are
established by parsing files with :mod:`ast`, not by importing them.

Round-3 note on what "pinned" has to mean here. An independent verifier showed
that seven of these claims survived mutants that made them false, because the
assertions checked *shape* instead of *meaning*: a substring anywhere in a file
instead of the argument at the call site, a keyword's name instead of its value,
a function's name instead of whether its body still reaches its broker call, a
list of forbidden strings instead of a closed equality, a parameter list instead
of where the parameter's value lands, and a bag of literals instead of the
key/value pairing that gives them their meaning. The helpers below exist to make
those the *default* shape of an assertion in this module — see
``_keyword_values`` (values, not names), ``_reaches_call`` (behaviour, not
existence), and ``_kept_outcome_reasons`` (pairing, not co-presence).
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
_STRATEGY_LOOP_CLI = "scripts.binance_demo_strategy_loop"
_FUTURES_SMOKE_CLI = "scripts.binance_futures_demo_smoke"
_DEMO_LEDGER_SERVICE = "app.services.brokers.binance.demo.ledger.service"
_DEMO_LEDGER_REPOSITORY = "app.services.brokers.binance.demo.ledger.repository"
_DEMO_LEDGER_MODEL = "app.models.binance_demo_order_ledger"

# Scheduler entry points: TaskIQ task modules, Prefect flow modules, and the
# job modules they delegate to. A recurring registration can only originate
# from one of these directories in this repository.
_SCHEDULER_ENTRYPOINT_DIRS = ("app/tasks", "app/flows")

# Every first-party source root, so a claim of the form "X is the only Y" can be
# checked against the whole repository instead of against the one file the claim
# happens to name. ``tests/`` is deliberately excluded: a test exercising the
# strategy loop is not an operator entry point into it.
_FIRST_PARTY_SOURCE_DIRS = ("app", "scripts", "research")

# The ledger's complete lifecycle-state universe, written out here rather than
# imported, and cross-checked against ``_ALLOWED_TRANSITIONS`` below. Any state
# added to the ledger must appear here, which is what turns the C3-2 predicate
# assertion from "these three are not named" into a closed equality.
_LEDGER_LIFECYCLE_STATES = frozenset(
    {
        "planned",
        "previewed",
        "validated",
        "submitted",
        "filled",
        "closed",
        "cancelled",
        "reconciled",
        "anomaly",
    }
)

# The three native terminal non-fill statuses the futures lane distinguishes.
_TERMINAL_NONFILL_STATUSES = frozenset({"CANCELED", "REJECTED", "EXPIRED"})


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


def _kept_outcome_reasons(module: str) -> tuple[frozenset[str], frozenset[str]]:
    """``(reasons paired with a literal ``action="kept"``, all literal actions)``.

    The **pairing** is the assertion's whole content. A helper that harvests
    every literal ``"reason"`` regardless of its sibling ``"action"`` cannot tell
    a ``kept`` outcome from a ``would_release`` one, so flipping a single
    branch's action leaves the harvested set byte-identical while the contract
    row ("these reasons are ``kept`` outcomes") becomes false.

    Outcome dicts whose ``reason`` is computed rather than literal are skipped:
    they are not enumerable and the contract does not enumerate them.
    """
    path = _module_file(module)
    assert path is not None, f"{module} has no source file"
    kept: set[str] = set()
    literal_actions: set[str] = set()
    for node in ast.walk(_parse(path)):
        if not isinstance(node, ast.Dict):
            continue
        entries: dict[str, ast.expr] = {
            key.value: value
            for key, value in zip(node.keys, node.values, strict=True)
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        }
        reason = entries.get("reason")
        action = entries.get("action")
        if not (isinstance(reason, ast.Constant) and isinstance(reason.value, str)):
            continue
        if not (isinstance(action, ast.Constant) and isinstance(action.value, str)):
            continue
        literal_actions.add(action.value)
        if action.value == "kept":
            kept.add(reason.value)
    return frozenset(kept), frozenset(literal_actions)


# ---------------------------------------------------------------------------
# Meaning-level AST helpers.
#
# Each of these replaces a shape-level check that an independent verifier's
# round-2 mutants walked straight through. The comment on each one names the
# escape it closes, because the weaker form is always the more convenient one to
# write and will otherwise creep back.
# ---------------------------------------------------------------------------
def _iter_calls(node: ast.AST, name: str) -> list[ast.Call]:
    """Every call in ``node`` whose callee spells ``name``.

    Matches ``name(...)`` and ``obj.name(...)`` alike, so a broker call reached
    through a client handle is not invisible.
    """
    found: list[ast.Call] = []
    for candidate in ast.walk(node):
        if not isinstance(candidate, ast.Call):
            continue
        func = candidate.func
        if isinstance(func, ast.Name) and func.id == name:
            found.append(candidate)
        elif isinstance(func, ast.Attribute) and func.attr == name:
            found.append(candidate)
    return found


def _keyword_values(call: ast.Call) -> dict[str, str]:
    """Keyword ``name -> unparsed value`` at one call site.

    Values, not names. A test that collects only keyword *names* stays green
    when ``confirm=args.confirm`` becomes ``confirm=False`` or
    ``broker_order_id=broker_order_id`` becomes ``broker_order_id=None`` — the
    name set is identical and the claim is now false.
    """
    return {
        keyword.arg: ast.unparse(keyword.value)
        for keyword in call.keywords
        if keyword.arg is not None
    }


def _reaches_call(node: ast.AsyncFunctionDef | ast.FunctionDef, callee: str) -> bool:
    """Whether ``callee`` is still reachable inside this function.

    Walks the top-level statement sequence in order. A top-level ``raise`` or
    ``return`` dominates everything after it, so a body whose first statement is
    an unconditional ``raise`` reaches nothing — which is exactly how a function
    keeps its name and its signature while no longer performing its phase.

    Bounded honestly: this kills a body that has been *emptied*. A raise nested
    inside an always-true ``if`` would still escape it, and no assertion in this
    module claims otherwise.
    """
    for statement in node.body:
        if _iter_calls(statement, callee):
            return True
        if isinstance(statement, ast.Raise | ast.Return):
            return False
    return False


def _method_node(
    module: str, class_name: str, method_name: str
) -> ast.AsyncFunctionDef | ast.FunctionDef:
    for statement in _class_node(module, class_name).body:
        if (
            isinstance(statement, ast.AsyncFunctionDef | ast.FunctionDef)
            and statement.name == method_name
        ):
            return statement
    raise AssertionError(f"{module}:{class_name} defines no {method_name}")


def _transition_writer_states(module: str, class_name: str) -> dict[str, str]:
    """``record_* -> new_state`` literal, read off each writer's own call.

    This is what makes "kind X has a lane-native writer" checkable: the typed
    lifecycle state each writer actually stamps, rather than the writer's name.
    """
    states: dict[str, str] = {}
    for statement in _class_node(module, class_name).body:
        if not isinstance(statement, ast.AsyncFunctionDef | ast.FunctionDef):
            continue
        if not statement.name.startswith("record_"):
            continue
        for call in _iter_calls(statement, "_transition"):
            raw = _keyword_values(call).get("new_state")
            if raw is not None:
                states[statement.name] = ast.literal_eval(raw)
    return states


def _module_level_dict_keys(module: str, name: str) -> frozenset[str]:
    """Literal string keys of a module-level dict assignment."""
    path = _module_file(module)
    assert path is not None, f"{module} has no source file"
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.AnnAssign):
            target: ast.expr | None = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        else:
            continue
        if not (isinstance(target, ast.Name) and target.id == name):
            continue
        assert isinstance(node.value, ast.Dict), f"{module}:{name} is not a dict"
        return frozenset(
            key.value
            for key in node.value.keys
            if isinstance(key, ast.Constant) and isinstance(key.value, str)
        )
    raise AssertionError(f"{module} defines no {name}")


def _module_level_str_constants(module: str, name: str) -> frozenset[str]:
    """Literal string members of a module-level ``frozenset({...})``/``{...}``."""
    path = _module_file(module)
    assert path is not None, f"{module} has no source file"
    for node in ast.walk(_parse(path)):
        if isinstance(node, ast.AnnAssign):
            target: ast.expr | None = node.target
        elif isinstance(node, ast.Assign) and len(node.targets) == 1:
            target = node.targets[0]
        else:
            continue
        if not (isinstance(target, ast.Name) and target.id == name):
            continue
        assert node.value is not None
        return frozenset(
            child.value
            for child in ast.walk(node.value)
            if isinstance(child, ast.Constant) and isinstance(child.value, str)
        )
    raise AssertionError(f"{module} defines no {name}")


def _body_string_constants(module: str, function_name: str) -> frozenset[str]:
    """String literals in a function body, **docstring excluded**."""
    body_tree = ast.parse(_function_body_source(module, function_name))
    return frozenset(
        child.value
        for child in ast.walk(body_tree)
        if isinstance(child, ast.Constant) and isinstance(child.value, str)
    )


def _first_party_source_files(*, exclude: tuple[str, ...] = ()) -> tuple[Path, ...]:
    """Every first-party ``*.py`` outside ``tests/``, minus ``exclude`` prefixes."""
    files: list[Path] = []
    for directory in _FIRST_PARTY_SOURCE_DIRS:
        root = REPO_ROOT / directory
        if not root.is_dir():
            continue
        for path in sorted(root.rglob("*.py")):
            posix = path.relative_to(REPO_ROOT).as_posix()
            if any(posix.startswith(prefix) for prefix in exclude):
                continue
            files.append(path)
    return tuple(files)


def _imports_package(tree: ast.Module, package: str) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            if (node.module or "").startswith(package):
                return True
        elif isinstance(node, ast.Import):
            if any(alias.name.startswith(package) for alias in node.names):
                return True
    return False


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


def test_the_strategy_loop_cli_is_the_only_run_tick_entry_point_and_wires_null_strategy() -> (
    None
):
    """§2's "only entry point" claim, checked against the whole repository.

    The round-2 form of this test parsed the one CLI the claim names and asserted
    its ``strategy=`` arguments. That cannot detect a *second* entry point: a
    verifier added ``scripts/<other>.py`` calling
    ``run_tick(strategy=object(), confirm=True)`` and every test in this module
    still passed, because nothing here had ever looked outside the named file.
    So the entry-point set is enumerated over ``app/``, ``scripts/`` and
    ``research/`` — anything that imports the strategy-loop package or calls
    ``run_tick`` — and asserted equal to the single file the contract names.
    """
    entry_points: dict[str, list[ast.Call]] = {}
    for path in _first_party_source_files(
        exclude=("app/services/brokers/binance/demo_strategy_loop/",)
    ):
        tree = _parse(path)
        run_tick_calls = _iter_calls(tree, "run_tick")
        if run_tick_calls or _imports_package(tree, _STRATEGY_LOOP_PACKAGE):
            entry_points[path.relative_to(REPO_ROOT).as_posix()] = run_tick_calls

    assert set(entry_points) == {"scripts/binance_demo_strategy_loop.py"}

    # ...and the one entry point wires the always-``None`` plugin. Asserted at
    # the call site, so a second ``run_tick`` call inside the same file with a
    # different plugin is caught too.
    (calls,) = entry_points.values()
    assert len(calls) == 1
    assert _keyword_values(calls[0])["strategy"] == "NullStrategy()"


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
    """Mutant 4 — the close path may never submit without reduceOnly.

    Asserted at the call site, not as a substring. The round-2 form checked that
    ``"reduce_only=True"`` appeared *somewhere* in the file, which a verifier
    defeated by flipping the real close ``submit_order`` to ``reduce_only=False``
    and leaving the other occurrences (the planned-row metadata, the echo
    expectation) untouched: the substring survived, the invariant did not.
    """
    close_leg = _function_node(_STRATEGY_LOOP_EXECUTION, "_close_with_reduce_only")

    submits = _iter_calls(close_leg, "submit_order")
    assert len(submits) == 1
    close_submit = _keyword_values(submits[0])
    assert close_submit["reduce_only"] == "True"
    assert close_submit["confirm"] == "True"

    # The dry-run validation that precedes it must describe the same order.
    order_tests = _iter_calls(close_leg, "order_test")
    assert len(order_tests) == 1
    assert _keyword_values(order_tests[0])["reduce_only"] == "True"

    # ...and the *echo* the broker sends back is checked against ``True`` too,
    # so an accepted order that came back without reduceOnly is rejected.
    echo_checks = [
        _keyword_values(call) for call in _iter_calls(close_leg, "_assert_order_echo")
    ]
    assert echo_checks
    assert all(check["expected_reduce_only"] == "True" for check in echo_checks)

    # Non-vacuity: the open leg is the counterpart that must NOT carry it, so the
    # pins above are about the close path specifically rather than about a file
    # in which ``reduce_only=True`` happens to be the only spelling present.
    open_leg = _function_node(_STRATEGY_LOOP_EXECUTION, "execute_signal_round_trip")
    open_submits = _iter_calls(open_leg, "submit_order")
    assert len(open_submits) == 1
    assert _keyword_values(open_submits[0])["reduce_only"] == "False"


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


def test_c3_1_futures_recovery_ownership_is_split_across_three_surfaces() -> None:
    """C3-1 — "exactly one recovery owner" is UNMET, and the failure is pinned.

    Three modules own recovery phases and no contract names one. Round 2 listed
    only two and a verifier showed the enumeration was inconsistent: the smoke
    CLI implements the *same* ``_close_with_reduce_only`` / ``_reconcile`` pair
    and calls it on the real confirm round trip, so counting the strategy loop's
    in-tick phase while omitting the CLI's isomorphic one had no principle behind
    it. The conclusion is unchanged — "exactly one" is still unmet — but the
    grounds are now complete.

    Each surface is checked for **behaviour**, not for its name. Round 2 asserted
    only that the functions existed, so a verifier put an unconditional
    ``raise RuntimeError`` on the first line of ``_close_with_reduce_only`` and
    every test still passed: the phase was gone and its owner still "owned" it.
    ``_reaches_call`` requires the phase's broker call to still be reachable past
    the top-level statement sequence.
    """
    # (module, phase function, the broker operation that phase exists to perform)
    owners = (
        (_RECONCILE_JOB, "reconcile_binance_demo_root_reservations", "_lookup_order"),
        (_STRATEGY_LOOP_EXECUTION, "_close_with_reduce_only", "submit_order"),
        (_STRATEGY_LOOP_EXECUTION, "_reconcile", "get_open_orders"),
        (_FUTURES_SMOKE_CLI, "_close_with_reduce_only", "submit_order"),
        (_FUTURES_SMOKE_CLI, "_reconcile", "get_open_orders"),
    )
    for module, function_name, broker_operation in owners:
        node = _function_node(module, function_name)
        assert _reaches_call(node, broker_operation), (
            f"{module}:{function_name} no longer reaches {broker_operation} — "
            "its recovery phase has been emptied, so §5.2's C3-1 grounds are stale"
        )

    assert {module for module, _, _ in owners} == {
        _RECONCILE_JOB,
        _STRATEGY_LOOP_EXECUTION,
        _FUTURES_SMOKE_CLI,
    }

    # No surface delegates to another, which is precisely why a single owner
    # cannot be named. Checked pairwise over all three rather than the one pair
    # round 2 happened to list.
    for holder in (_RECONCILE_JOB, _STRATEGY_LOOP_EXECUTION, _FUTURES_SMOKE_CLI):
        reachable = _reachable_modules(holder)
        for other in (_RECONCILE_JOB, _STRATEGY_LOOP_EXECUTION, _FUTURES_SMOKE_CLI):
            if other == holder:
                continue
            assert other not in reachable, f"{holder} delegates to {other}"


def test_c3_2_futures_restart_trigger_rediscovers_only_pre_ack_roots() -> None:
    """C3-2 — the rediscovery predicate, as a **closed** set of lifecycle states.

    Round 2 forbade three specific strings — ``submitted``, ``filled``,
    ``anomaly``. That is an open set, and a verifier walked through it by adding
    the terminal state ``cancelled`` to the candidate tuple: none of the three
    forbidden strings appeared, so the sweep silently grew to reclaim already
    released roots and the test stayed green.

    The fix is an equality against the ledger's whole state universe. Any state
    the predicate names must be one of the three pre-acknowledgement states, and
    any state added to the ledger in future must be added to
    ``_LEDGER_LIFECYCLE_STATES`` here — which fails this test until someone has
    decided, on the record, whether the sweep should claim it.
    """
    assert (
        _module_level_dict_keys(_DEMO_LEDGER_SERVICE, "_ALLOWED_TRANSITIONS")
        == _LEDGER_LIFECYCLE_STATES
    )

    predicate = _function_body_source(_RECONCILE_JOB, "_candidate_where_clauses")
    assert "planned_at <= stale_before" in predicate
    assert "broker_order_id.is_(None)" in predicate

    named_states = _body_string_constants(_RECONCILE_JOB, "_candidate_where_clauses")
    assert named_states & _LEDGER_LIFECYCLE_STATES == frozenset(
        {"planned", "previewed", "validated"}
    )
    # Non-vacuity: acknowledged roots carry broker evidence and every one of them
    # is outside the sweep, asserted as the complement rather than as three names.
    assert not named_states & (
        _LEDGER_LIFECYCLE_STATES - frozenset({"planned", "previewed", "validated"})
    )


def test_c3_3_futures_authoritative_readback_is_get_order() -> None:
    """C3-3 — the readback operation is named, not merely implied by a call set."""
    lookup = _function_body_source(_RECONCILE_JOB, "_lookup_order")

    assert "product == 'spot'" in lookup
    assert "client.get_order(symbol=symbol, client_order_id=cid)" in lookup
    assert "client.get_order_status(symbol=symbol, client_order_id=cid)" in lookup
    # The operation the futures branch dispatches actually exists on the client.
    assert hasattr(BinanceFuturesDemoExecutionClient, "get_order")


def test_c3_4_futures_evidence_writers_each_own_one_typed_state_and_ack_carries_broker_id() -> (
    None
):
    """C3-4 — the writer/state map, and the ACK's broker id reaching the row.

    The map is what makes the §5.2 C3-4 classification checkable: a kind is
    ``PRESENT`` when a typed lifecycle state is reached by that kind alone, and
    the map below is the only place that can be read off. ``record_planned`` is
    the insert writer (it reserves the root and inserts ``planned``), so it does
    not appear among the transition writers.

    The ACK half is a value-destination assertion, not a parameter list. Round 2
    asserted only that ``record_submitted`` *had* a ``broker_order_id``
    parameter, so a verifier changed its body to pass
    ``_transition(..., broker_order_id=None)``: the parameter set was identical,
    the ack no longer carried the broker id to the row, and the test stayed
    green.
    """
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
    assert "record_rejected" not in record_methods
    assert "record_expired" not in record_methods

    assert _transition_writer_states(
        _DEMO_LEDGER_SERVICE, "BinanceDemoLedgerService"
    ) == {
        "record_previewed": "previewed",
        "record_validated": "validated",
        "record_submitted": "submitted",
        "record_filled": "filled",
        "record_closed": "closed",
        "record_cancelled": "cancelled",
        "record_reconciled": "reconciled",
        "record_anomaly": "anomaly",
    }
    assert (
        _iter_calls(
            _method_node(
                _DEMO_LEDGER_SERVICE, "BinanceDemoLedgerService", "record_planned"
            ),
            "_transition",
        )
        == []
    )

    # The ACK's broker id must reach the row, not merely be accepted.
    ack_transitions = _iter_calls(
        _method_node(
            _DEMO_LEDGER_SERVICE, "BinanceDemoLedgerService", "record_submitted"
        ),
        "_transition",
    )
    assert len(ack_transitions) == 1
    ack = _keyword_values(ack_transitions[0])
    assert ack["new_state"] == "'submitted'"
    assert ack["broker_order_id"] == "broker_order_id"
    # ...and the repository actually persists what it is handed.
    update_state = _function_body_source(_DEMO_LEDGER_REPOSITORY, "update_state")
    assert "row.broker_order_id = broker_order_id" in update_state
    assert "if broker_order_id is not None:" in update_state

    # Partial fill is degraded rather than recorded: the fill writer takes no
    # quantity argument, and the ledger has no column to hold one.
    assert _method_parameter_names(
        _DEMO_LEDGER_SERVICE, "BinanceDemoLedgerService", "record_filled"
    ) == frozenset({"self", "client_order_id", "now", "extra_metadata_merge"})
    columns = _mapped_column_names(_DEMO_LEDGER_MODEL, "BinanceDemoOrderLedger")
    assert "executed_qty" not in columns
    assert "remaining_qty" not in columns
    assert "qty" in columns  # planned quantity, set at insert and never split


def test_c3_4_reject_and_expiry_leave_free_form_evidence_rather_than_none() -> None:
    """C3-4 — reject/expiry are ``DEGRADED``, not ``ABSENT``. Round 2 had this wrong.

    Round 2's contract row called reject and expiry ``absent``, having looked for
    ``record_rejected`` / ``record_expired`` method names. An independent
    verifier showed the source says the opposite: both native statuses *are*
    persisted, and distinguishably so. The round-2 table also counted ``unknown``
    as ``PRESENT`` off nothing more than ``record_anomaly`` plus a reason string,
    which is the same shape it called ``absent`` here — so the criterion was not
    one criterion.

    §5.2 now applies a single criterion to all seven kinds and this test pins the
    evidence it turns on: a typed state reached by the kind alone is
    ``PRESENT``; evidence that exists only in the free-form ``anomaly_reason``
    text or ``extra_metadata`` JSON is ``DEGRADED``; nothing persisted is
    ``ABSENT``. Reject and expiry land in the middle row, and so does unknown.
    """
    # Both futures execution surfaces treat the same three native statuses as
    # terminal non-fills, and both persist the native status verbatim.
    for module in (_STRATEGY_LOOP_EXECUTION, _FUTURES_SMOKE_CLI):
        assert (
            _module_level_str_constants(module, "_TERMINAL_NONFILL_STATUSES")
            == _TERMINAL_NONFILL_STATUSES
        )

    for module, function_name in (
        (_STRATEGY_LOOP_EXECUTION, "execute_signal_round_trip"),
        (_FUTURES_SMOKE_CLI, "_execute_confirm_lifecycle"),
    ):
        node = _function_node(module, function_name)

        acks = [_keyword_values(call) for call in _iter_calls(node, "record_submitted")]
        assert acks
        assert all("submit_status" in ack["extra_metadata_merge"] for ack in acks), (
            f"{module}:{function_name} stopped persisting the native submit status"
        )

        anomaly_reasons = [
            _keyword_values(call).get("reason", "")
            for call in _iter_calls(node, "record_anomaly")
        ]
        status_bearing = [
            reason
            for reason in anomaly_reasons
            if "open_did_not_take_effect" in reason and "status=" in reason
        ]
        assert len(status_bearing) == 1, (
            f"{module}:{function_name} no longer records the native status on the "
            "did-not-take-effect anomaly, which is the only place reject and "
            "expiry are distinguishable"
        )

    # ...and it is free-form only: no typed column holds a reject reason, an
    # expiry reason, or the submit status. That is precisely why the row reads
    # DEGRADED and not PRESENT.
    columns = _mapped_column_names(_DEMO_LEDGER_MODEL, "BinanceDemoOrderLedger")
    assert columns.isdisjoint(
        {"submit_status", "reject_reason", "expiry_reason", "terminal_status"}
    )
    assert {"anomaly_reason", "extra_metadata"} <= columns


def test_c3_6_futures_blocked_state_reasons_are_pinned_as_a_closed_set() -> None:
    """C3-6 — the operator-visible ``kept`` reasons are a closed literal set.

    Pinned as an equality rather than a membership sweep so that adding an
    eleventh reason fails here, instead of leaving the contract's enumeration
    stale. That is not hypothetical: the round-1 contract enumerated eight, and
    this assertion is what measured the real count at ten.

    The set is now built from the **pairing** of ``action`` and ``reason`` within
    each outcome dict. Round 2 harvested every literal ``reason`` in the module
    regardless of its sibling action, so a verifier changed
    ``client_unavailable``'s action from ``kept`` to ``would_release`` — turning a
    blocked-state row into a release — and the harvested set was byte-identical.
    The contract row says these ten reasons are ``kept`` *outcomes*; that is a
    claim about pairs, so the assertion is about pairs.
    """
    kept_reasons, literal_actions = _kept_outcome_reasons(_RECONCILE_JOB)

    assert kept_reasons == frozenset(
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
    # Every enumerable outcome in the job is a ``kept`` outcome — the pairing
    # claim stated directly. A branch re-pointed at any other action shows up
    # here even if its reason literal never moves.
    assert literal_actions == frozenset({"kept"})

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


def test_the_futures_runbook_scheduler_clause_is_inaccurate_as_written() -> None:
    """The literal clause does **not** survive. Only a narrower fact does.

    The runbook says no TaskIQ wiring *touches* the futures execution client. A
    TaskIQ module does, so the sentence as written is false — narrowly, but
    false. Round 2 recorded that verdict in the §4.1 table and then wrote "the
    first clause survives" two paragraphs later and "scheduler clause is
    accurate" in §8; a verifier flagged the three statements as mutually
    contradictory, and it was right. The contract now states one sentence in all
    three places, and it is this one:

        the literal clause is inaccurate as written; what holds is the narrower
        operational fact that the single reach carries no schedule and calls no
        order-producing method.

    Both halves are asserted below — the contradiction *and* the narrower fact —
    so neither can be dropped without this going red.
    """
    runbook = (REPO_ROOT / "docs/runbooks/binance-futures-demo-smoke.md").read_text(
        encoding="utf-8"
    )
    assert "No scheduler / TaskIQ / Prefect / cron / Hermes wiring touches the" in (
        runbook
    )
    assert "Futures Demo execution client" in runbook

    # (a) The literal clause is contradicted: a TaskIQ module reaches the client.
    reaching = _entrypoints_reaching(_FUTURES_EXECUTION_CLIENT)
    assert reaching == frozenset({_RECONCILE_TASK})
    assert reaching, "an empty reach set would make the contradiction vacuous"
    assert _RECONCILE_TASK.startswith("app.tasks."), (
        "the contradiction is that the reach is a *TaskIQ* module"
    )

    # (b) The narrower surviving fact, both of its halves.
    keywords = _broker_task_keywords(
        _RECONCILE_TASK, "binance_demo_root_reservation_reconcile"
    )
    assert "schedule" not in keywords
    called = _called_attribute_names((_RECONCILE_TASK, _RECONCILE_JOB))
    assert called.isdisjoint(
        {"submit_order", "cancel_order", "order_test", "set_leverage"}
    )


def test_futures_runbook_only_path_claim_is_contradicted_by_a_second_cli() -> None:
    """ROB-1271 finding F-1, futures half — pinned, not fixed.

    The futures runbook claims the smoke CLI is the **only** path producing real
    Demo futures orders. A second operator CLI also produces them:
    ``scripts/binance_demo_strategy_loop.py`` passes ``confirm=`` and
    ``signal_override=`` straight into ``run_tick`` (ROB-993 ``--paper-signal``
    is the end-to-end Demo round trip), and ``run_tick`` only short-circuits to
    a dry run when ``confirm`` is false.

    Neither clause of that sentence survives as written. The scheduler clause is
    narrowly inaccurate (a TaskIQ module does reach the client — see the test
    above); this one is false outright. They fail for different reasons and are
    pinned separately: the second producer is *not* a scheduler reach at all —
    both producers are operator-invoked foreground processes. J6C owns neither
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

    # Values, not keyword names. The round-2 form collected the *names*
    # ``confirm`` / ``signal_override`` and asserted they were present, which a
    # verifier defeated by rewriting ``confirm=args.confirm`` to
    # ``confirm=False``: the name set was unchanged, the second CLI became
    # permanently inert, and this row's claim ("a second CLI produces real
    # orders") became false with the test still green.
    run_tick_calls = _iter_calls(_parse(strategy_loop_cli), "run_tick")
    assert len(run_tick_calls) == 1
    keywords = _keyword_values(run_tick_calls[0])
    assert keywords["confirm"] == "args.confirm"
    assert keywords["signal_override"] == "signal_override"
    # Belt: neither is a constant, so no literal can be substituted for the
    # operator-controlled expressions above.
    forwarded = {
        keyword.arg: keyword.value
        for keyword in run_tick_calls[0].keywords
        if keyword.arg in {"confirm", "signal_override"}
    }
    assert not any(isinstance(value, ast.Constant) for value in forwarded.values()), (
        "run_tick's confirm/signal_override must stay operator-controlled"
    )

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
