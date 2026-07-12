"""ROB-842 AlpacaPaperSubmitCoordinator — the single application submit boundary.

DB-backed integration + concurrency tests proving:
- packet/hash/key fail-close BEFORE any broker call (AC#1,#2,#4,#5),
- exactly one broker HTTP submit for sequential AND concurrent duplicates (AC#3),
- winner submits, losers replay the stored result or return idempotency_in_progress,
- an in-flight/crash-recovered claim never triggers a second POST.

The broker is a counting fake — no live/paper HTTP is issued.
"""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.models.review import AlpacaPaperOrderLedger
from app.models.trading import InstrumentType
from app.services.alpaca_paper_ledger_service import AlpacaPaperLedgerService
from app.services.alpaca_paper_submit_service import (
    AlpacaPaperSubmitCoordinator,
    build_canonical_payload,
    canonical_hash,
    derive_automated_key,
)
from app.services.brokers.alpaca.schemas import Order

pytestmark = [pytest.mark.asyncio]

_NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
_FUTURE = datetime(2030, 1, 1, 12, 0, 0, tzinfo=UTC)
_CORR_PREFIX = "rob842-coord"


@pytest_asyncio.fixture(autouse=True)
async def _clean_rows(db_session):
    stmt = delete(AlpacaPaperOrderLedger).where(
        AlpacaPaperOrderLedger.lifecycle_correlation_id.like(f"{_CORR_PREFIX}%")
    )
    await db_session.execute(stmt)
    await db_session.commit()
    yield
    await db_session.execute(stmt)
    await db_session.commit()


# ---------------------------------------------------------------------------
# Counting fake broker (no HTTP; records submit calls)
# ---------------------------------------------------------------------------
class CountingBroker:
    def __init__(
        self, *, delay_s: float = 0.0, lookup_order: Order | None = None
    ) -> None:
        self.submit_calls: list[Any] = []
        self.lookup_calls: list[str] = []
        self._delay_s = delay_s
        self._lookup_order = lookup_order

    async def submit_order(self, request: Any) -> Order:
        self.submit_calls.append(request)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        return Order(
            id=f"paper-{len(self.submit_calls)}",
            client_order_id=getattr(request, "client_order_id", None),
            symbol=getattr(request, "symbol", "BTC/USD"),
            qty=getattr(request, "qty", None),
            notional=getattr(request, "notional", None),
            filled_qty=Decimal("0"),
            side=getattr(request, "side", "buy"),
            type=getattr(request, "type", "limit"),
            time_in_force=getattr(request, "time_in_force", "gtc"),
            status="accepted",
            limit_price=getattr(request, "limit_price", None),
        )

    async def get_order_by_client_order_id(self, client_order_id: str) -> Order | None:
        self.lookup_calls.append(client_order_id)
        return self._lookup_order

    async def get_position(self, symbol: str) -> Any:
        return None


def _canonical(limit_price: str = "50000") -> dict[str, Any]:
    return build_canonical_payload(
        symbol="BTC/USD",
        side="buy",
        type="limit",
        time_in_force="gtc",
        qty=None,
        notional=Decimal("10"),
        limit_price=Decimal(limit_price),
        asset_class="crypto",
    )


def _packet(
    canonical: dict[str, Any],
    corr: str,
    snapshot_id: str | None = None,
    **overrides: Any,
):
    from app.services.paper_approval_packet import PaperApprovalPacket

    snap = snapshot_id if snapshot_id is not None else f"{corr}-snap"
    defaults: dict[str, Any] = {
        "signal_source": "test",
        "artifact_id": uuid.uuid4(),
        "signal_symbol": "KRW-BTC",
        "signal_venue": "upbit",
        "execution_symbol": "BTC/USD",
        "execution_venue": "alpaca_paper",
        "execution_asset_class": "crypto",
        "side": "buy",
        "max_notional": Decimal("10"),
        "qty_source": "notional_estimate",
        "expected_lifecycle_step": "previewed",
        "lifecycle_correlation_id": corr,
        "client_order_id": derive_automated_key(
            correlation_id=corr, snapshot_id=snap, canonical=canonical
        ),
        "expires_at": _FUTURE,
        "account_mode": "alpaca_paper",
        "origin": "automated",
        "market_data_asof": _NOW - timedelta(seconds=10),
        "market_data_source": "upbit_ticker",
        "preview_payload_hash": canonical_hash(canonical),
        "snapshot_id": snap,
        "execution_order_type": canonical.get("type"),
        "execution_time_in_force": canonical.get("time_in_force"),
    }
    defaults.update(overrides)
    return PaperApprovalPacket(**defaults)


def _coordinator(
    db_session, broker: CountingBroker, **kwargs: Any
) -> AlpacaPaperSubmitCoordinator:
    return AlpacaPaperSubmitCoordinator(
        AlpacaPaperLedgerService(db_session),
        lambda: broker,
        now_fn=lambda: _NOW,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# Happy path + exact-one-submit (sequential)
# ---------------------------------------------------------------------------
async def test_winner_posts_once_and_records(db_session):
    broker = CountingBroker()
    canonical = _canonical()
    packet = _packet(canonical, f"{_CORR_PREFIX}-win")
    coord = _coordinator(db_session, broker)

    outcome = await coord.submit(packet, submit_canonical=canonical)

    assert outcome.status == "submitted"
    assert outcome.broker_called is True
    assert len(broker.submit_calls) == 1
    # Broker received the server-derived client_order_id.
    assert broker.submit_calls[0].client_order_id == packet.client_order_id


async def test_sequential_duplicate_replays_without_second_post(db_session):
    broker = CountingBroker()
    canonical = _canonical("50001")
    packet = _packet(canonical, f"{_CORR_PREFIX}-seq")
    coord = _coordinator(db_session, broker)

    first = await coord.submit(packet, submit_canonical=canonical)
    second = await coord.submit(packet, submit_canonical=canonical)

    assert first.status == "submitted"
    assert second.status == "replayed"
    assert second.broker_called is False
    assert second.order is not None  # stored broker result replayed
    assert len(broker.submit_calls) == 1


# ---------------------------------------------------------------------------
# Fail-close rejections (never reach the broker)
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("mutate", "expected_code"),
    [
        ({"market_data_asof": None}, "missing_source_timestamp"),
        ({"market_data_asof": _NOW - timedelta(minutes=30)}, "stale_quote"),
        ({"account_mode": "alpaca_live"}, "account_mode_mismatch"),
        ({"expires_at": datetime(2020, 1, 1, tzinfo=UTC)}, "stale_packet"),
        ({"preview_payload_hash": "deadbeef"}, "preview_hash_mismatch"),
        ({"client_order_id": "rob74-crypto-tampered000000"}, "server_key_mismatch"),
    ],
)
async def test_packet_rejections_fail_close_before_broker(
    db_session, mutate, expected_code
):
    broker = CountingBroker()
    canonical = _canonical("50002")
    packet = _packet(canonical, f"{_CORR_PREFIX}-rej", **mutate)
    coord = _coordinator(db_session, broker)

    outcome = await coord.submit(packet, submit_canonical=canonical)

    assert outcome.status == "rejected"
    assert outcome.reason_code == expected_code
    assert outcome.broker_called is False
    assert broker.submit_calls == []


async def test_caller_supplied_id_cannot_bypass_server_key(db_session):
    broker = CountingBroker()
    canonical = _canonical("50003")
    packet = _packet(canonical, f"{_CORR_PREFIX}-caller")
    coord = _coordinator(db_session, broker)

    outcome = await coord.submit(
        packet,
        submit_canonical=canonical,
        caller_client_order_id="attacker-chosen-id",
    )

    assert outcome.status == "rejected"
    assert outcome.reason_code == "caller_id_mismatch"
    assert broker.submit_calls == []


async def test_sell_without_current_position_rejected_before_broker(db_session):
    # No live position (broker.get_position -> None) => fail closed before POST.
    broker = CountingBroker()
    canonical = build_canonical_payload(
        symbol="BTC/USD",
        side="sell",
        type="limit",
        time_in_force="gtc",
        qty=Decimal("0.0001"),
        notional=None,
        limit_price=Decimal("50000"),
        asset_class="crypto",
    )
    packet = _packet(
        canonical,
        f"{_CORR_PREFIX}-sell",
        side="sell",
        max_notional=None,
        max_qty=Decimal("0.0001"),
        qty_source="ledger_filled_qty",
    )
    coord = _coordinator(db_session, broker)

    outcome = await coord.submit(packet, submit_canonical=canonical)

    assert outcome.status == "rejected"
    assert outcome.reason_code == "position_flat"
    assert broker.submit_calls == []


# ---------------------------------------------------------------------------
# In-flight / crash recovery: never a second POST
# ---------------------------------------------------------------------------
async def test_inflight_claim_recovery_returns_in_progress_no_post(db_session):
    broker = CountingBroker()
    canonical = _canonical("50004")
    packet = _packet(canonical, f"{_CORR_PREFIX}-inflight")

    # Simulate a winner that claimed but crashed before record_submit.
    ledger = AlpacaPaperLedgerService(db_session)
    claim = await ledger.claim_submit(
        client_order_id=packet.client_order_id,
        lifecycle_correlation_id=packet.lifecycle_correlation_id,
        execution_symbol="BTC/USD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        side="buy",
        order_type="limit",
        time_in_force="gtc",
        requested_notional=Decimal("10"),
        requested_price=Decimal("50000"),
    )
    assert claim.won is True

    coord = _coordinator(
        db_session, broker, inflight_max_polls=2, inflight_poll_interval_s=0.0
    )
    outcome = await coord.submit(packet, submit_canonical=canonical)

    assert outcome.status == "idempotency_in_progress"
    assert outcome.broker_called is False
    assert broker.submit_calls == []


# ---------------------------------------------------------------------------
# Concurrency: two independent sessions, exactly one broker submit (AC#3)
# ---------------------------------------------------------------------------
async def test_concurrent_two_coordinators_exactly_one_submit(db_session):
    from app.core.db import AsyncSessionLocal

    broker = CountingBroker(delay_s=0.02)
    canonical = _canonical("50005")
    corr = f"{_CORR_PREFIX}-concurrent"
    barrier = asyncio.Barrier(2)

    async def _run():
        async with AsyncSessionLocal() as session:
            coord = AlpacaPaperSubmitCoordinator(
                AlpacaPaperLedgerService(session),
                lambda: broker,
                now_fn=lambda: _NOW,
                inflight_max_polls=20,
                inflight_poll_interval_s=0.02,
            )
            packet = _packet(canonical, corr)
            await barrier.wait()
            return await coord.submit(packet, submit_canonical=canonical)

    outcomes = await asyncio.gather(_run(), _run())

    # Exactly one broker HTTP submit regardless of the race.
    assert len(broker.submit_calls) == 1
    statuses = sorted(o.status for o in outcomes)
    assert statuses.count("submitted") == 1
    other = [o for o in outcomes if o.status != "submitted"][0]
    assert other.status in {"replayed", "idempotency_in_progress", "recovered"}
    assert other.broker_called is False


# ---------------------------------------------------------------------------
# Order-within-packet authority binding (blocker 3)
# ---------------------------------------------------------------------------
async def test_order_notional_exceeds_packet_max_rejected(db_session):
    broker = CountingBroker()
    canonical = build_canonical_payload(
        symbol="BTC/USD",
        side="buy",
        type="limit",
        time_in_force="gtc",
        qty=None,
        notional=Decimal("50"),
        limit_price=Decimal("50000"),
        asset_class="crypto",
    )
    packet = _packet(canonical, f"{_CORR_PREFIX}-overmax", max_notional=Decimal("10"))
    coord = _coordinator(db_session, broker)

    outcome = await coord.submit(packet, submit_canonical=canonical)

    assert outcome.status == "rejected"
    assert outcome.reason_code == "notional_exceeds_max"
    assert broker.submit_calls == []


async def test_order_type_mismatch_rejected(db_session):
    broker = CountingBroker()
    canonical = _canonical("50008")  # type=limit
    packet = _packet(
        canonical, f"{_CORR_PREFIX}-typemis", execution_order_type="market"
    )
    coord = _coordinator(db_session, broker)

    outcome = await coord.submit(packet, submit_canonical=canonical)

    assert outcome.status == "rejected"
    assert outcome.reason_code == "order_type_mismatch"
    assert broker.submit_calls == []


# ---------------------------------------------------------------------------
# Crash-after-success recovery: reconcile from broker, never re-POST (blocker 5)
# ---------------------------------------------------------------------------
async def test_crash_after_success_recovered_without_repost(db_session):
    canonical = _canonical("50009")
    packet = _packet(canonical, f"{_CORR_PREFIX}-recover")

    # Winner claimed but crashed before record_submit → in-flight execution row.
    ledger = AlpacaPaperLedgerService(db_session)
    claim = await ledger.claim_submit(
        client_order_id=packet.client_order_id,
        lifecycle_correlation_id=packet.lifecycle_correlation_id,
        execution_symbol="BTC/USD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        side="buy",
        order_type="limit",
        time_in_force="gtc",
        requested_notional=Decimal("10"),
        requested_price=Decimal("50000"),
    )
    assert claim.won is True

    # ...but the submit HAD reached the broker before the crash.
    recovered_order = Order(
        id="paper-recovered",
        client_order_id=packet.client_order_id,
        symbol="BTC/USD",
        filled_qty=Decimal("0"),
        side="buy",
        type="limit",
        time_in_force="gtc",
        status="accepted",
    )
    broker = CountingBroker(lookup_order=recovered_order)
    coord = _coordinator(
        db_session, broker, inflight_max_polls=1, inflight_poll_interval_s=0.0
    )

    outcome = await coord.submit(packet, submit_canonical=canonical)

    assert outcome.status == "recovered"
    assert outcome.broker_called is False
    assert broker.submit_calls == []  # NO re-POST
    assert broker.lookup_calls == [packet.client_order_id]
