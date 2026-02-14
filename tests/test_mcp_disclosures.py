"""Tests for get_disclosures_impl with automatic code conversion."""

import pytest

from app.mcp_server.tooling import analysis_tool_handlers as handlers


class TestGetDisclosuresImplCodeConversion:
    """Test get_disclosures_impl automatic code to name conversion."""

    @pytest.mark.asyncio
    async def test_numeric_code_auto_conversion(self, monkeypatch):
        """Test numeric code is converted to Korean name before DART lookup."""
        captured_calls = []

        async def mock_get_stock_name_by_code(code):
            if code == "005930":
                return "삼성전자"
            return None

        async def mock_list_filings(symbol, days, limit, report_type):
            captured_calls.append(
                {
                    "symbol": symbol,
                    "days": days,
                    "limit": limit,
                    "report_type": report_type,
                }
            )
            return [{"title": "공시 제목", "date": "2026-01-01"}]

        async def mock_prime_index():
            pass

        monkeypatch.setattr(
            handlers, "get_stock_name_by_code", mock_get_stock_name_by_code
        )
        monkeypatch.setattr(handlers, "list_filings", mock_list_filings)
        monkeypatch.setattr(handlers, "NAME_TO_CORP", {"삼성전자": "00126380"})
        monkeypatch.setattr(handlers, "prime_index", mock_prime_index)

        result = await handlers.get_disclosures_impl(symbol="005930", days=30, limit=3)

        assert result["success"] is True
        assert len(captured_calls) == 1
        assert captured_calls[0]["symbol"] == "삼성전자"
        assert captured_calls[0]["days"] == 30
        assert captured_calls[0]["limit"] == 3

    @pytest.mark.asyncio
    async def test_korean_name_direct_input(self, monkeypatch):
        """Test Korean name is used directly without conversion."""
        conversion_called = False

        async def mock_get_stock_name_by_code(code):
            nonlocal conversion_called
            conversion_called = True
            return None

        captured_symbol = []

        async def mock_list_filings(symbol, days, limit, report_type):
            captured_symbol.append(symbol)
            return []

        async def mock_prime_index():
            pass

        monkeypatch.setattr(
            handlers, "get_stock_name_by_code", mock_get_stock_name_by_code
        )
        monkeypatch.setattr(handlers, "list_filings", mock_list_filings)
        monkeypatch.setattr(handlers, "NAME_TO_CORP", {"삼성전자": "00126380"})
        monkeypatch.setattr(handlers, "prime_index", mock_prime_index)

        result = await handlers.get_disclosures_impl(symbol="삼성전자", days=30)

        assert result["success"] is True
        assert not conversion_called, "Should not call conversion for Korean name"
        assert captured_symbol[0] == "삼성전자"

    @pytest.mark.asyncio
    async def test_conversion_failure_graceful_fallback(self, monkeypatch):
        """Test conversion returning None falls back to original code."""
        captured_symbol = []

        async def mock_get_stock_name_by_code(code):
            return None

        async def mock_list_filings(symbol, days, limit, report_type):
            captured_symbol.append(symbol)
            return []

        async def mock_prime_index():
            pass

        monkeypatch.setattr(
            handlers, "get_stock_name_by_code", mock_get_stock_name_by_code
        )
        monkeypatch.setattr(handlers, "list_filings", mock_list_filings)
        monkeypatch.setattr(handlers, "NAME_TO_CORP", {})
        monkeypatch.setattr(handlers, "prime_index", mock_prime_index)

        result = await handlers.get_disclosures_impl(symbol="999999", days=30)

        assert result["success"] is True
        assert captured_symbol[0] == "999999"

    @pytest.mark.asyncio
    async def test_conversion_exception_graceful_fallback(self, monkeypatch):
        """Test conversion raising exception falls back to original code."""
        captured_symbol = []

        async def mock_get_stock_name_by_code(code):
            raise RuntimeError("KRX API error")

        async def mock_list_filings(symbol, days, limit, report_type):
            captured_symbol.append(symbol)
            return []

        async def mock_prime_index():
            pass

        monkeypatch.setattr(
            handlers, "get_stock_name_by_code", mock_get_stock_name_by_code
        )
        monkeypatch.setattr(handlers, "list_filings", mock_list_filings)
        monkeypatch.setattr(handlers, "NAME_TO_CORP", {})
        monkeypatch.setattr(handlers, "prime_index", mock_prime_index)

        result = await handlers.get_disclosures_impl(symbol="005930", days=30)

        assert result["success"] is True
        assert captured_symbol[0] == "005930"

    @pytest.mark.asyncio
    async def test_prime_index_failure_returns_error(self, monkeypatch):
        """Test prime_index failure returns error payload."""

        async def mock_prime_index():
            raise RuntimeError("DART index load failed")

        async def mock_list_filings(symbol, days, limit, report_type):
            return []

        monkeypatch.setattr(handlers, "list_filings", mock_list_filings)
        monkeypatch.setattr(handlers, "NAME_TO_CORP", {})
        monkeypatch.setattr(handlers, "prime_index", mock_prime_index)

        result = await handlers.get_disclosures_impl(symbol="삼성전자", days=30)

        assert result["success"] is False
        assert "Failed to prime DART index" in result["error"]

    @pytest.mark.asyncio
    async def test_list_filings_none_returns_unavailable_error(self, monkeypatch):
        """Test list_filings=None returns unavailable error."""
        monkeypatch.setattr(handlers, "list_filings", None)

        result = await handlers.get_disclosures_impl(symbol="005930", days=30)

        assert result["success"] is False
        assert "dart_fss package not installed" in result["error"]
