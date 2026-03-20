# tests/test_screening_tvscreener_support.py
"""Verify tvscreener_support functions are importable."""


class TestTvScreenerSupportImports:
    def test_required_capabilities(self):
        from app.mcp_server.tooling.screening.tvscreener_support import _required_tvscreener_stock_capabilities
        caps = _required_tvscreener_stock_capabilities(
            market="kr", asset_type=None, category=None,
            sort_by="volume", min_market_cap=None, max_per=None,
            min_dividend_yield=None,
        )
        assert isinstance(caps, set)
        assert "volume" in caps

    def test_can_use_tvscreener_stock_path_no_snapshot(self):
        from app.mcp_server.tooling.screening.tvscreener_support import _can_use_tvscreener_stock_path
        result = _can_use_tvscreener_stock_path(
            market="kr", asset_type=None, category=None,
            sort_by="volume", min_market_cap=None, max_per=None,
            min_dividend_yield=None, capability_snapshot=None,
        )
        assert result is False

    def test_map_tvscreener_stock_row(self):
        from app.mcp_server.tooling.screening.tvscreener_support import _map_tvscreener_stock_row
        row = {"symbol": "005930", "name": "Samsung", "price": 70000, "volume": 1000}
        mapped = _map_tvscreener_stock_row(row, market="kr")
        assert mapped["code"] == "005930"
        assert mapped["market"] == "kr"

    def test_adapt_tvscreener_stock_response(self):
        from app.mcp_server.tooling.screening.tvscreener_support import _adapt_tvscreener_stock_response
        result = _adapt_tvscreener_stock_response({"stocks": [], "count": 0}, market="kr")
        assert result["market"] == "kr"
        assert result["results"] == []
