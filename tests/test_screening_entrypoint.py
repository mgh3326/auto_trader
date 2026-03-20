# tests/test_screening_entrypoint.py
"""Verify entrypoint functions are importable from the new location."""


class TestEntrypointImports:
    def test_screen_stocks_unified_importable(self):
        from app.mcp_server.tooling.screening.entrypoint import screen_stocks_unified
        assert callable(screen_stocks_unified)
