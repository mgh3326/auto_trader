"""Unit tests for paper portfolio handler."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from app.mcp_server.tooling.paper_portfolio_handler import (
    PaperAccountSelector,
    is_paper_account_token,
    parse_paper_account_token,
    resolve_paper_position_name,
)


class TestIsPaperAccountToken:
    def test_exact_paper(self):
        assert is_paper_account_token("paper") is True

    def test_paper_with_name(self):
        assert is_paper_account_token("paper:데이트레이딩") is True

    def test_case_insensitive(self):
        assert is_paper_account_token("PAPER") is True
        assert is_paper_account_token("Paper:swing") is True

    def test_paper_with_whitespace(self):
        assert is_paper_account_token("  paper  ") is True

    def test_non_paper(self):
        assert is_paper_account_token("kis") is False
        assert is_paper_account_token("upbit") is False
        assert is_paper_account_token("paperless") is False  # prefix-only match forbidden
        assert is_paper_account_token(None) is False
        assert is_paper_account_token("") is False


class TestParsePaperAccountToken:
    def test_bare_paper_returns_all_selector(self):
        sel = parse_paper_account_token("paper")
        assert sel == PaperAccountSelector(account_name=None)

    def test_paper_with_name(self):
        sel = parse_paper_account_token("paper:데이트레이딩")
        assert sel == PaperAccountSelector(account_name="데이트레이딩")

    def test_trims_whitespace(self):
        sel = parse_paper_account_token("  paper :   swing  ")
        assert sel == PaperAccountSelector(account_name="swing")

    def test_empty_name_after_colon(self):
        sel = parse_paper_account_token("paper:")
        assert sel == PaperAccountSelector(account_name=None)

    def test_non_paper_raises(self):
        with pytest.raises(ValueError, match="not a paper account token"):
            parse_paper_account_token("kis")


class TestResolvePaperPositionName:
    @pytest.mark.asyncio
    async def test_equity_kr_uses_stock_info(self, monkeypatch):
        fake_stock = type("S", (), {"name": "삼성전자"})()

        async def fake_get(self, symbol):
            assert symbol == "005930"
            return fake_stock

        monkeypatch.setattr(
            "app.services.stock_info_service.StockInfoService.get_stock_info_by_symbol",
            fake_get,
        )
        name = await resolve_paper_position_name("005930", "equity_kr", db=AsyncMock())
        assert name == "삼성전자"

    @pytest.mark.asyncio
    async def test_equity_us_uses_stock_info(self, monkeypatch):
        fake_stock = type("S", (), {"name": "Apple Inc."})()

        async def fake_get(self, symbol):
            return fake_stock

        monkeypatch.setattr(
            "app.services.stock_info_service.StockInfoService.get_stock_info_by_symbol",
            fake_get,
        )
        name = await resolve_paper_position_name("AAPL", "equity_us", db=AsyncMock())
        assert name == "Apple Inc."

    @pytest.mark.asyncio
    async def test_stock_info_missing_falls_back_to_symbol(self, monkeypatch):
        async def fake_get(self, symbol):
            return None

        monkeypatch.setattr(
            "app.services.stock_info_service.StockInfoService.get_stock_info_by_symbol",
            fake_get,
        )
        name = await resolve_paper_position_name("NEWCO", "equity_us", db=AsyncMock())
        assert name == "NEWCO"

    @pytest.mark.asyncio
    async def test_crypto_uses_upbit_universe(self, monkeypatch):
        monkeypatch.setattr(
            "app.mcp_server.tooling.paper_portfolio_handler."
            "get_upbit_korean_name_by_coin",
            AsyncMock(return_value="비트코인"),
        )
        name = await resolve_paper_position_name("KRW-BTC", "crypto", db=AsyncMock())
        assert name == "비트코인"

    @pytest.mark.asyncio
    async def test_crypto_lookup_failure_falls_back_to_symbol(self, monkeypatch):
        from app.services.upbit_symbol_universe_service import (
            UpbitSymbolNotRegisteredError,
        )

        async def boom(coin, quote_currency=None):
            raise UpbitSymbolNotRegisteredError("x")

        monkeypatch.setattr(
            "app.mcp_server.tooling.paper_portfolio_handler."
            "get_upbit_korean_name_by_coin",
            boom,
        )
        name = await resolve_paper_position_name("KRW-XYZ", "crypto", db=AsyncMock())
        assert name == "KRW-XYZ"
