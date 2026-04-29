from __future__ import annotations

import pytest
from pydantic import ValidationError

from app.schemas.strategy_events import StrategyEventCreateRequest


@pytest.mark.unit
def test_missing_event_type_rejected():
    with pytest.raises(ValidationError):
        StrategyEventCreateRequest(source_text="x")  # type: ignore[arg-type]


@pytest.mark.unit
def test_unknown_event_type_rejected():
    with pytest.raises(ValidationError):
        StrategyEventCreateRequest(
            event_type="not_a_type",  # type: ignore[arg-type]
            source_text="x",
        )


@pytest.mark.unit
def test_source_text_max_length_enforced():
    with pytest.raises(ValidationError):
        StrategyEventCreateRequest(
            event_type="operator_market_event",
            source_text="x" * 8001,
        )


@pytest.mark.unit
def test_severity_range_enforced():
    with pytest.raises(ValidationError):
        StrategyEventCreateRequest(
            event_type="operator_market_event",
            source_text="x",
            severity=6,
        )


@pytest.mark.unit
def test_extra_fields_forbidden():
    with pytest.raises(ValidationError):
        StrategyEventCreateRequest(
            event_type="operator_market_event",
            source_text="x",
            place_order=True,  # type: ignore[call-arg]
        )


@pytest.mark.unit
def test_affected_symbols_round_trip():
    req = StrategyEventCreateRequest(
        event_type="operator_market_event",
        source_text="x",
        affected_symbols=["  AAPL  ", "MSFT"],
        affected_themes=["AI", " growth "],
    )
    assert req.affected_symbols == ["AAPL", "MSFT"]
    assert req.affected_themes == ["AI", "growth"]
