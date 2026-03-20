# tests/test_screening_crypto.py
"""Verify crypto screening functions are importable from the new location."""


class TestCryptoScreeningImports:
    def test_screen_crypto_importable(self):
        from app.mcp_server.tooling.screening.crypto import _screen_crypto
        assert callable(_screen_crypto)

    def test_screen_crypto_via_tvscreener_importable(self):
        from app.mcp_server.tooling.screening.crypto import _screen_crypto_via_tvscreener
        assert callable(_screen_crypto_via_tvscreener)

    def test_screen_crypto_with_fallback_importable(self):
        from app.mcp_server.tooling.screening.crypto import _screen_crypto_with_fallback
        assert callable(_screen_crypto_with_fallback)

    def test_enrich_crypto_indicators_importable(self):
        from app.mcp_server.tooling.screening.crypto import _enrich_crypto_indicators
        assert callable(_enrich_crypto_indicators)

    def test_crypto_market_cap_cache(self):
        from app.mcp_server.tooling.screening.crypto import _CRYPTO_MARKET_CAP_CACHE
        assert _CRYPTO_MARKET_CAP_CACHE is not None
