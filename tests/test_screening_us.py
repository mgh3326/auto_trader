# tests/test_screening_us.py
"""Verify US screening functions are importable from the new location."""


class TestUsScreeningImports:
    def test_screen_us_importable(self):
        from app.mcp_server.tooling.screening.us import _screen_us

        assert callable(_screen_us)

    def test_screen_us_via_tvscreener_importable(self):
        from app.mcp_server.tooling.screening.us import _screen_us_via_tvscreener

        assert callable(_screen_us_via_tvscreener)

    def test_screen_us_with_fallback_importable(self):
        from app.mcp_server.tooling.screening.us import _screen_us_with_fallback

        assert callable(_screen_us_with_fallback)
