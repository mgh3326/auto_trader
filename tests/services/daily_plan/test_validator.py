from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.daily_plan.validator import (
    input_reason,
    normalize_symbol,
    parse_positive_decimal,
    require_base_version,
    validate_side,
)


def test_stale_base_version():
    require_base_version(2, 2)
    with pytest.raises(ValueError, match="stale_version"):
        require_base_version(1, 2)


def test_preserves_exact_decimal_and_refuses_json_number():
    assert parse_positive_decimal("9007199254740993.00001") == Decimal(
        "9007199254740993.00001"
    )
    with pytest.raises(ValueError, match="invalid_decimal"):
        parse_positive_decimal(9007199254740993)


def test_kr_symbol_venue_session_and_side():
    assert normalize_symbol(" 278470 ", "kr", "KRX", "regular") == "278470"
    with pytest.raises(ValueError, match="invalid_venue_session"):
        normalize_symbol("278470", "kr", "KRX", "premarket")
    with pytest.raises(ValueError, match="invalid_side"):
        validate_side("add", "sell")


NOW = datetime(2026, 11, 26, tzinfo=UTC)


@pytest.mark.parametrize(
    ("sources", "expected"),
    [
        ({}, "input_missing"),
        ({"price": {"event_at": NOW}}, "observed_time_unproven"),
        (
            {"price": {"event_at": NOW - timedelta(days=2), "first_observed_at": NOW}},
            "input_stale",
        ),
        ({"price": {"event_at": NOW, "first_observed_at": NOW}}, None),
    ],
)
def test_input_observation(sources, expected):
    assert (
        input_reason(
            sources,
            required=frozenset({"price"}),
            bundle_closed_at=NOW,
            oldest_accepted_at=NOW - timedelta(days=1),
        )
        == expected
    )
