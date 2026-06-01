# tests/test_screening_kr.py
"""Verify KR screening functions are importable from the new location."""

import pytest


class TestKrScreeningImports:
    def test_screen_kr_importable(self):
        from app.mcp_server.tooling.screening.kr import _screen_kr

        assert callable(_screen_kr)

    def test_screen_kr_via_tvscreener_importable(self):
        from app.mcp_server.tooling.screening.kr import _screen_kr_via_tvscreener

        assert callable(_screen_kr_via_tvscreener)

    def test_screen_kr_with_fallback_importable(self):
        from app.mcp_server.tooling.screening.kr import _screen_kr_with_fallback

        assert callable(_screen_kr_with_fallback)


class TestKrScreeningPhases:
    def test_build_kr_filters_importable(self):
        from app.mcp_server.tooling.screening.kr import _build_kr_filters

        assert callable(_build_kr_filters)

    def test_execute_kr_query_importable(self):
        from app.mcp_server.tooling.screening.kr import _execute_kr_query

        assert callable(_execute_kr_query)

    def test_normalize_kr_results_importable(self):
        from app.mcp_server.tooling.screening.kr import _normalize_kr_results

        assert callable(_normalize_kr_results)


class TestKrTradeAmountExposed:
    """Legacy KR screening exposes trade_amount derived from KRX traded value."""

    @pytest.mark.asyncio
    async def test_screen_kr_sets_trade_amount_from_value(self, monkeypatch):
        from app.mcp_server.tooling.screening import kr as kr_mod

        async def fake_fetch_stock_all_cached(market: str):
            return [
                {
                    "short_code": "000001",
                    "code": "000001",
                    "name": "AAA",
                    "value": 500.0,
                    "market_cap": 10.0,
                },
                {
                    "short_code": "000002",
                    "code": "000002",
                    "name": "BBB",
                    "value": 100.0,
                    "market_cap": 10.0,
                },
            ]

        async def fake_fetch_etf_all_cached():
            return []

        async def fake_fetch_valuation_all_cached(market: str):
            return {}

        monkeypatch.setattr(
            kr_mod, "fetch_stock_all_cached", fake_fetch_stock_all_cached
        )
        monkeypatch.setattr(kr_mod, "fetch_etf_all_cached", fake_fetch_etf_all_cached)
        monkeypatch.setattr(
            kr_mod, "fetch_valuation_all_cached", fake_fetch_valuation_all_cached
        )

        response = await kr_mod._screen_kr(
            market="kospi",
            asset_type="stock",
            category=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by="trade_amount",
            sort_order="desc",
            limit=10,
        )
        results = response["results"]
        assert [r["short_code"] for r in results] == ["000001", "000002"]
        assert results[0]["trade_amount"] == 500.0


class TestKrScreenSessionExpired:
    """KRXSessionExpiredError surfaces as a structured unavailable signal, not a raise."""

    @pytest.mark.asyncio
    async def test_session_expired_returns_unavailable_signal(self, monkeypatch):
        import httpx

        from app.mcp_server.tooling.screening import kr as kr_mod
        from app.services.krx import KRXSessionExpiredError

        async def fake_screen_kr(**kwargs):
            request = httpx.Request("POST", "https://example.invalid")
            response = httpx.Response(400, text="LOGOUT", request=request)
            raise KRXSessionExpiredError(
                "KRX session expired after re-auth",
                request=request,
                response=response,
            )

        # Force the legacy path and make it raise the typed error.
        monkeypatch.setattr(kr_mod, "_screen_kr", fake_screen_kr)
        monkeypatch.setattr(
            kr_mod, "_can_use_tvscreener_stock_path", lambda **kwargs: False
        )

        response = await kr_mod._screen_kr_with_fallback(
            market="kospi",
            asset_type="stock",
            category=None,
            sector=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            min_analyst_buy=None,
            max_rsi=None,
            sort_by="change_rate",
            sort_order="desc",
            limit=10,
        )

        assert response["results"] == []
        assert response["meta"]["data_state"] == "unavailable"
        assert response["meta"]["retryable"] is True
        assert response["meta"]["reason"] == "krx_session_expired"
        assert response.get("warnings")
