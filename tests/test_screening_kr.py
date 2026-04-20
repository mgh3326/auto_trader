# tests/test_screening_kr.py
"""Verify KR screening functions are importable from the new location."""


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
