"""ROB-1263 (J3B) — KIS mock coordination adapter acceptance tests.

Every zero-I/O claim below asserts an exact call count of zero on a fake, never
"no exception was raised".  The J3A fakes are *imported* rather than re-created:
a lane that reimplements the common primitive's test doubles is one step from
reimplementing the primitive.
"""

from __future__ import annotations

import ast
import asyncio
import contextlib
import pathlib
from dataclasses import FrozenInstanceError, replace
from decimal import Decimal
from typing import Any

import httpx
import pytest
import pytest_asyncio
from sqlalchemy import delete

import app.mcp_server.tooling.order_execution as oe
import app.mcp_server.tooling.orders_modify_cancel as omc
import app.services.kis_mock_runner.singleton as singleton
from app.models.review import OrderSendIntent
from app.services.brokers.client_order_ids import BrokerClientIdTarget
from app.services.brokers.kis.pre_send import PreSendFreshnessError
from app.services.brokers.kis.send_outcome import (
    OrderSendOutcomeTracker,
)
from app.services.kis_mock_attribution import InvalidStrategy, MissingAttribution
from app.services.mock_integration.coordination import (
    CoordinationError,
    CoordinationReasonCode,
    DurableSendClaimAdapter,
    MutationCallbackResult,
    MutationCertainty,
    TerminalClaimEvidence,
    ordered_advisory_keyset,
    physical_account_scope_for_entry,
)
from app.services.mock_integration.lineage import MockLineageFactory
from app.services.mock_lane_registry import LaneGuardError
from app.services.order_send_intent_service import OrderSendIntentService

# The merged J3A test doubles. Consumed, never copied.
from tests.services.mock_integration.test_coordination import (  # noqa: E402
    ConnectionFactory,
    FakeIntents,
    FakeLockConnection,
    FakeLockSpace,
    FakeUncertaintyGate,
    RecordingDispatchEvidence,
    RecordingPersistence,
    _attempt_envelope,
    _bound_registry,
    _fully_bound_entry,
)

REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
ADAPTER_SOURCE = REPO_ROOT / "app" / "services" / "kis_mock_runner" / "singleton.py"
DOMESTIC_ORDERS_SOURCE = (
    REPO_ROOT / "app" / "services" / "brokers" / "kis" / "domestic_orders.py"
)
CONTRACT_DOC = REPO_ROOT / "docs" / "contracts" / "rob-1263-kis-coordination-adapter.md"

# The exact J3B write fence (brief B-7).
J3B_WRITE_FENCE: frozenset[str] = frozenset(
    {
        "app/services/kis_mock_runner/singleton.py",
        "app/services/brokers/kis/domestic_orders.py",
        "app/mcp_server/tooling/order_execution.py",
        "app/mcp_server/tooling/orders_modify_cancel.py",
        "tests/services/kis_mock_runner/test_singleton.py",
        "tests/services/kis_mock_runner/test_domestic_route.py",
        "tests/test_mcp_place_order.py",
        "tests/services/mock_integration/test_kis_coordination_adapter.py",
        "docs/contracts/rob-1263-kis-coordination-adapter.md",
        # r5: orch granted an explicit fence extension for exactly these two
        # files, after measuring that their failures pass on origin/main and are
        # therefore this branch's regressions rather than pre-existing defects.
        # ("orch 가 r5 에서 명시 승인(회귀 귀속 확인 후)")
        "tests/test_services_kis_logging.py",
        "tests/test_rob750_mock_mirror_intent_release.py",
        # r6: orch approved the remaining six after the CI-driven inventory
        # confirmed every one of them passes on origin/main and fails here —
        # i.e. they are this branch's own regressions.
        # ("orch 가 r6 에서 명시 승인(회귀 귀속 CI 대조 확인 후)")
        "tests/test_kis_mock_order_ledger.py",
        "tests/brokers/kis/mock_scalping_exec/test_reservation.py",
        "tests/brokers/kis/mock_scalping_exec/test_pre_send_transport.py",
        "tests/test_kis_mock_cancel_modify.py",
        "tests/services/test_kis_mock_attribution_chain.py",
        "tests/test_kis_mock_lifecycle_reconciliation_acceptance.py",
    }
)
# ROB-1263 r3: the r2 branch had widened this set itself so its own out-of-fence
# edits would pass the check. A guard widened until it admits its own violation
# is not a guard. Widening it is an orch decision, and the two entries above are
# the only ones granted; anything else is asked for, not taken.
FENCE_EXEMPT_PREFIXES: tuple[str, ...] = (".smoke-out/",)

MOCK_NETLOC = "openapivts.koreainvestment.com:29443"
MOCK_BASE_URL = f"https://{MOCK_NETLOC}"
LIVE_BASE_URL = "https://openapi.koreainvestment.com:9443"
ORDER_PATH = "/uapi/domestic-stock/v1/trading/order-cash"

APP_KEY = "kis-mock-app-key-fixture"
ACCOUNT_NO = "5088888801"


# ===========================================================================
# Fixtures — a fake KIS client; no socket, no credential value, no broker
# ===========================================================================


class FakeSettingsView:
    """Structural stand-in for ``_KISSettingsView``."""

    def __init__(self, *, is_mock: bool, base_url: str) -> None:
        self._is_mock = is_mock
        self.kis_base_url = base_url
        self.kis_app_key = APP_KEY
        self.kis_account_no = ACCOUNT_NO


class FakeKISClient:
    """Structural stand-in for ``KISClient``; ``_kis_url`` is the real shape."""

    def __init__(
        self,
        *,
        is_mock_client: bool = True,
        view_is_mock: bool = True,
        base_url: str = MOCK_BASE_URL,
        url_override: str | None = None,
    ) -> None:
        self._is_mock_client = is_mock_client
        self._settings = FakeSettingsView(is_mock=view_is_mock, base_url=base_url)
        self._url_override = url_override
        self.transport_calls = 0

    def _kis_url(self, path: str) -> str:
        if self._url_override is not None:
            return self._url_override
        return f"{self._settings.kis_base_url.rstrip('/')}{path}"

    async def send(self) -> None:
        self.transport_calls += 1


@contextlib.contextmanager
def _held_grant(grant: singleton.KISMockCoordinationGrant):
    """Install `grant` as this task's live writer authority, as production does."""

    token = singleton._ACTIVE_WRITER_LEASE.set(
        singleton._WriterAuthority(
            account_mode=singleton.ACCOUNT_MODE,
            advisory_keys=grant.advisory_keys,
            grant=grant,
        )
    )
    try:
        yield grant
    finally:
        singleton._ACTIVE_WRITER_LEASE.reset(token)


def _bound_kis_entry(envelope: Any, **overrides: Any) -> Any:
    """A fully bound KIS mock entry whose identity is the real fingerprint."""

    values: dict[str, Any] = {
        "physical_account_id": singleton.kis_mock_account_fingerprint(
            app_key=APP_KEY, account_no=ACCOUNT_NO
        )
    }
    values.update(overrides)
    return _fully_bound_entry(envelope, "kr.kis.mock", **values)


def _kis_registry(envelope: Any, **overrides: Any) -> tuple[Any, ...]:
    return _bound_registry(
        envelope,
        "kr.kis.mock",
        physical_account_id=singleton.kis_mock_account_fingerprint(
            app_key=APP_KEY, account_no=ACCOUNT_NO
        ),
        **overrides,
    )


def _ready_ports(**overrides: Any) -> singleton.KISMockLanePorts:
    values: dict[str, Any] = {
        "persistence": RecordingPersistence(),
        "dispatch_evidence": RecordingDispatchEvidence(),
        "uncertainty_gate": FakeUncertaintyGate(),
        "evidence_kinds": singleton.KIS_MOCK_LANE_EVIDENCE_KINDS,
    }
    values.update(overrides)
    return singleton.KISMockLanePorts(**values)


def _stack(events: list[str] | None = None) -> dict[str, Any]:
    space = FakeLockSpace()
    connection = FakeLockConnection(space, events=events)
    return {
        "space": space,
        "connection": connection,
        "factory": ConnectionFactory(connection),
        "intents": FakeIntents(events=events),
        "persistence": RecordingPersistence(events=events),
        "evidence": RecordingDispatchEvidence(events=events),
        "gate": FakeUncertaintyGate(events=events),
    }


async def _coordinate(
    envelope: Any,
    stack: dict[str, Any],
    mutation: Any,
    *,
    lane_registry: tuple[Any, ...] | None = None,
    ports: singleton.KISMockLanePorts | None = None,
) -> Any:
    return await singleton.coordinate_kis_mock_mutation(
        envelope=envelope,
        ports=ports
        or _ready_ports(
            persistence=stack["persistence"],
            dispatch_evidence=stack["evidence"],
            uncertainty_gate=stack["gate"],
        ),
        claims=DurableSendClaimAdapter(stack["intents"]),
        connection_factory=stack["factory"],
        mutation=mutation,
        registry=lane_registry
        if lane_registry is not None
        else _kis_registry(envelope),
    )


def _ok(_grant: Any) -> Any:
    async def _run() -> MutationCallbackResult:
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="0000117058"
        )

    return _run()


# ---------------------------------------------------------------------------
# Shared helpers for the other KIS mock suites
#
# ROB-1263 changed what a caller must hold before a mock mutation reaches the
# wire. Suites whose subject is something else (ledger writes, reservations, TR
# routing, attribution) still have to do the caller's part, and they should not
# each grow their own copy of it. These are the one implementation.
# ---------------------------------------------------------------------------


@contextlib.asynccontextmanager
async def installed_kis_mock_route():
    """Install a coordinated route so a KIS mock send is authorized at all.

    The route-less refusal itself is untouched and is covered by
    `test_without_a_route_the_lane_sends_nothing_at_all`.
    """

    physical = singleton.kis_mock_account_fingerprint(
        app_key=APP_KEY, account_no=ACCOUNT_NO
    )
    _, envelope = _attempt_envelope()
    space = FakeLockSpace()
    route = singleton.KISMockCoordinationRoute(
        envelope=envelope,
        ports=singleton.KISMockLanePorts(
            persistence=RecordingPersistence(),
            dispatch_evidence=RecordingDispatchEvidence(),
            uncertainty_gate=FakeUncertaintyGate(),
            evidence_kinds=singleton.KIS_MOCK_LANE_EVIDENCE_KINDS,
        ),
        claims=DurableSendClaimAdapter(FakeIntents()),
        connection_factory=ConnectionFactory(FakeLockConnection(space)),
        registry=_bound_registry(envelope, "kr.kis.mock", physical_account_id=physical),
    )
    singleton.set_kis_mock_coordination_route_provider(lambda **_ctx: route)
    try:
        yield route
    finally:
        singleton.set_kis_mock_coordination_route_provider(None)


def stub_kis_mock_wire_lease(monkeypatch):
    """Keep the authority record, stand in for the PostgreSQL lease behind it.

    The guard's requirement is unchanged — an authority is still installed and
    still required. Only the database session it would otherwise open is stood
    in for, so suites that run without the run-owned test database keep their
    own subject.
    """

    @contextlib.asynccontextmanager
    async def _lease(**kwargs: Any):
        token = singleton._ACTIVE_WRITER_LEASE.set(
            singleton._WriterAuthority(
                account_mode=singleton.ACCOUNT_MODE,
                advisory_keys=(singleton.kis_mock_legacy_advisory_key(),),
                lease=object(),
            )
        )
        try:
            yield
        finally:
            singleton._ACTIVE_WRITER_LEASE.reset(token)

    monkeypatch.setattr(singleton, "enforce_kis_mock_mutation_writer", _lease)


def issued_followup_receipt(
    operation_name: str,
    *,
    order_id: str,
    is_mock: bool = True,
    claim_scope: str = "mockpa:v1:shared",
    claim_key: str = "mock-idempotency-v1:shared",
    remainder: str = "10",
):
    """A live FOLLOWUP receipt; a no-op for live orders.

    This is the caller doing what r3 requires of it, not the guard being
    relaxed: the receipt is really issued inside a short critical section and
    really expires with it.
    """

    from app.services.order_send_intent_service import OrderSendIntentReservation

    if not is_mock:
        return contextlib.nullcontext()

    async def _reservations(account_scope: str):
        return [
            OrderSendIntentReservation(row_id=1, idempotency_key=claim_key, side="buy")
        ]

    class _Lease:
        acquired = True

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    return singleton.issue_kis_mock_followup_capability(
        operation=singleton.KISMockOperation(operation_name),
        claim_account_scope=claim_scope,
        claim_idempotency_key=claim_key,
        attributed_broker_order_id=order_id,
        known_remainder=Decimal(remainder),
        reservations=_reservations,
        lease_factory=_Lease,
    )


# ===========================================================================
# §E-1 — dual-key integration
# ===========================================================================


def test_supplied_set_is_the_exact_physical_formula_plus_the_existing_legacy_fn():
    _, envelope = _attempt_envelope()
    entry = _bound_kis_entry(envelope)

    physical = physical_account_scope_for_entry(entry).advisory_key
    legacy = singleton.kis_mock_legacy_advisory_key()

    # The legacy key is the *pre-existing* function's result, not a re-hash.
    assert legacy == singleton.account_mode_advisory_key("kis_mock")
    assert singleton.kis_mock_advisory_keyset(entry) == ordered_advisory_keyset(
        (physical, legacy)
    )
    assert set(singleton.kis_mock_advisory_keyset(entry)) == {physical, legacy}


def test_common_primitive_order_is_numeric_with_physical_both_before_and_after():
    """Two vectors, opposite relative order, one rule: ascending numeric."""

    _, envelope = _attempt_envelope()
    legacy = singleton.kis_mock_legacy_advisory_key()

    before = ordered_advisory_keyset((legacy - 1, legacy))
    after = ordered_advisory_keyset((legacy + 1, legacy))

    assert before == (legacy - 1, legacy)
    assert after == (legacy, legacy + 1)
    # And the real derivation feeds the same primitive, never a lane-chosen order.
    entry = _bound_kis_entry(envelope)
    keys = singleton.kis_mock_advisory_keyset(entry)
    assert list(keys) == sorted(keys)


@pytest.mark.asyncio
async def test_old_process_holding_only_the_legacy_key_blocks_the_dual_key_writer():
    _, envelope = _attempt_envelope()
    stack = _stack()
    legacy = singleton.kis_mock_legacy_advisory_key()
    # An old build holds exactly the one key it knows about.
    stack["space"].try_lock(legacy, pid=999)

    called = {"n": 0}

    async def _mutation(_grant: Any) -> MutationCallbackResult:  # pragma: no cover
        called["n"] += 1
        raise AssertionError("a contended writer must never reach the callback")

    with pytest.raises(CoordinationError) as excinfo:
        await _coordinate(envelope, stack, _mutation)

    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED
    assert called["n"] == 0
    assert stack["intents"].reserve_calls == []


@pytest.mark.asyncio
async def test_process_holding_only_the_physical_key_blocks_the_writer():
    _, envelope = _attempt_envelope()
    stack = _stack()
    entry = _bound_kis_entry(envelope)
    stack["space"].try_lock(physical_account_scope_for_entry(entry).advisory_key, 999)

    called = {"n": 0}

    async def _mutation(_grant: Any) -> MutationCallbackResult:  # pragma: no cover
        called["n"] += 1
        raise AssertionError("a contended writer must never reach the callback")

    with pytest.raises(CoordinationError) as excinfo:
        await _coordinate(envelope, stack, _mutation)

    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_CONTENDED
    assert called["n"] == 0


@pytest.mark.asyncio
async def test_failed_second_acquire_rolls_the_first_back_and_leaks_no_authority():
    _, envelope = _attempt_envelope()
    stack = _stack()
    entry = _bound_kis_entry(envelope)
    keys = singleton.kis_mock_advisory_keyset(entry)
    # Contend on the *second* key in the primitive's own order.
    stack["space"].try_lock(keys[1], pid=999)

    with pytest.raises(CoordinationError):
        await _coordinate(envelope, stack, _ok)

    # The first key must not still be held by our backend.
    ours = stack["connection"].session_pid
    assert stack["space"].held.get(keys[0]) != ours
    assert stack["space"].held.get(keys[1]) == 999


@pytest.mark.asyncio
async def test_grant_proves_both_keys_and_kills_the_boolean_contextvar_mutant():
    _, envelope = _attempt_envelope()
    stack = _stack()
    entry = _bound_kis_entry(envelope)
    expected = singleton.kis_mock_advisory_keyset(entry)
    seen: dict[str, Any] = {}

    async def _mutation(grant: singleton.KISMockCoordinationGrant):
        seen["grant"] = grant
        seen["authority"] = singleton.active_writer_authority()
        await grant.assert_owned()
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="0000117058"
        )

    result = await _coordinate(envelope, stack, _mutation)

    grant = seen["grant"]
    assert grant.advisory_keys == expected
    assert grant.proves_keys(expected) is True
    assert grant.proves_keys([grant.physical_advisory_key]) is True
    assert grant.proves_keys([grant.legacy_advisory_key]) is True
    # A key that was never acquired is not proven by the grant.
    assert grant.proves_keys([max(expected) + 1]) is False
    assert tuple(result.lease_keys) == expected

    # The mutant: a bare boolean standing in for the grant. The authority the
    # adapter publishes is a typed record carrying the real grant, and the grant
    # itself is immutable, so `True` cannot be substituted for it.
    authority = seen["authority"]
    assert authority is not None and authority.grant is grant
    assert authority.advisory_keys == expected
    with pytest.raises(FrozenInstanceError):
        grant.advisory_keys = (0,)  # type: ignore[misc]


def test_a_released_lease_no_longer_reads_as_an_active_authority():
    """The exact ROB-853 boolean hole: `True` outliving the thing it described."""

    class _DeadLease:
        acquired = False

    class _LiveLease:
        acquired = True

    token = singleton._ACTIVE_WRITER_LEASE.set(
        singleton._WriterAuthority(
            account_mode="kis_mock", advisory_keys=(1,), lease=_DeadLease()
        )
    )
    try:
        assert singleton.active_writer_authority() is None
        assert singleton.has_active_writer_lease() is False
    finally:
        singleton._ACTIVE_WRITER_LEASE.reset(token)

    token = singleton._ACTIVE_WRITER_LEASE.set(
        singleton._WriterAuthority(
            account_mode="kis_mock", advisory_keys=(1,), lease=_LiveLease()
        )
    )
    try:
        assert singleton.has_active_writer_lease() is True
    finally:
        singleton._ACTIVE_WRITER_LEASE.reset(token)


def test_reentrancy_requires_the_exact_account_mode_and_every_required_key():
    authority = singleton._WriterAuthority(
        account_mode="kis_mock",
        advisory_keys=(11, 22),
        lease=None,
        grant=object(),  # type: ignore[arg-type]
    )
    assert authority.covers(account_mode="kis_mock", required_keys=(11,)) is True
    assert authority.covers(account_mode="kis_mock", required_keys=(11, 22)) is True
    assert authority.covers(account_mode="kis_mock", required_keys=(33,)) is False
    assert authority.covers(account_mode="kiwoom_mock", required_keys=(11,)) is False


def test_the_legacy_key_is_neither_deleted_nor_renamed():
    assert singleton.ACCOUNT_MODE == "kis_mock"
    assert callable(singleton.account_mode_advisory_key)
    assert (
        singleton.kis_mock_legacy_advisory_key()
        == singleton.account_mode_advisory_key("kis_mock")
    )
    source = ADAPTER_SOURCE.read_text(encoding="utf-8")
    assert "def account_mode_advisory_key(" in source


# ===========================================================================
# §E-2 — attribution and ordering
# ===========================================================================


def _execute_kwargs(**overrides: Any) -> dict[str, Any]:
    values: dict[str, Any] = {
        "normalized_symbol": "005930",
        "side": "buy",
        "order_type": "limit",
        "order_quantity": 2,
        "price": 70000,
        "market_type": "equity_kr",
        "current_price": 70000.0,
        "avg_price": 0.0,
        "dry_run_result": {"price": 70000, "quantity": 2, "estimated_value": 140000},
        "order_amount": 140000.0,
        "reason": "ROB-1263 J3B acceptance",
        "exit_reason": None,
        "thesis": None,
        "strategy": "rob1263_acceptance",
        "target_price": None,
        "stop_loss": None,
        "min_hold_days": None,
        "notes": None,
        "indicators_snapshot": None,
        "defensive_trim_ctx": None,
        "order_error_fn": lambda message: {"success": False, "error": message},
        "is_mock": True,
        "correlation_id": "kis-mock:rob1263:acceptance",
        "report_item_uuid": None,
        "approval_hash_digest": None,
        "idempotency_key": None,
        "mirror_cohort": "mock_counterfactual",
        "mirror_source_bucket": "place_original",
    }
    values.update(overrides)
    return values


@pytest_asyncio.fixture
async def _clean_intents(db_session):
    await db_session.execute(delete(OrderSendIntent))
    await db_session.commit()
    yield
    await db_session.execute(delete(OrderSendIntent))
    await db_session.commit()


@pytest.fixture
def _mock_route():
    """Install a coordinated route so a mock KR send is authorized at all.

    r4 §3: without a route the adapter refuses to send, so any test whose
    subject is something *other* than that refusal has to supply one.
    """

    events: list[str] = []
    stack = _stack(events)
    _, envelope = _attempt_envelope()
    route = singleton.KISMockCoordinationRoute(
        envelope=envelope,
        ports=_ready_ports(
            persistence=stack["persistence"],
            dispatch_evidence=stack["evidence"],
            uncertainty_gate=stack["gate"],
        ),
        claims=DurableSendClaimAdapter(stack["intents"]),
        connection_factory=stack["factory"],
        registry=_kis_registry(envelope),
    )
    singleton.set_kis_mock_coordination_route_provider(lambda **_ctx: route)
    stack["events"] = events
    try:
        yield stack
    finally:
        singleton.set_kis_mock_coordination_route_provider(None)


@pytest.fixture
def _traced(monkeypatch):
    """Trace the pre-send order and count every downstream call."""

    events: list[str] = []
    counters = {"signal": 0, "reserve": 0, "send": 0}

    def _resolve(**kwargs: Any):
        events.append("attribution_resolve")
        from app.services.kis_mock_attribution import resolve_attribution as real

        return real(**kwargs)

    async def _record(*args: Any, **kwargs: Any) -> None:
        counters["signal"] += 1
        events.append("signal_commit")

    async def _send(**kwargs: Any) -> dict[str, Any]:
        counters["send"] += 1
        events.append("http_send")
        return {"odno": "0000117058", "rt_cd": "0", "msg1": "ok"}

    async def _baseline(**kwargs: Any) -> None:
        return None

    import app.mcp_server.tooling.kis_mock_ledger as ledger

    monkeypatch.setattr(oe, "resolve_attribution", _resolve)
    monkeypatch.setattr(oe, "record_signal", _record)
    monkeypatch.setattr(oe, "_execute_order", _send)
    monkeypatch.setattr(ledger, "_fetch_kis_mock_baseline_qty", _baseline)

    real_service = oe.OrderSendIntentService

    class _TracingIntents(real_service):  # type: ignore[misc, valid-type]
        async def reserve(self, **kwargs: Any):
            counters["reserve"] += 1
            events.append("reserve")
            return await super().reserve(**kwargs)

    monkeypatch.setattr(oe, "OrderSendIntentService", _TracingIntents)
    return {"events": events, "counters": counters}


@pytest.mark.asyncio
async def test_missing_strategy_blocks_with_attribution_required_and_zero_downstream(
    _traced, _clean_intents
):
    # No strategy and no mirror cohort: nothing can name an owner for this send.
    result = await oe._execute_and_record(
        **_execute_kwargs(strategy=None, correlation_id=None, mirror_cohort=None)
    )

    assert result["success"] is False
    assert result["error_code"] == "attribution_required"
    assert result["missing_attribution"] == ["strategy"]
    assert _traced["counters"] == {"signal": 0, "reserve": 0, "send": 0}


@pytest.mark.asyncio
async def test_placeholder_strategy_blocks_before_signal_reserve_and_send(
    _traced, _clean_intents
):
    result = await oe._execute_and_record(**_execute_kwargs(strategy="   TBD   "))

    assert result["success"] is False
    # The existing, stricter literal is preserved rather than flattened into
    # `attribution_required`: a placeholder is a *named* failure, and B-3
    # forbids relaxing the boundary that already exists.
    assert result["error_code"] == InvalidStrategy.error_code == "placeholder_strategy"
    assert issubclass(InvalidStrategy, MissingAttribution)
    assert _traced["counters"] == {"signal": 0, "reserve": 0, "send": 0}


@pytest.mark.asyncio
async def test_signal_commit_failure_blocks_with_signal_record_unavailable(
    monkeypatch, _traced, _clean_intents
):
    async def _boom(*args: Any, **kwargs: Any) -> None:
        raise RuntimeError("simulated signal ledger outage")

    monkeypatch.setattr(oe, "record_signal", _boom)

    result = await oe._execute_and_record(**_execute_kwargs())

    assert result["success"] is False
    assert result["error_code"] == "signal_record_unavailable"
    assert _traced["counters"]["reserve"] == 0
    assert _traced["counters"]["send"] == 0


@pytest.mark.asyncio
async def test_ordered_trace_proves_signal_commit_precedes_reserve_and_http(
    _traced, _clean_intents, _mock_route
):
    await oe._execute_and_record(**_execute_kwargs())

    events = _traced["events"]
    assert events.index("attribution_resolve") < events.index("signal_commit")
    assert events.index("signal_commit") < events.index("reserve")
    assert events.index("reserve") < events.index("http_send")


def _decorator_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for decorator in getattr(node, "decorator_list", []):
        target = decorator.func if isinstance(decorator, ast.Call) else decorator
        if isinstance(target, ast.Name):
            names.add(target.id)
        elif isinstance(target, ast.Attribute):
            names.add(target.attr)
    return names


def test_ast_guard_every_catalogued_kis_mock_mutation_callsite_is_guarded():
    """An unguarded catalogued KRX mock mutation method fails right here."""

    tree = ast.parse(DOMESTIC_ORDERS_SOURCE.read_text(encoding="utf-8"))
    mutation_methods = {"order_korea_stock", "cancel_korea_order", "modify_korea_order"}
    found: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.AsyncFunctionDef) and node.name in mutation_methods:
            found[node.name] = _decorator_names(node)

    assert set(found) == mutation_methods, found
    for name, decorators in found.items():
        assert "_guard_kis_mock_writer" in decorators, name

    # The mutant this kills: drop the decorator from one method and the set
    # comparison above fails rather than the suite going quietly green.
    assert _decorator_names(ast.parse("async def f(): ...").body[0]) == set()


def test_writer_surface_catalog_covers_every_declared_mutation_surface():
    assert singleton.MUTATION_WRITER_SURFACES == {
        "runner",
        "watch_auto_execute",
        "smoke_cli",
        "manual_mcp_mutation",
        "b0x_adapter",
    }
    for surface in singleton.MUTATION_WRITER_SURFACES:
        singleton.assert_known_writer_surface(surface)
    with pytest.raises(singleton.WriterSurfaceUnknown):
        singleton.assert_known_writer_surface("uncatalogued_new_path")


# ===========================================================================
# §E-3 — nullable native client ID
# ===========================================================================


def test_kis_attempt_uses_both_cid_fields_none_with_nonblank_idempotency():
    _, envelope = _attempt_envelope()
    attempt = envelope.order_attempt
    assert attempt is not None
    assert envelope.broker_client_id_target is None
    assert envelope.lane_prefix is None
    assert attempt.broker_client_order_id is None
    assert isinstance(attempt.idempotency_key, str)
    assert attempt.idempotency_key.strip()


def test_ack_persists_the_exact_odno_only_as_broker_order_id():
    factory = MockLineageFactory()
    _, envelope = _attempt_envelope()

    acknowledged = factory.acknowledge_order_attempt(envelope, "0000117058")
    attempt = acknowledged.order_attempt
    assert attempt is not None
    assert attempt.broker_order_id == "0000117058"
    assert attempt.broker_client_order_id is None
    assert acknowledged.broker_client_id_target is None


@pytest.mark.parametrize("blank", ["", "   ", "\t\n"])
def test_a_blank_odno_is_not_an_acknowledgement(blank: str):
    factory = MockLineageFactory()
    _, envelope = _attempt_envelope()
    with pytest.raises(ValueError):
        factory.acknowledge_order_attempt(envelope, blank)


def test_broker_client_id_target_enum_snapshot_is_unchanged():
    assert [member.value for member in BrokerClientIdTarget] == [
        "toss",
        "binance_spot_demo",
        "alpaca_paper",
    ]
    assert not any(
        member.value.startswith(("kis", "kiwoom")) for member in BrokerClientIdTarget
    )


# ===========================================================================
# §E-4 — actual transport binding
# ===========================================================================


async def _grant_for(
    envelope: Any,
    entry: Any,
    *,
    claim_idempotency_key: str = "mock-idempotency-v1:acceptance",
) -> singleton.KISMockCoordinationGrant:
    """A grant whose ownership assertion is a fake, so no lease is required."""

    scope = physical_account_scope_for_entry(entry)

    class _Scope:
        def __init__(self) -> None:
            self.calls = 0

        async def assert_owned(self) -> None:
            self.calls += 1

    return singleton.KISMockCoordinationGrant(
        lane_id=entry.lane_id,
        claim_account_scope=scope.claim_account_scope,
        advisory_keys=singleton.kis_mock_advisory_keyset(entry),
        physical_advisory_key=scope.advisory_key,
        legacy_advisory_key=singleton.kis_mock_legacy_advisory_key(),
        credential_namespace=singleton.KIS_MOCK_CREDENTIAL_NAMESPACE,
        allowed_netlocs=(MOCK_NETLOC,),
        physical_account_id=str(entry.physical_account_id),
        entry=entry,
        claim_idempotency_key=claim_idempotency_key,
        _scope=_Scope(),
    )


@pytest.mark.asyncio
async def test_live_character_client_hidden_behind_is_mock_true_is_rejected():
    _, envelope = _attempt_envelope()
    entry = _bound_kis_entry(envelope)
    grant = await _grant_for(envelope, entry)
    # The caller declared `is_mock=True`; the actual client object did not.
    client = FakeKISClient(is_mock_client=False)

    with pytest.raises(singleton.KISMockSendBoundaryRejected) as excinfo:
        await singleton.assert_kis_mock_send_boundary(
            client=client, url=client._kis_url(ORDER_PATH), entry=entry, grant=grant
        )
    assert excinfo.value.reason_code == singleton.KIS_MOCK_TRANSPORT_NOT_MOCK_CLIENT
    assert client.transport_calls == 0


@pytest.mark.asyncio
async def test_mock_labelled_registry_with_an_actual_live_url_is_rejected():
    _, envelope = _attempt_envelope()
    entry = _bound_kis_entry(envelope)
    grant = await _grant_for(envelope, entry)
    client = FakeKISClient(base_url=LIVE_BASE_URL)

    with pytest.raises(singleton.KISMockSendBoundaryRejected) as excinfo:
        await singleton.assert_kis_mock_send_boundary(
            client=client, url=client._kis_url(ORDER_PATH), entry=entry, grant=grant
        )
    assert excinfo.value.reason_code == singleton.KIS_MOCK_TRANSPORT_HOST_MISMATCH
    assert client.transport_calls == 0


@pytest.mark.asyncio
async def test_a_widened_lane_host_allowlist_is_still_pinned_to_the_vts_host():
    """Isolates the lane's own netloc pin from the J2A guard in front of it.

    ``assert_mock_only_endpoint`` only checks membership in the entry's
    ``allowed_hosts``, so an entry whose allowlist grew by one host passes it.
    Without this vector the lane's pin is untested defence-in-depth: deleting it
    leaves the suite green.
    """

    _, envelope = _attempt_envelope()
    widened = _bound_kis_entry(
        envelope, allowed_hosts=(MOCK_NETLOC, "extra-mock-host.example")
    )
    grant = await _grant_for(envelope, _bound_kis_entry(envelope))
    client = FakeKISClient(url_override="https://extra-mock-host.example" + ORDER_PATH)

    with pytest.raises(singleton.KISMockSendBoundaryRejected) as excinfo:
        await singleton.assert_kis_mock_send_boundary(
            client=client,
            url=client._kis_url(ORDER_PATH),
            entry=widened,
            grant=grant,
        )
    assert excinfo.value.reason_code == singleton.KIS_MOCK_TRANSPORT_HOST_MISMATCH
    assert client.transport_calls == 0


@pytest.mark.asyncio
async def test_a_grant_that_does_not_carry_the_host_cannot_authorize_the_send():
    """The grant's own allowlist is a second, independent lock on the netloc."""

    _, envelope = _attempt_envelope()
    entry = _bound_kis_entry(envelope)
    grant = replace(await _grant_for(envelope, entry), allowed_netlocs=())
    client = FakeKISClient()

    with pytest.raises(singleton.KISMockSendBoundaryRejected) as excinfo:
        await singleton.assert_kis_mock_send_boundary(
            client=client, url=client._kis_url(ORDER_PATH), entry=entry, grant=grant
        )
    assert excinfo.value.reason_code == singleton.KIS_MOCK_TRANSPORT_HOST_MISMATCH
    assert client.transport_calls == 0


@pytest.mark.asyncio
async def test_mock_host_reached_from_a_live_settings_namespace_is_rejected():
    """The URL was rewritten to the mock host; the credentials were not."""

    _, envelope = _attempt_envelope()
    entry = _bound_kis_entry(envelope)
    grant = await _grant_for(envelope, entry)
    client = FakeKISClient(view_is_mock=False)

    with pytest.raises(singleton.KISMockSendBoundaryRejected) as excinfo:
        await singleton.assert_kis_mock_send_boundary(
            client=client, url=client._kis_url(ORDER_PATH), entry=entry, grant=grant
        )
    assert excinfo.value.reason_code == singleton.KIS_MOCK_CREDENTIAL_NAMESPACE_MISMATCH
    assert client.transport_calls == 0


@pytest.mark.asyncio
async def test_actual_mock_host_with_the_wrong_physical_profile_is_rejected():
    _, envelope = _attempt_envelope()
    entry = _bound_kis_entry(envelope, physical_account_id="kismock:v1:someone-else")
    grant = await _grant_for(envelope, entry)
    client = FakeKISClient()

    with pytest.raises(singleton.KISMockSendBoundaryRejected) as excinfo:
        await singleton.assert_kis_mock_send_boundary(
            client=client, url=client._kis_url(ORDER_PATH), entry=entry, grant=grant
        )
    assert excinfo.value.reason_code == singleton.KIS_MOCK_ACCOUNT_FINGERPRINT_MISMATCH
    assert client.transport_calls == 0


@pytest.mark.asyncio
async def test_a_foreign_lane_entry_is_rejected_before_anything_else():
    _, envelope = _attempt_envelope()
    kis_entry = _bound_kis_entry(envelope)
    grant = await _grant_for(envelope, kis_entry)
    kiwoom_entry = _fully_bound_entry(envelope, "kr.kiwoom.mock")

    with pytest.raises(singleton.KISMockSendBoundaryRejected) as excinfo:
        await singleton.assert_kis_mock_send_boundary(
            client=FakeKISClient(),
            url=MOCK_BASE_URL + ORDER_PATH,
            entry=kiwoom_entry,
            grant=grant,
        )
    assert excinfo.value.reason_code == singleton.KIS_MOCK_LANE_PROFILE_MISMATCH


@pytest.mark.asyncio
async def test_the_exact_mock_client_and_profile_passes_every_gate():
    _, envelope = _attempt_envelope()
    entry = _bound_kis_entry(envelope)
    grant = await _grant_for(envelope, entry)
    client = FakeKISClient()

    netloc = await singleton.assert_kis_mock_send_boundary(
        client=client, url=client._kis_url(ORDER_PATH), entry=entry, grant=grant
    )
    assert netloc == MOCK_NETLOC
    assert grant._scope.calls == 1  # type: ignore[attr-defined]


@pytest.mark.asyncio
async def test_the_pre_send_hook_reproves_the_gate_on_every_attempt():
    """Re-sends are separate POSTs; one attestation per callback is not enough."""

    _, envelope = _attempt_envelope()
    entry = _bound_kis_entry(envelope)
    grant = await _grant_for(envelope, entry)
    client = FakeKISClient()
    chained_calls = {"n": 0}

    async def _chained() -> None:
        chained_calls["n"] += 1

    hook = singleton.build_kis_mock_send_boundary_hook(
        client=client, path=ORDER_PATH, entry=entry, grant=grant, chained=_chained
    )
    await hook()
    await hook()
    await hook()

    assert grant._scope.calls == 3  # type: ignore[attr-defined]
    assert chained_calls["n"] == 3

    # And a client that drifts to a live host mid-flight is stopped at the very
    # next attempt rather than at release time.
    client._settings.kis_base_url = LIVE_BASE_URL
    with pytest.raises(singleton.KISMockSendBoundaryRejected):
        await hook()
    assert client.transport_calls == 0


@pytest.mark.asyncio
async def test_the_send_boundary_gate_runs_inside_a_real_coordinated_section():
    _, envelope = _attempt_envelope()
    stack = _stack()
    entry = _bound_kis_entry(envelope)
    client = FakeKISClient()
    proven: list[str] = []

    async def _mutation(grant: singleton.KISMockCoordinationGrant):
        hook = singleton.build_kis_mock_send_boundary_hook(
            client=client, path=ORDER_PATH, entry=entry, grant=grant
        )
        await hook()
        proven.append("gated")
        await client.send()
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="0000117058"
        )

    result = await _coordinate(envelope, stack, _mutation)

    assert proven == ["gated"]
    assert client.transport_calls == 1
    assert result.certainty is MutationCertainty.DEFINITIVE
    assert stack["evidence"].only.broker_order_id == "0000117058"


# ===========================================================================
# §E-5 — reservation / restart / release
# ===========================================================================


@pytest.mark.asyncio
async def test_an_explicit_pre_send_block_releases_only_the_exact_matched_row(
    monkeypatch, _clean_intents, _mock_route, db_session
):
    async def _blocked(**kwargs: Any):
        raise PreSendFreshnessError(("approval_window:EXPIRED",))

    async def _baseline(**kwargs: Any) -> None:
        return None

    import app.mcp_server.tooling.kis_mock_ledger as ledger

    monkeypatch.setattr(oe, "_execute_order", _blocked)
    monkeypatch.setattr(ledger, "_fetch_kis_mock_baseline_qty", _baseline)

    result = await oe._execute_and_record(**_execute_kwargs())
    assert result["pre_send_blocked"] is True

    row_id = await OrderSendIntentService(db_session).reserve(
        account_scope="kis_mock", idempotency_key="kis-mock:rob1263:acceptance"
    )
    assert isinstance(row_id, int)


@pytest.mark.asyncio
async def test_timeout_after_the_send_boundary_holds_the_claim_and_offers_no_retry(
    _traced, _clean_intents, _mock_route, db_session
):
    tracker = OrderSendOutcomeTracker()
    tracker.mark_dispatched()  # the POST crossed the boundary

    async def _fail(**kwargs: Any):
        raise httpx.ReadTimeout("no broker response")

    oe._execute_order = _fail  # type: ignore[assignment]
    with pytest.raises(oe.OrderSendOutcomeUnknown) as excinfo:
        await oe._execute_and_record(**_execute_kwargs(send_outcome=tracker))

    assert getattr(excinfo.value, "retry_allowed", False) is False
    augmented = oe._augment_error_for_unknown_outcome(
        {"success": False, "error": "ReadTimeout"},
        excinfo.value,
        market_type="equity_kr",
        is_mock=True,
    )
    assert augmented["outcome_unknown"] is True
    assert augmented.get("retry_allowed", False) is False
    # The claim is retained: a same-key repost fails closed.
    with pytest.raises(Exception) as dup:
        await OrderSendIntentService(db_session).reserve(
            account_scope="kis_mock", idempotency_key="kis-mock:rob1263:acceptance"
        )
    assert type(dup.value).__name__ == "DuplicateOrderIntent"


@pytest.mark.asyncio
async def test_a_missing_outcome_tracker_also_holds_the_claim(
    _traced, _clean_intents, _mock_route, db_session
):
    """No tracker proves less than an ambiguous one, so it cannot release."""

    async def _fail(**kwargs: Any):
        raise httpx.ConnectError("mock broker outage")

    oe._execute_order = _fail  # type: ignore[assignment]
    with pytest.raises(oe.OrderSendOutcomeUnknown) as excinfo:
        await oe._execute_and_record(**_execute_kwargs())

    assert getattr(excinfo.value, "retry_allowed", False) is False
    with pytest.raises(Exception) as dup:
        await OrderSendIntentService(db_session).reserve(
            account_scope="kis_mock", idempotency_key="kis-mock:rob1263:acceptance"
        )
    assert type(dup.value).__name__ == "DuplicateOrderIntent"


@pytest.mark.asyncio
async def test_a_restart_that_sees_an_unresolved_claim_makes_zero_repost(
    _traced, _clean_intents, _mock_route, db_session
):
    """A survivor claim blocks the identical send instead of re-issuing it."""

    async def _fail(**kwargs: Any):
        raise httpx.ReadTimeout("outcome unknown")

    oe._execute_order = _fail  # type: ignore[assignment]
    with pytest.raises(oe.OrderSendOutcomeUnknown):
        await oe._execute_and_record(**_execute_kwargs())

    before = _traced["counters"]["send"]
    # "Restart": a fresh call with the identical lineage.
    result = await oe._execute_and_record(**_execute_kwargs())
    assert result["success"] is False
    assert _traced["counters"]["send"] == before  # zero additional POSTs


@pytest.mark.parametrize(
    "evidence",
    [
        TerminalClaimEvidence(),  # nothing proven
        TerminalClaimEvidence(lane_native_terminal_evidence=True),  # no reconcile
        TerminalClaimEvidence(
            lane_native_terminal_evidence=True, account_position_reconciled=True
        ),  # unknown remainder
        TerminalClaimEvidence(authoritative_absence_proven=True),  # no reconcile
    ],
)
@pytest.mark.asyncio
async def test_soft_cancel_class_evidence_can_never_release_a_claim(evidence):
    intents = FakeIntents()
    claims = DurableSendClaimAdapter(intents)
    claim = await claims.reserve(
        scope=physical_account_scope_for_entry(
            _bound_kis_entry(_attempt_envelope()[1])
        ),
        idempotency_key="mock-idempotency-v1:deadbeef",
        side="buy",
    )
    with pytest.raises(CoordinationError) as excinfo:
        await claims.release_with_terminal_evidence(claim, evidence)
    assert (
        excinfo.value.reason_code is CoordinationReasonCode.TERMINAL_EVIDENCE_REQUIRED
    )
    assert intents.release_if_matches_calls == []
    assert intents.rows  # still reserved


@pytest.mark.asyncio
async def test_terminal_release_requires_full_evidence_and_an_exact_owner_match():
    intents = FakeIntents()
    claims = DurableSendClaimAdapter(intents)
    scope = physical_account_scope_for_entry(_bound_kis_entry(_attempt_envelope()[1]))
    claim = await claims.reserve(
        scope=scope, idempotency_key="mock-idempotency-v1:deadbeef", side="buy"
    )
    evidence = TerminalClaimEvidence(
        lane_native_terminal_evidence=True,
        account_position_reconciled=True,
        remainder_known=True,
    )

    # A foreign owner (wrong key) deletes nothing.
    foreign = replace(claim, idempotency_key="mock-idempotency-v1:someone-else")
    assert await claims.release_with_terminal_evidence(foreign, evidence) == 0
    assert intents.rows

    assert await claims.release_with_terminal_evidence(claim, evidence) == 1
    assert not intents.rows


# ===========================================================================
# §E-6 — cancellation and follow-up
# ===========================================================================


@pytest.mark.asyncio
async def test_cancellation_during_the_inner_write_retains_grant_and_claim():
    _, envelope = _attempt_envelope()
    started = asyncio.Event()
    release = asyncio.Event()
    stack = _stack()

    async def _mutation(grant: singleton.KISMockCoordinationGrant):
        started.set()
        await release.wait()
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="0000117058"
        )

    task = asyncio.ensure_future(_coordinate(envelope, stack, _mutation))
    await started.wait()
    task.cancel()
    await asyncio.sleep(0)
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task

    # The definite outcome was persisted before any cleanup, and the durable
    # claim was never released by the coordinator.
    assert stack["evidence"].calls == 1
    assert stack["evidence"].only.outer_cancellation_requested is True
    assert stack["intents"].release_if_matches_calls == []
    assert stack["intents"].rows


@pytest.mark.parametrize(
    ("kwargs", "why"),
    [
        ({"lane_capability_supports_operation": False}, "no broker capability"),
        ({"native_order_id": None}, "no attributed native order id"),
        ({"native_order_id": "   "}, "blank native order id"),
        ({"known_remainder": None}, "unknown remainder"),
        ({"fresh_guards_passed": False}, "stale guards"),
        ({"operation": "increase"}, "not a describable follow-up operation"),
        ({"claim_idempotency_key": None}, "no durable claim identity"),
        (
            {"claim_idempotency_key": "mock-idempotency-v1:someone-elses-order"},
            "a claim this grant does not own",
        ),
        (
            {"claim_account_scope": "mockpa:v1:another-account"},
            "a foreign account scope",
        ),
    ],
)
@pytest.mark.asyncio
async def test_each_missing_followup_element_independently_blocks(kwargs, why):
    _, envelope = _attempt_envelope()
    entry = _bound_kis_entry(envelope)
    grant = await _grant_for(envelope, entry)
    base: dict[str, Any] = {
        "operation": "cancel",
        "native_order_id": "0000117058",
        "known_remainder": Decimal("2"),
        "lane_capability_supports_operation": True,
        "fresh_guards_passed": True,
        "claim_account_scope": grant.claim_account_scope,
        "claim_idempotency_key": grant.claim_idempotency_key,
    }
    base.update(kwargs)
    with _held_grant(grant):
        decision = await singleton.authorize_kis_mock_claim_followup(**base)
    assert decision.authorized is False, why
    assert decision.reason_code == "claim_followup_not_authorized"
    assert decision.releases_durable_claim is False


@pytest.mark.asyncio
async def test_a_complete_followup_is_a_capability_and_still_never_releases_a_claim():
    _, envelope = _attempt_envelope()
    grant = await _grant_for(envelope, _bound_kis_entry(envelope))
    with _held_grant(grant):
        decision = await singleton.authorize_kis_mock_claim_followup(
            operation="cancel",
            native_order_id="0000117058",
            known_remainder=Decimal("2"),
            lane_capability_supports_operation=True,
            fresh_guards_passed=True,
            claim_account_scope=grant.claim_account_scope,
            claim_idempotency_key=grant.claim_idempotency_key,
        )
    assert decision.authorized is True
    assert decision.reason_code is None
    assert decision.releases_durable_claim is False
    with pytest.raises(FrozenInstanceError):
        decision.releases_durable_claim = True  # type: ignore[misc]


@pytest.mark.asyncio
async def test_unknown_remainder_is_never_replaced_with_quantity_one(monkeypatch):
    """The exact `int(...) or 1` defect: a guess sent to the broker as truth."""

    calls: list[dict[str, Any]] = []

    async def _resolve(order_no: str):
        return {
            "ledger_id": 1,
            "symbol": "005930",
            "side": "buy",
            "quantity": 0.0,  # remainder unknown
            "price": 70000.0,
            "krx_fwdg_ord_orgno": "00950",
            "instrument_type": "equity_kr",
            "lifecycle_state": "accepted",
        }

    async def _mark(**kwargs: Any) -> None:
        calls.append({"mark": kwargs})

    broker_clients: list[dict[str, Any]] = []

    def _client(**kwargs: Any):
        # Counted rather than raised: the caller wraps broker errors in a
        # `success: False` dict, so an exception here would look like a pass.
        broker_clients.append(kwargs)
        raise AssertionError("an unauthorized follow-up must make zero broker calls")

    import app.mcp_server.tooling.kis_mock_ledger as ledger

    monkeypatch.setattr(ledger, "resolve_mock_order_for_cancel", _resolve)
    monkeypatch.setattr(ledger, "mark_kis_mock_order_cancelled", _mark)
    monkeypatch.setattr(omc, "_create_kis_client", _client)

    result = await omc._cancel_kis_mock_domestic("0000117058", "005930")

    assert broker_clients == []  # zero transport, not merely a swallowed error
    assert result["success"] is False
    assert result.get("reason_code") == "claim_followup_not_authorized"
    assert result.get("claim_released") is False
    assert calls == []  # the ledger was not marked cancelled either


@pytest.mark.asyncio
async def test_a_missing_orgno_neither_calls_the_broker_nor_releases_the_claim(
    monkeypatch,
):
    calls: list[str] = []

    async def _resolve(order_no: str):
        return {
            "ledger_id": 1,
            "symbol": "005930",
            "side": "buy",
            "quantity": 2.0,
            "price": 70000.0,
            "krx_fwdg_ord_orgno": None,  # no native cancel capability
            "instrument_type": "equity_kr",
            "lifecycle_state": "accepted",
        }

    async def _mark(**kwargs: Any) -> None:
        calls.append("marked")

    import app.mcp_server.tooling.kis_mock_ledger as ledger

    monkeypatch.setattr(ledger, "resolve_mock_order_for_cancel", _resolve)
    monkeypatch.setattr(ledger, "mark_kis_mock_order_cancelled", _mark)
    monkeypatch.setattr(
        omc,
        "_create_kis_client",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("zero broker calls")),
    )

    result = await omc._cancel_kis_mock_domestic("0000117058", "005930")

    assert result["success"] is False
    assert result["reason_code"] == "claim_followup_not_authorized"
    assert result["claim_released"] is False
    assert calls == []


# ===========================================================================
# §83 — lane-native recovery ownership (an activation precondition)
# ===========================================================================


def test_the_lane_contract_names_exactly_one_recovery_owner():
    owner = singleton.KIS_MOCK_LANE_RECOVERY_CONTRACT["recovery_owner"]
    assert owner.strip()
    assert "TBD" not in owner.upper()


def test_the_lane_contract_names_a_restart_trigger_and_a_readback_operation():
    contract = singleton.KIS_MOCK_LANE_RECOVERY_CONTRACT
    assert contract["restart_trigger"].strip()
    assert "inquire_daily_order_domestic" in contract["readback_operation"]
    # C3-5 names the exact release condition. The lane states it as the only
    # entry point it is allowed to use — the evidence-gated adapter method —
    # rather than the unrestricted J3A delete underneath it.
    release_condition = contract["release_if_matches"]
    assert "release_with_terminal_evidence" in release_condition
    assert "TerminalClaimEvidence" in release_condition
    assert "remainder_known" in release_condition
    assert contract["blocked_state"] == "AUTO_READY_BLOCKED_BY_LIFECYCLE"


def test_all_seven_lane_native_evidence_kinds_are_declared():
    assert singleton.KIS_MOCK_LANE_EVIDENCE_KINDS == (
        "ack",
        "unknown",
        "reject",
        "expiry",
        "partial_fill",
        "cancel",
        "terminal_reconciliation",
    )


def test_a_lane_missing_any_lifecycle_element_is_blocked_not_merely_warned():
    blocked = singleton.describe_kis_mock_lifecycle_readiness(
        singleton.KISMockLanePorts()
    )
    assert blocked.status == "AUTO_READY_BLOCKED_BY_LIFECYCLE"
    assert blocked.ready is False
    assert "lineage_persistence_port" in blocked.missing
    assert "lane_evidence:partial_fill" in blocked.missing

    partial = singleton.describe_kis_mock_lifecycle_readiness(
        singleton.KISMockLanePorts(
            persistence=RecordingPersistence(),
            dispatch_evidence=RecordingDispatchEvidence(),
            uncertainty_gate=FakeUncertaintyGate(),
            evidence_kinds=("ack", "unknown"),
        )
    )
    assert partial.status == "AUTO_READY_BLOCKED_BY_LIFECYCLE"
    assert partial.ready is False


@pytest.mark.asyncio
async def test_a_lifecycle_blocked_lane_makes_zero_lease_claim_or_callback_calls():
    _, envelope = _attempt_envelope()
    stack = _stack()
    called = {"n": 0}

    async def _mutation(_grant: Any):  # pragma: no cover
        called["n"] += 1
        raise AssertionError("a blocked lane must never reach the callback")

    with pytest.raises(singleton.KISMockCoordinationBlocked) as excinfo:
        await _coordinate(
            envelope, stack, _mutation, ports=singleton.KISMockLanePorts()
        )

    assert excinfo.value.reason_code == singleton.KIS_MOCK_LIFECYCLE_PORTS_UNAVAILABLE
    assert "AUTO_READY_BLOCKED_BY_LIFECYCLE" in str(excinfo.value)
    assert called["n"] == 0
    assert stack["factory"].calls == 0
    assert stack["intents"].reserve_calls == []
    assert stack["persistence"].calls == 0
    assert stack["connection"].lock_calls == []


def test_the_canonical_registry_keeps_this_lane_unarmed():
    """Production identity is unknown, so the AUTO path is unreachable today."""

    from app.services import mock_lane_registry as registry

    entry = registry.get_lane_registry_entry("kr.kis.mock")
    assert entry.physical_account_id is None
    assert entry.identity_status == "UNKNOWN"
    assert entry.auto_order_enabled is False
    with pytest.raises(LaneGuardError):
        registry.assert_entry_execution_ready(entry)
    with pytest.raises(LaneGuardError):
        physical_account_scope_for_entry(entry)


# ===========================================================================
# §E-7 — static boundaries
# ===========================================================================


def _adapter_source() -> str:
    return ADAPTER_SOURCE.read_text(encoding="utf-8")


def test_the_adapter_opens_no_socket_and_imports_no_transport_or_llm():
    tree = ast.parse(_adapter_source())
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    forbidden_roots = (
        "httpx",
        "requests",
        "aiohttp",
        "socket",
        "websockets",
        "google.generativeai",
        "openai",
        "anthropic",
        "taskiq",
        "prefect",
        "celery",
        "alembic",
    )
    offenders = sorted(
        name
        for name in imported
        if any(name == root or name.startswith(f"{root}.") for root in forbidden_roots)
    )
    assert offenders == [], offenders


def test_the_adapter_registers_no_scheduler_and_writes_no_schema():
    source = _adapter_source().upper()
    for forbidden in (
        "CREATE TABLE",
        "ALTER TABLE",
        "INSERT INTO",
        "DELETE FROM",
        "OP.CREATE",
        "@BROKER.TASK",
        "CRONTAB",
        "LAUNCHCTL",
    ):
        assert forbidden not in source, forbidden


def test_the_adapter_does_not_reimplement_the_j3a_primitive():
    """J3A owns the key math, the row proof, the rollback, and the unlock."""

    source = _adapter_source()
    j3a_only = (
        "pg_locks",
        "objsubid",
        "ExclusiveLock",
        "pg_terminate_backend",
        "_rollback_partial_acquisition",
        "mock-physical-account-v1",
    )
    offenders = [marker for marker in j3a_only if marker in source]
    assert offenders == [], offenders

    # The evidence-gated delete is J3A's: the lane may *name* it in its recovery
    # contract but must neither define nor invoke it.
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            assert node.name not in {
                "release_if_matches",
                "release_with_terminal_evidence",
            }
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in {
                "release_if_matches",
                "release_with_terminal_evidence",
            }, ast.dump(node.func)

    # And the lane consumes the primitive's public symbols rather than shadowing
    # them: the physical key is never derived locally.
    assert "physical_account_scope_for_entry" in source
    assert "ordered_advisory_keyset" in source
    assert "coordinate_mock_order_mutation" in source


def test_the_lane_reason_codes_do_not_collide_with_j3a_or_j2b():
    from app.services.mock_integration.coordination import COORDINATION_REASON_CODES
    from app.services.mock_integration.lineage import LINEAGE_REASON_CODES

    assert not (singleton.KIS_MOCK_REASON_CODES & COORDINATION_REASON_CODES)
    assert not (singleton.KIS_MOCK_REASON_CODES & LINEAGE_REASON_CODES)


def test_c5_the_lane_introduces_no_taskgroup_or_timeout_around_the_send():
    """C5 is carried, not closed: the lane refuses to add the construct at all.

    J3A left the TaskGroup / ``asyncio.timeout`` cancellation-count question
    UNKNOWN and J4-V confirmed that state through five rounds.  J3B cannot
    resolve a question about J3A's internal retained-task semantics from outside
    it; what J3B *can* do — and does here — is guarantee the lane never wraps a
    coordinated section in either construct, so the lane adds no new instance of
    the unknown.  The status stays UNKNOWN and is recorded in the contract doc.
    """

    tree = ast.parse(_adapter_source())
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute):
            assert node.attr not in {"TaskGroup", "timeout", "wait_for"}, ast.dump(node)
        if isinstance(node, ast.Name):
            assert node.id != "TaskGroup"


def test_no_live_host_or_env_gate_was_widened():
    source = _adapter_source()
    assert "openapi.koreainvestment.com" not in source
    assert singleton.KIS_MOCK_VTS_NETLOC == "openapivts.koreainvestment.com:29443"
    # The lane never reads a credential value or prints one.
    assert "kis_app_secret" not in source
    assert "KIS_MOCK_APP_SECRET" not in source


def test_the_contract_document_exists_and_carries_the_lifecycle_status():
    text = CONTRACT_DOC.read_text(encoding="utf-8")
    for required in (
        "AUTO_READY_BLOCKED_BY_LIFECYCLE",
        "recovery owner",
        "inquire_daily_order_domestic",
        "release_if_matches",
        "terminal_reconciliation",
        "C5",
    ):
        assert required in text, required


# --- write fence: predicate + allow-list, checked without invoking git --------
#
# ROB-1263 r5: this used to shell out to `git merge-base origin/main HEAD` and
# assert over the real diff. It was correctly fail-closed — and therefore red in
# CI, where the checkout is shallow and `origin/main` is not a local ref, so the
# fence was *unprovable* rather than violated.
#
# The repository already settled this question. The merged fencefix (#1881)
# removed exactly this class of test from pytest and moved enforcement to
# `scripts/ci/write_fence_check.py`, run by the `write-fence-guard` job with
# `fetch-depth: 0` so the base commit is always resolvable. What stays in pytest
# is what pytest can actually prove: the allow-list itself and the predicate
# over it. The real diff for this branch is recorded in the round report.


def paths_within_write_fence(paths: object) -> bool:
    """The J3B write fence, as a pure predicate over changed paths."""

    considered = {
        path
        for path in paths  # type: ignore[union-attr]
        if not str(path).startswith(FENCE_EXEMPT_PREFIXES)
    }
    return considered <= set(J3B_WRITE_FENCE)


def test_the_write_fence_allow_list_is_exactly_the_briefed_set():
    """The allow-list is the B-7 set plus the two files orch approved in r5."""

    assert J3B_WRITE_FENCE == frozenset(
        {
            "app/services/kis_mock_runner/singleton.py",
            "app/services/brokers/kis/domestic_orders.py",
            "app/mcp_server/tooling/order_execution.py",
            "app/mcp_server/tooling/orders_modify_cancel.py",
            "tests/services/kis_mock_runner/test_singleton.py",
            "tests/services/kis_mock_runner/test_domestic_route.py",
            "tests/test_mcp_place_order.py",
            "tests/services/mock_integration/test_kis_coordination_adapter.py",
            "docs/contracts/rob-1263-kis-coordination-adapter.md",
            "tests/test_services_kis_logging.py",
            "tests/test_rob750_mock_mirror_intent_release.py",
            "tests/test_kis_mock_order_ledger.py",
            "tests/brokers/kis/mock_scalping_exec/test_reservation.py",
            "tests/brokers/kis/mock_scalping_exec/test_pre_send_transport.py",
            "tests/test_kis_mock_cancel_modify.py",
            "tests/services/test_kis_mock_attribution_chain.py",
            "tests/test_kis_mock_lifecycle_reconciliation_acceptance.py",
        }
    )


def test_the_write_fence_predicate_reds_on_an_out_of_fence_path():
    assert paths_within_write_fence(set(J3B_WRITE_FENCE)) is True
    assert paths_within_write_fence({".smoke-out/rob179-feed-research-evidence.json"})
    for out_of_fence in (
        # The files this job reverted rather than widened the fence to reach.
        "app/services/brokers/kis/overseas_orders.py",
        "tests/services/mock_integration/test_coordination.py",
        # And the upstream surfaces that are read/import only.
        "app/services/mock_integration/coordination.py",
        "app/services/mock_lane_registry.py",
        "app/services/brokers/client_order_ids.py",
        "alembic/versions/deadbeef_add_kis_mock.py",
        ".github/workflows/test.yml",
    ):
        assert paths_within_write_fence({out_of_fence}) is False, out_of_fence


# ===========================================================================
# r2 §B1 — the coordinator and the per-POST hook have production call sites
# ===========================================================================


class _FakeKISParent:
    """The minimum of ``KISClient`` that ``DomesticOrderClient`` touches."""

    def __init__(self, *, is_mock_client: bool = True) -> None:
        self._is_mock_client = is_mock_client
        self._settings_view = FakeSettingsView(
            is_mock=is_mock_client, base_url=MOCK_BASE_URL
        )
        self._settings_view.kis_access_token = "token"
        self._hdr_base = {"content-type": "application/json"}
        self.requests: list[dict[str, Any]] = []
        self.authority_at_wire: list[Any] = []

    @property
    def _settings(self) -> Any:
        return self._settings_view

    async def _ensure_token(self) -> None:
        return None

    def _kis_url(self, path: str) -> str:
        return f"{self._settings_view.kis_base_url.rstrip('/')}{path}"

    async def _request_with_rate_limit(self, method, url, **kwargs):
        # Recorded *at the wire*: this is the seam B-2 is about.
        self.authority_at_wire.append(singleton.active_writer_authority())
        self.requests.append({"method": method, "url": url, **kwargs})
        return {"rt_cd": "0", "odno": "0000117058", "ord_tmd": "0901"}


def _domestic_client(*, is_mock_client: bool = True):
    from app.services.brokers.kis.domestic_orders import DomesticOrderClient

    parent = _FakeKISParent(is_mock_client=is_mock_client)
    return DomesticOrderClient(parent), parent  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_env_gate_false_no_longer_lets_a_mock_mutation_reach_the_wire_unowned(
    monkeypatch,
):
    """The exact B-2 defect the round-1 branch left open.

    With `KIS_MOCK_RUNNER_ENABLED` unset, the old wrapper became a no-op and an
    `is_mock=True` place reached the transport holding no authority at all.
    """

    monkeypatch.delenv("KIS_MOCK_RUNNER_ENABLED", raising=False)
    client, parent = _domestic_client()

    await client.order_korea_stock("005930", "buy", 1, 70000, True)

    assert len(parent.authority_at_wire) == 1
    authority = parent.authority_at_wire[0]
    assert authority is not None, "a mock mutation reached the wire holding nothing"
    assert authority.account_mode == singleton.ACCOUNT_MODE
    assert singleton.kis_mock_legacy_advisory_key() in authority.advisory_keys
    # And the authority is dropped again once the mutation returns.
    assert singleton.active_writer_authority() is None


@pytest.mark.asyncio
async def test_the_live_branch_never_enters_lane_code(monkeypatch):
    """`is_mock=False` must be untouched: no authority, no lane hook, no gate."""

    monkeypatch.delenv("KIS_MOCK_RUNNER_ENABLED", raising=False)
    import app.services.brokers.kis.domestic_orders as domestic_orders

    async def _never(*args: Any, **kwargs: Any):  # pragma: no cover
        raise AssertionError("the live branch must not reach lane authority code")

    monkeypatch.setattr(singleton, "kis_mock_mutation_authority", _never, raising=True)
    monkeypatch.setattr(domestic_orders, "is_nxt_eligible", _always_false)

    client, parent = _domestic_client(is_mock_client=False)
    await client.order_korea_stock("005930", "buy", 1, 70000, False)

    assert len(parent.authority_at_wire) == 1
    assert parent.authority_at_wire[0] is None
    assert parent.requests[0].get("pre_send_hook") is None


async def _always_false(*args: Any, **kwargs: Any) -> bool:
    return False


@pytest.mark.asyncio
async def test_a_held_grant_composes_the_per_post_boundary_gate_into_the_transport():
    """`build_kis_mock_send_boundary_hook` has a real production caller.

    The decorator composes it into the transport's `pre_send_hook`, so firing
    that hook re-proves ownership — which is what per-POST means.
    """

    _, envelope = _attempt_envelope()
    entry = _bound_kis_entry(envelope)
    grant = await _grant_for(envelope, entry)
    client, parent = _domestic_client()

    with _held_grant(grant):
        await client.order_korea_stock("005930", "buy", 1, 70000, True)

    hook = parent.requests[0].get("pre_send_hook")
    assert hook is not None, "the coordinated wire path installed no boundary hook"
    before = grant._scope.calls  # type: ignore[attr-defined]
    await hook()
    assert grant._scope.calls == before + 1  # type: ignore[attr-defined]

    # And the composed hook really is the B-4 gate: drift the client to a live
    # host and the next attempt is refused before any transport work.
    parent._settings_view.kis_base_url = LIVE_BASE_URL
    with pytest.raises(singleton.KISMockSendBoundaryRejected):
        await hook()


@pytest.mark.asyncio
async def test_the_cancel_and_modify_wire_paths_also_receive_the_boundary_gate():
    """Follow-ups get the same per-POST gate — and need their own capability."""

    _, envelope = _attempt_envelope()
    entry = _bound_kis_entry(envelope)
    grant = await _grant_for(envelope, entry)

    for call, operation in (
        ("cancel", singleton.KISMockOperation.FOLLOWUP_CANCEL),
        ("modify", singleton.KISMockOperation.FOLLOWUP_MODIFY),
    ):
        client, parent = _domestic_client()
        with _held_grant(grant):
            async with await _issue(
                operation=operation,
                claim_account_scope=grant.claim_account_scope,
                claim_idempotency_key=grant.claim_idempotency_key,
                reservations=_FakeReservations((grant.claim_idempotency_key,)),
            ):
                if call == "cancel":
                    await client.cancel_korea_order(
                        "0000117058", "005930", 1, 70000, "buy", True, "00950"
                    )
                else:
                    await client.modify_korea_order(
                        "0000117058", "005930", 1, 70000, True, "00950"
                    )
        assert parent.requests[0].get("pre_send_hook") is not None, call


@pytest.mark.asyncio
async def test_a_followup_without_a_capability_never_reaches_the_kr_wire(monkeypatch):
    """The enforcement point: the transport boundary itself refuses.

    The legacy lease is stubbed to a no-op so that *removing the capability
    check* lets the call run all the way to the wire. Without this the mutant
    dies on an unrelated database error before reaching the assertion, which
    would score a red that proves nothing.
    """

    @contextlib.asynccontextmanager
    async def _no_db_lease(**kwargs: Any):
        token = singleton._ACTIVE_WRITER_LEASE.set(
            singleton._WriterAuthority(
                account_mode=singleton.ACCOUNT_MODE,
                advisory_keys=(singleton.kis_mock_legacy_advisory_key(),),
                lease=object(),
            )
        )
        try:
            yield
        finally:
            singleton._ACTIVE_WRITER_LEASE.reset(token)

    monkeypatch.setattr(singleton, "enforce_kis_mock_mutation_writer", _no_db_lease)

    client, parent = _domestic_client()
    with pytest.raises(singleton.KISMockFollowupNotAuthorized):
        await client.cancel_korea_order(
            "0000117058", "005930", 1, 70000, "buy", True, "00950"
        )
    assert parent.requests == []

    with pytest.raises(singleton.KISMockFollowupNotAuthorized):
        await client.modify_korea_order("0000117058", "005930", 1, 70000, True, "00950")
    assert parent.requests == []

    # Positive control: the same stubbed lease with a valid receipt *does* reach
    # the wire, so the assertion above is about the capability and nothing else.
    async with await _issue():
        await client.cancel_korea_order(
            "0000117058", "005930", 1, 70000, "buy", True, "00950"
        )
    assert len(parent.requests) == 1


@pytest.mark.asyncio
async def test_execute_and_record_routes_a_mock_send_through_the_coordinator(
    monkeypatch, _clean_intents
):
    """The production route: `_execute_and_record` → `coordinate_kis_mock_mutation`.

    This exercises the real `order_execution` entry point, not a hand-built
    callback. Deleting the `run_kis_mock_send(...)` call site makes it fail.
    """

    _, envelope = _attempt_envelope()
    events: list[str] = []
    stack = _stack(events)

    route = singleton.KISMockCoordinationRoute(
        envelope=envelope,
        ports=_ready_ports(
            persistence=stack["persistence"],
            dispatch_evidence=stack["evidence"],
            uncertainty_gate=stack["gate"],
        ),
        claims=DurableSendClaimAdapter(stack["intents"]),
        connection_factory=stack["factory"],
        registry=_kis_registry(envelope),
    )
    singleton.set_kis_mock_coordination_route_provider(lambda **_ctx: route)

    sends = {"n": 0}

    async def _send(**kwargs: Any) -> dict[str, Any]:
        sends["n"] += 1
        events.append("http_send")
        # Ownership must be provable from inside the coordinated section.
        authority = singleton.active_writer_authority()
        assert authority is not None and authority.grant is not None
        return {"odno": "0000117058", "rt_cd": "0", "msg1": "ok"}

    async def _baseline(**kwargs: Any) -> None:
        return None

    import app.mcp_server.tooling.kis_mock_ledger as ledger

    monkeypatch.setattr(oe, "_execute_order", _send)
    monkeypatch.setattr(ledger, "_fetch_kis_mock_baseline_qty", _baseline)

    try:
        await oe._execute_and_record(**_execute_kwargs())
    finally:
        singleton.set_kis_mock_coordination_route_provider(None)

    assert sends["n"] == 1
    # J3A's ordering carried the real send: lineage persisted and the binary
    # claim was committed before the POST, and the typed dispatch evidence
    # landed after it.
    assert (
        events.index("persist_pre")
        < events.index("reserve")
        < events.index("http_send")
    )
    assert events.index("http_send") < events.index("evidence")
    assert stack["connection"].lock_calls, "no advisory lease was taken"
    assert stack["evidence"].only.broker_order_id == "0000117058"


@pytest.mark.asyncio
async def test_without_a_route_the_lane_sends_nothing_at_all():
    """r4 §3 — the adapter is the final enforcement point.

    This previously sent anyway and merely withheld the AUTO label, which takes
    the label off a bypass instead of closing it. A send with no coordination
    authority does not happen.
    """

    sends = {"n": 0}

    async def _send() -> dict[str, Any]:
        sends["n"] += 1  # pragma: no cover - must never run
        return {"odno": "0000117058"}

    singleton.set_kis_mock_coordination_route_provider(None)
    with pytest.raises(singleton.KISMockCoordinationBlocked) as excinfo:
        await singleton.run_kis_mock_send(send=_send)

    assert sends["n"] == 0
    assert excinfo.value.reason_code == singleton.KIS_MOCK_LIFECYCLE_PORTS_UNAVAILABLE
    assert "AUTO_READY_BLOCKED_BY_LIFECYCLE" in str(excinfo.value)


def test_the_coordinator_call_site_exists_in_the_production_order_path():
    """A route can only be honoured if `order_execution` actually calls it."""

    source = (
        REPO_ROOT / "app" / "mcp_server" / "tooling" / "order_execution.py"
    ).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "run_kis_mock_send" in called

    adapter = _adapter_source()
    adapter_tree = ast.parse(adapter)
    inner_calls = {
        node.func.id
        for node in ast.walk(adapter_tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "coordinate_mock_order_mutation" in inner_calls
    assert "build_kis_mock_send_boundary_hook" in inner_calls


# ===========================================================================
# r2 §B3 — pre-send release is exact-row matched
# ===========================================================================


@pytest.mark.asyncio
async def test_a_stale_pre_send_release_cannot_delete_a_replacement_reservation(
    monkeypatch, _clean_intents, _mock_route, db_session
):
    """Releasing by (scope, key) alone would delete someone else's row."""

    key = "kis-mock:rob1263:acceptance"
    service = OrderSendIntentService(db_session)
    captured: dict[str, Any] = {}

    async def _blocked(**kwargs: Any):
        await service.release(account_scope="kis_mock", idempotency_key=key)
        captured["replacement_id"] = await service.reserve(
            account_scope="kis_mock", idempotency_key=key, side="buy"
        )
        raise PreSendFreshnessError(("approval_window:EXPIRED",))

    async def _baseline(**kwargs: Any) -> None:
        return None

    import app.mcp_server.tooling.kis_mock_ledger as ledger

    monkeypatch.setattr(oe, "_execute_order", _blocked)
    monkeypatch.setattr(ledger, "_fetch_kis_mock_baseline_qty", _baseline)

    await oe._execute_and_record(**_execute_kwargs())

    remaining = await service.list_reservations(account_scope="kis_mock")
    assert captured["replacement_id"] in {row.row_id for row in remaining}, (
        "the stale release deleted a replacement reservation"
    )


# ===========================================================================
# r3 §1/§2/§4 — no unguarded KIS mock mutation transport boundary exists
# ===========================================================================

OVERSEAS_ORDERS_SOURCE = (
    REPO_ROOT / "app" / "services" / "brokers" / "kis" / "overseas_orders.py"
)


def _posting_methods(path: pathlib.Path) -> dict[str, set[str]]:
    """Every async method in `path` that issues a POST, and its decorators.

    Discovery, not a hardcoded list: a *new* mutation method added tomorrow
    appears here automatically, so "we forgot to guard it" is a test failure
    rather than a silent bypass.
    """

    tree = ast.parse(path.read_text(encoding="utf-8"))
    posting: dict[str, set[str]] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.AsyncFunctionDef):
            continue
        for call in ast.walk(node):
            if not isinstance(call, ast.Call):
                continue
            func = call.func
            name = func.attr if isinstance(func, ast.Attribute) else None
            if name is None or not name.startswith("_request_with_rate_limit"):
                continue
            first = call.args[0] if call.args else None
            if isinstance(first, ast.Constant) and first.value == "POST":
                posting[node.name] = _decorator_names(node)
                break
    return posting


# The US (`us.kis.mock`) POST sites live in a module outside the B-7 write
# fence and are handled as a separate job (operator §87 ④). They are enumerated
# here so a *new* one cannot appear unnoticed — but their guard state is
# deliberately NOT asserted: pinning "unguarded" would make a known defect into
# a normal condition the suite defends. The exposure is recorded in
# `docs/contracts/rob-1263-kis-coordination-adapter.md`.
US_POST_SITES: frozenset[str] = frozenset(
    {
        # `sell_overseas_stock` delegates to `order_overseas_stock` and issues no
        # POST of its own, so it is not a transport boundary.
        "order_overseas_stock",
        "cancel_overseas_order",
        "modify_overseas_order",
    }
)


def test_every_kr_kis_posting_mutation_method_is_guarded():
    """The exhaustive bypass check over the fenced module.

    Discovery, not a list: a new POST site added to `domestic_orders.py`
    tomorrow shows up here and fails until it is guarded.
    """

    domestic = _posting_methods(DOMESTIC_ORDERS_SOURCE)
    assert set(domestic) == {
        "order_korea_stock",
        "cancel_korea_order",
        "modify_korea_order",
    }, domestic
    for name, decorators in domestic.items():
        assert "_guard_kis_mock_writer" in decorators, name


def test_the_us_post_sites_are_enumerated_without_pinning_their_guard_state():
    """Inventory only. Guarding them must not require touching this test."""

    overseas = _posting_methods(OVERSEAS_ORDERS_SOURCE)
    assert set(overseas) == US_POST_SITES, (
        "the US POST surface changed; update the separate US job and the contract"
    )
    assert "us.kis.mock" in _lane_ids_in_registry()


def _lane_ids_in_registry() -> tuple[str, ...]:
    from app.services.mock_lane_registry import CANONICAL_LANE_IDS

    return CANONICAL_LANE_IDS


# ===========================================================================
# r3 §6 — stale / foreign-lane / foreign-account grants are refused
# ===========================================================================


@pytest.mark.asyncio
async def test_a_grant_for_another_lane_cannot_authorize_this_transport():
    """A KR grant is not authority for a US send, and vice versa."""

    _, envelope = _attempt_envelope()
    grant = await _grant_for(envelope, _bound_kis_entry(envelope))
    client, parent = _domestic_client()

    with _held_grant(grant):
        with pytest.raises(singleton.KISMockSendBoundaryRejected) as excinfo:
            async with singleton.kis_mock_mutation_authority(
                client=client._parent, path=ORDER_PATH, lane_id="us.kis.mock"
            ):
                pass  # pragma: no cover - the gate raises first

    assert excinfo.value.reason_code == singleton.KIS_MOCK_LANE_PROFILE_MISMATCH
    assert parent.requests == []


@pytest.mark.asyncio
async def test_a_grant_for_another_physical_account_is_refused_at_the_boundary():
    _, envelope = _attempt_envelope()
    ours = _bound_kis_entry(envelope)
    theirs = _bound_kis_entry(
        envelope, physical_account_id="kismock:v1:a-different-account"
    )
    grant = await _grant_for(envelope, theirs)
    client = FakeKISClient()

    with pytest.raises(singleton.KISMockSendBoundaryRejected) as excinfo:
        await singleton.assert_kis_mock_send_boundary(
            client=client, url=client._kis_url(ORDER_PATH), entry=ours, grant=grant
        )
    assert excinfo.value.reason_code == singleton.KIS_MOCK_ACCOUNT_FINGERPRINT_MISMATCH
    assert client.transport_calls == 0


@pytest.mark.asyncio
async def test_a_stale_grant_whose_section_ended_cannot_authorize_a_send():
    """A captured grant must stop working once its coordinated section closes."""

    _, envelope = _attempt_envelope()
    stack = _stack()
    captured: list[Any] = []

    async def _mutation(grant: singleton.KISMockCoordinationGrant):
        captured.append(grant)
        return MutationCallbackResult(
            certainty=MutationCertainty.DEFINITIVE, broker_order_id="0000117058"
        )

    await _coordinate(envelope, stack, _mutation)

    stale = captured[0]
    entry = _bound_kis_entry(envelope)
    client = FakeKISClient()
    with pytest.raises(CoordinationError) as excinfo:
        await singleton.assert_kis_mock_send_boundary(
            client=client, url=client._kis_url(ORDER_PATH), entry=entry, grant=stale
        )
    assert excinfo.value.reason_code is CoordinationReasonCode.LEASE_LOST
    assert client.transport_calls == 0


@pytest.mark.asyncio
async def test_a_grant_with_no_coordinated_scope_cannot_be_asserted():
    _, envelope = _attempt_envelope()
    entry = _bound_kis_entry(envelope)
    scope = physical_account_scope_for_entry(entry)
    scopeless = singleton.KISMockCoordinationGrant(
        lane_id=entry.lane_id,
        claim_account_scope=scope.claim_account_scope,
        advisory_keys=singleton.kis_mock_advisory_keyset(entry),
        physical_advisory_key=scope.advisory_key,
        legacy_advisory_key=singleton.kis_mock_legacy_advisory_key(),
        credential_namespace=singleton.KIS_MOCK_CREDENTIAL_NAMESPACE,
        allowed_netlocs=(MOCK_NETLOC,),
        physical_account_id=str(entry.physical_account_id),
        entry=entry,
    )
    with pytest.raises(singleton.KISMockCoordinationBlocked):
        await scopeless.assert_owned()


# ===========================================================================
# r3 §7 — follow-up capability: short lease, J3B verifies, J5 decides
# ===========================================================================


class _FakeReservations:
    def __init__(self, keys: tuple[str, ...]) -> None:
        self._keys = keys
        self.calls: list[str] = []

    async def __call__(self, account_scope: str) -> list[Any]:
        self.calls.append(account_scope)
        from app.services.order_send_intent_service import OrderSendIntentReservation

        return [
            OrderSendIntentReservation(row_id=i + 1, idempotency_key=key, side="buy")
            for i, key in enumerate(self._keys)
        ]


CLAIM_SCOPE = "mockpa:v1:acceptance"
CLAIM_KEY = "mock-idempotency-v1:acceptance"


class _NoopLease:
    acquired = True

    async def __aenter__(self) -> _NoopLease:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None


async def _issue(**overrides: Any):
    values: dict[str, Any] = {
        "operation": singleton.KISMockOperation.FOLLOWUP_CANCEL,
        "claim_account_scope": CLAIM_SCOPE,
        "claim_idempotency_key": CLAIM_KEY,
        "attributed_broker_order_id": "0000117058",
        "known_remainder": Decimal("2"),
        "reservations": _FakeReservations((CLAIM_KEY,)),
        "lease_factory": _NoopLease,
    }
    values.update(overrides)
    return singleton.issue_kis_mock_followup_capability(**values)


@pytest.mark.asyncio
async def test_a_followup_capability_needs_a_matching_durable_claim():
    async with await _issue() as capability:
        assert capability.operation is singleton.KISMockOperation.FOLLOWUP_CANCEL
        assert capability.alive is True

    with pytest.raises(singleton.KISMockFollowupNotAuthorized) as excinfo:
        async with await _issue(reservations=_FakeReservations(("someone-else",))):
            pass  # pragma: no cover
    assert "no durable claim matches" in str(excinfo.value)


@pytest.mark.asyncio
async def test_the_capability_dies_with_its_short_critical_section():
    """The receipt must not outlive the lease — the boolean defect, again."""

    async with await _issue() as capability:
        assert singleton.active_followup_capability() is capability
    assert capability.alive is False
    assert singleton.active_followup_capability() is None
    with pytest.raises(singleton.KISMockFollowupNotAuthorized):
        singleton.verify_kis_mock_followup_capability(
            capability, operation=singleton.KISMockOperation.FOLLOWUP_CANCEL
        )


@pytest.mark.parametrize(
    ("overrides", "why"),
    [
        ({"attributed_broker_order_id": "  "}, "no attributed native order id"),
        ({"known_remainder": Decimal("0")}, "remainder is unknown"),
        ({"claim_idempotency_key": ""}, "no durable claim identity"),
    ],
)
@pytest.mark.asyncio
async def test_each_missing_capability_precondition_refuses_issuance(overrides, why):
    with pytest.raises(singleton.KISMockFollowupNotAuthorized):
        async with await _issue(
            reservations=_FakeReservations(
                (overrides.get("claim_idempotency_key", CLAIM_KEY) or CLAIM_KEY,)
            ),
            **overrides,
        ):
            pass  # pragma: no cover


def test_a_capability_for_a_different_operation_or_lane_is_refused():
    live = singleton._ScopeLiveness()
    capability = singleton.KISMockOperationCapability(
        operation=singleton.KISMockOperation.FOLLOWUP_MODIFY,
        lane_id="kr.kis.mock",
        claim_account_scope=CLAIM_SCOPE,
        claim_idempotency_key=CLAIM_KEY,
        attributed_broker_order_id="0000117058",
        known_remainder=Decimal("2"),
        claim_row_id=1,
        _live=live,
    )
    with pytest.raises(singleton.KISMockFollowupNotAuthorized):
        singleton.verify_kis_mock_followup_capability(
            capability, operation=singleton.KISMockOperation.FOLLOWUP_CANCEL
        )
    with pytest.raises(singleton.KISMockFollowupNotAuthorized):
        singleton.verify_kis_mock_followup_capability(
            capability,
            operation=singleton.KISMockOperation.FOLLOWUP_MODIFY,
            lane_id="us.kis.mock",
        )
    # The exact operation and lane pass.
    assert (
        singleton.verify_kis_mock_followup_capability(
            capability, operation=singleton.KISMockOperation.FOLLOWUP_MODIFY
        )
        is capability
    )


@pytest.mark.asyncio
async def test_a_cancel_without_a_capability_makes_zero_broker_calls(monkeypatch):
    created: list[dict[str, Any]] = []

    async def _resolve(order_no: str):
        return {
            "ledger_id": 1,
            "symbol": "005930",
            "side": "buy",
            "quantity": 2.0,
            "price": 70000.0,
            "krx_fwdg_ord_orgno": "00950",
            "instrument_type": "equity_kr",
            "lifecycle_state": "accepted",
        }

    import app.mcp_server.tooling.kis_mock_ledger as ledger

    monkeypatch.setattr(ledger, "resolve_mock_order_for_cancel", _resolve)
    monkeypatch.setattr(
        omc, "_create_kis_client", lambda **kw: created.append(kw) or object()
    )

    assert singleton.active_followup_capability() is None
    result = await omc._cancel_kis_mock_domestic("0000117058", "005930")

    assert created == []
    assert result["success"] is False
    assert result["reason_code"] == "claim_followup_not_authorized"
    assert result["claim_released"] is False


@pytest.mark.asyncio
async def test_a_cancel_holding_a_valid_capability_reaches_the_broker(monkeypatch):
    """The positive control: verification permits, it does not merely block."""

    async def _resolve(order_no: str):
        return {
            "ledger_id": 1,
            "symbol": "005930",
            "side": "buy",
            "quantity": 2.0,
            "price": 70000.0,
            "krx_fwdg_ord_orgno": "00950",
            "instrument_type": "equity_kr",
            "lifecycle_state": "accepted",
        }

    marked: list[dict[str, Any]] = []

    async def _mark(**kwargs: Any) -> None:
        marked.append(kwargs)

    class _FakeKis:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def cancel_korea_order(self, **kwargs: Any) -> dict[str, Any]:
            self.calls.append(kwargs)
            return {"odno": "REV-1", "ord_tmd": "0901"}

    fake = _FakeKis()
    import app.mcp_server.tooling.kis_mock_ledger as ledger

    monkeypatch.setattr(ledger, "resolve_mock_order_for_cancel", _resolve)
    monkeypatch.setattr(ledger, "mark_kis_mock_order_cancelled", _mark)
    monkeypatch.setattr(omc, "_create_kis_client", lambda **kw: fake)

    async with await _issue():
        result = await omc._cancel_kis_mock_domestic("0000117058", "005930")

    assert fake.calls, "a valid capability must permit the transport cancel"
    assert result["success"] is True
    assert result["broker_cancel_confirmed"] is True
    assert marked and marked[0]["broker_confirmed"] is True


def test_j3b_verifies_capabilities_and_never_issues_or_decides_one():
    """The decision belongs to J5 / the lane lifecycle owner, not the adapter."""

    for path in (
        REPO_ROOT / "app" / "mcp_server" / "tooling" / "orders_modify_cancel.py",
        REPO_ROOT / "app" / "mcp_server" / "tooling" / "order_execution.py",
        DOMESTIC_ORDERS_SOURCE,
        OVERSEAS_ORDERS_SOURCE,
    ):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                name = (
                    func.id
                    if isinstance(func, ast.Name)
                    else func.attr
                    if isinstance(func, ast.Attribute)
                    else None
                )
                assert name != "issue_kis_mock_followup_capability", path.name


# ===========================================================================
# r3 §① — live is untouched, measured rather than claimed
# ===========================================================================


@pytest.mark.asyncio
async def test_live_intent_release_is_unreachable_on_the_transport_error_path(
    monkeypatch, _clean_intents, db_session
):
    """operator §87 ① — the live call condition, restored and measured.

    The vector is the one the verifier used: `is_mock=False` (`kis_live`) with a
    NOT_CREATED tracker and an `httpx.ConnectError`. On `origin/main` the branch
    passes no `proven_not_sent`, so the guard returns before touching the
    reservation and **no release method is called at all**. r2/r3 passed it and
    recorded `['release']`; recording a method name proved only that the new
    behaviour existed. This asserts reachability instead.
    """

    called: list[str] = []
    real_release = OrderSendIntentService.release
    real_exact = OrderSendIntentService.release_if_matches

    async def _release(self, **kwargs: Any):
        called.append("release")
        return await real_release(self, **kwargs)

    async def _release_if_matches(self, **kwargs: Any):
        called.append("release_if_matches")
        return await real_exact(self, **kwargs)

    monkeypatch.setattr(OrderSendIntentService, "release", _release)
    monkeypatch.setattr(
        OrderSendIntentService, "release_if_matches", _release_if_matches
    )

    async def _fail(**kwargs: Any):
        raise httpx.ConnectError("token setup failed before dispatch")

    monkeypatch.setattr(oe, "_execute_order", _fail)

    tracker = OrderSendOutcomeTracker()  # NOT_CREATED
    with pytest.raises(oe.OrderSendNotCreated):
        await oe._execute_and_record(
            **_execute_kwargs(
                is_mock=False,
                strategy=None,
                correlation_id=None,
                mirror_cohort=None,
                mirror_source_bucket=None,
                idempotency_key="rob1263-r4-live-key",
                send_outcome=tracker,
            )
        )

    assert called == [], called
    # And the live reservation is still there: nothing released it.
    with pytest.raises(Exception) as dup:
        await OrderSendIntentService(db_session).reserve(
            account_scope="kis_live", idempotency_key="rob1263-r4-live-key"
        )
    assert type(dup.value).__name__ == "DuplicateOrderIntent"


@pytest.mark.asyncio
async def test_only_the_mock_scope_uses_the_exact_row_release(
    monkeypatch, _clean_intents, _mock_route, db_session
):
    """The exact-row delete is mock-scoped, and reachable only pre-send."""

    called: list[str] = []
    real_release = OrderSendIntentService.release
    real_exact = OrderSendIntentService.release_if_matches

    async def _release(self, **kwargs: Any):
        called.append("release")
        return await real_release(self, **kwargs)

    async def _release_if_matches(self, **kwargs: Any):
        called.append("release_if_matches")
        return await real_exact(self, **kwargs)

    monkeypatch.setattr(OrderSendIntentService, "release", _release)
    monkeypatch.setattr(
        OrderSendIntentService, "release_if_matches", _release_if_matches
    )

    async def _blocked(**kwargs: Any):
        raise PreSendFreshnessError(("approval_window:EXPIRED",))

    async def _baseline(**kwargs: Any) -> None:
        return None

    import app.mcp_server.tooling.kis_mock_ledger as ledger

    monkeypatch.setattr(oe, "_execute_order", _blocked)
    monkeypatch.setattr(ledger, "_fetch_kis_mock_baseline_qty", _baseline)

    result = await oe._execute_and_record(**_execute_kwargs())

    assert result["pre_send_blocked"] is True
    assert called == ["release_if_matches"], called


def test_no_lane_code_is_reachable_from_the_live_branch_of_the_guard():
    """Structural: the live branch returns before any lane import or call."""

    tree = ast.parse(DOMESTIC_ORDERS_SOURCE.read_text(encoding="utf-8"))
    guarded = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "guarded"
    )
    # The first statement after binding is the is_mock test, and its body is a
    # bare `return await method(...)` — nothing lane-owned runs on that path.
    live_branch = next(
        node
        for node in ast.walk(guarded)
        if isinstance(node, ast.If)
        and any(isinstance(stmt, ast.Return) for stmt in node.body)
    )
    assert len(live_branch.body) == 1
    returned = live_branch.body[0]
    assert isinstance(returned, ast.Return)
    assert isinstance(returned.value, ast.Await)
    for child in ast.walk(live_branch):
        assert not isinstance(child, (ast.Import, ast.ImportFrom))
        if isinstance(child, ast.Call) and isinstance(child.func, ast.Name):
            assert "kis_mock" not in child.func.id


@pytest.mark.asyncio
async def test_a_receipt_for_one_order_cannot_cancel_a_different_order(monkeypatch):
    """r4 §2 — a receipt is authority for its own ODNO, not for whatever is asked.

    Without this vector the ODNO binding is unreachable by the suite: every other
    cancel test happens to pass the same order id the receipt was issued for.
    """

    created: list[dict[str, Any]] = []

    async def _resolve(order_no: str):
        return {
            "ledger_id": 1,
            "symbol": "005930",
            "side": "buy",
            "quantity": 2.0,
            "price": 70000.0,
            "krx_fwdg_ord_orgno": "00950",
            "instrument_type": "equity_kr",
            "lifecycle_state": "accepted",
        }

    import app.mcp_server.tooling.kis_mock_ledger as ledger

    monkeypatch.setattr(ledger, "resolve_mock_order_for_cancel", _resolve)
    monkeypatch.setattr(
        omc, "_create_kis_client", lambda **kw: created.append(kw) or object()
    )

    # The receipt names 0000117058; the cancel asks for a different order.
    async with await _issue():
        result = await omc._cancel_kis_mock_domestic("9999999999", "005930")

    assert created == [], "a foreign-order receipt reached the broker"
    assert result["success"] is False
    assert result["reason_code"] == "claim_followup_not_authorized"
    assert "different native broker order id" in result["detail"]


@pytest.mark.asyncio
async def test_the_entry_and_the_grant_are_each_checked_against_the_real_account():
    """Q3-4 — both halves of the fingerprint check are load-bearing.

    The account the client actually holds must equal the pinned *entry*'s
    physical account **and** the *grant*'s. Without a vector for each half, one
    can be deleted while the other keeps the suite green.
    """

    _, envelope = _attempt_envelope()
    real = _bound_kis_entry(envelope)  # physical id == the fake client's account
    other = "kismock:v1:a-different-account"
    client = FakeKISClient()

    # (a) grant agrees with the client, the pinned entry does not.
    grant_ok = await _grant_for(envelope, real)
    entry_wrong = replace(real, physical_account_id=other)
    with pytest.raises(singleton.KISMockSendBoundaryRejected) as first:
        await singleton.assert_kis_mock_send_boundary(
            client=client,
            url=client._kis_url(ORDER_PATH),
            entry=entry_wrong,
            grant=grant_ok,
        )
    assert first.value.reason_code == singleton.KIS_MOCK_ACCOUNT_FINGERPRINT_MISMATCH

    # (b) the pinned entry agrees with the client, the grant does not.
    grant_wrong = replace(grant_ok, physical_account_id=other)
    with pytest.raises(singleton.KISMockSendBoundaryRejected) as second:
        await singleton.assert_kis_mock_send_boundary(
            client=client,
            url=client._kis_url(ORDER_PATH),
            entry=real,
            grant=grant_wrong,
        )
    assert second.value.reason_code == singleton.KIS_MOCK_ACCOUNT_FINGERPRINT_MISMATCH

    assert client.transport_calls == 0
