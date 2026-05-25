"""ROB-313 PR2 — ScalpTradeAnalyticsService persistence tests (DB-backed).

One row per reconciled round-trip, keyed by the open leg's client_order_id.
Uses the shared ``db_session`` fixture (does not roll back between tests),
so we use unique client_order_ids per test.
"""

from __future__ import annotations

import datetime as dt
import uuid
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.models.crypto_instruments import CryptoInstrument
from app.services.brokers.binance.demo_scalping_exec.analytics import (
    ScalpTradeAnalyticsService,
)

_NOW = dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=dt.UTC)


@pytest_asyncio.fixture
async def analytics_service(db_session) -> ScalpTradeAnalyticsService:
    return ScalpTradeAnalyticsService(db_session)


@pytest_asyncio.fixture
async def instrument_id(db_session) -> int:
    existing = await db_session.scalar(
        select(CryptoInstrument).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == "usdm_futures",
            CryptoInstrument.venue_symbol == "XRPUSDT",
        )
    )
    if existing is not None:
        return existing.id
    inst = CryptoInstrument(
        venue="binance",
        product="usdm_futures",
        venue_symbol="XRPUSDT",
        base_asset="XRP",
        quote_asset="USDT",
        status="active",
    )
    db_session.add(inst)
    await db_session.flush()
    await db_session.refresh(inst)
    return inst.id


def _coid() -> str:
    return "rob313-" + uuid.uuid4().hex[:20]


@pytest.mark.asyncio
async def test_record_and_read_round_trip(analytics_service, instrument_id) -> None:
    open_cid = _coid()
    row = await analytics_service.record(
        open_client_order_id=open_cid,
        close_client_order_id=_coid(),
        instrument_id=instrument_id,
        product="usdm_futures",
        symbol="XRPUSDT",
        side="BUY",
        qty=Decimal("20"),
        entry_price=Decimal("0.5000"),
        exit_price=Decimal("0.5020"),
        entry_notional_usdt=Decimal("10"),
        fee_rate_bps=Decimal("5"),
        entry_fee_usdt=Decimal("0.005"),
        exit_fee_usdt=Decimal("0.005"),
        entry_slippage_bps=Decimal("1.2"),
        exit_slippage_bps=Decimal("0.8"),
        entry_spread_bps=Decimal("2.0"),
        exit_spread_bps=Decimal("2.5"),
        mae_bps=Decimal("-18"),
        mfe_bps=Decimal("42"),
        gross_pnl_usdt=Decimal("0.04"),
        net_pnl_usdt=Decimal("0.03"),
        net_return_bps=Decimal("30"),
        holding_seconds=428,
        exit_reason="take_profit",
        session_tag="us",
        signal_snapshot={"sma_fast": "0.5001", "sma_slow": "0.4990"},
        now=_NOW,
    )
    assert row.id is not None

    fetched = await analytics_service.get_by_open_client_order_id(open_cid)
    assert fetched is not None
    assert fetched.side == "BUY"
    assert fetched.net_pnl_usdt == Decimal("0.03")
    assert fetched.entry_slippage_bps == Decimal("1.2")
    assert fetched.mae_bps == Decimal("-18")
    assert fetched.mfe_bps == Decimal("42")
    assert fetched.exit_reason == "take_profit"
    assert fetched.fee_rate_bps == Decimal("5")
    assert fetched.signal_snapshot == {"sma_fast": "0.5001", "sma_slow": "0.4990"}


@pytest.mark.asyncio
async def test_open_client_order_id_is_unique(analytics_service, instrument_id) -> None:
    open_cid = _coid()
    kwargs = {
        "open_client_order_id": open_cid,
        "instrument_id": instrument_id,
        "product": "usdm_futures",
        "symbol": "XRPUSDT",
        "side": "SELL",
        "qty": Decimal("20"),
        "entry_price": Decimal("0.5"),
        "entry_notional_usdt": Decimal("10"),
        "fee_rate_bps": Decimal("5"),
        "now": _NOW,
    }
    await analytics_service.record(**kwargs)
    with pytest.raises(IntegrityError):  # unique violation on open_client_order_id
        await analytics_service.record(**kwargs)


@pytest.mark.asyncio
async def test_anomaly_row_allows_null_exit(analytics_service, instrument_id) -> None:
    """A close-leg anomaly records a row with no exit price/pnl — never a fake success."""
    open_cid = _coid()
    await analytics_service.record(
        open_client_order_id=open_cid,
        instrument_id=instrument_id,
        product="usdm_futures",
        symbol="XRPUSDT",
        side="BUY",
        qty=Decimal("20"),
        entry_price=Decimal("0.5"),
        entry_notional_usdt=Decimal("10"),
        fee_rate_bps=Decimal("5"),
        exit_reason="anomaly",
        now=_NOW,
    )
    fetched = await analytics_service.get_by_open_client_order_id(open_cid)
    assert fetched is not None
    assert fetched.exit_price is None
    assert fetched.net_pnl_usdt is None
    assert fetched.exit_reason == "anomaly"
