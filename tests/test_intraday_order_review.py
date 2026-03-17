"""Tests for intraday order review classification logic."""

from app.services.intraday_order_review import (
    check_needs_attention,
    classify_fill_proximity,
    format_fill_proximity,
)


class TestClassifyFillProximity:
    def test_near_for_small_gap(self):
        assert classify_fill_proximity(1.5) == "near"
        assert classify_fill_proximity(-1.5) == "near"

    def test_moderate_for_medium_gap(self):
        assert classify_fill_proximity(3.0) == "moderate"
        assert classify_fill_proximity(-4.5) == "moderate"

    def test_far_for_large_gap(self):
        assert classify_fill_proximity(7.0) == "far"
        assert classify_fill_proximity(-8.5) == "far"

    def test_very_far_for_extreme_gap(self):
        assert classify_fill_proximity(15.0) == "very_far"
        assert classify_fill_proximity(-20.0) == "very_far"

    def test_unknown_for_none(self):
        assert classify_fill_proximity(None) == "unknown"

    def test_custom_thresholds(self):
        thresholds = {"near": 3.0, "moderate": 6.0, "far": 12.0}
        assert classify_fill_proximity(2.5, thresholds) == "near"
        assert classify_fill_proximity(4.0, thresholds) == "moderate"


class TestFormatFillProximity:
    def test_format_near(self):
        assert "체결 임박" in format_fill_proximity("near")

    def test_format_unknown(self):
        assert format_fill_proximity("unknown") == "알 수 없음"


class TestCheckNeedsAttention:
    def test_near_fill_triggers_attention(self):
        order = {"gap_pct": 1.5, "side": "buy"}
        needs_attention, reason = check_needs_attention(order, {})
        assert needs_attention is True
        assert reason is not None and "체결 임박" in reason

    def test_market_volatility_triggers_attention(self):
        order = {"gap_pct": 10.0, "side": "buy"}
        indicators = {"change_24h_pct": 6.0}
        needs_attention, reason = check_needs_attention(order, indicators)
        assert needs_attention is True
        assert reason is not None and "급변" in reason

    def test_rsi_overbought_buy_order(self):
        order = {"gap_pct": 5.0, "side": "buy"}
        indicators = {"rsi_14": 75}
        needs_attention, reason = check_needs_attention(order, indicators)
        assert needs_attention is True
        assert reason is not None and "과매수" in reason

    def test_rsi_oversold_sell_order(self):
        order = {"gap_pct": 5.0, "side": "sell"}
        indicators = {"rsi_14": 25}
        needs_attention, reason = check_needs_attention(order, indicators)
        assert needs_attention is True
        assert reason is not None and "과매도" in reason

    def test_far_order_triggers_attention(self):
        order = {"gap_pct": -20.0, "side": "buy"}
        needs_attention, reason = check_needs_attention(order, {})
        assert needs_attention is True
        assert reason is not None and "자금 묶임" in reason

    def test_no_attention_needed(self):
        order = {"gap_pct": 8.0, "side": "buy"}
        indicators = {"change_24h_pct": 2.0, "rsi_14": 55}
        needs_attention, reason = check_needs_attention(order, indicators)
        assert needs_attention is False
        assert reason is None
