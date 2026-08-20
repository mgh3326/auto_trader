from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.spike_attribution.contract import DailyBar
from app.services.spike_attribution.detect import (
    BASIS_CLOSE_TO_CLOSE,
    BASIS_INTRADAY_EXTREME,
    SpikeDetectionError,
    classify_bar,
    detect_spikes,
    session_close_at,
)


def bar(day: int, o: str, h: str, low: str, c: str, v: str = "1000") -> DailyBar:
    return DailyBar(
        symbol="TEST",
        session_date=dt.date(2026, 8, day),
        open=Decimal(o),
        high=Decimal(h),
        low=Decimal(low),
        close=Decimal(c),
        volume=Decimal(v),
    )


def test_close_to_close_spike_fires_and_carries_direction() -> None:
    prev = bar(19, "100", "101", "99", "100")
    today = bar(20, "101", "107", "100", "106")
    event = classify_bar(market="kr", symbol="TEST", bar=today, prev_bar=prev)
    assert event is not None
    assert event.direction == "up"
    assert BASIS_CLOSE_TO_CLOSE in event.triggered_bases
    assert event.close_to_close_pct == Decimal("6.0000")


def test_intraday_only_spike_fires_on_the_intraday_basis() -> None:
    # Touched +6% intraday but closed +2%: the operator's "5% 급등" can be an
    # intraday event, and collapsing to close-only would lose it.
    prev = bar(19, "100", "101", "99", "100")
    today = bar(20, "101", "106", "100", "102")
    event = classify_bar(market="kr", symbol="TEST", bar=today, prev_bar=prev)
    assert event is not None
    assert event.triggered_bases == (BASIS_INTRADAY_EXTREME,)
    assert event.close_to_close_pct == Decimal("2.0000")
    assert event.intraday_extreme_pct == Decimal("6.0000")
    assert event.direction == "up"


def test_gap_up_that_closes_red_is_a_down_session() -> None:
    # Touched +6% intraday, closed -1%. Direction must follow the close, or the
    # follow-through denominator (close - prev_close) would be negative while
    # the event claimed "up" — and the forecast would flip at_or_above.
    prev = bar(19, "100", "101", "99", "100")
    today = bar(20, "105", "106", "98", "99")
    event = classify_bar(market="kr", symbol="TEST", bar=today, prev_bar=prev)
    assert event is not None
    assert event.triggered_bases == (BASIS_INTRADAY_EXTREME,)
    assert event.direction == "down"
    assert event.close < event.prev_close


def test_unchanged_close_falls_back_to_the_triggering_extreme() -> None:
    prev = bar(19, "100", "101", "99", "100")
    today = bar(20, "100", "106", "100", "100")
    event = classify_bar(market="kr", symbol="TEST", bar=today, prev_bar=prev)
    assert event is not None
    assert event.close_to_close_pct == Decimal("0.0000")
    assert event.direction == "up"


def test_down_spike_is_in_scope() -> None:
    prev = bar(19, "100", "101", "99", "100")
    today = bar(20, "99", "99", "92", "93")
    event = classify_bar(market="kr", symbol="TEST", bar=today, prev_bar=prev)
    assert event is not None
    assert event.direction == "down"
    assert event.close_to_close_pct == Decimal("-7.0000")


def test_below_threshold_is_not_a_spike() -> None:
    prev = bar(19, "100", "101", "99", "100")
    today = bar(20, "100", "104", "99", "103")
    assert classify_bar(market="kr", symbol="TEST", bar=today, prev_bar=prev) is None


def test_threshold_is_inclusive_at_exactly_five_percent() -> None:
    prev = bar(19, "100", "101", "99", "100")
    today = bar(20, "101", "105", "100", "105")
    event = classify_bar(market="kr", symbol="TEST", bar=today, prev_bar=prev)
    assert event is not None


def test_evidence_window_spans_prev_close_to_spike_close() -> None:
    prev = bar(19, "100", "101", "99", "100")
    today = bar(20, "101", "107", "100", "106")
    event = classify_bar(market="kr", symbol="TEST", bar=today, prev_bar=prev)
    assert event is not None
    assert event.window_start_exclusive == session_close_at("kr", dt.date(2026, 8, 19))
    assert event.window_end_inclusive == session_close_at("kr", dt.date(2026, 8, 20))
    assert event.window_end_inclusive.hour == 15
    assert event.window_end_inclusive.minute == 30


def test_us_window_uses_the_us_session_close() -> None:
    close = session_close_at("us", dt.date(2026, 8, 20))
    assert (close.hour, close.minute) == (16, 0)
    assert close.tzinfo is not None


def test_unsupported_market_is_rejected() -> None:
    with pytest.raises(SpikeDetectionError):
        session_close_at("crypto", dt.date(2026, 8, 20))


def test_non_positive_prev_close_refuses_rather_than_dividing() -> None:
    prev = bar(19, "0", "0", "0", "0")
    today = bar(20, "1", "1", "1", "1")
    with pytest.raises(SpikeDetectionError):
        classify_bar(market="kr", symbol="TEST", bar=today, prev_bar=prev)


def test_first_bar_has_no_prev_close_and_is_skipped() -> None:
    bars = [bar(20, "101", "107", "100", "106")]
    event, diagnostics = detect_spikes(
        market="kr", symbol="TEST", bars=bars, session_date=dt.date(2026, 8, 20)
    )
    assert event is None
    assert diagnostics["skipped"] == "skip_not_spike"


def test_halted_suspect_is_excluded_with_its_reason_not_silently_dropped() -> None:
    # Three frozen sessions then a "spike" print: ROB-1236 says this series is
    # not trustworthy, and the exclusion has to arrive with its evidence.
    frozen = [
        DailyBar(
            symbol="TEST",
            session_date=dt.date(2026, 8, day),
            open=Decimal("100"),
            high=Decimal("100"),
            low=Decimal("100"),
            close=Decimal("100"),
            volume=Decimal("0"),
        )
        for day in (17, 18, 19, 20)
    ]
    event, diagnostics = detect_spikes(
        market="kr", symbol="TEST", bars=frozen, session_date=dt.date(2026, 8, 20)
    )
    assert event is None
    assert diagnostics["skipped"] == "halted_suspect"
    assert diagnostics["halted_suspect"]["suspected"] is True
    assert diagnostics["halted_suspect"]["krx_halt_master"] == "unavailable"


def test_live_series_is_reported_with_a_halt_verdict_even_when_clean() -> None:
    bars = [
        bar(17, "100", "101", "99", "100"),
        bar(18, "100", "101", "99", "100", v="900"),
        bar(19, "100", "102", "99", "101", v="800"),
        bar(20, "101", "108", "101", "107", v="2000"),
    ]
    event, diagnostics = detect_spikes(
        market="kr", symbol="TEST", bars=bars, session_date=dt.date(2026, 8, 20)
    )
    assert event is not None
    assert diagnostics["halted_suspect"]["suspected"] is False


def test_duplicate_session_dates_are_refused() -> None:
    bars = [bar(20, "100", "101", "99", "100"), bar(20, "100", "101", "99", "100")]
    with pytest.raises(SpikeDetectionError):
        detect_spikes(
            market="kr", symbol="TEST", bars=bars, session_date=dt.date(2026, 8, 20)
        )
