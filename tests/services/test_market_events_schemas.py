"""Pydantic schema tests for market events (ROB-128)."""

from __future__ import annotations

from datetime import date
from decimal import Decimal

import pytest


@pytest.mark.unit
def test_market_event_value_response_round_trip():
    from app.schemas.market_events import MarketEventValueResponse

    payload = {
        "metric_name": "eps",
        "period": "Q1-2026",
        "actual": Decimal("-0.38"),
        "forecast": Decimal("-0.3593"),
        "previous": None,
        "unit": "USD",
        "surprise": None,
        "surprise_pct": None,
    }
    obj = MarketEventValueResponse.model_validate(payload)
    assert obj.metric_name == "eps"
    assert float(obj.actual) == pytest.approx(-0.38)


@pytest.mark.unit
def test_market_event_response_includes_held_watched():
    from app.schemas.market_events import MarketEventResponse, MarketEventValueResponse

    obj = MarketEventResponse(
        category="earnings",
        market="us",
        symbol="IONQ",
        title="IONQ earnings release",
        event_date=date(2026, 5, 7),
        time_hint="after_close",
        held=True,
        watched=False,
        values=[],
        source="finnhub",
        source_event_id=None,
    )
    dumped = obj.model_dump()
    assert dumped["held"] is True
    assert dumped["watched"] is False
    assert dumped["values"] == []


@pytest.mark.unit
def test_market_events_day_response_shape():
    from app.schemas.market_events import MarketEventsDayResponse

    obj = MarketEventsDayResponse(date=date(2026, 5, 7), events=[])
    assert obj.model_dump()["date"] == date(2026, 5, 7)
