"""Unit tests for app/mcp_server/tooling/market_normalization.py"""

from __future__ import annotations

import pytest


class TestIsKoreanEquityCode:
    def test_standard_6char_code(self):
        from app.mcp_server.tooling.market_normalization import is_korean_equity_code

        assert is_korean_equity_code("005930") is True
        assert is_korean_equity_code("035420") is True

    def test_a_prefixed_7char_code(self):
        from app.mcp_server.tooling.market_normalization import is_korean_equity_code

        assert is_korean_equity_code("A196170") is True

    def test_us_ticker_rejected(self):
        from app.mcp_server.tooling.market_normalization import is_korean_equity_code

        assert is_korean_equity_code("AAPL") is False
        assert is_korean_equity_code("TSLA") is False

    def test_crypto_symbol_rejected(self):
        from app.mcp_server.tooling.market_normalization import is_korean_equity_code

        assert is_korean_equity_code("KRW-BTC") is False

    def test_empty_string_rejected(self):
        from app.mcp_server.tooling.market_normalization import is_korean_equity_code

        assert is_korean_equity_code("") is False

    def test_5char_code_rejected(self):
        from app.mcp_server.tooling.market_normalization import is_korean_equity_code

        assert is_korean_equity_code("05930") is False


class TestIsCryptoMarket:
    def test_krw_prefix(self):
        from app.mcp_server.tooling.market_normalization import is_crypto_market

        assert is_crypto_market("KRW-BTC") is True
        assert is_crypto_market("KRW-ETH") is True

    def test_usdt_prefix(self):
        from app.mcp_server.tooling.market_normalization import is_crypto_market

        assert is_crypto_market("USDT-BTC") is True

    def test_lowercase_recognized(self):
        from app.mcp_server.tooling.market_normalization import is_crypto_market

        assert is_crypto_market("krw-btc") is True

    def test_kr_equity_rejected(self):
        from app.mcp_server.tooling.market_normalization import is_crypto_market

        assert is_crypto_market("005930") is False

    def test_us_equity_rejected(self):
        from app.mcp_server.tooling.market_normalization import is_crypto_market

        assert is_crypto_market("AAPL") is False


class TestIsUsEquitySymbol:
    def test_standard_ticker(self):
        from app.mcp_server.tooling.market_normalization import is_us_equity_symbol

        assert is_us_equity_symbol("AAPL") is True
        assert is_us_equity_symbol("TSLA") is True

    def test_dot_share_class(self):
        from app.mcp_server.tooling.market_normalization import is_us_equity_symbol

        assert is_us_equity_symbol("BRK.B") is True

    def test_crypto_rejected(self):
        from app.mcp_server.tooling.market_normalization import is_us_equity_symbol

        assert is_us_equity_symbol("KRW-BTC") is False

    def test_empty_string_rejected(self):
        from app.mcp_server.tooling.market_normalization import is_us_equity_symbol

        assert is_us_equity_symbol("") is False


class TestNormalizeMarket:
    @pytest.mark.parametrize(
        "alias,expected",
        [
            ("kr", "equity_kr"),
            ("krx", "equity_kr"),
            ("kospi", "equity_kr"),
            ("kosdaq", "equity_kr"),
            ("kis", "equity_kr"),
            ("equity_kr", "equity_kr"),
            ("us", "equity_us"),
            ("usa", "equity_us"),
            ("nasdaq", "equity_us"),
            ("equity_us", "equity_us"),
            ("crypto", "crypto"),
            ("upbit", "crypto"),
            ("krw", "crypto"),
        ],
    )
    def test_known_aliases(self, alias, expected):
        from app.mcp_server.tooling.market_normalization import normalize_market

        assert normalize_market(alias) == expected

    def test_none_returns_none(self):
        from app.mcp_server.tooling.market_normalization import normalize_market

        assert normalize_market(None) is None

    def test_empty_string_returns_none(self):
        from app.mcp_server.tooling.market_normalization import normalize_market

        assert normalize_market("") is None

    def test_unknown_alias_returns_none(self):
        from app.mcp_server.tooling.market_normalization import normalize_market

        assert normalize_market("unknown") is None


class TestResolveMarketType:
    def test_crypto_with_explicit_market(self):
        from app.mcp_server.tooling.market_normalization import resolve_market_type

        market_type, symbol = resolve_market_type("KRW-BTC", "crypto")
        assert market_type == "crypto"
        assert symbol == "KRW-BTC"

    def test_kr_equity_with_explicit_market(self):
        from app.mcp_server.tooling.market_normalization import resolve_market_type

        market_type, symbol = resolve_market_type("005930", "kr")
        assert market_type == "equity_kr"
        assert symbol == "005930"

    def test_us_equity_with_explicit_market(self):
        from app.mcp_server.tooling.market_normalization import resolve_market_type

        market_type, symbol = resolve_market_type("AAPL", "us")
        assert market_type == "equity_us"
        assert symbol == "AAPL"

    def test_autodetect_crypto_by_symbol(self):
        from app.mcp_server.tooling.market_normalization import resolve_market_type

        market_type, _ = resolve_market_type("KRW-ETH", None)
        assert market_type == "crypto"

    def test_autodetect_kr_by_symbol(self):
        from app.mcp_server.tooling.market_normalization import resolve_market_type

        market_type, _ = resolve_market_type("035420", None)
        assert market_type == "equity_kr"

    def test_autodetect_us_by_symbol(self):
        from app.mcp_server.tooling.market_normalization import resolve_market_type

        market_type, _ = resolve_market_type("TSLA", None)
        assert market_type == "equity_us"


class TestNormalizeEquityMarket:
    def test_kr_aliases(self):
        from app.mcp_server.tooling.market_normalization import normalize_equity_market

        assert normalize_equity_market("kr") == "kr"
        assert normalize_equity_market("kospi") == "kr"
        assert normalize_equity_market("equity_kr") == "kr"

    def test_us_aliases(self):
        from app.mcp_server.tooling.market_normalization import normalize_equity_market

        assert normalize_equity_market("us") == "us"
        assert normalize_equity_market("nasdaq") == "us"
        assert normalize_equity_market("equity_us") == "us"

    def test_crypto_raises_value_error(self):
        from app.mcp_server.tooling.market_normalization import normalize_equity_market

        with pytest.raises(ValueError):
            normalize_equity_market("crypto")

    def test_unknown_raises_value_error(self):
        from app.mcp_server.tooling.market_normalization import normalize_equity_market

        with pytest.raises(ValueError):
            normalize_equity_market("unknown")


class TestNormalizeMarketWithCrypto:
    def test_crypto_aliases(self):
        from app.mcp_server.tooling.market_normalization import (
            normalize_market_with_crypto,
        )

        assert normalize_market_with_crypto("crypto") == "crypto"
        assert normalize_market_with_crypto("upbit") == "crypto"

    def test_kr_aliases(self):
        from app.mcp_server.tooling.market_normalization import (
            normalize_market_with_crypto,
        )

        assert normalize_market_with_crypto("kr") == "kr"

    def test_us_aliases(self):
        from app.mcp_server.tooling.market_normalization import (
            normalize_market_with_crypto,
        )

        assert normalize_market_with_crypto("us") == "us"

    def test_unknown_raises_value_error(self):
        from app.mcp_server.tooling.market_normalization import (
            normalize_market_with_crypto,
        )

        with pytest.raises(ValueError):
            normalize_market_with_crypto("unknown")


class TestDetectEquityMarket:
    def test_explicit_market_kr(self):
        from app.mcp_server.tooling.market_normalization import detect_equity_market

        assert detect_equity_market("005930", "kr") == "kr"

    def test_explicit_market_us(self):
        from app.mcp_server.tooling.market_normalization import detect_equity_market

        assert detect_equity_market("AAPL", "us") == "us"

    def test_autodetect_kr_from_symbol(self):
        from app.mcp_server.tooling.market_normalization import detect_equity_market

        assert detect_equity_market("005930", None) == "kr"

    def test_autodetect_us_from_symbol(self):
        from app.mcp_server.tooling.market_normalization import detect_equity_market

        assert detect_equity_market("TSLA", None) == "us"

    def test_crypto_symbol_raises_value_error(self):
        from app.mcp_server.tooling.market_normalization import detect_equity_market

        with pytest.raises(ValueError):
            detect_equity_market("KRW-BTC", None)


class TestBackwardCompatShared:
    def test_is_korean_equity_code_importable_from_shared(self):
        from app.mcp_server.tooling.shared import is_korean_equity_code

        assert is_korean_equity_code("005930") is True

    def test_is_crypto_market_importable_from_shared(self):
        from app.mcp_server.tooling.shared import is_crypto_market

        assert is_crypto_market("KRW-BTC") is True

    def test_normalize_market_importable_from_shared(self):
        from app.mcp_server.tooling.shared import normalize_market

        assert normalize_market("kr") == "equity_kr"

    def test_resolve_market_type_importable_from_shared(self):
        from app.mcp_server.tooling.shared import resolve_market_type

        market_type, _ = resolve_market_type("005930", None)
        assert market_type == "equity_kr"
