"""Deterministic news entity matcher (ROB-130).

Pure functions over the built-in alias dictionaries. Future enhancement (ROB-129):
when `news_articles` carry candidate metadata from the news-ingestor pipeline,
prefer those. Until then, this module is the only entity-tagging layer.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass
from urllib.parse import urlsplit

from app.services.news_entity_alias_data import (
    ALL_ALIASES,
    CRYPTO_ALIASES,
    KR_ALIASES,
    US_ALIASES,
    US_BIG_TECH_GROUP_SYMBOLS,
    US_BROAD_MARKET_TERMS,
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


def match_kr_universe_symbols(
    text: str,
    universe: Iterable[tuple[str, str]],
) -> list[SymbolMatch]:
    """Longest-match-wins full-name matcher over a DB-backed KR symbol universe.

    ROB-916: supplementary mapping source for `news_article_related_symbols`
    when the upstream news-ingestor's own candidate extraction misses an
    explicit company-name mention (the curated `KR_ALIASES` dict is
    intentionally small — this covers the long-tail via `kr_symbol_universe`).

    ``universe`` is a caller-supplied ``(symbol, name)`` sequence (e.g. all
    active `kr_symbol_universe` rows) so this function stays DB-free/pure.
    Matching is done against the *full* registered company name only (never a
    truncated alias), and overlapping matches are resolved by trying longest
    names first and skipping any shorter match whose span is already claimed —
    this is what prevents a short name that is a literal substring of a
    longer one (e.g. "한화" inside a "한화오션" mention) from also firing as a
    spurious extra symbol tag alongside the correct longer match.
    """
    if not text:
        return []
    haystack = text.lower()
    entries = sorted(
        (
            (symbol, name.strip())
            for symbol, name in universe
            if symbol and name and name.strip()
        ),
        key=lambda pair: len(pair[1]),
        reverse=True,
    )
    claimed: list[tuple[int, int]] = []
    matches: dict[str, SymbolMatch] = {}
    for symbol, name in entries:
        if symbol in matches:
            continue
        needle = name.lower()
        if needle not in haystack:
            continue
        if _is_ascii_term(name):
            pattern = r"(?<![A-Za-z0-9])" + re.escape(needle) + r"(?![A-Za-z0-9])"
            occurrences = re.finditer(pattern, haystack)
        else:
            occurrences = re.finditer(re.escape(needle), haystack)
        for occurrence in occurrences:
            start, end = occurrence.span()
            if any(start < c_end and end > c_start for c_start, c_end in claimed):
                continue
            claimed.append((start, end))
            matches[symbol] = SymbolMatch(
                symbol=symbol,
                market="kr",
                canonical_name=name,
                matched_term=name,
                reason="kr_symbol_universe_name",
            )
            break
    return sorted(matches.values(), key=lambda m: m.symbol)


_URL_METADATA_PREFIXES = (
    "canonical_url:",
    "source_url:",
    "url:",
    "fingerprint:",
    "source:",
    "feed_source:",
    "publisher:",
)
_URL_SCHEME_PREFIXES = (f"{'http'}://", f"{'https'}://")


def _is_url_or_domain_token(token: str) -> bool:
    stripped = token.strip().strip("'\"()[]{}<>,")
    if not stripped:
        return False
    lowered = stripped.lower()
    if lowered.startswith(_URL_SCHEME_PREFIXES):
        return True
    if any(lowered.startswith(prefix) for prefix in _URL_METADATA_PREFIXES):
        return True
    candidate = lowered if "://" in lowered else f"//{lowered}"
    try:
        hostname = urlsplit(candidate).hostname or ""
    except ValueError:
        # Malformed URL/domain-like metadata should be dropped, not allowed to
        # crash feed rendering. This includes bracket/IPv6-like fragments from
        # scraped keywords or source/canonical URL metadata.
        return True
    if "." not in hostname:
        return False
    labels = hostname.split(".")
    return all(label.replace("-", "").isalnum() for label in labels)


def _clean_article_text(value: object) -> str:
    """Strip URL/domain text before matching so metadata links don't create aliases."""
    text = str(value or "").strip()
    if not text:
        return ""
    return " ".join(
        token for token in text.split() if not _is_url_or_domain_token(token)
    )


def _clean_keyword_text(keyword: object) -> str:
    """Drop URL-like keyword metadata so domains do not create entity false positives."""
    return _clean_article_text(keyword)


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
        if cleaned_title := _clean_article_text(title):
            parts.append(cleaned_title)
    if summary:
        if cleaned_summary := _clean_article_text(summary):
            parts.append(cleaned_summary)
    if keywords:
        parts.append(
            " ".join(cleaned for k in keywords if (cleaned := _clean_keyword_text(k)))
        )
    return match_symbols(" \n ".join(parts), market=market)


# ROB-155: US article scope classification.

# Specific-anchor patterns that override a broad-market frame and indicate the
# article is really about a named company action (not just a market-wide piece).
_SPECIFIC_ANCHOR_TERMS: tuple[str, ...] = (
    "earnings",
    "guidance",
    "revenue",
    "quarterly results",
    "sec filing",
    "sec charges",
    "lawsuit",
    "acquisition",
    "merger",
    "buyback",
    "ceo",
    "product launch",
    "job cuts",
    "layoffs",
    "ipo",
    "dividend",
)


@dataclass(frozen=True)
class ArticleScopeResult:
    """Result of US article scope classification (ROB-155)."""

    scope: str  # "market_wide" | "symbol_specific" | "mixed"
    tags: list[str]
    demoted_symbols: list[str]
    reasons: list[str]


def classify_article_scope(
    title: str | None,
    *,
    summary: str | None = None,
    keywords: Iterable[str] | None = None,
    market: str | None = None,
    matches: list[SymbolMatch] | None = None,
) -> ArticleScopeResult:
    """Classify a US article's scope and identify incidentally-mentioned big-tech symbols.

    Returns an ArticleScopeResult with scope, tags, demoted_symbols, and reasons.
    For non-US markets (or when market is None) this is effectively a no-op that
    returns scope=symbol_specific with empty demotion — callers should only demote
    for market == "us".
    """
    tags: list[str] = []
    demoted_symbols: list[str] = []
    reasons: list[str] = []

    if market != "us":
        return ArticleScopeResult(
            scope="symbol_specific",
            tags=tags,
            demoted_symbols=demoted_symbols,
            reasons=reasons,
        )

    combined_parts: list[str] = []
    if title:
        combined_parts.append(str(title).lower())
    if summary:
        combined_parts.append(str(summary).lower())
    if keywords:
        combined_parts.append(" ".join(str(k).lower() for k in keywords if k))
    text_lower = " ".join(combined_parts)
    title_lower = (title or "").lower()

    # Count broad-market term hits in title + body.
    broad_hits = [t for t in US_BROAD_MARKET_TERMS if t in text_lower]
    broad_title_hits = [t for t in US_BROAD_MARKET_TERMS if t in title_lower]

    # Count matched big-tech symbols.
    resolved_matches = matches or []
    big_tech_matched = [
        m.symbol
        for m in resolved_matches
        if m.symbol in US_BIG_TECH_GROUP_SYMBOLS and m.market == "us"
    ]
    non_big_tech_matched = [
        m.symbol
        for m in resolved_matches
        if m.symbol not in US_BIG_TECH_GROUP_SYMBOLS and m.market == "us"
    ]

    has_specific_anchor = any(term in text_lower for term in _SPECIFIC_ANCHOR_TERMS)
    has_broad_frame = bool(broad_hits) or bool(broad_title_hits)

    if not has_broad_frame:
        # No broad-market frame — standard symbol-specific article.
        return ArticleScopeResult(
            scope="symbol_specific",
            tags=tags,
            demoted_symbols=demoted_symbols,
            reasons=reasons,
        )

    # Broad market frame detected. Classify based on anchoring.
    tags.extend(["broad_market"])
    if broad_title_hits:
        tags.append("macro")
    if len(big_tech_matched) >= 2:
        tags.append("big_tech_group")

    if has_specific_anchor and len(big_tech_matched) == 1 and not non_big_tech_matched:
        # Single explicitly anchored big-tech company → symbol_specific even if
        # the article has a broad market frame in the headline/body.
        scope = "symbol_specific"
    elif has_specific_anchor and non_big_tech_matched:
        # Broad frame + specific non-big-tech anchor → mixed.
        scope = "mixed"
        if len(big_tech_matched) > 1:
            demoted_symbols.extend(big_tech_matched)
            reasons.extend(
                [f"broad_frame_incidental_big_tech:{s}" for s in big_tech_matched]
            )
    else:
        # Broad frame without a clear single-company anchor → market_wide.
        scope = "market_wide"
        demoted_symbols.extend(big_tech_matched)
        reasons.extend(
            [f"broad_frame_incidental_big_tech:{s}" for s in big_tech_matched]
        )

    return ArticleScopeResult(
        scope=scope,
        tags=list(dict.fromkeys(tags)),  # dedupe preserving order
        demoted_symbols=demoted_symbols,
        reasons=reasons,
    )
