# tests/services/action_report/snapshot_backed/test_action_verdict.py
"""ROB-335 — sub-verdict vocabulary, bucket mapping, held-symbol rules."""

from __future__ import annotations

import pytest

from app.models.investment_symbol_intermediate_reports import DECISION_BUCKETS
from app.services.action_report.snapshot_backed.action_verdict import (
    ACTION_VERDICTS,
    VERDICT_TO_BUCKET,
    classify_candidate_symbol,
    classify_held_symbol,
)

pytestmark = pytest.mark.unit


def test_every_verdict_maps_to_a_locked_decision_bucket() -> None:
    # B/A: sub-verdicts are sub-labels over the locked 5-value enum — every
    # verdict must map onto an existing decision_bucket (no new enum value).
    assert set(VERDICT_TO_BUCKET) == set(ACTION_VERDICTS)
    for verdict, bucket in VERDICT_TO_BUCKET.items():
        assert bucket in DECISION_BUCKETS, (verdict, bucket)


def test_held_unactionable_quote_is_data_gap() -> None:
    holding = {"ticker": "005930", "sellable_quantity": 10}
    quote = {"status": "unavailable"}
    assert (
        classify_held_symbol(holding, quote, in_candidate_universe=False) == "data_gap"
    )


def test_held_missing_quote_is_data_gap() -> None:
    holding = {"ticker": "005930", "sellable_quantity": 10}
    assert (
        classify_held_symbol(holding, None, in_candidate_universe=False) == "data_gap"
    )


def test_held_sellable_with_actionable_quote_is_sell_review() -> None:
    holding = {"ticker": "005930", "sellable_quantity": 10}
    quote = {"status": "ok", "best_bid": 1.0, "best_ask": 2.0, "bid_depth": 5.0}
    assert (
        classify_held_symbol(holding, quote, in_candidate_universe=False)
        == "sell_review"
    )


def test_held_not_sellable_but_trending_is_no_add() -> None:
    holding = {"ticker": "005930", "sellable_quantity": 0}
    quote = {"status": "ok", "best_bid": 1.0, "best_ask": 2.0, "ask_depth": 5.0}
    assert classify_held_symbol(holding, quote, in_candidate_universe=True) == "no_add"


def test_held_not_sellable_not_trending_is_keep() -> None:
    holding = {"ticker": "005930", "sellable_quantity": 0}
    quote = {"status": "ok", "best_bid": 1.0, "best_ask": 2.0, "ask_depth": 5.0}
    assert classify_held_symbol(holding, quote, in_candidate_universe=False) == "keep"


_OK_QUOTE = {
    "status": "ok",
    "best_bid": 100,
    "best_ask": 101,
    "bid_depth": 5,
    "ask_depth": 5,
    "spread_bps": 10,
}
_DEAD_QUOTE = {
    "status": "ok",
    "best_bid": 0,
    "best_ask": 0,
    "bid_depth": 0,
    "ask_depth": 0,
}


@pytest.mark.parametrize(
    "quote, present, useful, expected",
    [
        (None, False, True, "data_gap"),  # no snapshot at all
        (_DEAD_QUOTE, True, True, "watch_only"),  # 저유동성
        (_OK_QUOTE, True, False, "watch_only"),  # screener stale
        (_OK_QUOTE, True, True, "buy_review"),  # actionable + useful
    ],
)
def test_classify_candidate_symbol(quote, present, useful, expected):
    assert (
        classify_candidate_symbol(
            quote, universe_useful=useful, quote_snapshot_present=present
        )
        == expected
    )


def test_classify_candidate_symbol_never_rejects():
    # Honest-verdict only: rejected / limit_wait are Hermes-only.
    for present in (True, False):
        for useful in (True, False):
            v = classify_candidate_symbol(
                _DEAD_QUOTE, universe_useful=useful, quote_snapshot_present=present
            )
            assert v in {"data_gap", "watch_only", "buy_review"}
