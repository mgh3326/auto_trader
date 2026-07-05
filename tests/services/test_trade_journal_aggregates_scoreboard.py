from datetime import UTC, datetime

import pytest

from app.services.trade_journal import aggregates as agg
from app.services.trade_journal.aggregates import (
    ClosedTrade,
    TagInfo,
    TradeMetrics,
    aggregate_by_tag,
)


def _tm(pnl_pct, r, tag="pullback_long", tag_source="strategy_key", link="symbol_window"):
    ct = ClosedTrade(
        market="kr", symbol="005930", account="a", qty=10,
        entry_price=100.0, exit_price=100.0 * (1 + pnl_pct),
        entry_ts=datetime(2026, 6, 1, tzinfo=UTC),
        exit_ts=datetime(2026, 6, 2, tzinfo=UTC),
        pnl_abs=1000.0 * pnl_pct, pnl_pct=pnl_pct, fees=0.0,
        entry_item_uuids=(), exit_item_uuid=None,
        entry_correlation_ids=(), exit_correlation_id=None,
    )
    return TradeMetrics(
        trade=ct,
        tag=TagInfo(tag, tag_source, link),
        r_multiple=r, mae=-0.03, mfe=0.08,
    )


def test_aggregate_math():
    rows = [_tm(0.10, 2.0), _tm(-0.05, -1.0), _tm(0.20, 3.0)]
    [g] = aggregate_by_tag(rows)
    assert g["tag"] == "pullback_long"
    assert g["n"] == 3
    assert g["wins"] == 2 and g["losses"] == 1
    assert g["win_rate"] == pytest.approx(2 / 3)
    assert g["expectancy_pct"] == pytest.approx((0.10 - 0.05 + 0.20) / 3)
    assert g["expectancy_r"] == pytest.approx((2.0 - 1.0 + 3.0) / 3)
    # profit factor = gross wins / |gross losses| = (100+200)/50
    assert g["profit_factor"] == pytest.approx(300 / 50)
    assert g["insufficient_sample"] is True  # n < 10


def test_insufficient_sample_flag_clears_at_10():
    rows = [_tm(0.01, 1.0) for _ in range(10)]
    [g] = aggregate_by_tag(rows)
    assert g["n"] == 10
    assert g["insufficient_sample"] is False


@pytest.mark.asyncio
async def test_scoreboard_fail_open_on_ohlcv_error(db_session, monkeypatch):
    async def boom(*a, **k):
        raise RuntimeError("provider down")

    monkeypatch.setattr(agg, "get_ohlcv", boom)
    result = await agg.build_trading_scoreboard(db_session, use_cache=False)
    assert result["count"] == 0
    assert result["groups"] == []
