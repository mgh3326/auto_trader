"""Unit tests for analyst rating normalizer."""

from __future__ import annotations

from app.services.analyst_normalizer import (
    build_consensus,
    is_strong_buy,
    normalize_rating_label,
    rating_to_bucket,
)


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
    """Tests for build_consensus function."""

    def test_buy_only_consensus(self) -> None:
        opinions = [
            {"rating": "Buy", "target_price": 100},
            {"rating": "Strong Buy", "target_price": 110},
            {"rating": "Buy", "target_price": 95},
        ]
        consensus = build_consensus(opinions, 90)

        assert consensus["buy_count"] == 3
        assert consensus["hold_count"] == 0
        assert consensus["sell_count"] == 0
        assert consensus["strong_buy_count"] == 1
        assert consensus["total_count"] == 3
        assert consensus["count"] == 3
        assert consensus["avg_target_price"] == 101
        assert consensus["current_price"] == 90
        assert consensus["upside_pct"] == 12.22

    def test_mixed_consensus(self) -> None:
        opinions = [
            {"rating": "Buy", "target_price": 100},
            {"rating": "Hold", "target_price": 95},
            {"rating": "Sell", "target_price": 90},
        ]
        consensus = build_consensus(opinions, 92)

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
        consensus = build_consensus([], 100)

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
            {"rating_bucket": "buy", "target_price": 100},
            {"rating_bucket": "buy", "target_price": 110},
            {"rating_bucket": "hold", "target_price": None},
        ]
        consensus = build_consensus(opinions, 95)

        assert consensus["buy_count"] == 2
        assert consensus["hold_count"] == 1
        assert consensus["strong_buy_count"] == 0

    def test_target_price_statistics(self) -> None:
        opinions = [
            {"rating": "Buy", "target_price": 90},
            {"rating": "Buy", "target_price": 100},
            {"rating": "Buy", "target_price": 110},
            {"rating": "Buy", "target_price": 120},
        ]
        consensus = build_consensus(opinions, 95)

        assert consensus["avg_target_price"] == 105
        assert consensus["median_target_price"] == 105
        assert consensus["min_target_price"] == 90
        assert consensus["max_target_price"] == 120

    def test_median_calculation_even(self) -> None:
        opinions = [
            {"rating": "Buy", "target_price": 100},
            {"rating": "Buy", "target_price": 200},
        ]
        consensus = build_consensus(opinions, 150)

        assert consensus["median_target_price"] == 150

    def test_median_calculation_odd(self) -> None:
        opinions = [
            {"rating": "Buy", "target_price": 100},
            {"rating": "Buy", "target_price": 200},
            {"rating": "Buy", "target_price": 300},
        ]
        consensus = build_consensus(opinions, 150)

        assert consensus["median_target_price"] == 200

    def test_upside_percentage_calculation(self) -> None:
        opinions = [{"rating": "Buy", "target_price": 110}]
        consensus = build_consensus(opinions, 100)

        assert consensus["upside_pct"] == 10.0
        assert consensus["upside_potential"] == 10.0

    def test_upside_percentage_none_without_current_price(self) -> None:
        opinions = [{"rating": "Buy", "target_price": 100}]
        consensus = build_consensus(opinions, None)

        assert consensus["upside_pct"] is None
        assert consensus["upside_potential"] is None

    def test_ignores_invalid_target_prices(self) -> None:
        opinions = [
            {"rating": "Buy", "target_price": 100},
            {"rating": "Buy", "target_price": -50},  # Invalid: negative
            {"rating": "Buy", "target_price": None},  # Invalid: None
            {"rating": "Buy", "target_price": "abc"},  # Invalid: string
        ]
        consensus = build_consensus(opinions, 90)

        assert consensus["avg_target_price"] == 100
        assert consensus["min_target_price"] == 100
        assert consensus["max_target_price"] == 100

    def test_rating_label_bucket_fallback(self) -> None:
        """Test that rating_label is used when rating_bucket is not provided."""
        opinions = [
            {"rating_label": "Strong Buy", "target_price": 100},
            {"rating_label": "Buy", "target_price": 95},
        ]
        consensus = build_consensus(opinions, 90)

        assert consensus["buy_count"] == 2
        assert consensus["strong_buy_count"] == 1

    def test_korean_ratings_in_consensus(self) -> None:
        opinions = [
            {"rating": "매수", "target_price": 100},
            {"rating": "강력매수", "target_price": 110},
            {"rating": "중립", "target_price": 95},
        ]
        consensus = build_consensus(opinions, 90)

        assert consensus["buy_count"] == 2
        assert consensus["hold_count"] == 1
        assert consensus["strong_buy_count"] == 1
