# tests/test_screening_compat.py
"""Verify backward compatibility — old import paths still work."""


class TestBackwardCompat:
    """All existing imports from analysis_screen_core must still resolve."""

    def test_screen_stocks_unified_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import screen_stocks_unified
        assert callable(screen_stocks_unified)

    def test_normalize_screen_request_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import normalize_screen_request
        assert callable(normalize_screen_request)

    def test_screen_kr_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _screen_kr
        assert callable(_screen_kr)

    def test_screen_us_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _screen_us
        assert callable(_screen_us)

    def test_screen_crypto_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _screen_crypto
        assert callable(_screen_crypto)

    def test_build_screen_response_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _build_screen_response
        assert callable(_build_screen_response)

    def test_clean_text_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _clean_text
        assert callable(_clean_text)

    def test_to_optional_float_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _to_optional_float
        assert callable(_to_optional_float)

    def test_to_optional_int_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _to_optional_int
        assert callable(_to_optional_int)

    def test_enrich_crypto_indicators_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _enrich_crypto_indicators
        assert callable(_enrich_crypto_indicators)

    def test_screen_kr_via_tvscreener_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _screen_kr_via_tvscreener
        assert callable(_screen_kr_via_tvscreener)

    def test_screen_us_via_tvscreener_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _screen_us_via_tvscreener
        assert callable(_screen_us_via_tvscreener)

    def test_screen_crypto_via_tvscreener_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _screen_crypto_via_tvscreener
        assert callable(_screen_crypto_via_tvscreener)

    def test_validate_screen_filters_from_old_path(self):
        from app.mcp_server.tooling.analysis_screen_core import _validate_screen_filters
        assert callable(_validate_screen_filters)

    def test_new_package_init_exports(self):
        from app.mcp_server.tooling.screening import screen_stocks_unified, normalize_screen_request
        assert callable(screen_stocks_unified)
        assert callable(normalize_screen_request)
