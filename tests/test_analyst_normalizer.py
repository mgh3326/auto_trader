"""Unit tests for analyst rating normalizer."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest

from app.services.analyst_normalizer import (
    build_consensus,
    is_strong_buy,
    normalize_rating_label,
    rating_to_bucket,
)

# ROB-486: 모든 build_consensus 테스트는 now 를 주입해 시한폭탄을 차단한다.
_NOW = datetime(2026, 6, 10, 12, 0, tzinfo=UTC)


def _days_ago(days: int) -> str:
    return (_NOW.date() - timedelta(days=days)).isoformat()


def _dated(op: dict[str, Any], days: int = 30) -> dict[str, Any]:
    return {**op, "date": _days_ago(days)}


class TestNormalizeRatingLabel:
    """Tests for normalize_rating_label function."""

    def test_korean_buy_ratings(self) -> None:
        assert normalize_rating_label("매수") == "Buy"
        assert normalize_rating_label("강력매수") == "Strong Buy"
        assert normalize_rating_label("강매") == "Strong Buy"
        assert normalize_rating_label("비중확대") == "Buy"

    def test_korean_sell_ratings(self) -> None:
        assert normalize_rating_label("매도") == "Sell"
        assert normalize_rating_label("비중축소") == "Sell"

    def test_korean_hold_ratings(self) -> None:
        assert normalize_rating_label("중립") == "Hold"
        assert normalize_rating_label("보유") == "Hold"
        assert normalize_rating_label("매수유지") == "Hold"
        assert normalize_rating_label("투자의견") == "Hold"

    def test_english_buy_ratings(self) -> None:
        assert normalize_rating_label("buy") == "Buy"
        assert normalize_rating_label("strong buy") == "Strong Buy"
        assert normalize_rating_label("trading buy") == "Buy"
        assert normalize_rating_label("overweight") == "Overweight"
        assert normalize_rating_label("outperform") == "Buy"

    def test_english_sell_ratings(self) -> None:
        assert normalize_rating_label("sell") == "Sell"
        assert normalize_rating_label("strong sell") == "Sell"
        assert normalize_rating_label("underweight") == "Underweight"
        assert normalize_rating_label("underperform") == "Sell"

    def test_english_hold_ratings(self) -> None:
        assert normalize_rating_label("hold") == "Hold"
        assert normalize_rating_label("neutral") == "Hold"
        assert normalize_rating_label("market perform") == "Hold"
        assert normalize_rating_label("equal weight") == "Hold"

    def test_case_insensitive(self) -> None:
        assert normalize_rating_label("BUY") == "Buy"
        assert normalize_rating_label("Strong Buy") == "Strong Buy"
        assert normalize_rating_label("HOLD") == "Hold"
        assert normalize_rating_label("SELL") == "Sell"

    def test_whitespace_handling(self) -> None:
        assert normalize_rating_label("  buy  ") == "Buy"
        assert normalize_rating_label("  Strong Buy  ") == "Strong Buy"

    def test_unknown_ratings(self) -> None:
        assert normalize_rating_label("unknown") == "Hold"
        assert normalize_rating_label("") == "Hold"
        assert normalize_rating_label(None) == "Hold"


class TestRatingToBucket:
    """Tests for rating_to_bucket function."""

    def test_strong_buy_bucket(self) -> None:
        assert rating_to_bucket("Strong Buy") == "buy"
        assert rating_to_bucket("strong buy") == "buy"
        assert rating_to_bucket("STRONG BUY") == "buy"

    def test_overweight_bucket(self) -> None:
        assert rating_to_bucket("Overweight") == "buy"
        assert rating_to_bucket("overweight") == "buy"
        assert rating_to_bucket("OVERWEIGHT") == "buy"

    def test_buy_bucket(self) -> None:
        assert rating_to_bucket("Buy") == "buy"
        assert rating_to_bucket("buy") == "buy"
        assert rating_to_bucket("BUY") == "buy"
        assert rating_to_bucket("outperform") == "buy"

    def test_sell_bucket(self) -> None:
        assert rating_to_bucket("Sell") == "sell"
        assert rating_to_bucket("sell") == "sell"
        assert rating_to_bucket("SELL") == "sell"
        assert rating_to_bucket("underperform") == "sell"

    def test_underweight_bucket(self) -> None:
        assert rating_to_bucket("Underweight") == "sell"
        assert rating_to_bucket("underweight") == "sell"
        assert rating_to_bucket("UNDERWEIGHT") == "sell"

    def test_hold_bucket(self) -> None:
        assert rating_to_bucket("Hold") == "hold"
        assert rating_to_bucket("hold") == "hold"
        assert rating_to_bucket("HOLD") == "hold"
        assert rating_to_bucket("neutral") == "hold"
        assert rating_to_bucket("Equal Weight") == "hold"

    def test_unknown_bucket_defaults_to_hold(self) -> None:
        assert rating_to_bucket("unknown") == "hold"
        assert rating_to_bucket("") == "hold"
        assert rating_to_bucket(None) == "hold"


class TestIsStrongBuy:
    """Tests for is_strong_buy function."""

    def test_strong_buy_true(self) -> None:
        assert is_strong_buy("Strong Buy") is True
        assert is_strong_buy("strong buy") is True
        assert is_strong_buy("STRONG BUY") is True

    def test_non_strong_buy_false(self) -> None:
        assert is_strong_buy("Buy") is False
        assert is_strong_buy("Hold") is False
        assert is_strong_buy("Sell") is False
        assert is_strong_buy("Overweight") is False

    def test_whitespace_handling(self) -> None:
        assert is_strong_buy("  Strong Buy  ") is True

    def test_empty_string(self) -> None:
        assert is_strong_buy("") is False
        assert is_strong_buy(None) is False


class TestBuildConsensus:
    """Tests for build_consensus (ROB-486: 모든 fixture 는 윈도우 내 date + now 주입)."""

    def test_buy_only_consensus(self) -> None:
        opinions = [
            _dated({"rating": "Buy", "target_price": 100}),
            _dated({"rating": "Strong Buy", "target_price": 110}),
            _dated({"rating": "Buy", "target_price": 95}),
        ]
        consensus = build_consensus(opinions, 90, now=_NOW)

        assert consensus["buy_count"] == 3
        assert consensus["hold_count"] == 0
        assert consensus["sell_count"] == 0
        assert consensus["strong_buy_count"] == 1
        assert consensus["total_count"] == 3
        assert consensus["avg_target_price"] == 101
        assert consensus["current_price"] == 90
        assert consensus["upside_pct"] == pytest.approx(12.22)
        assert consensus["rows_total"] == 3
        assert consensus["rows_used"] == 3

    def test_mixed_consensus(self) -> None:
        opinions = [
            _dated({"rating": "Buy", "target_price": 100}),
            _dated({"rating": "Hold", "target_price": 95}),
            _dated({"rating": "Sell", "target_price": 90}),
        ]
        consensus = build_consensus(opinions, 92, now=_NOW)

        assert consensus["buy_count"] == 1
        assert consensus["hold_count"] == 1
        assert consensus["sell_count"] == 1
        assert consensus["strong_buy_count"] == 0
        assert consensus["total_count"] == 3
        assert consensus["avg_target_price"] == 95
        assert consensus["median_target_price"] == 95
        assert consensus["min_target_price"] == 90
        assert consensus["max_target_price"] == 100

    def test_empty_opinions(self) -> None:
        consensus = build_consensus([], 100, now=_NOW)

        assert consensus["buy_count"] == 0
        assert consensus["hold_count"] == 0
        assert consensus["sell_count"] == 0
        assert consensus["strong_buy_count"] == 0
        assert consensus["total_count"] == 0
        assert consensus["avg_target_price"] is None
        assert consensus["median_target_price"] is None
        assert consensus["upside_pct"] is None

    def test_with_rating_bucket(self) -> None:
        opinions = [
            _dated({"rating_bucket": "buy", "target_price": 100}),
            _dated({"rating_bucket": "buy", "target_price": 110}),
            _dated({"rating_bucket": "hold", "target_price": None}),
        ]
        consensus = build_consensus(opinions, 95, now=_NOW)

        assert consensus["buy_count"] == 2
        assert consensus["hold_count"] == 1
        assert consensus["strong_buy_count"] == 0

    def test_target_price_statistics(self) -> None:
        opinions = [
            _dated({"rating": "Buy", "target_price": 90}),
            _dated({"rating": "Buy", "target_price": 100}),
            _dated({"rating": "Buy", "target_price": 110}),
            _dated({"rating": "Buy", "target_price": 120}),
        ]
        consensus = build_consensus(opinions, 95, now=_NOW)

        assert consensus["avg_target_price"] == 105
        assert consensus["median_target_price"] == 105
        assert consensus["min_target_price"] == 90
        assert consensus["max_target_price"] == 120

    def test_median_calculation_even(self) -> None:
        opinions = [
            _dated({"rating": "Buy", "target_price": 100}),
            _dated({"rating": "Buy", "target_price": 200}),
        ]
        consensus = build_consensus(opinions, 150, now=_NOW)

        assert consensus["median_target_price"] == 150

    def test_median_calculation_odd(self) -> None:
        opinions = [
            _dated({"rating": "Buy", "target_price": 100}),
            _dated({"rating": "Buy", "target_price": 200}),
            _dated({"rating": "Buy", "target_price": 300}),
        ]
        consensus = build_consensus(opinions, 150, now=_NOW)

        assert consensus["median_target_price"] == 200

    def test_upside_percentage_calculation(self) -> None:
        opinions = [_dated({"rating": "Buy", "target_price": 110})]
        consensus = build_consensus(opinions, 100, now=_NOW)

        assert consensus["upside_pct"] == pytest.approx(10.0)

    def test_upside_percentage_none_without_current_price(self) -> None:
        opinions = [_dated({"rating": "Buy", "target_price": 100})]
        consensus = build_consensus(opinions, None, now=_NOW)

        assert consensus["upside_pct"] is None

    def test_ignores_invalid_target_prices(self) -> None:
        opinions = [
            _dated({"rating": "Buy", "target_price": 100}),
            _dated({"rating": "Buy", "target_price": -50}),  # Invalid: negative
            _dated({"rating": "Buy", "target_price": None}),  # Invalid: None
            _dated({"rating": "Buy", "target_price": "abc"}),  # Invalid: string
        ]
        consensus = build_consensus(opinions, 90, now=_NOW)

        assert consensus["avg_target_price"] == 100
        assert consensus["min_target_price"] == 100
        assert consensus["max_target_price"] == 100

    def test_excludes_stale_dated_opinions_from_consensus(self) -> None:
        opinions = [
            {"rating": "Buy", "target_price": 900_000, "date": "2025-01-01"},
            {"rating": "Hold", "target_price": 100_000, "date": "2026-06-01"},
        ]

        consensus = build_consensus(opinions, 90_000, as_of="2026-06-10")

        assert consensus["buy_count"] == 0
        assert consensus["hold_count"] == 1
        assert consensus["total_count"] == 1
        assert consensus["raw_count"] == 2
        assert consensus["stale_opinion_count"] == 1
        assert consensus["avg_target_price"] == 100_000
        assert consensus["target_price_honest"] is False

    def test_masks_target_price_outliers_but_keeps_fresh_rating_counts(self) -> None:
        opinions = [
            {"rating": "Buy", "target_price": 500_000, "date": "2026-06-01"},
            {"rating": "Hold", "target_price": 110_000, "date": "2026-06-01"},
        ]

        consensus = build_consensus(opinions, 100_000, as_of="2026-06-10")

        assert consensus["buy_count"] == 1
        assert consensus["hold_count"] == 1
        assert consensus["total_count"] == 2
        assert consensus["avg_target_price"] == 110_000
        assert consensus["upside_pct"] == pytest.approx(10.0)
        assert consensus["target_price_count"] == 1
        assert consensus["target_price_outlier_count"] == 1
        assert consensus["target_price_honest"] is False

    def test_masks_downside_corporate_action_outlier(self) -> None:
        """ROB-486 repro 에스에이엠티: 현재가 15,380 vs 목표가 2,700 (-82.4%).

        abs-upside 가드는 하한 -100% 때문에 하방에서 절대 발화하지 못하므로
        별도 하방 임계(-75%)가 필요하다.
        """
        opinions = [
            {"rating": "Buy", "target_price": 2_700, "date": "2026-06-01"},
            {"rating": "Buy", "target_price": 17_000, "date": "2026-06-01"},
        ]

        consensus = build_consensus(opinions, 15_380, as_of="2026-06-10")

        assert consensus["buy_count"] == 2  # 등급 카운트는 보존
        assert consensus["target_price_count"] == 1
        assert consensus["target_price_outlier_count"] == 1
        assert consensus["avg_target_price"] == 17_000
        assert consensus["target_price_honest"] is False

    def test_keeps_genuine_bearish_target(self) -> None:
        """진성 약세 목표가(-30%대)는 하방 가드(-75%)에 걸리지 않아야 한다."""
        opinions = [{"rating": "Sell", "target_price": 70_000, "date": "2026-06-01"}]

        consensus = build_consensus(opinions, 100_000, as_of="2026-06-10")

        assert consensus["target_price_count"] == 1
        assert consensus["avg_target_price"] == 70_000
        assert consensus["target_price_honest"] is True

    def test_undated_opinion_is_kept_fail_open(self) -> None:
        """날짜 없는 의견은 recency 가드에서 제외하지 않는다(fail-open) —
        outlier 가드가 여전히 명목가 쓰레기를 방어한다."""
        opinions = [
            {"rating": "Buy", "target_price": 110_000},  # undated, sane
            {"rating": "Buy", "target_price": 2_000_000},  # undated, garbage
        ]

        consensus = build_consensus(opinions, 100_000, as_of="2026-06-10")

        assert consensus["total_count"] == 2
        assert consensus["stale_opinion_count"] == 0
        assert consensus["target_price_count"] == 1
        assert consensus["avg_target_price"] == 110_000

    def test_rating_label_bucket_fallback(self) -> None:
        """Test that rating_label is used when rating_bucket is not provided."""
        opinions = [
            _dated({"rating_label": "Strong Buy", "target_price": 100}),
            _dated({"rating_label": "Buy", "target_price": 95}),
        ]
        consensus = build_consensus(opinions, 90, now=_NOW)

        assert consensus["buy_count"] == 2
        assert consensus["strong_buy_count"] == 1

    def test_korean_ratings_in_consensus(self) -> None:
        opinions = [
            _dated({"rating": "매수", "target_price": 100}),
            _dated({"rating": "강력매수", "target_price": 110}),
            _dated({"rating": "중립", "target_price": 95}),
        ]
        consensus = build_consensus(opinions, 90, now=_NOW)

        assert consensus["buy_count"] == 2
        assert consensus["hold_count"] == 1
        assert consensus["strong_buy_count"] == 1


class TestBuildConsensusRecencyWindow:
    """ROB-486: 12개월 recency 윈도우 + fail-closed 메타데이터."""

    def test_stale_only_yields_null_targets_and_zero_counts(self) -> None:
        """031330 실측 모양 — 2015/2019 행만 존재 → 무필터 폴백 없이 전부 None/0."""
        opinions = [
            {"rating": "Hold", "target_price": None, "date": "2019-12-27"},
            {"rating": "Buy", "target_price": 2700, "date": "2015-08-24"},
        ]
        consensus = build_consensus(opinions, 15360, now=_NOW)

        assert consensus["avg_target_price"] is None
        assert consensus["median_target_price"] is None
        assert consensus["min_target_price"] is None
        assert consensus["max_target_price"] is None
        assert consensus["upside_pct"] is None
        assert consensus["buy_count"] == 0
        assert consensus["hold_count"] == 0
        assert consensus["sell_count"] == 0
        assert consensus["strong_buy_count"] == 0
        assert consensus["total_count"] == 0
        assert consensus["rows_total"] == 2
        assert consensus["rows_used"] == 0
        assert consensus["rows_excluded_stale"] == 2
        assert consensus["rows_excluded_undated"] == 0
        assert consensus["newest_opinion_date"] == "2019-12-27"
        assert consensus["window_months"] == 12

    def test_mixed_stale_and_recent_aggregates_recent_only(self) -> None:
        """005880 실측 모양 — 12개월 내 행(3,000 Buy + tp 없는 Hold)만 집계."""
        opinions = [
            {"rating": "Buy", "target_price": 3000, "date": "2026-05-18"},
            {"rating": "Hold", "target_price": None, "date": "2025-12-05"},
            {"rating": "Buy", "target_price": 3600, "date": "2023-08-11"},
            {"rating": "Buy", "target_price": 4000, "date": "2022-05-20"},
            {"rating": "Buy", "target_price": 23000, "date": "2020-06-12"},
            {"rating": "Buy", "target_price": 31000, "date": "2019-11-14"},
            {"rating": "Buy", "target_price": 31000, "date": "2019-08-14"},
            {"rating": "Buy", "target_price": 31000, "date": "2019-05-15"},
            {"rating": "Buy", "target_price": 35000, "date": "2018-10-29"},
        ]
        consensus = build_consensus(opinions, 1914, now=_NOW)

        # 무필터였다면 avg 20,200 / median 27,000 (버그 리포트 실측치)였을 입력.
        assert consensus["avg_target_price"] == 3000
        assert consensus["median_target_price"] == 3000
        assert consensus["upside_pct"] == pytest.approx(56.74, abs=0.01)
        assert consensus["buy_count"] == 1
        assert consensus["hold_count"] == 1
        assert consensus["total_count"] == 2
        assert consensus["rows_total"] == 9
        assert consensus["rows_used"] == 2
        assert consensus["rows_excluded_stale"] == 7
        assert consensus["rows_excluded_undated"] == 0
        assert consensus["newest_opinion_date"] == "2026-05-18"

    def test_undated_rows_excluded_and_counted(self) -> None:
        """ROB-486+ROB-488 통합: undated 행은 fail-open으로 유지(ROB-488)하되
        rows_excluded_undated 메타데이터로 카운트(ROB-486). 목표가 통계는 outlier
        가드(ROB-488 ±300%/-75%)가 쓰레기 undated 목표가를 차단한다."""
        opinions = [
            {"rating": "Buy", "target_price": 100, "date": _days_ago(10)},
            {"rating": "Buy", "target_price": 999},  # date 키 자체 없음 — outlier (+1010%)
            {"rating": "Buy", "target_price": 888, "date": None},  # outlier (+887%)
            {"rating": "Buy", "target_price": 777, "date": "not-a-date"},  # outlier (+763%)
        ]
        consensus = build_consensus(opinions, 90, now=_NOW)

        # fail-open: 4개 모두 fresh_opinions에 포함 → total_count=4, buy_count=4
        assert consensus["total_count"] == 4
        assert consensus["buy_count"] == 4
        assert consensus["rows_total"] == 4
        assert consensus["rows_used"] == 4
        # undated 3개는 rows_excluded_undated 메타데이터로 카운트
        assert consensus["rows_excluded_undated"] == 3
        assert consensus["rows_excluded_stale"] == 0
        # outlier 가드가 999/888/777 (모두 +700%+ > 300%)를 제거 → 100만 남음
        assert consensus["avg_target_price"] == 100
        assert consensus["target_price_outlier_count"] == 3
        assert consensus["target_price_count"] == 1

    def test_window_boundary_inclusive(self) -> None:
        """cutoff 당일(now 기준 정확히 window_months 개월 전)은 생존, 하루 전은 stale."""
        opinions = [
            {"rating": "Buy", "target_price": 100, "date": "2025-06-10"},
            {"rating": "Buy", "target_price": 200, "date": "2025-06-09"},
        ]
        consensus = build_consensus(opinions, 90, now=_NOW)

        assert consensus["rows_used"] == 1
        assert consensus["avg_target_price"] == 100
        assert consensus["rows_excluded_stale"] == 1

    def test_window_months_override(self) -> None:
        opinions = [
            {"rating": "Buy", "target_price": 100, "date": "2025-06-09"},
        ]
        wide = build_consensus(opinions, 90, window_months=24, now=_NOW)
        narrow = build_consensus(opinions, 90, window_months=12, now=_NOW)

        assert wide["rows_used"] == 1
        assert wide["avg_target_price"] == 100
        assert wide["window_months"] == 24
        assert narrow["rows_used"] == 0
        assert narrow["avg_target_price"] is None

    def test_now_injection_controls_anchor(self) -> None:
        """동일 입력이라도 now 가 미래면 stale 로 떨어진다 (시한폭탄 방지 근거)."""
        opinions = [{"rating": "Buy", "target_price": 100, "date": "2026-05-18"}]

        current = build_consensus(opinions, 90, now=_NOW)
        future = build_consensus(opinions, 90, now=datetime(2027, 7, 1, tzinfo=UTC))

        assert current["rows_used"] == 1
        assert future["rows_used"] == 0
        assert future["avg_target_price"] is None

    def test_empty_opinions_metadata(self) -> None:
        consensus = build_consensus([], 100, now=_NOW)

        assert consensus["rows_total"] == 0
        assert consensus["rows_used"] == 0
        assert consensus["rows_excluded_stale"] == 0
        assert consensus["rows_excluded_undated"] == 0
        assert consensus["newest_opinion_date"] is None
        assert consensus["window_months"] == 12
