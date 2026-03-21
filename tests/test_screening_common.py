# tests/test_screening_common.py
"""Verify screening.common re-exports match the original module."""

import pytest


class TestCommonReExports:
    """Ensure every util from common.py is importable."""

    def test_timeout_seconds(self):
        from app.mcp_server.tooling.screening.common import _timeout_seconds

        assert _timeout_seconds("tvscreener") == 30.0

    def test_to_optional_float_valid(self):
        from app.mcp_server.tooling.screening.common import _to_optional_float

        assert _to_optional_float("3.14") == pytest.approx(3.14)

    def test_to_optional_float_none(self):
        from app.mcp_server.tooling.screening.common import _to_optional_float

        assert _to_optional_float(None) is None

    def test_to_optional_int_valid(self):
        from app.mcp_server.tooling.screening.common import _to_optional_int

        assert _to_optional_int("42") == 42

    def test_clean_text(self):
        from app.mcp_server.tooling.screening.common import _clean_text

        assert _clean_text("  hello  ") == "hello"

    def test_normalize_screen_request_returns_dict(self):
        from app.mcp_server.tooling.screening.common import normalize_screen_request

        result = normalize_screen_request(
            market="kr",
            asset_type=None,
            category=None,
            sector=None,
            strategy=None,
            sort_by=None,
            sort_order=None,
            min_market_cap=None,
            max_per=None,
            max_pbr=None,
            min_dividend_yield=None,
            min_dividend=None,
            min_analyst_buy=None,
            max_rsi=None,
            limit=20,
        )
        assert isinstance(result, dict)
        assert result["market"] == "kr"

    def test_validate_screen_filters_empty(self):
        from app.mcp_server.tooling.screening.common import _validate_screen_filters

        # Should not raise for no filters
        _validate_screen_filters(
            market="kr",
            asset_type=None,
            min_market_cap=None,
            max_per=None,
            min_dividend_yield=None,
            max_rsi=None,
            sort_by=None,
        )

    def test_build_screen_response(self):
        from app.mcp_server.tooling.screening.common import _build_screen_response

        resp = _build_screen_response(
            results=[],
            total_count=0,
            filters_applied={},
            market="kr",
        )
        assert resp["market"] == "kr"
        assert resp["results"] == []

    def test_normalize_dividend_yield_threshold(self):
        from app.mcp_server.tooling.screening.common import (
            _normalize_dividend_yield_threshold,
        )

        original, normalized = _normalize_dividend_yield_threshold(3.0)
        assert original == 3.0
        assert normalized == pytest.approx(0.03)

    def test_empty_rsi_enrichment_diagnostics(self):
        from app.mcp_server.tooling.screening.common import (
            _empty_rsi_enrichment_diagnostics,
        )

        d = _empty_rsi_enrichment_diagnostics()
        assert isinstance(d, dict)
        assert "attempted" in d

    def test_market_cap_cache_class(self):
        from app.mcp_server.tooling.screening.common import MarketCapCache

        cache = MarketCapCache(ttl=10)
        assert cache is not None

    def test_constants_exist(self):
        from app.mcp_server.tooling.screening.common import (
            CRYPTO_TOP_BY_VOLUME,
            DEFAULT_TIMEOUTS,
            DROP_THRESHOLD,
        )

        assert DROP_THRESHOLD == -0.30
        assert "tvscreener" in DEFAULT_TIMEOUTS
        assert CRYPTO_TOP_BY_VOLUME == 100
