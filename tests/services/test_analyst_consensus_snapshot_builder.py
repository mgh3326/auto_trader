"""Builder behaviour for analyst consensus snapshots (ROB-641).

Covers the market-local snapshot_date convention (kr → Asia/Seoul,
us → America/New_York), the limit=30 opinions fetch, the meaningful-data
gate, and analyst_count wiring.
"""

from __future__ import annotations

import datetime as dt
from typing import Any
from zoneinfo import ZoneInfo

import pytest

from app.services.analyst_consensus_snapshots.builder import (
    OPINIONS_LIMIT,
    build_consensus_snapshots,
    default_consensus_fetcher,
)

_KST = ZoneInfo("Asia/Seoul")


def _kr_consensus(**overrides: Any) -> dict[str, Any]:
    consensus: dict[str, Any] = {
        "buy_count": 10,
        "hold_count": 5,
        "sell_count": 2,
        "strong_buy_count": 3,
        "total_count": 17,
        "avg_target_price": 100000,
        "median_target_price": 95000,
        "min_target_price": 80000,
        "max_target_price": 120000,
        "upside_pct": 15.5,
        "current_price": 86000,
    }
    consensus.update(overrides)
    return consensus


def _fake_fetcher(consensus: dict[str, Any], *, source: str = "naver_finance"):
    async def fetch(market: str, symbol: str) -> dict[str, Any]:
        return {
            "source": source,
            "consensus": consensus,
            "opinions": [],
            "opinions_limit": OPINIONS_LIMIT,
            "newest_opinion_date": None,
        }

    return fetch


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_date_kst_morning_is_kst_date_for_kr() -> None:
    """00:30 KST must book the KST calendar date, not the previous UTC day."""
    now = dt.datetime(2026, 7, 2, 0, 30, tzinfo=_KST)  # 2026-07-01T15:30Z
    result = await build_consensus_snapshots(
        market="kr",
        symbols=["005930"],
        now=now,
        fetcher=_fake_fetcher(_kr_consensus()),
    )
    assert len(result.payloads) == 1
    assert result.payloads[0].snapshot_date == dt.date(2026, 7, 2)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_snapshot_date_us_uses_eastern_calendar_date() -> None:
    """01:30 KST Jul 2 is 12:30 ET Jul 1 → US rows book the ET date."""
    now = dt.datetime(2026, 7, 2, 1, 30, tzinfo=_KST)
    result = await build_consensus_snapshots(
        market="us",
        symbols=["AAPL"],
        now=now,
        fetcher=_fake_fetcher(_kr_consensus(), source="yfinance"),
    )
    assert len(result.payloads) == 1
    assert result.payloads[0].snapshot_date == dt.date(2026, 7, 1)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_default_fetcher_passes_limit_30(monkeypatch) -> None:
    captured: dict[str, Any] = {}

    async def fake_handler(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return {"consensus": _kr_consensus(), "opinions": []}

    monkeypatch.setattr(
        "app.mcp_server.tooling.fundamentals._valuation.handle_get_investment_opinions",
        fake_handler,
    )
    data = await default_consensus_fetcher("kr", "005930")
    assert captured["limit"] == 30
    assert data["opinions_limit"] == 30


@pytest.mark.unit
@pytest.mark.asyncio
async def test_raw_payload_records_opinions_limit() -> None:
    result = await build_consensus_snapshots(
        market="kr",
        symbols=["005930"],
        now=dt.datetime(2026, 7, 2, 9, 0, tzinfo=_KST),
        fetcher=_fake_fetcher(_kr_consensus()),
    )
    assert result.payloads[0].raw_payload["opinions_limit"] == OPINIONS_LIMIT


@pytest.mark.unit
@pytest.mark.asyncio
async def test_meaningful_data_gate_skips_current_price_only_rows() -> None:
    """A row carrying only current_price is a quote, not consensus data."""
    consensus = {
        "buy_count": None,
        "hold_count": None,
        "sell_count": None,
        "strong_buy_count": None,
        "total_count": None,
        "avg_target_price": None,
        "median_target_price": None,
        "min_target_price": None,
        "max_target_price": None,
        "upside_pct": None,
        "current_price": 185.5,
    }
    result = await build_consensus_snapshots(
        market="us",
        symbols=["NOCOV"],
        now=dt.datetime(2026, 7, 2, 9, 0, tzinfo=_KST),
        fetcher=_fake_fetcher(consensus, source="yfinance"),
    )
    assert result.payloads == ()
    assert any("NOCOV" in warning for warning in result.warnings)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_meaningful_data_gate_keeps_target_only_rows() -> None:
    """Target fields without counts still qualify (e.g. Yahoo target-only)."""
    consensus = {
        "total_count": None,
        "avg_target_price": 210.0,
        "current_price": 185.5,
    }
    result = await build_consensus_snapshots(
        market="us",
        symbols=["TGTONLY"],
        now=dt.datetime(2026, 7, 2, 9, 0, tzinfo=_KST),
        fetcher=_fake_fetcher(consensus, source="yfinance"),
    )
    assert len(result.payloads) == 1


@pytest.mark.unit
@pytest.mark.asyncio
async def test_analyst_count_kr_uses_target_price_count() -> None:
    result = await build_consensus_snapshots(
        market="kr",
        symbols=["005930"],
        now=dt.datetime(2026, 7, 2, 9, 0, tzinfo=_KST),
        fetcher=_fake_fetcher(_kr_consensus(target_price_count=12)),
    )
    payload = result.payloads[0]
    assert payload.total_count == 17
    assert payload.analyst_count == 12


@pytest.mark.unit
@pytest.mark.asyncio
async def test_analyst_count_us_uses_number_of_analyst_opinions() -> None:
    result = await build_consensus_snapshots(
        market="us",
        symbols=["AAPL"],
        now=dt.datetime(2026, 7, 2, 9, 0, tzinfo=_KST),
        fetcher=_fake_fetcher(
            _kr_consensus(total_count=30, number_of_analyst_opinions=25),
            source="yfinance",
        ),
    )
    payload = result.payloads[0]
    assert payload.total_count == 30
    assert payload.analyst_count == 25


@pytest.mark.unit
@pytest.mark.asyncio
async def test_analyst_count_falls_back_to_total_count() -> None:
    result = await build_consensus_snapshots(
        market="kr",
        symbols=["005930"],
        now=dt.datetime(2026, 7, 2, 9, 0, tzinfo=_KST),
        fetcher=_fake_fetcher(_kr_consensus()),
    )
    payload = result.payloads[0]
    assert payload.analyst_count == payload.total_count == 17
