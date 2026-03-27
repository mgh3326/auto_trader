"""Tests for fundamentals helpers (market normalization)."""

import pytest

from app.mcp_server.tooling.fundamentals._helpers import (
    detect_equity_market,
    normalize_equity_market,
    normalize_market_with_crypto,
)


class TestNormalizeEquityMarket:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("kr", "kr"),
            ("KR", "kr"),
            ("krx", "kr"),
            ("korea", "kr"),
            ("kospi", "kr"),
            ("kosdaq", "kr"),
            ("kis", "kr"),
            ("equity_kr", "kr"),
            ("naver", "kr"),
            ("us", "us"),
            ("USA", "us"),
            ("nyse", "us"),
            ("nasdaq", "us"),
            ("yahoo", "us"),
            ("equity_us", "us"),
        ],
    )
    def test_valid_markets(self, raw: str, expected: str) -> None:
        assert normalize_equity_market(raw) == expected

    @pytest.mark.parametrize("raw", ["crypto", "upbit", "invalid", ""])
    def test_invalid_markets(self, raw: str) -> None:
        with pytest.raises(ValueError, match="market must be"):
            normalize_equity_market(raw)


class TestNormalizeMarketWithCrypto:
    @pytest.mark.parametrize(
        "raw,expected",
        [
            ("kr", "kr"),
            ("us", "us"),
            ("crypto", "crypto"),
            ("upbit", "crypto"),
            ("krw", "crypto"),
            ("usdt", "crypto"),
        ],
    )
    def test_valid_markets(self, raw: str, expected: str) -> None:
        assert normalize_market_with_crypto(raw) == expected

    def test_invalid_market(self) -> None:
        with pytest.raises(ValueError, match="market must be"):
            normalize_market_with_crypto("invalid")


class TestDetectEquityMarket:
    def test_korean_code(self) -> None:
        assert detect_equity_market("005930", None) == "kr"

    def test_us_symbol(self) -> None:
        assert detect_equity_market("AAPL", None) == "us"

    def test_explicit_market_overrides(self) -> None:
        assert detect_equity_market("AAPL", "kr") == "kr"

    def test_crypto_raises(self) -> None:
        with pytest.raises(ValueError, match="not available for crypto"):
            detect_equity_market("KRW-BTC", None)
