# tests/test_screening_entrypoint.py
"""Verify entrypoint functions are importable from the new location."""


class TestEntrypointImports:
    def test_screen_stocks_unified_importable(self):
        from app.mcp_server.tooling.screening.entrypoint import screen_stocks_unified

        assert callable(screen_stocks_unified)


class TestEntrypointDispatchHelpers:
    def test_dispatch_kr_screen_importable(self):
        from app.mcp_server.tooling.screening.entrypoint import _dispatch_kr_screen

        assert callable(_dispatch_kr_screen)

    def test_dispatch_us_screen_importable(self):
        from app.mcp_server.tooling.screening.entrypoint import _dispatch_us_screen

        assert callable(_dispatch_us_screen)

    def test_dispatch_crypto_screen_importable(self):
        from app.mcp_server.tooling.screening.entrypoint import _dispatch_crypto_screen

        assert callable(_dispatch_crypto_screen)

    def test_dispatch_unsupported_market_importable(self):
        from app.mcp_server.tooling.screening.entrypoint import (
            _dispatch_unsupported_market,
        )

        assert callable(_dispatch_unsupported_market)
