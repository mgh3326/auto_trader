"""ROB-842 5th-round: freshness re-check at broker send (G3) and sell reservation
lifecycle — only OPEN sells consume position; cancel releases (G4)."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace
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
    derive_client_order_id,
)
from app.services.brokers.alpaca.schemas import Order

pytestmark = [pytest.mark.asyncio]

_NOW = datetime(2026, 7, 12, 12, 0, 0, tzinfo=UTC)
_CORR = "rob842-r5"


@pytest_asyncio.fixture(autouse=True)
async def _clean(db_session):
    stmt = delete(AlpacaPaperOrderLedger).where(
        AlpacaPaperOrderLedger.lifecycle_correlation_id.like(f"{_CORR}%")
        | AlpacaPaperOrderLedger.client_order_id.like("rob74-crypto-%")
        | AlpacaPaperOrderLedger.client_order_id.like("rob73-%")
    )
    await db_session.execute(stmt)
    await db_session.commit()
    yield
    await db_session.execute(stmt)
    await db_session.commit()


class Broker:
    def __init__(self, *, position: Any = None, delay_s: float = 0.0):
        self.submit_calls: list[Any] = []
        self.position = position
        self._delay_s = delay_s

    async def submit_order(self, request: Any) -> Order:
        self.submit_calls.append(request)
        if self._delay_s:
            await asyncio.sleep(self._delay_s)
        return Order(
            id=f"paper-{len(self.submit_calls)}",
            client_order_id=getattr(request, "client_order_id", None),
            symbol="BTC/USD",
            filled_qty=Decimal("0"),
            side=getattr(request, "side", "buy"),
            type="limit",
            time_in_force="gtc",
            status="accepted",
        )

    async def get_position(self, symbol: str) -> Any:
        return self.position

    async def get_order_by_client_order_id(self, client_order_id: str) -> Order | None:
        return None


def _canonical(side="buy", *, qty=None, notional="10"):
    return build_canonical_payload(
        symbol="BTC/USD",
        side=side,
        type="limit",
        time_in_force="gtc",
        qty=Decimal(str(qty)) if qty is not None else None,
        notional=Decimal(str(notional)) if notional is not None else None,
        limit_price=Decimal("50000"),
        asset_class="crypto",
    )


def _packet(canonical, corr, *, origin="automated", now_ref=None, max_age_s=300, **ov):
    from app.services.paper_approval_packet import PaperApprovalPacket

    snap = f"{corr}-snap"
    if origin == "manual":
        coid, corr_id, snap_id = derive_client_order_id(canonical), None, None
        coid = derive_client_order_id(canonical)
        corr_id = coid
    else:
        coid = derive_automated_key(
            correlation_id=corr, snapshot_id=snap, canonical=canonical
        )
        corr_id, snap_id = corr, snap
    asof = now_ref if now_ref is not None else _NOW - timedelta(seconds=10)
    d: dict[str, Any] = {
        "signal_source": "test",
        "artifact_id": uuid.uuid4(),
        "signal_symbol": "KRW-BTC",
        "signal_venue": "upbit",
        "execution_symbol": "BTC/USD",
        "execution_venue": "alpaca_paper",
        "execution_asset_class": "crypto",
        "side": canonical["side"],
        "max_notional": Decimal("10"),
        "qty_source": "notional_estimate"
        if canonical["side"] == "buy"
        else "manual_operator",
        "expected_lifecycle_step": "previewed",
        "lifecycle_correlation_id": corr_id,
        "client_order_id": coid,
        "expires_at": _NOW + timedelta(hours=1),
        "account_mode": "alpaca_paper",
        "origin": origin,
        "market_data_asof": asof,
        "market_data_source": "upbit_ticker",
        "preview_payload_hash": canonical_hash(canonical),
        "snapshot_id": snap_id,
        "execution_order_type": "limit",
        "execution_time_in_force": "gtc",
        "reference_price": Decimal("50000"),
    }
    d.update(ov)
    return PaperApprovalPacket(**d)


class _Clock:
    def __init__(self, values):
        self.values = list(values)
        self.i = 0

    def __call__(self):
        v = self.values[min(self.i, len(self.values) - 1)]
        self.i += 1
        return v


# ---------------------------------------------------------------------------
# G3 — freshness re-checked at the moment of the broker POST
# ---------------------------------------------------------------------------
async def test_freshness_rechecked_at_send_blocks_stale_buy(db_session):
    corr = f"{_CORR}-fresh-buy"
    canonical = _canonical("buy")
    # market_data as-of = _NOW; passes at +299s, stale at +301s (max_age=300s).
    packet = _packet(canonical, corr, now_ref=_NOW, max_age_s=300)
    broker = Broker()
    # now_fn: initial check +299s (fresh), then at-send +301s (stale).
    clock = _Clock([_NOW + timedelta(seconds=299), _NOW + timedelta(seconds=301)])
    coord = AlpacaPaperSubmitCoordinator(
        AlpacaPaperLedgerService(db_session),
        lambda: broker,
        now_fn=clock,
        quote_max_age=timedelta(seconds=300),
    )
    outcome = await coord.submit(packet, submit_canonical=canonical)
    assert outcome.status == "rejected"
    assert outcome.reason_code == "stale_quote"
    assert broker.submit_calls == []  # POST 0 despite passing the initial check


async def test_freshness_rechecked_at_send_blocks_stale_sell(db_session):
    corr = f"{_CORR}-fresh-sell"
    canonical = _canonical("sell", qty="0.5", notional=None)
    packet = _packet(
        canonical,
        corr,
        origin="manual",
        now_ref=_NOW,
        max_notional=None,
        max_qty=Decimal("1"),
    )
    broker = Broker(position=SimpleNamespace(symbol="BTCUSD", qty=Decimal("1")))
    clock = _Clock([_NOW + timedelta(seconds=299), _NOW + timedelta(seconds=301)])
    coord = AlpacaPaperSubmitCoordinator(
        AlpacaPaperLedgerService(db_session),
        lambda: broker,
        now_fn=clock,
        quote_max_age=timedelta(seconds=300),
    )
    outcome = await coord.submit(packet, submit_canonical=canonical)
    assert outcome.status == "rejected"
    assert outcome.reason_code == "stale_quote"
    assert broker.submit_calls == []


async def test_fresh_at_send_still_posts(db_session):
    corr = f"{_CORR}-fresh-ok"
    canonical = _canonical("buy")
    packet = _packet(canonical, corr, now_ref=_NOW)
    broker = Broker()
    coord = AlpacaPaperSubmitCoordinator(
        AlpacaPaperLedgerService(db_session),
        lambda: broker,
        now_fn=lambda: _NOW,
    )
    outcome = await coord.submit(packet, submit_canonical=canonical)
    assert outcome.status == "submitted"
    assert len(broker.submit_calls) == 1


# ---------------------------------------------------------------------------
# G4 — reservation lifecycle: only OPEN sells consume; filled already in position
# ---------------------------------------------------------------------------
async def _seed_sell_row(
    db_session, *, coid, symbol, qty, lifecycle, cancel_status=None
):
    """Directly insert an execution sell row in a chosen lifecycle."""
    row = AlpacaPaperOrderLedger(
        client_order_id=coid,
        lifecycle_correlation_id=coid,
        record_kind="execution",
        broker="alpaca",
        account_mode="alpaca_paper",
        lifecycle_state=lifecycle,
        execution_symbol=symbol,
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        side="sell",
        order_type="limit",
        currency="USD",
        requested_qty=Decimal(str(qty)),
        cancel_status=cancel_status,
        submitted_at=datetime.now(UTC),
        broker_order_id=f"b-{coid}",
        confirm_flag=True,
    )
    db_session.add(row)
    await db_session.commit()
    db_session.expire_all()


async def test_filled_sell_not_double_counted(db_session):
    # A filled sell 0.6 has already reduced the live position to 0.4; a new 0.4
    # sell must be allowed (available = 0.4, not 0.4 - 0.6).
    ledger = AlpacaPaperLedgerService(db_session)
    await _seed_sell_row(
        db_session,
        coid=f"{_CORR}-filled",
        symbol="BTC/USD",
        qty="0.6",
        lifecycle="filled",
    )
    claim = await ledger.reserve_sell_and_claim(
        client_order_id=f"{_CORR}-new04",
        lifecycle_correlation_id=f"{_CORR}-new04",
        execution_symbol="BTC/USD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        requested_qty=Decimal("0.4"),
        position_qty=Decimal("0.4"),
    )
    assert claim.insufficient is False
    assert claim.won is True
    assert claim.available == Decimal("0.4")


async def test_open_submitted_sell_is_reserved(db_session):
    ledger = AlpacaPaperLedgerService(db_session)
    await _seed_sell_row(
        db_session,
        coid=f"{_CORR}-open05",
        symbol="BTC/USD",
        qty="0.5",
        lifecycle="submitted",
    )
    claim = await ledger.reserve_sell_and_claim(
        client_order_id=f"{_CORR}-new06",
        lifecycle_correlation_id=f"{_CORR}-new06",
        execution_symbol="BTC/USD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        requested_qty=Decimal("0.6"),
        position_qty=Decimal("1"),
    )
    assert claim.insufficient is True  # 0.6 > (1 - 0.5 open)
    assert claim.available == Decimal("0.5")


async def test_canceled_sell_releases_reservation(db_session):
    ledger = AlpacaPaperLedgerService(db_session)
    # open sell holds 0.5; a 0.6 sell is blocked...
    await _seed_sell_row(
        db_session,
        coid=f"{_CORR}-cxl",
        symbol="BTC/USD",
        qty="0.5",
        lifecycle="submitted",
    )
    blocked = await ledger.reserve_sell_and_claim(
        client_order_id=f"{_CORR}-b1",
        lifecycle_correlation_id=f"{_CORR}-b1",
        execution_symbol="BTC/USD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        requested_qty=Decimal("0.6"),
        position_qty=Decimal("1"),
    )
    assert blocked.insufficient is True

    # ...cancel it (sets cancel_status) => reservation released...
    await ledger.record_cancel(f"{_CORR}-cxl", cancel_status="canceled")
    db_session.expire_all()

    allowed = await ledger.reserve_sell_and_claim(
        client_order_id=f"{_CORR}-b2",
        lifecycle_correlation_id=f"{_CORR}-b2",
        execution_symbol="BTC/USD",
        execution_venue="alpaca_paper",
        instrument_type=InstrumentType.crypto,
        requested_qty=Decimal("0.6"),
        position_qty=Decimal("1"),
    )
    assert allowed.insufficient is False
    assert allowed.available == Decimal("1")  # full position free again
