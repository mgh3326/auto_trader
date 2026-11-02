"""The shared paper/demo intent contract needs no database or broker."""

from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest

from app.models.mock_strategy import VENUES
from app.services.mock_strategy.recorder import MockStrategyRecorder, OrderIntent


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


@pytest.mark.asyncio
async def test_recorder_keeps_first_open_timestamp_with_fake_session():
    row = SimpleNamespace(
        status="accepted", broker_order_id=None, opened_at=None, meta={}
    )

    class Session:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        def begin(self):
            return self

        async def scalars(self, statement):
            return SimpleNamespace(one=lambda: row)

        async def flush(self):
            pass

        def expunge(self, value):
            assert value is row

    recorder = MockStrategyRecorder(session_factory=Session)
    first_open = datetime(2026, 11, 9, 14, 30, tzinfo=UTC)
    await recorder.update("fixture-entry", status="filled", opened_at=first_open)
    refreshed = await recorder.update(
        "fixture-entry", status="filled", opened_at=first_open + timedelta(days=1)
    )
    assert refreshed.opened_at == first_open
