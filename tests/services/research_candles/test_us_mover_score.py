"""hk 6554: the pure US mover score (order buckets, asset exclusion, null-safe inputs)."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.services.research_candles.us_mover_score import (
    REASON_NOT_COMMON_OR_ETF,
    UsMoverRow,
    plain_decimal,
    score_us_movers,
)

pytestmark = pytest.mark.unit

T = datetime(2026, 11, 25, 12, 20, tzinfo=UTC)
FLOOR = Decimal("100000")


def _row(symbol, rank, trade_value, asset_type="common_stock", at=T):
    return UsMoverRow(
        symbol=symbol,
        snapshot_at=at,
        rank=rank,
        trade_value=None if trade_value is None else Decimal(trade_value),
        asset_type=asset_type,
    )


def _order(rows):
    ordered, _ = score_us_movers(rows, dollar_volume_floor=FLOOR)
    return [s.symbol for s in ordered]


def test_liquid_name_precedes_a_better_ranked_thin_name() -> None:
    # URBN (rank 14, liquid) must come before a thin name at rank 1: the floor is
    # a bucket that ranks first, so the thin name is not a reason to keep URBN out.
    rows = [_row("THIN", 1, "20000"), _row("URBN", 14, "2000000")]
    assert _order(rows) == ["URBN", "THIN"]


def test_within_the_same_liquidity_bucket_the_better_rank_comes_first() -> None:
    rows = [_row("AAA", 9, "300000"), _row("BBB", 3, "400000")]
    assert _order(rows) == ["BBB", "AAA"]


def test_best_rank_across_runs_is_used_not_the_first_capture_rank() -> None:
    rows = [
        _row("URBN", 14, "2000000", at=T),
        _row("URBN", 9, "2100000", at=T.replace(minute=35)),
    ]
    (urbn,), _ = score_us_movers(rows, dollar_volume_floor=FLOOR)
    assert urbn.best_rank == 9
    assert urbn.first_captured_at == T  # the earliest capture is still recorded
    assert plain_decimal(urbn.dollar_volume) == "2100000"  # the largest dollar volume


def test_a_null_dollar_volume_is_below_the_floor_and_reported_null() -> None:
    (score,), _ = score_us_movers([_row("NUL", 1, None)], dollar_volume_floor=FLOOR)
    assert score.liquidity == "below_floor"
    assert score.dollar_volume is None
    assert score.as_report(protected=False, position=None)["dollar_volume"] is None


def test_at_equal_rank_a_null_dollar_volume_sorts_after_a_known_one() -> None:
    # rank is compared before dollar volume inside a bucket; NULL only loses ties
    rows = [_row("NUL", 5, None), _row("KNOWN", 5, "20000")]
    assert _order(rows) == ["KNOWN", "NUL"]


def test_a_warrant_is_excluded_with_its_reason_and_never_a_candidate() -> None:
    rows = [
        _row("WRNT", 1, "5000000", asset_type="right_or_warrant"),
        _row("ABC", 9, "1000"),
    ]
    candidates, excluded = score_us_movers(rows, dollar_volume_floor=FLOOR)
    assert [s.symbol for s in candidates] == ["ABC"]
    assert [s.symbol for s in excluded] == ["WRNT"]
    assert excluded[0].asset_type == "right_or_warrant"
    assert REASON_NOT_COMMON_OR_ETF == "not_common_or_etf"


def test_unknown_asset_type_is_never_excluded_but_goes_after_known_classes() -> None:
    rows = [
        _row("UNK", 1, "9000000", asset_type=None),
        _row("KNW", 9, "1000", asset_type="common_stock"),
    ]
    candidates, excluded = score_us_movers(rows, dollar_volume_floor=FLOOR)
    assert excluded == []
    assert [s.symbol for s in candidates] == ["KNW", "UNK"]
    assert candidates[1].asset_type == "unknown"


def test_a_warrant_row_in_any_run_excludes_the_symbol() -> None:
    rows = [
        _row("MIX", 3, "500000", asset_type="common_stock"),
        _row("MIX", 4, "500000", asset_type="right_or_warrant"),
    ]
    candidates, excluded = score_us_movers(rows, dollar_volume_floor=FLOOR)
    assert candidates == []
    assert [s.symbol for s in excluded] == ["MIX"]


def test_non_finite_dollar_volume_is_ignored_not_ranked() -> None:
    rows = [_row("NAN", 1, "NaN"), _row("NAN", 2, "150000")]
    (score,), _ = score_us_movers(rows, dollar_volume_floor=FLOOR)
    assert plain_decimal(score.dollar_volume) == "150000"
    assert score.liquidity == "above_floor"


def test_plain_decimal_is_exact_text_without_an_exponent() -> None:
    assert plain_decimal(Decimal("20000.00")) == "20000"
    assert plain_decimal(Decimal("2E+6")) == "2000000"
    assert plain_decimal(Decimal("0.5")) == "0.5"
    assert plain_decimal(None) is None
