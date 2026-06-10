"""Deterministic relevance *hints* for symbol news (ROB-491).

Non-authoritative signals only. auto_trader never excludes an article based on
these — the external judgment job reads them as context; only the token-authed
ingest route transitions an article's status.
"""

from __future__ import annotations

from typing import Any

from app.services.news_entity_alias_data import (
    KR_ALIASES,
    KR_BROAD_MARKET_TERMS,
    KR_INVEST_KEYWORDS,
)

_KR_SYMBOL_ALIASES: dict[str, tuple[str, ...]] = {
    entry.symbol: entry.aliases for entry in KR_ALIASES
}

# ROB-491-local hint terms. NOT added to the shared KR_INVEST_KEYWORDS tuple on
# purpose: that tuple also feeds the ROB-169 briefing scorer, and broad
# substrings like "투자" (matches 투자자/투자심리/…) would shift its scoring.
_KR_EXTRA_INVEST_HINT_TERMS: tuple[str, ...] = (
    "투자",
    "스타트업",
)


def _matched_terms(text: str, terms: tuple[str, ...]) -> list[str]:
    lowered = text.lower()
    return [term for term in terms if term.lower() in lowered]


def _target_terms(symbol: str, market: str) -> tuple[str, ...]:
    if market == "kr":
        return (*_KR_SYMBOL_ALIASES.get(symbol, ()), symbol)
    return (symbol,)


def build_relevance_hints(
    *,
    symbol: str,
    market: str,
    title: str,
    summary: str | None = None,
) -> dict[str, Any] | None:
    """Deterministic signals for one article, or None when nothing matched."""
    text = " ".join(part for part in (title, summary) if part)
    hints: dict[str, Any] = {}
    if alias_match := _matched_terms(text, _target_terms(symbol, market)):
        hints["alias_match"] = alias_match
    if market == "kr":
        if invest := _matched_terms(
            text, (*KR_INVEST_KEYWORDS, *_KR_EXTRA_INVEST_HINT_TERMS)
        ):
            hints["invest_keywords"] = invest
        if market_terms := _matched_terms(text, KR_BROAD_MARKET_TERMS):
            hints["market_terms"] = market_terms
    return hints or None
