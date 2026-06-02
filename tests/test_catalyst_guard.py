# tests/test_catalyst_guard.py
import datetime as dt

import pytest

from app.services.market_events.catalyst.contract import CatalystEvent
from app.services.market_events.catalyst.guard import evaluate_catalyst_guard


def _ev(symbol, polarity, days, category="conference"):
    return CatalystEvent(
        symbol=symbol, category=category, title="t",
        event_date=dt.date(2026, 6, 2) + dt.timedelta(days=days),
        days_until=days, polarity=polarity, source="manual",
    )


@pytest.mark.unit
def test_trim_with_positive_catalyst_flags():
    g = evaluate_catalyst_guard([_ev("035420", "positive", 3)], side="trim", within_days=7)
    assert g.flag == "upcoming_positive_catalyst"
    assert g.nearest_days == 3
    assert len(g.positive) == 1
    assert g.reason


@pytest.mark.unit
def test_buy_with_negative_catalyst_flags():
    g = evaluate_catalyst_guard([_ev("005930", "negative", 2, category="policy_regulation")], side="buy", within_days=7)
    assert g.flag == "upcoming_negative_catalyst"
    assert g.nearest_days == 2


@pytest.mark.unit
def test_trim_with_only_negative_no_flag():
    g = evaluate_catalyst_guard([_ev("005930", "negative", 2, category="lockup_expiry")], side="trim", within_days=7)
    assert g.flag is None


@pytest.mark.unit
def test_out_of_window_no_flag():
    g = evaluate_catalyst_guard([_ev("035420", "positive", 30)], side="trim", within_days=7)
    assert g.flag is None


@pytest.mark.unit
def test_deterministic():
    events = [_ev("035420", "positive", 5), _ev("000660", "positive", 2)]
    a = evaluate_catalyst_guard(events, side="trim", within_days=7)
    b = evaluate_catalyst_guard(events, side="trim", within_days=7)
    assert a == b
    assert a.nearest_days == 2  # 가장 가까운 positive
