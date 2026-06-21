"""Task 1 — run_demo_scalping_review_refresh: unit tests (TDD Step 1).

Helpers copied from:
  - tests/services/scalping_reviews/test_service.py : 22-67  (_DATE/_NOW/_instrument/_analytics)
  - tests/services/brokers/binance/demo_scalping_exec/test_benchmark_runner.py : _FakeMD
"""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.jobs.binance_demo_scalping_review import run_demo_scalping_review_refresh
from app.models.crypto_instruments import CryptoInstrument
from app.models.scalp_trade_analytics import ScalpTradeAnalytics
from app.services.brokers.binance.demo_scalping.signal import Candle
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
# Fake market-data helper (adapted from test_benchmark_runner.py)
# ---------------------------------------------------------------------------


class _FakeMD:
    def __init__(self, prices: dict[str, tuple[float, float]]) -> None:
        self._prices = prices

    async def fetch_klines(self, product, symbol, *, interval="1m", limit=50):
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

    async def aclose(self) -> None:  # pragma: no cover - injected, not owned
        return None


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_disabled_gate_is_noop(monkeypatch) -> None:
    monkeypatch.setattr(settings, "binance_demo_scalping_review_flow_enabled", False)
    result = await run_demo_scalping_review_refresh(
        review_date=_DATE, products=("usdm_futures",), now=_NOW
    )
    assert result == {"status": "disabled"}


@pytest.mark.asyncio
async def test_enabled_builds_review_and_benchmark(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "binance_demo_scalping_review_flow_enabled", True)
    iid = await _instrument(db_session, "XRPUSDT")
    await _analytics(
        db_session,
        iid,
        tag="w",
        symbol="XRPUSDT",
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"),
        net_pnl_usdt=Decimal("0.9"),
        gross_pnl_usdt=Decimal("1.0"),
        exit_reason="take_profit",
    )
    md = _FakeMD({"XRPUSDT": (100.0, 101.0)})  # +100 bps
    result = await run_demo_scalping_review_refresh(
        session=db_session,
        market_data=md,
        review_date=_DATE,
        products=("usdm_futures",),
        now=_NOW,
    )
    assert result["status"] == "ran"
    assert result["reviewDate"] == _DATE.isoformat()
    assert result["errors"] == []
    [summary] = result["products"]
    assert summary == {
        "product": "usdm_futures",
        "tradeCount": 1,
        "netReturnBps": "90.0000",
        "benchmarkReturnBps": "100.00",  # (101/100-1)*10000 = Decimal('100.00')
    }
    review = await ScalpingReviewService(db_session)._get_by_key(
        _DATE, "usdm_futures", "binance_demo", ""
    )
    assert review is not None and review.benchmark_return_bps == Decimal("100")


@pytest.mark.asyncio
async def test_product_failure_is_isolated(db_session, monkeypatch) -> None:
    monkeypatch.setattr(settings, "binance_demo_scalping_review_flow_enabled", True)
    iid = await _instrument(db_session, "XRPUSDT")
    await _analytics(
        db_session,
        iid,
        tag="w",
        symbol="XRPUSDT",
        entry_price=Decimal("100"),
        exit_price=Decimal("101"),
        entry_notional_usdt=Decimal("100"),
        net_pnl_usdt=Decimal("0.9"),
        gross_pnl_usdt=Decimal("1.0"),
        exit_reason="take_profit",
    )
    md = _FakeMD({"XRPUSDT": (100.0, 101.0)})
    orig = ScalpingReviewService.build_draft

    async def flaky(self, *, review_date, product, now, **kw):
        if product == "spot":
            raise RuntimeError("boom")
        return await orig(self, review_date=review_date, product=product, now=now, **kw)

    monkeypatch.setattr(ScalpingReviewService, "build_draft", flaky)
    result = await run_demo_scalping_review_refresh(
        session=db_session,
        market_data=md,
        review_date=_DATE,
        products=("spot", "usdm_futures"),
        now=_NOW,
    )
    assert result["status"] == "ran"
    assert [s["product"] for s in result["products"]] == ["usdm_futures"]
    assert [e["product"] for e in result["errors"]] == ["spot"]
