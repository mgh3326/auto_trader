from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from inspect import signature

import pytest

from app.services.buy_gate_ab_shadow.scoring import (
    CohortSample,
    DailyBar,
    ScoringError,
    compare_cohorts,
    score_window,
)

_DECISION = date(2026, 8, 20)
_SCORING_AS_OF = datetime(2026, 9, 18, 15, 0, tzinfo=UTC)


def _bars(
    n: int, *, start: date = date(2026, 8, 21), close: str = "110"
) -> tuple[DailyBar, ...]:
    rows: list[DailyBar] = []
    for index in range(n):
        session = start + timedelta(days=index)
        px = Decimal(close)
        rows.append(DailyBar(session_date=session, high=px + 1, low=px - 1, close=px))
    return tuple(rows)


def test_score_window_does_not_take_a_variant_argument() -> None:
    assert "variant" not in signature(score_window).parameters


def test_same_entry_and_bars_yield_identical_scores_regardless_of_label() -> None:
    bars = _bars(20)
    score = score_window(
        entry=Decimal("100"),
        bars=bars,
        decision_date=_DECISION,
        scoring_as_of=_SCORING_AS_OF,
        window_trading_days=5,
    )
    assert score.scoreable is True
    assert score.simple_return_to_close == Decimal("0.10")
    assert score.max_drawdown_from_entry_close_peak == Decimal("0")
    assert score.simple_return_to_window_high == Decimal("0.11")
    assert score.simple_return_to_window_low == Decimal("0.09")


def test_bars_after_scoring_as_of_are_ignored_even_if_one_arm_is_fed_them() -> None:
    cutoff = datetime(2026, 8, 24, 0, 0, tzinfo=UTC)
    future = _bars(20)
    clipped = score_window(
        entry=Decimal("100"),
        bars=future,
        decision_date=_DECISION,
        scoring_as_of=cutoff,
        window_trading_days=5,
    )
    assert clipped.scoreable is False
    assert clipped.reason == "insufficient_bars"


def test_missing_bars_are_not_imputed() -> None:
    score = score_window(
        entry=Decimal("100"),
        bars=_bars(4),
        decision_date=_DECISION,
        scoring_as_of=_SCORING_AS_OF,
        window_trading_days=5,
    )
    assert score.scoreable is False
    assert score.simple_return_to_close is None


def test_drawdown_uses_entry_as_initial_peak() -> None:
    bars = (
        DailyBar(
            session_date=date(2026, 8, 21),
            high=Decimal("95"),
            low=Decimal("90"),
            close=Decimal("92"),
        ),
        DailyBar(
            session_date=date(2026, 8, 22),
            high=Decimal("101"),
            low=Decimal("91"),
            close=Decimal("100"),
        ),
        DailyBar(
            session_date=date(2026, 8, 24),
            high=Decimal("103"),
            low=Decimal("80"),
            close=Decimal("85"),
        ),
        DailyBar(
            session_date=date(2026, 8, 25),
            high=Decimal("90"),
            low=Decimal("84"),
            close=Decimal("88"),
        ),
        DailyBar(
            session_date=date(2026, 8, 26),
            high=Decimal("110"),
            low=Decimal("88"),
            close=Decimal("105"),
        ),
    )
    score = score_window(
        entry=Decimal("100"),
        bars=bars,
        decision_date=_DECISION,
        scoring_as_of=_SCORING_AS_OF,
        window_trading_days=5,
    )
    assert score.simple_return_to_close == Decimal("0.05")
    assert score.max_drawdown_from_entry_close_peak == Decimal("-0.15")


def test_compare_cohorts_uses_one_as_of_and_refuses_to_name_a_winner() -> None:
    bars = _bars(20)
    report = compare_cohorts(
        [
            CohortSample(
                variant="A",
                symbol="005930",
                decision_date=_DECISION,
                entry=Decimal("100"),
                bars=bars,
                actual_fill_price=Decimal("99"),
            ),
            CohortSample(
                variant="B",
                symbol="000660",
                decision_date=_DECISION,
                entry=Decimal("100"),
                bars=bars,
            ),
        ],
        scoring_as_of=_SCORING_AS_OF,
        collection_start=date(2026, 8, 20),
    )
    assert report["winner_declaration"] == "forbidden"
    assert report["intermediate_use_forbidden"] is True
    assert report["actual_fill_return_is_sensitivity_only"] is True
    assert report["scoring_as_of"] == _SCORING_AS_OF.isoformat()
    a5 = report["arms"]["A"]["windows"]["5"]["primary"]
    b5 = report["arms"]["B"]["windows"]["5"]["primary"]
    assert a5 == b5
    fill = report["arms"]["A"]["sensitivity_actual_fill_vs_frozen_entry"]
    assert fill["n"] == 1
    assert fill["mean"] == str(Decimal("-0.01"))
    assert report["arms"]["B"]["sensitivity_actual_fill_vs_frozen_entry"]["n"] == 0


def test_incomplete_collection_is_flagged_not_for_policy() -> None:
    report = compare_cohorts(
        [
            CohortSample(
                variant="B",
                symbol="005930",
                decision_date=_DECISION,
                entry=Decimal("100"),
                bars=_bars(20),
            )
        ],
        scoring_as_of=datetime(2026, 8, 25, tzinfo=UTC),
        collection_start=date(2026, 8, 20),
    )
    assert report["collection_complete"] is False
    assert report["status"] == "collection_incomplete"
    assert report["policy_implication"] == "none_until_collection_complete"
    assert report["score_computation"] == "refused_until_collection_complete"
    assert "arms" not in report


def test_unregistered_window_is_rejected() -> None:
    with pytest.raises(ScoringError, match="not pre-registered"):
        score_window(
            entry=Decimal("100"),
            bars=_bars(20),
            decision_date=_DECISION,
            scoring_as_of=_SCORING_AS_OF,
            window_trading_days=10,
        )
