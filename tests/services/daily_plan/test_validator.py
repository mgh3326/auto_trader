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


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("0256C0", "0256C0"), (" 0256c0 ", "0256C0"), ("005930", "005930")],
)
def test_alphanumeric_kr_short_code_is_accepted(raw, expected):
    assert normalize_symbol(raw, "kr", "KRX", "regular") == expected


@pytest.mark.parametrize("raw", ["25C0A", "0256C01", "0256-0", "０２５６C0", ""])
def test_other_symbol_shapes_are_refused(raw):
    with pytest.raises(ValueError, match="invalid_symbol"):
        normalize_symbol(raw, "kr", "KRX", "regular")
