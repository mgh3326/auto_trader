from __future__ import annotations

from app.services.portfolio_indicator_cards import (
    build_bollinger_card,
    build_ema_card,
    build_macd_card,
    build_rsi_card,
    build_sma_card,
    build_stoch_rsi_card,
)


class TestBuildRsiCard:
    def test_none_returns_none(self) -> None:
        assert build_rsi_card(None) is None

    def test_string_returns_none(self) -> None:
        assert build_rsi_card("x") is None

    def test_oversold(self) -> None:
        card = build_rsi_card(25.0)
        assert card == {
            "label": "RSI(14)",
            "value": "25.0",
            "tone": "oversold",
            "description": "과매도",
        }

    def test_overbought(self) -> None:
        card = build_rsi_card(75.0)
        assert card is not None
        assert card["tone"] == "overbought"
        assert card["description"] == "과매수"

    def test_neutral(self) -> None:
        card = build_rsi_card(50.0)
        assert card is not None
        assert card["tone"] == "neutral"
        assert card["description"] == "중립"


class TestBuildStochRsiCard:
    def test_k_none_returns_none(self) -> None:
        assert build_stoch_rsi_card(None, 50.0) is None

    def test_d_none_returns_none(self) -> None:
        assert build_stoch_rsi_card(50.0, None) is None

    def test_oversold(self) -> None:
        card = build_stoch_rsi_card(10.0, 15.0)
        assert card is not None
        assert card["tone"] == "oversold"

    def test_overbought(self) -> None:
        card = build_stoch_rsi_card(85.0, 90.0)
        assert card is not None
        assert card["tone"] == "overbought"

    def test_neutral(self) -> None:
        card = build_stoch_rsi_card(50.0, 50.0)
        assert card is not None
        assert card["tone"] == "neutral"

    def test_value_format(self) -> None:
        card = build_stoch_rsi_card(10.0, 15.0)
        assert card is not None
        assert card["value"] == "K 10.0 / D 15.0"


class TestBuildMacdCard:
    def test_macd_none_returns_none(self) -> None:
        assert build_macd_card(None, 1.0, 0.5) is None

    def test_signal_none_returns_none(self) -> None:
        assert build_macd_card(1.0, None, 0.5) is None

    def test_bullish(self) -> None:
        card = build_macd_card(1.0, 0.5, 0.5)
        assert card is not None
        assert card["tone"] == "bullish"
        assert card["value"] == "Bullish"

    def test_bearish(self) -> None:
        card = build_macd_card(0.5, 1.0, None)
        assert card is not None
        assert card["tone"] == "bearish"
        assert "Hist" not in card["description"]

    def test_histogram_included_when_valid(self) -> None:
        card = build_macd_card(1.0, 0.5, 0.3)
        assert card is not None
        assert "Hist" in card["description"]


class TestBuildBollingerCard:
    def test_price_none_returns_none(self) -> None:
        assert build_bollinger_card(None, 110.0, 100.0, 90.0) is None

    def test_upper_none_returns_none(self) -> None:
        assert build_bollinger_card(100.0, None, 100.0, 90.0) is None

    def test_oversold_near_lower(self) -> None:
        card = build_bollinger_card(91.0, 110.0, 100.0, 90.0)
        assert card is not None
        assert card["tone"] == "oversold"

    def test_overbought_near_upper(self) -> None:
        card = build_bollinger_card(109.0, 110.0, 100.0, 90.0)
        assert card is not None
        assert card["tone"] == "overbought"

    def test_neutral_near_middle(self) -> None:
        card = build_bollinger_card(100.0, 110.0, 100.0, 90.0)
        assert card is not None
        assert card["tone"] == "neutral"

    def test_description_format(self) -> None:
        card = build_bollinger_card(91.0, 110.0, 100.0, 90.0)
        assert card is not None
        assert "110.00" in card["description"]
        assert "100.00" in card["description"]
        assert "90.00" in card["description"]


class TestBuildEmaCard:
    def test_price_none_returns_none(self) -> None:
        assert build_ema_card(None, 100.0, 90.0, 80.0) is None

    def test_ema20_none_returns_none(self) -> None:
        assert build_ema_card(120.0, None, 90.0, 80.0) is None

    def test_bullish_full_alignment(self) -> None:
        card = build_ema_card(120.0, 110.0, 100.0, 90.0)
        assert card is not None
        assert card["tone"] == "bullish"
        assert card["label"] == "EMA"

    def test_bearish_full_alignment(self) -> None:
        card = build_ema_card(80.0, 90.0, 100.0, 110.0)
        assert card is not None
        assert card["tone"] == "bearish"

    def test_neutral_mixed(self) -> None:
        card = build_ema_card(100.0, 110.0, 90.0, 80.0)
        assert card is not None
        assert card["tone"] == "neutral"


class TestBuildSmaCard:
    def test_price_none_returns_none(self) -> None:
        assert build_sma_card(None, 100.0, 90.0, 80.0) is None

    def test_sma20_none_returns_none(self) -> None:
        assert build_sma_card(120.0, None, 90.0, 80.0) is None

    def test_bullish_full_alignment(self) -> None:
        card = build_sma_card(120.0, 110.0, 100.0, 90.0)
        assert card is not None
        assert card["tone"] == "bullish"
        assert card["label"] == "SMA"

    def test_bearish_full_alignment(self) -> None:
        card = build_sma_card(80.0, 90.0, 100.0, 110.0)
        assert card is not None
        assert card["tone"] == "bearish"

    def test_neutral_mixed(self) -> None:
        card = build_sma_card(100.0, 110.0, 90.0, 80.0)
        assert card is not None
        assert card["tone"] == "neutral"

    def test_label_is_sma_not_ema(self) -> None:
        card = build_sma_card(120.0, 110.0, 100.0, 90.0)
        assert card is not None
        assert card["label"] == "SMA"
