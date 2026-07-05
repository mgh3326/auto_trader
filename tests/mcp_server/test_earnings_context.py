"""ROB-722 — earnings auto-inject context builder."""

from __future__ import annotations

import datetime

import pytest

from app.mcp_server.tooling import earnings_context as ec

TODAY = datetime.date(2026, 7, 6)


def test_map_timing_variants():
    assert ec._map_timing("bmo") == "BMO"
    assert ec._map_timing("AMC") == "AMC"
    assert ec._map_timing("dmh") == "DMH"
    assert ec._map_timing("") == "unknown"
    assert ec._map_timing(None) == "unknown"
    assert ec._map_timing("weird") == "unknown"


def test_compact_earnings_us_upcoming_picks_nearest_future():
    tool_result = {
        "symbol": "NVDA",
        "source": "finnhub",
        "earnings": [
            {"date": "2026-01-01", "hour": "amc"},  # past — excluded
            {"date": "2026-07-25", "hour": "bmo", "eps_estimate": 0.9},
            {"date": "2026-07-18", "hour": "amc", "eps_estimate": 0.84,
             "revenue_estimate": 26500000000, "quarter": 2, "year": 2026},
        ],
    }
    ctx = ec._compact_earnings(tool_result, today=TODAY, freshness="live", data_as_of=None)
    assert ctx["market"] == "us"
    assert ctx["source"] == "finnhub"
    assert ctx["freshness"] == "live"
    assert ctx["window_days"] == 30
    assert ctx["as_of"] == "2026-07-06"
    assert "data_as_of" not in ctx
    assert ctx["has_upcoming"] is True
    ne = ctx["next_earnings"]
    assert ne["date"] == "2026-07-18"       # nearest future, not the earlier past row
    assert ne["d_minus"] == 12
    assert ne["timing"] == "AMC"
    assert ne["eps_estimate"] == 0.84
    assert ne["quarter"] == 2


def test_compact_earnings_no_upcoming_is_explicit_signal():
    tool_result = {"symbol": "HCA", "source": "finnhub", "earnings": []}
    ctx = ec._compact_earnings(tool_result, today=TODAY, freshness="live", data_as_of=None)
    assert ctx["has_upcoming"] is False
    assert ctx["next_earnings"] is None
    assert ctx["note"] == "no scheduled earnings within 30 days"


def test_compact_earnings_kr_carries_freshness_and_data_as_of():
    tool_result = {
        "symbol": "005930",
        "market": "kr",
        "source": "market_events",
        "earnings": [
            {"date": "2026-07-25", "time_hint": "unknown", "status": "scheduled",
             "quarter": 2, "year": 2026},
        ],
    }
    ctx = ec._compact_earnings(
        tool_result, today=TODAY, freshness="stale", data_as_of="2026-07-01"
    )
    assert ctx["market"] == "kr"
    assert ctx["source"] == "market_events"
    assert ctx["freshness"] == "stale"
    assert ctx["data_as_of"] == "2026-07-01"
    assert ctx["next_earnings"]["timing"] == "unknown"
    assert ctx["next_earnings"]["status"] == "scheduled"


def test_compact_earnings_error_payload_degrades():
    tool_result = {"symbol": "NVDA", "source": "finnhub", "error": "429 quota"}
    ctx = ec._compact_earnings(tool_result, today=TODAY, freshness="live", data_as_of=None)
    assert ctx["has_upcoming"] is False
    assert ctx["next_earnings"] is None
    assert "degraded" in ctx["note"]
