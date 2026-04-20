# tests/test_screening_crypto.py
"""Verify crypto screening functions are importable from the new location."""


class TestCryptoScreeningImports:
    def test_screen_crypto_via_tvscreener_importable(self):
        from app.mcp_server.tooling.screening.crypto import (
            _screen_crypto_via_tvscreener,
        )

        assert callable(_screen_crypto_via_tvscreener)

    def test_screen_crypto_with_fallback_importable(self):
        from app.mcp_server.tooling.screening.crypto import _screen_crypto_with_fallback

        assert callable(_screen_crypto_with_fallback)

    def test_crypto_market_cap_cache(self):
        from app.mcp_server.tooling.screening.crypto import _CRYPTO_MARKET_CAP_CACHE

        assert _CRYPTO_MARKET_CAP_CACHE is not None


class TestCryptoScreeningPhases:
    def test_build_crypto_filters_importable(self):
        from app.mcp_server.tooling.screening.crypto import _build_crypto_filters

        assert callable(_build_crypto_filters)

    def test_execute_crypto_query_importable(self):
        from app.mcp_server.tooling.screening.crypto import _execute_crypto_query

        assert callable(_execute_crypto_query)

    def test_normalize_crypto_results_importable(self):
        from app.mcp_server.tooling.screening.crypto import _normalize_crypto_results

        assert callable(_normalize_crypto_results)
