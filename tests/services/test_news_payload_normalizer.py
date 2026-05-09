"""Unit tests for app/services/news_payload_normalizer.py"""

from __future__ import annotations

from datetime import UTC
from types import SimpleNamespace
from typing import Any

import pytest

# ---------------------------------------------------------------------------
# _looks_like_url_metadata
# ---------------------------------------------------------------------------


class TestLooksLikeUrlMetadata:
    def test_canonical_url_prefix(self):
        from app.services.news_payload_normalizer import _looks_like_url_metadata

        assert _looks_like_url_metadata("canonical_url:https://example.com") is True

    def test_url_prefix(self):
        from app.services.news_payload_normalizer import _looks_like_url_metadata

        assert _looks_like_url_metadata("url:https://example.com") is True

    def test_http_scheme(self):
        from app.services.news_payload_normalizer import _looks_like_url_metadata

        assert _looks_like_url_metadata("https://naver.com/news") is True

    def test_www_prefix(self):
        from app.services.news_payload_normalizer import _looks_like_url_metadata

        assert _looks_like_url_metadata("www.naver.com") is True

    def test_normal_symbol_not_url(self):
        from app.services.news_payload_normalizer import _looks_like_url_metadata

        assert _looks_like_url_metadata("005930") is False
        assert _looks_like_url_metadata("AAPL") is False
        assert _looks_like_url_metadata("삼성전자") is False


# ---------------------------------------------------------------------------
# _normalize_related_symbol_market
# ---------------------------------------------------------------------------


class TestNormalizeRelatedSymbolMarket:
    def test_kr_aliases(self):
        from app.services.news_payload_normalizer import (
            _normalize_related_symbol_market,
        )

        assert _normalize_related_symbol_market("kospi") == "kr"
        assert _normalize_related_symbol_market("kosdaq") == "kr"
        assert _normalize_related_symbol_market("krx") == "kr"

    def test_us_aliases(self):
        from app.services.news_payload_normalizer import (
            _normalize_related_symbol_market,
        )

        assert _normalize_related_symbol_market("nasdaq") == "us"
        assert _normalize_related_symbol_market("nyse") == "us"
        assert _normalize_related_symbol_market("amex") == "us"

    def test_crypto_alias(self):
        from app.services.news_payload_normalizer import (
            _normalize_related_symbol_market,
        )

        assert _normalize_related_symbol_market("upbit") == "crypto"

    def test_direct_canonical(self):
        from app.services.news_payload_normalizer import (
            _normalize_related_symbol_market,
        )

        assert _normalize_related_symbol_market("kr") == "kr"
        assert _normalize_related_symbol_market("us") == "us"
        assert _normalize_related_symbol_market("crypto") == "crypto"

    def test_unsupported_returns_none(self):
        from app.services.news_payload_normalizer import (
            _normalize_related_symbol_market,
        )

        assert _normalize_related_symbol_market("unknown") is None

    def test_none_value_uses_fallback(self):
        from app.services.news_payload_normalizer import (
            _normalize_related_symbol_market,
        )

        assert _normalize_related_symbol_market(None, fallback="kr") == "kr"

    def test_empty_value_uses_fallback(self):
        from app.services.news_payload_normalizer import (
            _normalize_related_symbol_market,
        )

        assert _normalize_related_symbol_market("", fallback="us") == "us"


# ---------------------------------------------------------------------------
# _normalize_related_symbol_symbol
# ---------------------------------------------------------------------------


class TestNormalizeRelatedSymbolSymbol:
    def test_kr_symbol_zero_padded(self):
        from app.services.news_payload_normalizer import (
            _normalize_related_symbol_symbol,
        )

        assert _normalize_related_symbol_symbol("5930", "kr") == "005930"

    def test_us_symbol_uppercased(self):
        from app.services.news_payload_normalizer import (
            _normalize_related_symbol_symbol,
        )

        assert _normalize_related_symbol_symbol("aapl", "us") == "AAPL"

    def test_crypto_symbol_uppercased(self):
        from app.services.news_payload_normalizer import (
            _normalize_related_symbol_symbol,
        )

        assert _normalize_related_symbol_symbol("btc", "crypto") == "BTC"

    def test_url_metadata_rejected(self):
        from app.services.news_payload_normalizer import (
            _normalize_related_symbol_symbol,
        )

        assert _normalize_related_symbol_symbol("www.naver.com", "us") is None
        assert (
            _normalize_related_symbol_symbol("canonical_url:https://x.com", "kr")
            is None
        )

    def test_empty_string_rejected(self):
        from app.services.news_payload_normalizer import (
            _normalize_related_symbol_symbol,
        )

        assert _normalize_related_symbol_symbol("", "kr") is None

    def test_none_rejected(self):
        from app.services.news_payload_normalizer import (
            _normalize_related_symbol_symbol,
        )

        assert _normalize_related_symbol_symbol(None, "us") is None

    def test_truncated_at_40_chars(self):
        from app.services.news_payload_normalizer import (
            _normalize_related_symbol_symbol,
        )

        long_symbol = "A" * 50
        result = _normalize_related_symbol_symbol(long_symbol, "us")
        assert result is not None
        assert len(result) == 40


# ---------------------------------------------------------------------------
# _coerce_stock_candidate
# ---------------------------------------------------------------------------


class TestCoerceStockCandidate:
    def test_dict_passthrough(self):
        from app.services.news_payload_normalizer import _coerce_stock_candidate

        candidate = {"symbol": "005930", "market": "kr"}
        assert _coerce_stock_candidate(candidate) == candidate

    def test_plain_string_becomes_symbol_dict(self):
        from app.services.news_payload_normalizer import _coerce_stock_candidate

        result = _coerce_stock_candidate("005930")
        assert result == {"symbol": "005930"}

    def test_url_string_rejected(self):
        from app.services.news_payload_normalizer import _coerce_stock_candidate

        assert _coerce_stock_candidate("canonical_url:https://x.com") is None

    def test_non_string_non_dict_rejected(self):
        from app.services.news_payload_normalizer import _coerce_stock_candidate

        assert _coerce_stock_candidate(123) is None
        assert _coerce_stock_candidate(None) is None


# ---------------------------------------------------------------------------
# _iter_raw_stock_candidates
# ---------------------------------------------------------------------------


class TestIterRawStockCandidates:
    def test_stock_candidates_key(self):
        from app.services.news_payload_normalizer import _iter_raw_stock_candidates

        raw = {"stock_candidates": [{"symbol": "AAPL", "market": "us"}]}
        result = _iter_raw_stock_candidates(raw)
        assert len(result) == 1
        assert result[0]["symbol"] == "AAPL"

    def test_related_symbols_fallback_key(self):
        from app.services.news_payload_normalizer import _iter_raw_stock_candidates

        raw = {"related_symbols": [{"symbol": "005930", "market": "kr"}]}
        result = _iter_raw_stock_candidates(raw)
        assert len(result) == 1

    def test_empty_raw_returns_empty(self):
        from app.services.news_payload_normalizer import _iter_raw_stock_candidates

        assert _iter_raw_stock_candidates({}) == []

    def test_non_dict_returns_empty(self):
        from app.services.news_payload_normalizer import _iter_raw_stock_candidates

        assert _iter_raw_stock_candidates(None) == []
        assert _iter_raw_stock_candidates("string") == []

    def test_single_dict_wrapped_in_list(self):
        from app.services.news_payload_normalizer import _iter_raw_stock_candidates

        raw = {"stock_candidates": {"symbol": "TSLA", "market": "us"}}
        result = _iter_raw_stock_candidates(raw)
        assert len(result) == 1

    def test_url_metadata_string_filtered(self):
        from app.services.news_payload_normalizer import _iter_raw_stock_candidates

        raw = {
            "stock_candidates": [
                "canonical_url:https://finance.naver.com/news",
                {"symbol": "005930", "market": "kr"},
            ]
        }
        result = _iter_raw_stock_candidates(raw)
        assert len(result) == 1
        assert result[0]["symbol"] == "005930"


# ---------------------------------------------------------------------------
# _prefer_related_symbol_row
# ---------------------------------------------------------------------------


class TestPreferRelatedSymbolRow:
    def test_none_existing_always_prefer(self):
        from app.services.news_payload_normalizer import _prefer_related_symbol_row

        assert _prefer_related_symbol_row(existing=None, candidate={"rank": 1}) is True

    def test_lower_rank_prefers_candidate(self):
        from app.services.news_payload_normalizer import _prefer_related_symbol_row

        existing = {"rank": 3, "score": 0.9}
        candidate = {"rank": 1, "score": 0.5}
        assert (
            _prefer_related_symbol_row(existing=existing, candidate=candidate) is True
        )

    def test_higher_rank_keeps_existing(self):
        from app.services.news_payload_normalizer import _prefer_related_symbol_row

        existing = {"rank": 1, "score": 0.9}
        candidate = {"rank": 3, "score": 0.5}
        assert (
            _prefer_related_symbol_row(existing=existing, candidate=candidate) is False
        )

    def test_same_rank_higher_score_prefers_candidate(self):
        from app.services.news_payload_normalizer import _prefer_related_symbol_row

        existing = {"rank": 2, "score": 0.5}
        candidate = {"rank": 2, "score": 0.95}
        assert (
            _prefer_related_symbol_row(existing=existing, candidate=candidate) is True
        )

    def test_same_rank_lower_score_keeps_existing(self):
        from app.services.news_payload_normalizer import _prefer_related_symbol_row

        existing = {"rank": 2, "score": 0.9}
        candidate = {"rank": 2, "score": 0.3}
        assert (
            _prefer_related_symbol_row(existing=existing, candidate=candidate) is False
        )


# ---------------------------------------------------------------------------
# _related_symbol_values_from_ingestor_payload — integration of helpers
# ---------------------------------------------------------------------------


def _make_article_data(raw: Any, market: str = "kr") -> Any:
    return SimpleNamespace(raw=raw, market=market)


class TestRelatedSymbolValuesFromIngestorPayload:
    def test_normalizes_multiple_candidates_sorted_by_rank(self):
        from app.services.news_payload_normalizer import (
            _related_symbol_values_from_ingestor_payload,
        )

        raw = {
            "stock_candidates": [
                {
                    "code": "5930",
                    "market": "kr",
                    "name": "삼성전자",
                    "score": "0.91",
                    "rank": 2,
                },
                {
                    "symbol": "aapl",
                    "market": "us",
                    "display_name": "Apple",
                    "score": 0.8,
                    "rank": 1,
                },
            ]
        }
        rows = _related_symbol_values_from_ingestor_payload(
            article_id=1, article_data=_make_article_data(raw)
        )

        assert [(row["market"], row["symbol"]) for row in rows] == [
            ("us", "AAPL"),
            ("kr", "005930"),
        ]
        assert rows[1]["display_name"] == "삼성전자"

    def test_drops_invalid_and_url_metadata(self):
        from app.services.news_payload_normalizer import (
            _related_symbol_values_from_ingestor_payload,
        )

        raw = {
            "stock_candidates": [
                {"symbol": "", "market": "kr"},
                {"symbol": "035420", "market": "unsupported"},
                "canonical_url:https://finance.naver.com/market_info_read.naver",
                {"symbol": "www.naver.com", "market": "us"},
            ]
        }
        rows = _related_symbol_values_from_ingestor_payload(
            article_id=1, article_data=_make_article_data(raw)
        )
        assert rows == []

    def test_dedupes_by_rank_and_score(self):
        from app.services.news_payload_normalizer import (
            _related_symbol_values_from_ingestor_payload,
        )

        raw = {
            "stock_candidates": [
                {"symbol": "005930", "market": "kr", "score": 0.5, "rank": 3},
                {"symbol": "5930", "market": "kr", "score": 0.9, "rank": 2},
                {"symbol": "005930", "market": "kr", "score": 0.95, "rank": 2},
            ]
        }
        rows = _related_symbol_values_from_ingestor_payload(
            article_id=1, article_data=_make_article_data(raw)
        )
        assert len(rows) == 1
        assert rows[0]["symbol"] == "005930"
        assert rows[0]["score"] == pytest.approx(0.95)

    def test_empty_raw_returns_empty(self):
        from app.services.news_payload_normalizer import (
            _related_symbol_values_from_ingestor_payload,
        )

        rows = _related_symbol_values_from_ingestor_payload(
            article_id=1, article_data=_make_article_data({})
        )
        assert rows == []

    def test_missing_raw_attr_returns_empty(self):
        from app.services.news_payload_normalizer import (
            _related_symbol_values_from_ingestor_payload,
        )

        article_data = SimpleNamespace(market="kr")  # no .raw
        rows = _related_symbol_values_from_ingestor_payload(
            article_id=1, article_data=article_data
        )
        assert rows == []


# ---------------------------------------------------------------------------
# _article_values_from_ingestor_payload
# ---------------------------------------------------------------------------


class TestArticleValuesFromIngestorPayload:
    def test_maps_all_fields(self):
        from datetime import datetime

        from app.services.news_payload_normalizer import (
            _article_values_from_ingestor_payload,
        )

        article_data = SimpleNamespace(
            url="  https://example.com/news/1  ",
            title="  Test Title  ",
            content="body text",
            summary="short summary",
            source="연합뉴스",
            author="홍길동",
            stock_symbol="005930",
            stock_name="삼성전자",
            published_at=datetime(2026, 5, 9, 10, 0, 0, tzinfo=UTC),
            market="kr",
            feed_source="browser_naver_mainnews",
            keywords=["fingerprint:fp-1"],
        )

        values = _article_values_from_ingestor_payload(article_data)

        assert values["url"] == "https://example.com/news/1"
        assert values["title"] == "Test Title"
        assert values["market"] == "kr"
        assert values["feed_source"] == "browser_naver_mainnews"
        assert values["article_content"] == "body text"
        assert values["source"] == "연합뉴스"

    def test_none_published_at_produces_none(self):
        from app.services.news_payload_normalizer import (
            _article_values_from_ingestor_payload,
        )

        article_data = SimpleNamespace(
            url="https://example.com/2",
            title="Title",
            content=None,
            summary=None,
            source=None,
            author=None,
            stock_symbol=None,
            stock_name=None,
            published_at=None,
            market="us",
            feed_source="rss_yahoo_finance",
            keywords=None,
        )

        values = _article_values_from_ingestor_payload(article_data)
        assert values["article_published_at"] is None
