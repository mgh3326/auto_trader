from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.services.spike_attribution.contract import DailyBar
from app.services.spike_attribution.scoring import (
    MIN_EVENTS_PER_TYPE,
    VERDICT_EXTENDED,
    VERDICT_FADED,
    VERDICT_RETAINED,
    VERDICT_REVERSED,
    VERDICT_UNSCORABLE,
    ScoringError,
    aggregate_by_class,
    classify_ratio,
    score_event,
)
from tests.services.spike_attribution.test_attribute import (
    build_attribution,
    make_event,
    materials,
)


def record():
    # prev_close 208000 → close 219500, so the spike move is 11500.
    return build_attribution(event=make_event(), materials=materials())


def bars(*closes: str, start_day: int = 21) -> list[DailyBar]:
    out = []
    for offset, close in enumerate(closes):
        value = Decimal(close)
        out.append(
            DailyBar(
                symbol="035420",
                session_date=dt.date(2026, 8, start_day + offset),
                open=value,
                high=value,
                low=value,
                close=value,
                volume=Decimal("1000"),
            )
        )
    return out


@pytest.mark.parametrize(
    ("ratio", "verdict"),
    [
        ("1.5", VERDICT_EXTENDED),
        ("1.0", VERDICT_EXTENDED),
        ("0.9", VERDICT_RETAINED),
        ("0.5", VERDICT_RETAINED),
        ("0.4", VERDICT_FADED),
        ("0.0", VERDICT_FADED),
        ("-0.1", VERDICT_REVERSED),
    ],
)
def test_verdict_bins_are_the_pinned_boundaries(ratio: str, verdict: str) -> None:
    assert classify_ratio(Decimal(ratio)) == verdict


def test_full_retention_scores_extended() -> None:
    score = score_event(
        attribution=record(),
        subsequent_bars=bars("215000", "217000", "219500"),
        window_trading_days=3,
    )
    assert score.verdict == VERDICT_EXTENDED
    assert score.retention_ratio == Decimal("1.0000")
    assert score.bars_used == 3


def test_giving_the_whole_move_back_scores_faded() -> None:
    score = score_event(
        attribution=record(),
        subsequent_bars=bars("215000", "210000", "208000"),
        window_trading_days=3,
    )
    assert score.verdict == VERDICT_FADED
    assert score.retention_ratio == Decimal("0.0000")


def test_trading_through_the_pre_spike_close_scores_reversed() -> None:
    score = score_event(
        attribution=record(),
        subsequent_bars=bars("212000", "205000", "202000"),
        window_trading_days=3,
    )
    assert score.verdict == VERDICT_REVERSED
    assert score.retention_ratio < 0


def test_a_down_spike_scores_on_the_same_formula() -> None:
    from app.services.spike_attribution.contract import SpikeEvent

    event = make_event()
    down = SpikeEvent(
        market=event.market,
        symbol=event.symbol,
        session_date=event.session_date,
        direction="down",
        prev_close=Decimal("100"),
        close=Decimal("90"),
        high=Decimal("100"),
        low=Decimal("89"),
        close_to_close_pct=Decimal("-10.0000"),
        intraday_extreme_pct=Decimal("-11.0000"),
        triggered_bases=("close_to_close",),
        window_start_exclusive=event.window_start_exclusive,
        window_end_inclusive=event.window_end_inclusive,
    )
    attribution = build_attribution(event=down, materials=materials())
    # Still at 90 three days later: the down move fully held.
    held = score_event(
        attribution=attribution,
        subsequent_bars=bars("90", "90", "90"),
        window_trading_days=3,
    )
    assert held.retention_ratio == Decimal("1.0000")
    assert held.verdict == VERDICT_EXTENDED
    # Bounced all the way back: the down move was undone.
    undone = score_event(
        attribution=attribution,
        subsequent_bars=bars("95", "98", "100"),
        window_trading_days=3,
    )
    assert undone.retention_ratio == Decimal("0.0000")
    assert undone.verdict == VERDICT_FADED


def test_missing_bars_are_unscorable_not_imputed() -> None:
    score = score_event(
        attribution=record(),
        subsequent_bars=bars("215000", "217000"),
        window_trading_days=3,
    )
    assert score.verdict == VERDICT_UNSCORABLE
    assert score.retention_ratio is None
    assert score.unscorable_reason == "insufficient_bars_2_of_3"


def test_bars_past_the_window_are_ignored() -> None:
    score = score_event(
        attribution=record(),
        subsequent_bars=bars("215000", "217000", "219500", "300000", "310000"),
        window_trading_days=3,
    )
    assert score.bars_used == 3
    assert score.retention_ratio == Decimal("1.0000")


def test_bars_on_or_before_the_spike_session_are_dropped() -> None:
    stale = bars("999999", start_day=20)
    score = score_event(
        attribution=record(),
        subsequent_bars=stale + bars("215000", "217000", "219500"),
        window_trading_days=3,
    )
    assert score.bars_used == 3
    assert score.retention_ratio == Decimal("1.0000")


def test_excursions_are_reported_as_sensitivity() -> None:
    highs = [
        DailyBar(
            symbol="035420",
            session_date=dt.date(2026, 8, 21 + offset),
            open=Decimal("219500"),
            high=Decimal("231000"),
            low=Decimal("208000"),
            close=Decimal("219500"),
            volume=Decimal("1"),
        )
        for offset in range(3)
    ]
    score = score_event(
        attribution=record(), subsequent_bars=highs, window_trading_days=3
    )
    assert score.max_favorable_excursion_ratio == Decimal("2.0000")
    assert score.max_adverse_excursion_ratio == Decimal("0.0000")


def test_an_unregistered_window_is_refused() -> None:
    with pytest.raises(ScoringError):
        score_event(
            attribution=record(),
            subsequent_bars=bars("215000"),
            window_trading_days=7,
        )


def test_aggregate_withholds_cross_class_comparison_below_the_floor() -> None:
    scores = [
        score_event(
            attribution=record(),
            subsequent_bars=bars("219500", "219500", "219500"),
            window_trading_days=3,
        )
    ]
    aggregate = aggregate_by_class(scores)
    assert aggregate["min_events_per_type_for_comparison"] == MIN_EVENTS_PER_TYPE
    assert aggregate["cross_class_comparison_allowed"] is False
    assert aggregate["by_class"]["unattributed"]["n"] == 1
    assert aggregate["by_class"]["unattributed"]["meets_comparison_floor"] is False
    assert aggregate["winner_declaration"] == "forbidden_until_floor_met"


def test_unscorable_rows_do_not_count_toward_the_floor() -> None:
    scores = [
        score_event(
            attribution=record(),
            subsequent_bars=bars("219500"),
            window_trading_days=3,
        )
        for _ in range(MIN_EVENTS_PER_TYPE + 5)
    ]
    aggregate = aggregate_by_class(scores)
    bucket = aggregate["by_class"]["unattributed"]
    assert bucket["n"] == MIN_EVENTS_PER_TYPE + 5
    assert bucket["n_scorable"] == 0
    assert bucket["meets_comparison_floor"] is False
    assert aggregate["cross_class_comparison_allowed"] is False


def test_empty_input_never_reads_as_comparison_ready() -> None:
    assert aggregate_by_class([])["cross_class_comparison_allowed"] is False
