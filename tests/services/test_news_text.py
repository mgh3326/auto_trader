"""Unit tests for app/services/news_text.py (ROB-628)."""

from __future__ import annotations

import pytest

from app.services.news_text import (
    NEWS_RESPONSE_MAX_CHARS,
    NEWS_SUMMARY_MAX_CHARS,
    truncate_text,
)

pytestmark = pytest.mark.unit


class TestTruncateText:
    def test_none_input_returns_none(self):
        assert truncate_text(None) is None
        assert truncate_text(None, 240) is None

    def test_blank_after_strip_returns_none(self):
        # Whitespace-only / tag-only collapses to empty -> None.
        assert truncate_text("   ") is None
        assert truncate_text("\n\t  ") is None
        assert truncate_text("<br/><p></p>") is None

    def test_short_text_unchanged_without_max_length(self):
        assert truncate_text("Hello world") == "Hello world"

    def test_short_text_unchanged_with_generous_max_length(self):
        assert truncate_text("Hello world", 240) == "Hello world"

    def test_whitespace_is_collapsed_and_stripped(self):
        assert truncate_text("  Hello   \n  world  ") == "Hello world"

    def test_html_tags_stripped_and_entities_unescaped(self):
        raw = (
            '<p><a rel="nofollow" href="https://x.test">Bitcoin Magazine</a>'
            '<br /> <img src="https://x.test/i.jpg" />'
            "Bitcoin bounces as Iran strike unsettles risk assets &amp; oil.</p>"
        )
        result = truncate_text(raw)
        assert result is not None
        assert "<" not in result and ">" not in result
        assert "&amp;" not in result
        assert "&" in result  # entity was unescaped, not dropped
        assert result == (
            "Bitcoin Magazine Bitcoin bounces as Iran strike "
            "unsettles risk assets & oil."
        )

    def test_long_text_truncated_with_ellipsis(self):
        result = truncate_text("abcdefgh", 5)
        assert result == "abcd…"
        assert len(result) == 5
        assert result.endswith("…")

    def test_exact_boundary_not_truncated(self):
        # len(text) == max_length -> returned unchanged, no ellipsis.
        result = truncate_text("abcde", 5)
        assert result == "abcde"
        assert "…" not in result

    def test_one_over_boundary_is_truncated(self):
        # len(text) == max_length + 1 -> truncated to max_length chars.
        result = truncate_text("abcdef", 5)
        assert result == "abcd…"
        assert len(result) == 5

    def test_truncation_right_strips_before_ellipsis(self):
        # Cut lands on a space -> rstrip removes it before appending ellipsis.
        result = truncate_text("ab cdef", 4)
        # cleaned text "ab cdef" len 7 > 4 -> "ab "[:3].rstrip()="ab" + "…"
        assert result == "ab…"

    def test_truncation_applies_after_html_strip(self):
        # HTML stripped first, then length measured on cleaned text.
        result = truncate_text("<b>abcdefgh</b>", 5)
        assert result == "abcd…"

    def test_summary_cap_truncates_long_korean_summary(self):
        body = "가" * (NEWS_SUMMARY_MAX_CHARS + 50)
        result = truncate_text(body, NEWS_SUMMARY_MAX_CHARS)
        assert result is not None
        assert len(result) == NEWS_SUMMARY_MAX_CHARS
        assert result.endswith("…")

    def test_non_str_value_is_coerced(self):
        # ported behaviour: str(value) coercion before stripping.
        assert truncate_text(12345) == "12345"  # type: ignore[arg-type]


class TestConstants:
    def test_summary_cap_value(self):
        assert NEWS_SUMMARY_MAX_CHARS == 240

    def test_response_cap_value(self):
        assert NEWS_RESPONSE_MAX_CHARS == 8000

    def test_response_cap_larger_than_summary_cap(self):
        assert NEWS_RESPONSE_MAX_CHARS > NEWS_SUMMARY_MAX_CHARS
