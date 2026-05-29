"""Unit tests for the lookahead-safe earnings decision-time labeler (ROB-371)."""

from __future__ import annotations

from datetime import date

import pytest

from app.services.market_events.earnings_decision_time import (
    label_earnings_decision_time,
)


@pytest.mark.unit
def test_bmo_on_trading_day_reacts_same_session_next_open():
    # 2025-07-07 Monday session: BMO news is public before the open, so the
    # event session's own open is the first lookahead-safe reaction bar.
    label = label_earnings_decision_time(date(2025, 7, 7), "before_open")
    assert label.decision_session == date(2025, 7, 7)
    assert label.anchor == "next_open"
    assert label.is_lookahead_safe is True
    assert label.is_intraday_rejected is False


@pytest.mark.unit
def test_bmo_on_holiday_moves_to_next_session():
    # BMO labeled on a holiday -> first actual session is the next one (07-07).
    label = label_earnings_decision_time(date(2025, 7, 4), "before_open")
    assert label.decision_session == date(2025, 7, 7)
    assert label.anchor == "next_open"


@pytest.mark.unit
def test_bmo_on_weekend_moves_to_next_session():
    # 2025-07-05 Saturday BMO -> next session Monday 07-07.
    label = label_earnings_decision_time(date(2025, 7, 5), "before_open")
    assert label.decision_session == date(2025, 7, 7)
    assert label.anchor == "next_open"


@pytest.mark.unit
def test_amc_reacts_next_session_next_close():
    # 2025-07-07 Mon AMC -> next session Tue 2025-07-08.
    # anchor semantics: the NEXT session's CLOSE (not its open).
    label = label_earnings_decision_time(date(2025, 7, 7), "after_close")
    assert label.decision_session == date(2025, 7, 8)
    assert label.anchor == "next_close"
    assert label.is_lookahead_safe is True


@pytest.mark.unit
def test_amc_across_holiday_and_weekend_skips_to_next_session():
    # 2025-07-03 Thu AMC; 07-04 holiday, 07-05/06 weekend -> next session 07-07.
    label = label_earnings_decision_time(date(2025, 7, 3), "after_close")
    assert label.decision_session == date(2025, 7, 7)
    assert label.anchor == "next_close"


@pytest.mark.unit
def test_during_market_is_intraday_rejected_next_full_session():
    label = label_earnings_decision_time(date(2025, 7, 7), "during_market")
    assert label.anchor == "whole_day_uncertain"
    assert label.is_intraday_rejected is True
    assert label.decision_session == date(2025, 7, 8)  # next clean full session


@pytest.mark.unit
def test_unknown_time_maps_to_next_session_conservatively():
    # B1 (lookahead): unknown could have been AMC, so the event session's close
    # may be history before the news broke. Conservatively use the NEXT session.
    label = label_earnings_decision_time(date(2025, 7, 7), "unknown")
    assert label.decision_session == date(2025, 7, 8)
    assert label.anchor == "whole_day_uncertain"
    assert label.is_lookahead_safe is True
    assert label.is_intraday_rejected is False


@pytest.mark.unit
def test_none_time_hint_behaves_like_unknown():
    label = label_earnings_decision_time(date(2025, 7, 7), None)
    assert label.decision_session == date(2025, 7, 8)
    assert label.anchor == "whole_day_uncertain"


@pytest.mark.unit
def test_far_future_event_is_unmappable():
    label = label_earnings_decision_time(date(2100, 1, 1), "before_open")
    assert label.anchor == "unmappable"
    assert label.decision_session is None
    assert label.is_lookahead_safe is False


@pytest.mark.unit
def test_far_past_event_is_unmappable():
    label = label_earnings_decision_time(date(1900, 1, 1), "after_close")
    assert label.anchor == "unmappable"
    assert label.decision_session is None
    assert label.is_lookahead_safe is False
