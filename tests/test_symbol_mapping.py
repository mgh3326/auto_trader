"""
Tests for symbol mapping utilities.
"""

import pytest

from app.utils.symbol_mapping import (
    InvalidTradingViewSymbolError,
    InvalidUpbitSymbolError,
    SymbolMappingError,
    UnsupportedQuoteCurrencyError,
    is_valid_tradingview_symbol,
    is_valid_upbit_symbol,
    tradingview_to_upbit,
    upbit_to_tradingview,
)


class TestUpbitToTradingView:
    """Test Upbit to TradingView symbol conversion."""

    def test_basic_krw_conversion(self):
        """Test basic KRW market conversion."""
        result = upbit_to_tradingview("KRW-BTC")
        assert result == "UPBIT:BTCKRW"

    def test_multiple_krw_symbols(self):
        """Test multiple KRW market conversions."""
        test_cases = [
            ("KRW-BTC", "UPBIT:BTCKRW"),
            ("KRW-ETH", "UPBIT:ETHKRW"),
            ("KRW-XRP", "UPBIT:XRPKRW"),
            ("KRW-ADA", "UPBIT:ADAKRW"),
            ("KRW-DOGE", "UPBIT:DOGEKRW"),
        ]
        for upbit_symbol, expected_tv_symbol in test_cases:
            result = upbit_to_tradingview(upbit_symbol)
            assert result == expected_tv_symbol

    def test_btc_quoted_symbol(self):
        """Test BTC-quoted market conversion."""
        result = upbit_to_tradingview("BTC-ETH")
        assert result == "UPBIT:ETHBTC"

    def test_usdt_quoted_symbol(self):
        """Test USDT-quoted market conversion."""
        result = upbit_to_tradingview("USDT-BTC")
        assert result == "UPBIT:BTCUSDT"

    def test_case_normalization(self):
        """Test that lowercase symbols are normalized to uppercase."""
        result = upbit_to_tradingview("krw-btc")
        assert result == "UPBIT:BTCKRW"

    def test_with_target_quote(self):
        """Test conversion with target quote currency override."""
        result = upbit_to_tradingview("KRW-BTC", target_quote="USDT")
        assert result == "UPBIT:BTCUSDT"

    def test_target_quote_overrides_original(self):
        """Test that target_quote overrides the original quote currency."""
        result = upbit_to_tradingview("BTC-ETH", target_quote="KRW")
        assert result == "UPBIT:ETHKRW"

    def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises InvalidUpbitSymbolError."""
        with pytest.raises(InvalidUpbitSymbolError, match="cannot be empty"):
            upbit_to_tradingview("")

    def test_none_symbol_raises_error(self):
        """Test that None symbol raises InvalidUpbitSymbolError."""
        with pytest.raises(InvalidUpbitSymbolError, match="cannot be empty"):
            upbit_to_tradingview(None)

    def test_missing_separator_raises_error(self):
        """Test that symbol without hyphen raises InvalidUpbitSymbolError."""
        with pytest.raises(InvalidUpbitSymbolError, match="Invalid Upbit symbol format"):
            upbit_to_tradingview("KRWBTC")

    def test_empty_quote_currency_raises_error(self):
        """Test that empty quote currency raises InvalidUpbitSymbolError."""
        with pytest.raises(InvalidUpbitSymbolError, match="must be non-empty"):
            upbit_to_tradingview("-BTC")

    def test_empty_base_currency_raises_error(self):
        """Test that empty base currency raises InvalidUpbitSymbolError."""
        with pytest.raises(InvalidUpbitSymbolError, match="must be non-empty"):
            upbit_to_tradingview("KRW-")

    def test_whitespace_handling(self):
        """Test that whitespace is properly handled."""
        result = upbit_to_tradingview("  KRW-BTC  ")
        assert result == "UPBIT:BTCKRW"

    def test_empty_target_quote_raises_error(self):
        """Test that empty target_quote raises UnsupportedQuoteCurrencyError."""
        with pytest.raises(UnsupportedQuoteCurrencyError, match="cannot be empty"):
            upbit_to_tradingview("KRW-BTC", target_quote="")


class TestTradingViewToUpbit:
    """Test TradingView to Upbit symbol conversion."""

    def test_basic_krw_conversion(self):
        """Test basic KRW pair conversion."""
        result = tradingview_to_upbit("UPBIT:BTCKRW")
        assert result == "KRW-BTC"

    def test_multiple_krw_symbols(self):
        """Test multiple KRW pair conversions."""
        test_cases = [
            ("UPBIT:BTCKRW", "KRW-BTC"),
            ("UPBIT:ETHKRW", "KRW-ETH"),
            ("UPBIT:XRPKRW", "KRW-XRP"),
            ("UPBIT:ADAKRW", "KRW-ADA"),
            ("UPBIT:DOGEKRW", "KRW-DOGE"),
        ]
        for tv_symbol, expected_upbit_symbol in test_cases:
            result = tradingview_to_upbit(tv_symbol)
            assert result == expected_upbit_symbol

    def test_usdt_quoted_pair(self):
        """Test USDT-quoted pair conversion."""
        result = tradingview_to_upbit("UPBIT:BTCUSDT")
        assert result == "USDT-BTC"

    def test_btc_quoted_pair(self):
        """Test BTC-quoted pair conversion."""
        result = tradingview_to_upbit("UPBIT:ETHBTC")
        assert result == "BTC-ETH"

    def test_usdc_quoted_pair(self):
        """Test USDC-quoted pair conversion."""
        result = tradingview_to_upbit("UPBIT:BTCUSDC")
        assert result == "USDC-BTC"

    def test_case_normalization(self):
        """Test that lowercase symbols are normalized to uppercase."""
        result = tradingview_to_upbit("upbit:btckrw")
        assert result == "KRW-BTC"

    def test_custom_default_quote(self):
        """Test conversion with custom default quote for unknown pairs."""
        # UPBIT:BTCXYZ doesn't match known quotes, should use default
        result = tradingview_to_upbit("UPBIT:BTCXYZ", default_quote="USD")
        assert result == "USD-BTCXYZ"

    def test_empty_symbol_raises_error(self):
        """Test that empty symbol raises InvalidTradingViewSymbolError."""
        with pytest.raises(InvalidTradingViewSymbolError, match="cannot be empty"):
            tradingview_to_upbit("")

    def test_none_symbol_raises_error(self):
        """Test that None symbol raises InvalidTradingViewSymbolError."""
        with pytest.raises(InvalidTradingViewSymbolError, match="cannot be empty"):
            tradingview_to_upbit(None)

    def test_wrong_exchange_raises_error(self):
        """Test that non-Upbit exchange raises InvalidTradingViewSymbolError."""
        with pytest.raises(InvalidTradingViewSymbolError, match="Invalid TradingView symbol"):
            tradingview_to_upbit("BINANCE:BTCUSDT")

    def test_missing_prefix_raises_error(self):
        """Test that missing UPBIT: prefix raises InvalidTradingViewSymbolError."""
        with pytest.raises(InvalidTradingViewSymbolError, match="Expected format"):
            tradingview_to_upbit("BTCKRW")

    def test_empty_pair_raises_error(self):
        """Test that empty pair after prefix raises InvalidTradingViewSymbolError."""
        with pytest.raises(InvalidTradingViewSymbolError, match="Pair component is empty"):
            tradingview_to_upbit("UPBIT:")

    def test_whitespace_handling(self):
        """Test that whitespace is properly handled."""
        result = tradingview_to_upbit("  UPBIT:BTCKRW  ")
        assert result == "KRW-BTC"

    def test_quote_precedence(self):
        """Test that longer quote currencies are matched first."""
        # USDT should be matched before USD
        result = tradingview_to_upbit("UPBIT:BTCUSDT")
        assert result == "USDT-BTC"


class TestValidationFunctions:
    """Test symbol validation utility functions."""

    def test_valid_upbit_symbols(self):
        """Test validation of valid Upbit symbols."""
        valid_symbols = [
            "KRW-BTC",
            "KRW-ETH",
            "BTC-ETH",
            "USDT-BTC",
        ]
        for symbol in valid_symbols:
            assert is_valid_upbit_symbol(symbol) is True

    def test_invalid_upbit_symbols(self):
        """Test validation of invalid Upbit symbols."""
        invalid_symbols = [
            "UPBIT:BTCKRW",  # TradingView format
            "BTCKRW",  # No separator
            "KRW-",  # Missing base
            "-BTC",  # Missing quote
            "",  # Empty
            "INVALID",  # Invalid format
        ]
        for symbol in invalid_symbols:
            assert is_valid_upbit_symbol(symbol) is False

    def test_valid_tradingview_symbols(self):
        """Test validation of valid TradingView symbols."""
        valid_symbols = [
            "UPBIT:BTCKRW",
            "UPBIT:ETHKRW",
            "UPBIT:BTCUSDT",
            "UPBIT:ETHBTC",
        ]
        for symbol in valid_symbols:
            assert is_valid_tradingview_symbol(symbol) is True

    def test_invalid_tradingview_symbols(self):
        """Test validation of invalid TradingView symbols."""
        invalid_symbols = [
            "KRW-BTC",  # Upbit format
            "BINANCE:BTCUSDT",  # Wrong exchange
            "BTCKRW",  # Missing prefix
            "UPBIT:",  # Empty pair
            "",  # Empty
            "INVALID",  # Invalid format
        ]
        for symbol in invalid_symbols:
            assert is_valid_tradingview_symbol(symbol) is False


class TestRoundTripConversion:
    """Test bidirectional symbol conversion."""

    def test_upbit_to_tv_to_upbit(self):
        """Test converting Upbit → TradingView → Upbit preserves symbol."""
        original_symbols = [
            "KRW-BTC",
            "KRW-ETH",
            "USDT-BTC",
            "BTC-ETH",
        ]
        for original in original_symbols:
            tv_symbol = upbit_to_tradingview(original)
            result = tradingview_to_upbit(tv_symbol)
            assert result == original.upper()

    def test_tv_to_upbit_to_tv(self):
        """Test converting TradingView → Upbit → TradingView preserves symbol."""
        original_symbols = [
            "UPBIT:BTCKRW",
            "UPBIT:ETHKRW",
            "UPBIT:BTCUSDT",
            "UPBIT:ETHBTC",
        ]
        for original in original_symbols:
            upbit_symbol = tradingview_to_upbit(original)
            result = upbit_to_tradingview(upbit_symbol)
            assert result == original.upper()


class TestExceptionHierarchy:
    """Test exception hierarchy and relationships."""

    def test_exception_inheritance(self):
        """Test that custom exceptions inherit from base exception."""
        assert issubclass(InvalidUpbitSymbolError, SymbolMappingError)
        assert issubclass(InvalidTradingViewSymbolError, SymbolMappingError)
        assert issubclass(UnsupportedQuoteCurrencyError, SymbolMappingError)
        assert issubclass(SymbolMappingError, ValueError)

    def test_catching_base_exception(self):
        """Test that base exception catches all derived exceptions."""
        with pytest.raises(SymbolMappingError):
            upbit_to_tradingview("")

        with pytest.raises(SymbolMappingError):
            tradingview_to_upbit("")

        with pytest.raises(SymbolMappingError):
            upbit_to_tradingview("KRW-BTC", target_quote="")


class TestEdgeCases:
    """Test edge cases and boundary conditions."""

    def test_very_long_symbol_names(self):
        """Test conversion with very long cryptocurrency names."""
        result = upbit_to_tradingview("KRW-VERYLONGCRYPTOCURRENCYNAME")
        assert result == "UPBIT:VERYLONGCRYPTOCURRENCYNAMEKRW"

    def test_single_character_base(self):
        """Test conversion with single-character base currency."""
        result = upbit_to_tradingview("KRW-X")
        assert result == "UPBIT:XKRW"

    def test_numeric_in_symbol(self):
        """Test conversion with numeric characters in symbol."""
        result = upbit_to_tradingview("KRW-BTC2")
        assert result == "UPBIT:BTC2KRW"

    def test_special_characters_preserved(self):
        """Test that valid special characters are preserved."""
        # Note: Real symbols typically don't have special chars, but test normalization
        result = upbit_to_tradingview("KRW-BTC")
        assert ":" in result  # Colon is part of TradingView format
        assert "-" not in result  # Hyphen should not be in TradingView format

    def test_multiple_hyphens_in_upbit_symbol(self):
        """Test that only first hyphen is used as separator."""
        # This tests the split behavior with maxsplit=1
        result = upbit_to_tradingview("KRW-BTC-ETH")
        assert result == "UPBIT:BTC-ETHKRW"

    def test_ambiguous_quote_detection(self):
        """Test handling of ambiguous quote currency detection."""
        # BTCETH could be BTC-ETH or could be something else
        # The function should detect ETH as the quote
        result = tradingview_to_upbit("UPBIT:BTCETH")
        assert result == "ETH-BTC"

    def test_unknown_quote_with_default(self):
        """Test that unknown quote currency uses default."""
        result = tradingview_to_upbit("UPBIT:ABCXYZ", default_quote="KRW")
        assert result == "KRW-ABCXYZ"
