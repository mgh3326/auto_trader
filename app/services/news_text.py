"""Shared news text normalization helpers (ROB-628).

HTML-strip + whitespace-collapse + ellipsis truncation, ported verbatim from
``news_radar_service._plain_text`` so every news MCP tool shares one
truncation contract instead of re-implementing it.

Read-only pure helpers. No DB writes. No broker calls. No mutation.
"""

from __future__ import annotations

import re
from html import unescape

# Per-item summary cap (e.g. each article's snippet in detail="summary").
NEWS_SUMMARY_MAX_CHARS = 240
# Whole-response soft cap used by size-capping callers (truncated_for_size).
NEWS_RESPONSE_MAX_CHARS = 8000

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def truncate_text(value: str | None, max_length: int | None = None) -> str | None:
    """Strip HTML, collapse whitespace, and optionally ellipsis-truncate.

    Behaviour is ported 1:1 from ``news_radar_service._plain_text``:

    - ``None`` input -> ``None``.
    - HTML entities are unescaped (``&amp;`` -> ``&``), HTML tags are replaced
      with a single space, and runs of whitespace are collapsed to one space
      then stripped.
    - If the cleaned text is empty after stripping -> ``None``.
    - When ``max_length`` is provided and the cleaned text is longer than it,
      the result is hard-capped to ``max_length`` characters total: the first
      ``max_length - 1`` characters (right-stripped) followed by a single
      ellipsis ("…"). At exactly ``max_length`` characters the text is returned
      unchanged.
    - When ``max_length`` is ``None`` no truncation is applied (strip-only).
    """
    if value is None:
        return None
    text = unescape(str(value))
    text = _HTML_TAG_RE.sub(" ", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    if not text:
        return None
    if max_length is not None and len(text) > max_length:
        return text[: max_length - 1].rstrip() + "…"
    return text
