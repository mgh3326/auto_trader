from __future__ import annotations

import datetime as dt

from app.services.social_sentiment_probe.models import (
    build_social_sentiment_evidence,
    source_result,
    strip_markup,
    truncate_preview,
)


def _now() -> dt.datetime:
    return dt.datetime(2026, 7, 6, 1, 2, 3, tzinfo=dt.UTC)


def test_strip_markup_removes_naver_b_tags_and_unescapes_entities() -> None:
    assert strip_markup("<b>삼성전자</b> &amp; SK하이닉스") == "삼성전자 & SK하이닉스"


def test_strip_markup_ignores_non_string_values() -> None:
    assert strip_markup(123) is None
    assert strip_markup(["bad"]) is None


def test_truncate_preview_preserves_short_values_and_caps_long_values() -> None:
    assert truncate_preview("abc", limit=3) == "abc"
    assert truncate_preview("abcdef", limit=3) == "abc"
    assert truncate_preview(None) is None


def test_truncate_preview_ignores_non_string_values() -> None:
    assert truncate_preview(["bad"]) is None
    assert truncate_preview({"text": "bad"}) is None


def test_source_result_counts_items_and_omits_empty_error() -> None:
    out = source_result(
        source="bluesky",
        market="us",
        query="AAPL",
        status="ok",
        items=[{"title": "AAPL"}],
        observed_at=_now(),
    )
    assert out["source"] == "bluesky"
    assert out["item_count"] == 1
    assert out["observed_at"] == "2026-07-06T01:02:03+00:00"
    assert "error_reason" not in out


def test_build_social_sentiment_evidence_is_advisory_and_zero_cost() -> None:
    src = source_result(
        source="reddit",
        market="us",
        query="NVDA",
        status="ok",
        items=[{"title": "NVDA volume"}],
        observed_at=_now(),
    )
    out = build_social_sentiment_evidence(
        market="us",
        symbol="NVDA",
        query="NVDA",
        source_results=[src],
        observed_at=_now(),
    )
    assert out["source"] == "free_social_sources_v0"
    assert out["advisory_only"] is True
    assert out["cost_usd"] == 0
    assert out["summary"] == {
        "source_count": 1,
        "ok_source_count": 1,
        "total_item_count": 1,
    }
    assert out["sources"] == [src]
