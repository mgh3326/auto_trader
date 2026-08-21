"""ROB-1267 J5B §6.1 — the lab lane and the default lane never collapse.

Three independent mechanisms keep the two Alpaca paper lanes apart, and the
contract in `docs/contracts/rob-1267-us-alpaca-lab-recovery.md` §6.1 depends on
all three holding at once:

1. profile routing — one broker profile per account mode, none shared;
2. ledger pinning — one normalized account mode per service instance, carried
   in the predicate of every read and the values of every claim;
3. packet/coordinator binding — an account-mode mismatch is refused before any
   broker or ledger work, symmetrically in both directions.

Every assertion below is a *separation* assertion.  Nothing here submits,
cancels, or reconciles against a broker; there is no database session, no
credential, and no HTTP.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
from sqlalchemy.dialects import postgresql

from app.mcp_server.tooling import alpaca_paper_orders as orders_mod
from app.services.alpaca_paper_account_modes import (
    ALPACA_PAPER_ACCOUNT_MODE,
    ALPACA_PAPER_ACCOUNT_MODES,
    ALPACA_PAPER_CRYPTO_ACCOUNT_MODE,
    ALPACA_PAPER_LAB_ACCOUNT_MODE,
    profile_for_account_mode,
)
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService
from app.services.alpaca_paper_submit_service import (
    AlpacaPaperSubmitCoordinator,
    build_canonical_payload,
    canonical_hash,
    derive_automated_key,
)
from app.services.paper_approval_packet import PaperApprovalPacket

pytestmark = pytest.mark.unit

_FUTURE = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)


# ---------------------------------------------------------------------------
# 1. profile routing
# ---------------------------------------------------------------------------
def test_every_supported_account_mode_routes_to_its_own_profile() -> None:
    """No two paper account modes may share a broker profile."""
    profiles = {
        mode: profile_for_account_mode(mode) for mode in ALPACA_PAPER_ACCOUNT_MODES
    }
    assert len(set(profiles.values())) == len(profiles), (
        f"paper account modes collapsed onto a shared profile: {profiles}"
    )
    # And specifically: the lab mode does not land on the default lane's route.
    assert profiles[ALPACA_PAPER_LAB_ACCOUNT_MODE] is not None
    assert (
        profiles[ALPACA_PAPER_LAB_ACCOUNT_MODE]
        != profiles[ALPACA_PAPER_ACCOUNT_MODE]
        != profiles[ALPACA_PAPER_CRYPTO_ACCOUNT_MODE]
    )


def test_service_for_lab_account_mode_is_built_from_the_lab_profile(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    built: list[str | None] = []

    class RecordingService:
        def __init__(self, *, profile: str | None = None) -> None:
            built.append(profile)

    monkeypatch.setattr(orders_mod, "AlpacaPaperBrokerService", RecordingService)
    orders_mod._service_for_account_mode(ALPACA_PAPER_LAB_ACCOUNT_MODE)
    assert built == [profile_for_account_mode(ALPACA_PAPER_LAB_ACCOUNT_MODE)]

    built.clear()
    # The default lane is constructed without a profile, so the two lanes never
    # share one construction argument.
    orders_mod._service_for_account_mode(ALPACA_PAPER_ACCOUNT_MODE)
    assert built == [profile_for_account_mode(ALPACA_PAPER_ACCOUNT_MODE)]
    assert built != [profile_for_account_mode(ALPACA_PAPER_LAB_ACCOUNT_MODE)]


# ---------------------------------------------------------------------------
# 2. ledger pinning
# ---------------------------------------------------------------------------
class _NullResult:
    def scalar_one_or_none(self) -> None:
        return None

    def scalars(self) -> _NullResult:
        return self

    def all(self) -> list[Any]:
        return []


class _RecordingSession:
    """Captures every statement a ledger read compiles, and executes none."""

    def __init__(self) -> None:
        self.statements: list[Any] = []

    async def execute(self, statement: Any, *args: Any, **kwargs: Any) -> _NullResult:
        self.statements.append(statement)
        return _NullResult()

    def expire_all(self) -> None:
        return None


_READS = (
    ("get_by_client_order_id", ("cid",), {}),
    ("get_by_id", (1,), {}),
    ("list_recent", (), {"limit": 5}),
    ("list_reconcile_candidates", (), {"limit": 5}),
    ("get_execution_by_client_order_id", ("cid",), {}),
    ("get_preview_by_client_order_id", ("cid",), {}),
)


@pytest.mark.asyncio
@pytest.mark.parametrize("method,args,kwargs", _READS, ids=[r[0] for r in _READS])
async def test_lab_pinned_ledger_reads_carry_the_lab_account_mode(
    method: str, args: tuple[Any, ...], kwargs: dict[str, Any]
) -> None:
    session = _RecordingSession()
    ledger = AlpacaPaperLedgerService(
        session,  # type: ignore[arg-type]
        account_mode=ALPACA_PAPER_LAB_ACCOUNT_MODE,
    )
    await getattr(ledger, method)(*args, **kwargs)

    assert session.statements, f"{method} issued no statement"
    for statement in session.statements:
        sql = str(
            statement.compile(
                dialect=postgresql.dialect(),
                compile_kwargs={"literal_binds": True},
            )
        )
        assert f"'{ALPACA_PAPER_LAB_ACCOUNT_MODE}'" in sql, (
            f"{method}: read is not scoped to the lab account mode:\n{sql}"
        )
        assert f"'{ALPACA_PAPER_ACCOUNT_MODE}'" not in sql.replace(
            f"'{ALPACA_PAPER_LAB_ACCOUNT_MODE}'", ""
        ), f"{method}: read reaches the default lane as well:\n{sql}"


def test_ledger_instance_pins_exactly_one_normalized_mode() -> None:
    session = _RecordingSession()
    lab = AlpacaPaperLedgerService(
        session,  # type: ignore[arg-type]
        account_mode=f"  {ALPACA_PAPER_LAB_ACCOUNT_MODE.upper()}  ",
    )
    assert lab._account_mode == ALPACA_PAPER_LAB_ACCOUNT_MODE
    default = AlpacaPaperLedgerService(session)  # type: ignore[arg-type]
    assert default._account_mode == ALPACA_PAPER_ACCOUNT_MODE
    assert lab._account_mode != default._account_mode

    with pytest.raises(ValueError):
        AlpacaPaperLedgerService(session, account_mode="alpaca_paper_labs")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 3. packet / coordinator binding
# ---------------------------------------------------------------------------
class _ExplodingLedger:
    """Any attribute touch is a contract violation: the reject precedes work."""

    def __getattr__(self, name: str) -> Any:
        raise AssertionError(
            f"ledger was touched before the account-mode check: {name}"
        )


_CORRELATION = "rob1267-boundary"
_SNAPSHOT = "rob1267-snapshot"


def _canonical() -> dict[str, Any]:
    return build_canonical_payload(
        symbol="AAPL",
        side="buy",
        asset_class="us_equity",
        type="limit",
        time_in_force="day",
        qty=None,
        notional=Decimal("300"),
        limit_price=Decimal("100"),
    )


def _packet(*, account_mode: str, client_order_id: str) -> PaperApprovalPacket:
    """A packet that passes every binding check except the account-mode one."""
    canonical = _canonical()
    return PaperApprovalPacket(
        signal_source="rob1267-boundary",
        artifact_id=uuid.uuid4(),
        signal_symbol="AAPL",
        signal_venue="policy_table_us",
        execution_symbol="AAPL",
        execution_venue="alpaca_paper",
        execution_asset_class="us_equity",
        side="buy",
        max_notional=Decimal("300"),
        qty_source="notional",
        expected_lifecycle_step="submitted",
        lifecycle_correlation_id=_CORRELATION,
        client_order_id=client_order_id,
        expires_at=_FUTURE,
        account_mode=account_mode,
        origin="automated",
        snapshot_id=_SNAPSHOT,
        preview_payload_hash=canonical_hash(canonical),
        execution_order_type="limit",
        execution_time_in_force="day",
    )


def _server_key(account_mode: str) -> str:
    return derive_automated_key(
        correlation_id=_CORRELATION,
        snapshot_id=_SNAPSHOT,
        canonical=_canonical(),
        account_mode=account_mode,
    )


def test_the_derived_idempotency_key_is_namespaced_per_account_mode() -> None:
    """Even the claim key cannot collide across the two lanes."""
    keys = {
        mode: _server_key(mode)
        for mode in (ALPACA_PAPER_ACCOUNT_MODE, ALPACA_PAPER_LAB_ACCOUNT_MODE)
    }
    assert len(set(keys.values())) == 2, f"lane keys collided: {keys}"


class _CountingBrokerFactory:
    def __init__(self) -> None:
        self.calls = 0

    def __call__(self) -> Any:
        self.calls += 1
        raise AssertionError("broker was constructed for a mismatched packet")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "expected,packet_mode",
    [
        (ALPACA_PAPER_LAB_ACCOUNT_MODE, ALPACA_PAPER_ACCOUNT_MODE),
        (ALPACA_PAPER_ACCOUNT_MODE, ALPACA_PAPER_LAB_ACCOUNT_MODE),
    ],
    ids=["default-packet-to-lab-coordinator", "lab-packet-to-default-coordinator"],
)
async def test_account_mode_mismatch_is_refused_before_broker_or_ledger_work(
    expected: str, packet_mode: str
) -> None:
    broker_factory = _CountingBrokerFactory()
    coordinator = AlpacaPaperSubmitCoordinator(
        _ExplodingLedger(),  # type: ignore[arg-type]
        broker_factory,
        expected_account_mode=expected,
        now_fn=lambda: _FUTURE - timedelta(minutes=1),
    )
    # The key is derived for the coordinator's own mode, so every earlier
    # binding check passes and the account-mode check is what actually fires.
    packet = _packet(account_mode=packet_mode, client_order_id=_server_key(expected))

    outcome = await coordinator.submit(packet, submit_canonical=_canonical())

    assert outcome.status == "rejected"
    assert outcome.reason_code == "account_mode_mismatch"
    assert outcome.broker_called is False
    assert outcome.submitted is False
    assert outcome.success is False
    assert broker_factory.calls == 0


@pytest.mark.asyncio
async def test_coordinator_normalizes_its_expected_mode_rather_than_widening_it() -> (
    None
):
    """A coordinator is bound to one mode; an unknown mode is not accepted."""
    coordinator = AlpacaPaperSubmitCoordinator(
        _ExplodingLedger(),  # type: ignore[arg-type]
        _CountingBrokerFactory(),
        expected_account_mode=f" {ALPACA_PAPER_LAB_ACCOUNT_MODE.upper()} ",
    )
    assert coordinator._expected_account_mode == ALPACA_PAPER_LAB_ACCOUNT_MODE

    with pytest.raises(ValueError):
        AlpacaPaperSubmitCoordinator(
            _ExplodingLedger(),  # type: ignore[arg-type]
            _CountingBrokerFactory(),
            expected_account_mode="alpaca_paper_labs",
        )


@pytest.mark.asyncio
async def test_lab_reconcile_tool_pins_both_ledger_and_broker_to_the_lab_lane(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """`alpaca_paper_reconcile_orders` may not hand a default service to the lab."""
    seen: dict[str, Any] = {}

    class _Session:
        async def __aenter__(self) -> _Session:
            return self

        async def __aexit__(self, *exc: Any) -> None:
            return None

    monkeypatch.setattr(orders_mod, "_session_factory", lambda: _Session)

    def _ledger(db: Any, account_mode: str = ALPACA_PAPER_ACCOUNT_MODE) -> str:
        seen["ledger_mode"] = account_mode
        return f"ledger:{account_mode}"

    def _service(account_mode: str) -> str:
        seen.setdefault("service_modes", []).append(account_mode)
        return f"broker:{account_mode}"

    class _Reconcile:
        def __init__(self, ledger: Any, broker: Any) -> None:
            seen["pair"] = (ledger, broker)

        async def reconcile(self, **kwargs: Any) -> dict[str, Any]:
            return {"success": True, "dry_run": True, "reconciled": [], "count": 0}

    monkeypatch.setattr(orders_mod, "AlpacaPaperLedgerService", _ledger)
    monkeypatch.setattr(orders_mod, "_service_for_account_mode", _service)
    monkeypatch.setattr(orders_mod, "AlpacaPaperReconcileService", _Reconcile)

    result = await orders_mod.alpaca_paper_reconcile_orders(
        account_mode=ALPACA_PAPER_LAB_ACCOUNT_MODE, dry_run=True
    )

    assert result["account_mode"] == ALPACA_PAPER_LAB_ACCOUNT_MODE
    assert seen["ledger_mode"] == ALPACA_PAPER_LAB_ACCOUNT_MODE
    assert set(seen["service_modes"]) == {ALPACA_PAPER_LAB_ACCOUNT_MODE}
    assert seen["pair"] == (
        f"ledger:{ALPACA_PAPER_LAB_ACCOUNT_MODE}",
        f"broker:{ALPACA_PAPER_LAB_ACCOUNT_MODE}",
    )
