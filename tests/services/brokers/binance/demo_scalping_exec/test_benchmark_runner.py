"""ROB-315 Phase 1 — BenchmarkRunner: compute + store daily buy&hold benchmark.

Tests the ``compute_and_store_daily_benchmark`` orchestrator function which
bridges market-data (fake / error) with ScalpingReviewService.set_benchmark.
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.models.crypto_instruments import CryptoInstrument
from app.models.scalp_trade_analytics import ScalpTradeAnalytics
from app.services.brokers.binance.demo_scalping.signal import Candle
from app.services.brokers.binance.demo_scalping_exec.benchmark_runner import (
    compute_and_store_daily_benchmark,
)
from app.services.scalping_reviews.service import ScalpingReviewService

# ---------------------------------------------------------------------------
# Helpers — copied from tests/services/scalping_reviews/test_service.py:22-67
# ---------------------------------------------------------------------------

_DATE = dt.date(2026, 5, 25)
_NOW = dt.datetime(2026, 5, 25, 12, 0, 0, tzinfo=dt.UTC)


async def _instrument(session, symbol="RVWXRPUSDT") -> int:
    """Get-or-create — crypto_instruments persists across tests in the shared
    test DB, so a fixed insert would collide on its unique key."""
    existing = await session.scalar(
        select(CryptoInstrument).where(
            CryptoInstrument.venue == "binance",
            CryptoInstrument.product == "usdm_futures",
            CryptoInstrument.venue_symbol == symbol,
        )
    )
    if existing is not None:
        return existing.id
    inst = CryptoInstrument(
        venue="binance",
        product="usdm_futures",
        venue_symbol=symbol,
        base_asset="XRP",
        quote_asset="USDT",
        status="active",
    )
    session.add(inst)
    await session.flush()
    return inst.id


async def _analytics(session, instrument_id, *, created_at=_NOW, **kw):
    base = {
        "open_client_order_id": f"o-{kw.get('tag', '0')}",
        "instrument_id": instrument_id,
        "product": "usdm_futures",
        "symbol": "XRPUSDT",
        "side": "BUY",
        "qty": Decimal("1"),
        "created_at": created_at,
        "updated_at": created_at,
    }
    kw.pop("tag", None)
    base.update(kw)
    row = ScalpTradeAnalytics(**base)
    session.add(row)
    await session.flush()
    return row


# ---------------------------------------------------------------------------
# Fake market-data helpers
# ---------------------------------------------------------------------------


class _FakeMD:
    """day candle per symbol: {symbol: (open, close)}."""

    def __init__(self, prices: dict[str, tuple[float, float]]) -> None:
        self._prices = prices
        self.calls: list[tuple] = []

    async def fetch_klines(self, product, symbol, *, interval="1m", limit=50):
        self.calls.append((product, symbol, interval, limit))
        o, c = self._prices[symbol]
        return [
            Candle(
                open_time_ms=0,
                open=Decimal(str(o)),
                high=Decimal(str(max(o, c))),
                low=Decimal(str(min(o, c))),
                close=Decimal(str(c)),
                close_time_ms=0,
            )
        ]


class _BoomMD:
    async def fetch_klines(self, *a, **k):
        raise RuntimeError("network down")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_runner_stores_notional_weighted_benchmark(db_session) -> None:
    iid_x = await _instrument(db_session, "XRPUSDT")
    iid_d = await _instrument(db_session, "DOGEUSDT")
    await _analytics(
        db_session,
        iid_x,
        tag="x",
        symbol="XRPUSDT",
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"),
        net_pnl_usdt=Decimal("0.9"),
        gross_pnl_usdt=Decimal("1.0"),
        exit_reason="take_profit",
    )
    await _analytics(
        db_session,
        iid_d,
        tag="d",
        symbol="DOGEUSDT",
        entry_price=Decimal("0.1"),
        exit_price=Decimal("0.1"),
        entry_notional_usdt=Decimal("300"),
        net_pnl_usdt=Decimal("0"),
        gross_pnl_usdt=Decimal("0"),
        exit_reason="timeout",
    )
    svc = ScalpingReviewService(db_session)
    await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)
    md = _FakeMD(
        {"XRPUSDT": (100.0, 101.0), "DOGEUSDT": (100.0, 99.8)}
    )  # +100bps, -20bps
    value = await compute_and_store_daily_benchmark(
        session=db_session,
        market_data=md,
        review_date=_DATE,
        product="usdm_futures",
        now=_NOW,
    )
    # (100*100 + 300*-20) / 400 = 10
    assert value == Decimal("10")
    review = await svc._get_by_key(_DATE, "usdm_futures", "binance_demo", "")
    assert review.benchmark_return_bps == Decimal("10")
    assert "XRPUSDT" in review.source_payload["benchmark"]
    assert all(c[2] == "1d" and c[3] == 1 for c in md.calls)  # day candle only


@pytest.mark.asyncio
async def test_runner_returns_none_when_market_data_fails(db_session) -> None:
    iid = await _instrument(db_session, "XRPUSDT")
    await _analytics(
        db_session,
        iid,
        tag="x",
        symbol="XRPUSDT",
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"),
        net_pnl_usdt=Decimal("0.9"),
        gross_pnl_usdt=Decimal("1.0"),
        exit_reason="take_profit",
    )
    svc = ScalpingReviewService(db_session)
    await svc.build_draft(review_date=_DATE, product="usdm_futures", now=_NOW)

    # Seed a non-NULL sentinel so we can prove stale values are reset to NULL.
    await svc.set_benchmark(
        review_date=_DATE,
        product="usdm_futures",
        value=Decimal("99"),
        now=_NOW,
        session_tag="",
        account_scope="binance_demo",
        detail=None,
    )
    pre = await svc._get_by_key(_DATE, "usdm_futures", "binance_demo", "")
    assert pre.benchmark_return_bps == Decimal("99"), "sentinel not set"

    value = await compute_and_store_daily_benchmark(
        session=db_session,
        market_data=_BoomMD(),
        review_date=_DATE,
        product="usdm_futures",
        now=_NOW,
    )
    assert value is None
    review = await svc._get_by_key(_DATE, "usdm_futures", "binance_demo", "")
    # stale sentinel must be cleared to NULL — not merely left as-is
    assert review.benchmark_return_bps is None
