"""ROB-1263 (J3B) — KIS mock coordination adapter acceptance tests.

Every zero-I/O claim below asserts an exact call count of zero on a fake, never
"no exception was raised".  The J3A fakes are *imported* rather than re-created:
a lane that reimplements the common primitive's test doubles is one step from
reimplementing the primitive.
"""

from __future__ import annotations

import ast
import asyncio
import pathlib
import shutil
import subprocess
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
from app.services.brokers.kis.send_outcome import (
    OrderSendDisposition,
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
    }
)
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
    _traced, _clean_intents
):
    await oe._execute_and_record(**_execute_kwargs())

    events = _traced["events"]
    assert events.index("attribution_resolve") < events.index("signal_commit")
    assert events.index("signal_commit") < events.index("reserve")
    assert events.index("reserve") < events.index("http_send")


def _decorator_names(node: ast.AST) -> set[str]:
    names: set[str] = set()
    for decorator in getattr(node, "decorator_list", []):
        if isinstance(decorator, ast.Name):
            names.add(decorator.id)
        elif isinstance(decorator, ast.Attribute):
            names.add(decorator.attr)
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


async def _grant_for(envelope: Any, entry: Any) -> singleton.KISMockCoordinationGrant:
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
async def test_proven_not_created_releases_only_the_exact_matched_reservation(
    _traced, _clean_intents, db_session
):
    tracker = OrderSendOutcomeTracker()  # defaults to NOT_CREATED

    async def _fail(**kwargs: Any):
        raise httpx.ConnectError("token setup failed before dispatch")

    oe._execute_order = _fail  # type: ignore[assignment]
    with pytest.raises(oe.OrderSendNotCreated):
        await oe._execute_and_record(**_execute_kwargs(send_outcome=tracker))

    assert tracker.disposition is OrderSendDisposition.NOT_CREATED
    # The exact key is releasable again; nothing else was touched.
    row_id = await OrderSendIntentService(db_session).reserve(
        account_scope="kis_mock", idempotency_key="kis-mock:rob1263:acceptance"
    )
    assert isinstance(row_id, int)


@pytest.mark.asyncio
async def test_timeout_after_the_send_boundary_holds_the_claim_and_offers_no_retry(
    _traced, _clean_intents, db_session
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
    _traced, _clean_intents, db_session
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
    _traced, _clean_intents, db_session
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
    ],
)
def test_each_missing_followup_element_independently_blocks(kwargs, why):
    base: dict[str, Any] = {
        "operation": "cancel",
        "native_order_id": "0000117058",
        "known_remainder": Decimal("2"),
        "lane_capability_supports_operation": True,
        "fresh_guards_passed": True,
    }
    base.update(kwargs)
    decision = singleton.authorize_kis_mock_claim_followup(**base)
    assert decision.authorized is False, why
    assert decision.reason_code == "claim_followup_not_authorized"
    assert decision.releases_durable_claim is False


def test_a_complete_followup_is_a_capability_and_still_never_releases_a_claim():
    decision = singleton.authorize_kis_mock_claim_followup(
        operation="cancel",
        native_order_id="0000117058",
        known_remainder=Decimal("2"),
        lane_capability_supports_operation=True,
        fresh_guards_passed=True,
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


# --- write fence: what this job wrote ---------------------------------------


def _git(*args: str) -> str | None:
    git = shutil.which("git")
    if git is None:
        return None
    proc = subprocess.run(
        [git, "-C", str(REPO_ROOT), *args], capture_output=True, text=True, check=False
    )
    return None if proc.returncode != 0 else proc.stdout


def _parse_porcelain_z(payload: str) -> set[str]:
    paths: set[str] = set()
    fields = [field for field in payload.split("\0") if field]
    index = 0
    while index < len(fields):
        record = fields[index]
        index += 1
        if len(record) < 4:
            continue
        status, path = record[:2], record[3:]
        paths.add(path)
        if status[0] in {"R", "C"} and index < len(fields):
            paths.add(fields[index])
            index += 1
    return paths


def test_the_actual_job_diff_stays_inside_the_j3b_write_fence():
    """Scoped to *this branch*, not to a frozen SHA.

    A fence pinned to a literal base SHA turns every later unrelated merge into
    a false red — which is exactly what happened to the J3A fence after ROB-1255
    landed between its base and its own merge.  The merge base is recomputed, so
    once this branch is merged the assertion degrades to "nothing to check"
    rather than to a failure about someone else's files.
    """

    base = _git("merge-base", "origin/main", "HEAD")
    status = _git("status", "--porcelain=v1", "-z", "--untracked-files=all")
    if base is None or status is None:
        pytest.skip("git or origin/main is unavailable; the fence is in the report")
    diff = _git("diff", "--name-only", "-z", f"{base.strip()}..HEAD")
    if diff is None:
        pytest.skip("git diff unavailable; the fence is verified in the report")

    changed = {field for field in diff.split("\0") if field}
    changed |= _parse_porcelain_z(status)
    considered = {
        path for path in changed if not path.startswith(FENCE_EXEMPT_PREFIXES)
    }
    assert considered <= set(J3B_WRITE_FENCE), sorted(considered - J3B_WRITE_FENCE)
