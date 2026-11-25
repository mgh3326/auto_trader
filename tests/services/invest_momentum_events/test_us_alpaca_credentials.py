"""hk 6510 follow-up: Alpaca screener credentials fall back to the paper pair."""

from __future__ import annotations

import pytest

from app.services.invest_momentum_events.us_sources import alpaca_data_credentials


def test_data_pair_wins() -> None:
    env = {
        "ALPACA_DATA_API_KEY_ID": "dk",
        "ALPACA_DATA_API_SECRET_KEY": "ds",
        "ALPACA_PAPER_API_KEY": "pk",
        "ALPACA_PAPER_API_SECRET": "ps",
    }
    assert alpaca_data_credentials(env) == ("dk", "ds")


def test_paper_pair_used_when_data_pair_unset() -> None:
    env = {"ALPACA_PAPER_API_KEY": "pk", "ALPACA_PAPER_API_SECRET": "ps"}
    assert alpaca_data_credentials(env) == ("pk", "ps")


def test_none_when_nothing_set() -> None:
    assert alpaca_data_credentials({}) is None


def test_half_data_pair_still_fails_closed() -> None:
    with pytest.raises(ValueError):
        alpaca_data_credentials(
            {
                "ALPACA_DATA_API_KEY_ID": "dk",
                "ALPACA_PAPER_API_KEY": "pk",
                "ALPACA_PAPER_API_SECRET": "ps",
            }
        )


def test_half_paper_pair_fails_closed() -> None:
    with pytest.raises(ValueError):
        alpaca_data_credentials({"ALPACA_PAPER_API_KEY": "pk"})
