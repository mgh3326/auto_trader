"""The shared paper/demo intent contract needs no database or broker."""

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models.mock_strategy import VENUES
from app.services.mock_strategy.recorder import OrderIntent


def intent(**overrides):
    values = {
        "strategy_id": "fixture.v1",
        "venue": "alpaca_paper",
        "account_ref": "fixture",
        "episode_id": "episode",
        "symbol": "AAPL",
        "side": "buy",
        "qty": None,
        "notional": Decimal("10000"),
        "client_order_id": "fixture-entry",
        "exit_due_at": datetime(2026, 11, 9, 14, 30, tzinfo=UTC),
        "meta": {},
    }
    return OrderIntent(**(values | overrides))


@pytest.mark.parametrize("venue", VENUES)
def test_all_four_mock_venues_and_large_paper_sizes(venue):
    assert intent(venue=venue, notional=Decimal("100000000")).venue == venue


@pytest.mark.parametrize(
    "changes",
    [
        {"venue": "alpaca_live"},
        {"side": "liquidate"},
        {"strategy_id": " "},
        {"qty": None, "notional": None},
        {"notional": Decimal("NaN")},
        {"notional": Decimal("Infinity")},
        {"qty": Decimal("-1")},
        {"exit_due_at": datetime(2026, 11, 9)},
    ],
)
def test_invalid_boundary(changes):
    with pytest.raises(ValueError):
        replace(intent(), **changes)
