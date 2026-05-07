"""Unit tests for market event prioritization (ROB-138)."""

from __future__ import annotations

from datetime import date

import pytest

from app.schemas.market_events import MarketEventResponse
from app.services.market_events.prioritization import (
    MAJOR_TICKERS,
    Priority,
    compute_priority,
)
from app.services.market_events.user_context import UserEventContext


def _evt(**kw) -> MarketEventResponse:
    base = dict(
        category="earnings",
        market="us",
        symbol=None,
        event_date=date(2026, 5, 7),
        source="finnhub",
        importance=None,
    )
    base.update(kw)
    return MarketEventResponse(**base)


def _ctx(held: set[str] | None = None, watched: set[str] | None = None) -> UserEventContext:
    return UserEventContext(
        held_tickers=frozenset(held or set()),
        watched_tickers=frozenset(watched or set()),
    )


@pytest.mark.unit
def test_held_beats_watched_and_major():
    ctx = _ctx(held={"AAPL"}, watched={"AAPL"})
    p = compute_priority(_evt(symbol="AAPL"), ctx)
    assert p == Priority.HELD


@pytest.mark.unit
def test_watched_beats_major():
    ctx = _ctx(watched={"AAPL"})
    p = compute_priority(_evt(symbol="AAPL"), ctx)
    assert p == Priority.WATCHED


@pytest.mark.unit
def test_major_when_in_allowlist():
    assert "AAPL" in MAJOR_TICKERS["us"]
    p = compute_priority(_evt(symbol="AAPL"), _ctx())
    assert p == Priority.MAJOR


@pytest.mark.unit
def test_high_importance_when_economic_high():
    p = compute_priority(_evt(category="economic", market="global", importance=3), _ctx())
    assert p == Priority.HIGH_IMPORTANCE


@pytest.mark.unit
def test_medium_importance_when_economic_medium():
    p = compute_priority(_evt(category="economic", market="global", importance=2), _ctx())
    assert p == Priority.MEDIUM_IMPORTANCE


@pytest.mark.unit
def test_other_for_random_earnings():
    p = compute_priority(_evt(symbol="OBSCURE_TICKER_123"), _ctx())
    assert p == Priority.OTHER


@pytest.mark.unit
def test_symbol_normalized_for_match():
    """Holdings stored as `BRK.B`; events sometimes carry `BRK.B`. Match case-insensitive."""
    ctx = _ctx(held={"BRK.B"})
    assert compute_priority(_evt(symbol="brk.b"), ctx) == Priority.HELD
