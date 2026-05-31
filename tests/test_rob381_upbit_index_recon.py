"""ROB-381 reconnaissance — Upbit digital-asset-index / altseason fixtures.

PR1 is a reconnaissance spike, not a production integration. These tests lock
the *observed response schema* of the unofficial Upbit web endpoints that power
``upbit.com/trends`` (코인동향), proving the data is fixture-able and that a future
PR2 parser has a stable contract to target.

Fixtures in ``tests/fixtures/upbit_index/`` are trimmed, sanitized samples of
public market-data responses (no secrets, no account/order data). See the recon
verdict in ``docs/runbooks/rob-381-upbit-index-altseason-recon.md``.

No network calls. No MCP tool / collector is exercised here (none exists yet).
"""

import json
import pathlib

import pytest

_FIXTURE_DIR = pathlib.Path(__file__).parent / "fixtures" / "upbit_index"


def _load(name: str):
    return json.loads((_FIXTURE_DIR / name).read_text(encoding="utf-8"))


@pytest.mark.unit
def test_index_master_catalog_shape():
    """index/master is a catalog of indices keyed by IDX.UPBIT.* codes."""
    master = _load("index_master.json")
    assert isinstance(master, list) and master, "expected non-empty catalog"

    by_symbol = {row["symbol"]: row for row in master}
    # Altseason-relevant composite indices must be present and discoverable.
    assert "UBAI" in by_symbol, "Upbit Altcoin Index (altseason proxy) missing"
    assert "UBMI" in by_symbol, "Upbit Market Index missing"

    ubai = by_symbol["UBAI"]
    assert ubai["code"] == "IDX.UPBIT.UBAI"
    assert ubai["categoryType"] == "market"
    # Long history → usable for regime baselines.
    assert ubai["listingDate"] == "2017-10-01"

    # Category taxonomy used by the trends page.
    categories = {row["categoryType"] for row in master}
    assert {"market", "sector", "strategy", "theme"} <= categories


@pytest.mark.unit
def test_index_summary_exposes_regime_stats():
    """index/summary carries per-index yield + risk stats (regime signal)."""
    summary = _load("index_summary_sample.json")
    assert isinstance(summary, list) and summary

    stats = summary[0]["stats"]
    expected = {
        "dailyYield",
        "weeklyYield",
        "monthlyYield",
        "quarterlyYield",
        "yearlyYield",
        "winningRate",
        "volatility",
        "beta",
        "sharpeRatio",
    }
    assert expected <= set(stats), f"missing regime stats: {expected - set(stats)}"
    assert isinstance(stats["beta"], (int, float))


@pytest.mark.unit
def test_interval_change_rate_supports_breadth():
    """interval_change_rate gives multi-period returns per market → breadth.

    This proves the *shape* a PR2 breadth calc would consume. In PR2 the breadth
    fraction is derived from the official Open API ticker, not this robots-
    disallowed crix endpoint (see verdict), but the schema is identical.
    """
    rows = _load("interval_change_rate_sample.json")
    assert isinstance(rows, list) and rows

    periods = {"changeRate7Days", "changeRate30Days", "changeRate90Days"}
    for row in rows:
        assert row["code"].startswith("CRIX.UPBIT."), row["code"]
        assert periods <= set(row), f"missing period fields in {row['code']}"

    # A BTC reference row must exist so alt-vs-BTC breadth is computable.
    codes = {row["code"] for row in rows}
    assert "CRIX.UPBIT.KRW-BTC" in codes


@pytest.mark.unit
def test_index_candles_timeseries_shape():
    """index/candles/lines returns OHLC bars with KST timestamps."""
    payload = _load("index_candles_ubai_sample.json")
    assert "candles" in payload and payload["candles"]

    bar = payload["candles"][0]
    for field in ("openingPrice", "highPrice", "lowPrice", "tradePrice"):
        assert isinstance(bar[field], (int, float)), field
    assert bar["code"] == "IDX.UPBIT.UBAI"
    assert "candleDateTimeKst" in bar


@pytest.mark.unit
@pytest.mark.parametrize(
    "name",
    ["weekly_change_rate_sample.json", "daily_volume_power_bid_sample.json"],
)
def test_trends_market_ranking_shape(name):
    """weekly_change_rate + daily_volume_power share a {markets:[...]} shape."""
    payload = _load(name)
    assert "markets" in payload and payload["markets"]
    first = payload["markets"][0]
    assert first["code"].startswith("CRIX.UPBIT.")
    assert "rank" in first
