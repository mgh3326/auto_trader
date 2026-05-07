"""Deterministic news entity matcher (ROB-130).

Pure functions over the built-in alias dictionaries. Future enhancement (ROB-129):
when `news_articles` carry candidate metadata from the news-ingestor pipeline,
prefer those. Until then, this module is the only entity-tagging layer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

from app.services.news_entity_alias_data import (
    ALL_ALIASES,
    CRYPTO_ALIASES,
    KR_ALIASES,
    US_ALIASES,
    AliasEntry,
)


@dataclass(frozen=True)
class SymbolMatch:
    symbol: str
    market: str
    canonical_name: str
    matched_term: str
    reason: str  # "alias_dict" | "candidate_metadata" | "exact_symbol"


def _aliases_for_market(market: str | None) -> tuple[AliasEntry, ...]:
    if market == "kr":
        return KR_ALIASES
    if market == "us":
        return US_ALIASES
    if market == "crypto":
        return CRYPTO_ALIASES
    return ALL_ALIASES


def _is_ascii_term(term: str) -> bool:
    return bool(term) and all(ord(c) < 128 for c in term)


def _term_matches(haystack_lower: str, term: str) -> bool:
    """Korean/non-ASCII -> substring match.
    ASCII (English/ticker) -> word-boundary match to avoid 'AMD' in 'amid'.
    """
    if not term:
        return False
    needle = term.lower()
    if not _is_ascii_term(term):
        return needle in haystack_lower
    pattern = r"(?<![A-Za-z0-9])" + re.escape(needle) + r"(?![A-Za-z0-9])"
    return re.search(pattern, haystack_lower) is not None


def match_symbols(
    text: str,
    *,
    market: str | None = None,
) -> list[SymbolMatch]:
    """Return symbol matches found in `text`, deduped by symbol.

    Sorted deterministically by (market, symbol). Empty list when no matches.
    """
    if not text:
        return []
    haystack = text.lower()
    candidates = _aliases_for_market(market)
    seen: dict[str, SymbolMatch] = {}
    for entry in candidates:
        for alias in entry.aliases:
            if _term_matches(haystack, alias):
                if entry.symbol not in seen:
                    seen[entry.symbol] = SymbolMatch(
                        symbol=entry.symbol,
                        market=entry.market,
                        canonical_name=entry.canonical_name,
                        matched_term=alias,
                        reason="alias_dict",
                    )
                break  # first alias hit per symbol is enough
    return sorted(seen.values(), key=lambda m: (m.market, m.symbol))


def match_symbols_for_article(
    *,
    title: str | None,
    summary: str | None = None,
    keywords: Iterable[str] | None = None,
    market: str | None = None,
) -> list[SymbolMatch]:
    """Convenience wrapper: combine article fields then call `match_symbols`."""
    parts: list[str] = []
    if title:
        parts.append(title)
    if summary:
        parts.append(summary)
    if keywords:
        parts.append(" ".join(str(k) for k in keywords if k))
    return match_symbols(" \n ".join(parts), market=market)
