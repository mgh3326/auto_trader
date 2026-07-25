"""ROB-722 — earnings auto-inject context builder."""

from __future__ import annotations

import datetime

import pytest

from app.mcp_server.tooling import earnings_context as ec
from app.services.market_events.freshness_service import STALE_AFTER_HOURS

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
            {
                "date": "2026-07-18",
                "hour": "amc",
                "eps_estimate": 0.84,
                "revenue_estimate": 26500000000,
                "quarter": 2,
                "year": 2026,
            },
        ],
    }
    ctx = ec._compact_earnings(
        tool_result, today=TODAY, freshness="live", data_as_of=None
    )
    assert ctx["market"] == "us"
    assert ctx["source"] == "finnhub"
    assert ctx["freshness"] == "live"
    assert ctx["window_days"] == 30
    assert ctx["as_of"] == "2026-07-06"
    assert "data_as_of" not in ctx
    assert ctx["has_upcoming"] is True
    ne = ctx["next_earnings"]
    assert ne["date"] == "2026-07-18"  # nearest future, not the earlier past row
    assert ne["d_minus"] == 12
    assert ne["timing"] == "AMC"
    assert ne["eps_estimate"] == 0.84
    assert ne["quarter"] == 2


def test_compact_earnings_no_upcoming_is_explicit_signal():
    tool_result = {"symbol": "HCA", "source": "finnhub", "earnings": []}
    ctx = ec._compact_earnings(
        tool_result, today=TODAY, freshness="live", data_as_of=None
    )
    assert ctx["has_upcoming"] is False
    assert ctx["next_earnings"] is None
    assert ctx["note"] == "no scheduled earnings within 30 days"


def test_compact_earnings_kr_carries_freshness_and_data_as_of():
    tool_result = {
        "symbol": "005930",
        "market": "kr",
        "source": "market_events",
        "earnings": [
            {
                "date": "2026-07-25",
                "time_hint": "unknown",
                "status": "scheduled",
                "quarter": 2,
                "year": 2026,
            },
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
    ctx = ec._compact_earnings(
        tool_result, today=TODAY, freshness="live", data_as_of=None
    )
    assert ctx["has_upcoming"] is False
    assert ctx["next_earnings"] is None
    assert "degraded" in ctx["note"]


class _FakeScalarResult:
    def __init__(self, value):
        self._value = value

    def scalar_one_or_none(self):
        return self._value


class _FakeDB:
    def __init__(self, value):
        self._value = value

    async def execute(self, _stmt):
        return _FakeScalarResult(self._value)


@pytest.mark.asyncio
async def test_kr_freshness_none_partition_is_unknown():
    freshness, as_of = await ec._kr_ingestion_freshness(_FakeDB(None))
    assert freshness == "unknown"
    assert as_of is None


@pytest.mark.asyncio
async def test_kr_freshness_recent_is_fresh():
    recent = datetime.datetime.now(datetime.UTC) - datetime.timedelta(hours=1)
    freshness, as_of = await ec._kr_ingestion_freshness(_FakeDB(recent))
    assert freshness == "fresh"
    assert as_of == recent.date().isoformat()


@pytest.mark.asyncio
async def test_kr_freshness_old_is_stale():
    old = datetime.datetime.now(datetime.UTC) - datetime.timedelta(
        hours=STALE_AFTER_HOURS + 5
    )
    freshness, as_of = await ec._kr_ingestion_freshness(_FakeDB(old))
    assert freshness == "stale"
    assert as_of == old.date().isoformat()


@pytest.mark.asyncio
async def test_build_earnings_context_crypto_returns_none():
    ctx = await ec.build_earnings_context("BTC", "crypto", today=TODAY)
    assert ctx is None


def test_normalize_earnings_market_accepts_real_market_type_values():
    # resolve_market_type produces equity_kr/equity_us/crypto — the gate must
    # accept the equity_* forms (the {"kr","us"}-only gate was a production
    # no-op) while still taking the bare tool-param forms.
    assert ec.normalize_earnings_market("equity_us") == "us"
    assert ec.normalize_earnings_market("equity_kr") == "kr"
    assert ec.normalize_earnings_market("us") == "us"
    assert ec.normalize_earnings_market("kr") == "kr"
    assert ec.normalize_earnings_market(" Equity_US ") == "us"
    assert ec.normalize_earnings_market("crypto") is None
    assert ec.normalize_earnings_market("") is None
    assert ec.normalize_earnings_market(None) is None


@pytest.mark.asyncio
async def test_build_earnings_context_equity_us_passes_gate(monkeypatch):
    async def _fake_handler(symbol, from_date, to_date, market):
        assert market == "us"  # normalized before dispatch
        return {"symbol": symbol, "source": "finnhub", "earnings": []}

    monkeypatch.setattr(ec, "handle_get_earnings_calendar", _fake_handler)
    ctx = await ec.build_earnings_context("NVDA", "equity_us", today=TODAY)
    assert ctx is not None
    assert ctx["market"] == "us"
    assert ctx["has_upcoming"] is False


@pytest.mark.asyncio
async def test_build_earnings_context_us_calls_handler_and_shapes(monkeypatch):
    async def _fake_handler(symbol, from_date, to_date, market):
        assert symbol == "NVDA"
        assert market == "us"
        assert from_date == "2026-07-06"
        assert to_date == "2026-08-05"  # today + 30d
        return {
            "symbol": "NVDA",
            "source": "finnhub",
            "earnings": [{"date": "2026-07-18", "hour": "amc", "eps_estimate": 0.84}],
        }

    monkeypatch.setattr(ec, "handle_get_earnings_calendar", _fake_handler)
    ctx = await ec.build_earnings_context("NVDA", "us", today=TODAY)
    assert ctx["market"] == "us"
    assert ctx["freshness"] == "live"
    assert ctx["has_upcoming"] is True
    assert ctx["next_earnings"]["d_minus"] == 12


@pytest.mark.asyncio
async def test_build_earnings_context_kr_uses_passed_freshness(monkeypatch):
    async def _fake_handler(symbol, from_date, to_date, market):
        return {
            "symbol": "005930",
            "market": "kr",
            "source": "market_events",
            "earnings": [{"date": "2026-07-25", "time_hint": "unknown"}],
        }

    monkeypatch.setattr(ec, "handle_get_earnings_calendar", _fake_handler)
    ctx = await ec.build_earnings_context(
        "005930", "kr", today=TODAY, kr_freshness=("stale", "2026-07-01")
    )
    assert ctx["freshness"] == "stale"
    assert ctx["data_as_of"] == "2026-07-01"
