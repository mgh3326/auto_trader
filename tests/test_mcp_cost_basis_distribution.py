"""ROB-450: get_cost_basis_distribution handler (VPVR holder cost-basis estimate)."""

from __future__ import annotations

import pandas as pd
import pytest

from app.mcp_server.tooling.fundamentals import _cost_basis_distribution as mod

pytestmark = [pytest.mark.unit, pytest.mark.asyncio]


def _ohlcv(n=20, start=100.0):
    rows = []
    for i in range(n):
        close = start + i  # 100..119
        rows.append(
            {
                "date": f"2026-05-{(i % 28) + 1:02d}",
                "open": close - 0.5,
                "high": close + 1.0,
                "low": close - 1.0,
                "close": close,
                "volume": 1000.0,
            }
        )
    return pd.DataFrame(rows)


async def test_cost_basis_estimate_shape_and_sums():
    df = _ohlcv()  # closes 100..119, current = 119
    out = await mod.get_cost_basis_distribution_impl(
        "005930", market="kr", buckets=10, preloaded_df=df
    )
    assert out["estimate"] is True
    assert out["method"] == "vpvr_self_ohlcv"
    assert out["instrument_type"] == "equity_kr"
    assert out["current_price"] == pytest.approx(119.0)
    # buckets present + holder shares sum ~100%
    assert len(out["buckets"]) == 10
    share_sum = sum(b["holder_share_pct"] or 0.0 for b in out["buckets"])
    assert share_sum == pytest.approx(100.0, abs=0.5)
    # underwater + in_profit ~100% (mutually exclusive partition by bucket midpoint)
    assert out["pct_holders_underwater"] + out[
        "pct_holders_in_profit"
    ] == pytest.approx(100.0, abs=0.5)
    # most holders bought below 119 → mostly in profit
    assert out["pct_holders_in_profit"] > out["pct_holders_underwater"]
    assert out["heaviest_bucket"] is not None
    assert out["vwap_estimate"] is not None


async def test_buckets_clamped():
    df = _ohlcv()
    out = await mod.get_cost_basis_distribution_impl(
        "005930", market="kr", buckets=1, preloaded_df=df
    )
    assert len(out["buckets"]) == 2  # clamped to >=2


async def test_empty_data_fail_open(monkeypatch):
    async def empty_fetch(symbol, market_type, period_days):
        return pd.DataFrame()

    monkeypatch.setattr(mod, "_fetch_ohlcv_for_volume_profile", empty_fetch)
    out = await mod.get_cost_basis_distribution_impl("005930", market="kr")
    assert "error" in out  # fail-open structured error, not a raise


async def test_requires_symbol():
    with pytest.raises(ValueError, match="symbol is required"):
        await mod.get_cost_basis_distribution_impl("")
