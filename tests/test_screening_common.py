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

    def test_get_tvscreener_attr_prefers_supported_pbr_alias(self):
        from app.mcp_server.tooling.screening.common import _get_tvscreener_attr

        enum_obj = type(
            "StockField",
            (),
            {
                "PRICE_TO_BOOK_FQ": None,
                "PRICE_TO_BOOK_MRQ": "price_to_book_mrq",
            },
        )

        assert (
            _get_tvscreener_attr(
                enum_obj,
                "PRICE_TO_BOOK_FQ",
                "PRICE_TO_BOOK_MRQ",
                "PRICE_BOOK_CURRENT",
            )
            == "price_to_book_mrq"
        )

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


class TestInitTvscreenerResult:
    def test_basic_structure(self):
        from app.mcp_server.tooling.screening.common import _init_tvscreener_result

        result = _init_tvscreener_result({"market": "us"})
        assert result == {
            "stocks": [],
            "source": "tvscreener",
            "count": 0,
            "filters_applied": {"market": "us"},
            "error": None,
        }


class TestAggregateAnalystRecommendations:
    def test_all_present(self):
        from app.mcp_server.tooling.screening.common import (
            _aggregate_analyst_recommendations,
        )

        row = {
            "recommendation_buy": 5,
            "recommendation_over": 3,
            "recommendation_hold": 2,
            "recommendation_sell": 1,
            "recommendation_under": 1,
        }
        agg = _aggregate_analyst_recommendations(row)
        assert agg == {"analyst_buy": 8, "analyst_hold": 2, "analyst_sell": 2}

    def test_partial_none(self):
        from app.mcp_server.tooling.screening.common import (
            _aggregate_analyst_recommendations,
        )

        row = {"recommendation_buy": 5}
        agg = _aggregate_analyst_recommendations(row)
        assert agg == {"analyst_buy": 5}
        assert "analyst_hold" not in agg
        assert "analyst_sell" not in agg

    def test_all_none(self):
        from app.mcp_server.tooling.screening.common import (
            _aggregate_analyst_recommendations,
        )

        agg = _aggregate_analyst_recommendations({})
        assert agg == {}


class TestFilterByMinAnalystBuy:
    def test_none_threshold_returns_all(self):
        from app.mcp_server.tooling.screening.common import _filter_by_min_analyst_buy

        stocks = [{"analyst_buy": 5}, {"analyst_buy": 1}]
        assert _filter_by_min_analyst_buy(stocks, None) is stocks

    def test_filters_below_threshold(self):
        from app.mcp_server.tooling.screening.common import _filter_by_min_analyst_buy

        stocks = [
            {"analyst_buy": 5, "name": "A"},
            {"analyst_buy": 1, "name": "B"},
            {"name": "C"},
        ]
        result = _filter_by_min_analyst_buy(stocks, 3)
        assert len(result) == 1
        assert result[0]["name"] == "A"


class TestBuildRsiAdxConditions:
    def test_no_filters_returns_empty(self):
        from app.mcp_server.tooling.screening.common import _build_rsi_adx_conditions

        conditions = _build_rsi_adx_conditions(min_rsi=None, max_rsi=None, min_adx=None)
        assert conditions == []

    def test_all_filters_returns_three(self):
        from app.mcp_server.tooling.screening.common import _build_rsi_adx_conditions

        # Mocking fields for testing
        class MockField:
            def __init__(self, name):
                self.name = name

            def __ge__(self, other):
                return f"{self.name} >= {other}"

            def __le__(self, other):
                return f"{self.name} <= {other}"

        rsi_field = MockField("RSI")
        adx_field = MockField("ADX")

        conditions = _build_rsi_adx_conditions(
            min_rsi=30,
            max_rsi=70,
            min_adx=25,
            rsi_field=rsi_field,
            adx_field=adx_field,
        )
        assert len(conditions) == 3
        assert "RSI >= 30" in conditions
        assert "RSI <= 70" in conditions
        assert "ADX >= 25" in conditions


class TestComputeAvgTargetAndUpside:
    def test_with_delta(self):
        from app.mcp_server.tooling.screening.common import (
            _compute_avg_target_and_upside,
        )

        row = {
            "price_target_1y": 150.0,
            "price_target_1y_delta": 10.5,
        }
        avg, upside = _compute_avg_target_and_upside(row, current_price=100.0)
        assert avg == 150.0
        assert upside == 10.5

    def test_fallback_computed(self):
        from app.mcp_server.tooling.screening.common import (
            _compute_avg_target_and_upside,
        )

        row = {"price_target_1y": 120.0}
        avg, upside = _compute_avg_target_and_upside(row, current_price=100.0)
        assert avg == 120.0
        assert upside == 20.0

    def test_no_target(self):
        from app.mcp_server.tooling.screening.common import (
            _compute_avg_target_and_upside,
        )

        avg, upside = _compute_avg_target_and_upside({}, current_price=100.0)
        assert avg is None
        assert upside is None
