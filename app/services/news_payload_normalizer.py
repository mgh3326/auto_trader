"""Payload normalization helpers for news-ingestor bulk ingestion.

These pure-dict transformation functions were extracted from
``app.services.llm_news_service`` to improve cohesion. The public API of
``llm_news_service`` is unchanged.

No ORM models or DB sessions are imported here.
"""

from __future__ import annotations

from typing import Any

from app.core.timezone import now_kst_naive, to_kst_naive

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_RELATED_SYMBOL_MARKETS = {"kr", "us", "crypto"}
_URL_METADATA_PREFIXES = (
    "canonical_url:",
    "source_url:",
    "url:",
    "fingerprint:",
)


# ---------------------------------------------------------------------------
# Article value mapping
# ---------------------------------------------------------------------------


def _article_values_from_ingestor_payload(article_data: Any) -> dict[str, Any]:
    return {
        "url": article_data.url.strip(),
        "title": article_data.title.strip(),
        "article_content": article_data.content,
        "summary": article_data.summary,
        "source": article_data.source,
        "author": article_data.author,
        "stock_symbol": article_data.stock_symbol,
        "stock_name": article_data.stock_name,
        "article_published_at": to_kst_naive(article_data.published_at)
        if article_data.published_at
        else None,
        "market": article_data.market,
        "feed_source": article_data.feed_source,
        "keywords": article_data.keywords,
        "scraped_at": now_kst_naive(),
        "created_at": now_kst_naive(),
    }


# ---------------------------------------------------------------------------
# Related symbol helpers
# ---------------------------------------------------------------------------


def _normalize_related_symbol_market(
    value: Any, fallback: str | None = None
) -> str | None:
    market = str(value or fallback or "").strip().lower()
    if market in ("kospi", "kosdaq", "krx"):
        market = "kr"
    elif market in ("nasdaq", "nyse", "amex"):
        market = "us"
    elif market == "upbit":
        market = "crypto"
    return market if market in _RELATED_SYMBOL_MARKETS else None


def _looks_like_url_metadata(value: str) -> bool:
    lowered = value.strip().lower()
    return (
        lowered.startswith(_URL_METADATA_PREFIXES)
        or "://" in lowered
        or lowered.startswith("www.")
    )


def _normalize_related_symbol_symbol(value: Any, market: str) -> str | None:
    symbol = str(value or "").strip()
    if not symbol or _looks_like_url_metadata(symbol):
        return None
    if market == "kr" and symbol.isdigit():
        symbol = symbol.zfill(6)
    elif market in ("us", "crypto"):
        symbol = symbol.upper()
    return symbol[:40]


def _candidate_field(candidate: dict[str, Any], *names: str) -> Any:
    for name in names:
        if name in candidate and candidate[name] not in (None, ""):
            return candidate[name]
    return None


def _coerce_stock_candidate(candidate: Any) -> dict[str, Any] | None:
    if isinstance(candidate, dict):
        return candidate
    if isinstance(candidate, str):
        if _looks_like_url_metadata(candidate):
            return None
        return {"symbol": candidate}
    return None


# ROB-161: TradingView prefix → (market, symbol) fallback for raw.tv_related_symbols.
_TRADINGVIEW_PREFIX_TO_MARKET: dict[str, str] = {
    "KRX": "kr",
    "KOSPI": "kr",
    "KOSDAQ": "kr",
    "KONEX": "kr",
    "NASDAQ": "us",
    "NYSE": "us",
    "AMEX": "us",
    "BATS": "us",
    "ARCA": "us",
    "BINANCE": "crypto",
    "BITSTAMP": "crypto",
    "COINBASE": "crypto",
    "KRAKEN": "crypto",
    "BYBIT": "crypto",
    "OKX": "crypto",
    "UPBIT": "crypto",
    "BITHUMB": "crypto",
}


def _parse_tradingview_symbol(token: Any) -> tuple[str, str] | None:
    """Parse a 'PREFIX:SYMBOL' TradingView token into (market, symbol).

    Returns None for unsupported prefixes (LSE/TSE/FX/INDEX/...) and for any
    URL/empty/malformed input. Symbols are normalized via the same rules as
    _normalize_related_symbol_symbol (zero-padding for KR codes,
    upper-casing for US/crypto).
    """
    if not isinstance(token, str):
        return None
    raw = token.strip()
    if not raw or _looks_like_url_metadata(raw):
        return None
    if ":" not in raw:
        return None
    prefix, _, rest = raw.partition(":")
    market = _TRADINGVIEW_PREFIX_TO_MARKET.get(prefix.strip().upper())
    if market is None:
        return None
    if not rest.strip():
        return None
    symbol = _normalize_related_symbol_symbol(rest, market)
    if symbol is None:
        return None
    return market, symbol


def _iter_raw_stock_candidates(raw: Any) -> list[dict[str, Any]]:
    if not isinstance(raw, dict):
        return []
    candidates = raw.get("stock_candidates")
    if candidates is None:
        candidates = raw.get("related_symbols")
    if candidates is None:
        # ROB-161: fall back to news-ingestor's TradingView raw tokens.
        tv_tokens = raw.get("tv_related_symbols")
        if isinstance(tv_tokens, list) and tv_tokens:
            synthesized: list[dict[str, Any]] = []
            for token in tv_tokens:
                parsed = _parse_tradingview_symbol(token)
                if parsed is None:
                    continue
                market, symbol = parsed
                synthesized.append(
                    {
                        "market": market,
                        "symbol": symbol,
                        "source": "tv_related_symbol",
                    }
                )
            return synthesized
        return []
    if isinstance(candidates, dict):
        candidates = [candidates]
    if not isinstance(candidates, list):
        return []
    out: list[dict[str, Any]] = []
    for candidate in candidates:
        coerced = _coerce_stock_candidate(candidate)
        if coerced is not None:
            out.append(coerced)
    return out


def _coerce_int_or_default(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _score_from_candidate(candidate: dict[str, Any]) -> float | None:
    score_value = _candidate_field(candidate, "score", "confidence")
    try:
        return float(score_value) if score_value is not None else None
    except (TypeError, ValueError):
        return None


def _prefer_related_symbol_row(
    *,
    existing: dict[str, Any] | None,
    candidate: dict[str, Any],
) -> bool:
    if existing is None:
        return True
    candidate_rank = candidate.get("rank")
    rank = int(candidate_rank if candidate_rank is not None else 10_000)
    existing_rank = existing.get("rank")
    previous_rank = int(existing_rank if existing_rank is not None else rank + 1)
    if rank < previous_rank:
        return True
    existing_score = existing.get("score")
    candidate_score = candidate.get("score")
    return (
        rank == existing_rank
        and candidate_score is not None
        and (existing_score is None or candidate_score > float(existing_score))
    )


def _related_symbol_row_from_candidate(
    *,
    article_id: int,
    candidate: dict[str, Any],
    default_market: Any,
    ordinal: int,
) -> dict[str, Any] | None:
    market = _normalize_related_symbol_market(
        _candidate_field(candidate, "market", "market_type", "exchange"),
        default_market,
    )
    if market is None:
        return None
    symbol = _normalize_related_symbol_symbol(
        _candidate_field(candidate, "symbol", "ticker", "code", "stock_symbol"),
        market,
    )
    if symbol is None:
        return None
    display_name = _candidate_field(
        candidate,
        "display_name",
        "displayName",
        "name",
        "stock_name",
        "canonical_name",
    )
    matched_term = _candidate_field(candidate, "matched_term", "matchedTerm", "term")
    return {
        "article_id": article_id,
        "market": market,
        "symbol": symbol,
        "display_name": str(display_name).strip()[:120]
        if display_name is not None and str(display_name).strip()
        else None,
        "source": str(candidate.get("source") or "candidate_metadata").strip()[:80]
        or "candidate_metadata",
        "matched_term": str(matched_term).strip()[:120]
        if matched_term is not None and str(matched_term).strip()
        else None,
        "score": _score_from_candidate(candidate),
        "rank": _coerce_int_or_default(
            _candidate_field(candidate, "rank", "order"), ordinal
        ),
        "raw": dict(candidate),
        "created_at": now_kst_naive(),
    }


def _kr_universe_related_symbol_row(
    *,
    article_id: int,
    symbol: str,
    matched_term: str,
    canonical_name: str,
) -> dict[str, Any]:
    """Row for the ROB-916 supplementary `kr_symbol_universe_name` source.

    Distinct ``source`` value from the ingestor-provided candidates
    (``candidate_metadata``/``tv_related_symbol``) so both can coexist per
    article under the ``(article_id, market, symbol, source)`` unique
    constraint — this is additive, never a replacement for ingestor tags.
    """
    return {
        "article_id": article_id,
        "market": "kr",
        "symbol": symbol,
        "display_name": canonical_name.strip()[:120] or None,
        "source": "kr_symbol_universe_name",
        "matched_term": matched_term.strip()[:120] or None,
        "score": None,
        "rank": None,
        "raw": {"matcher": "kr_symbol_universe_name"},
        "created_at": now_kst_naive(),
    }


def _related_symbol_values_from_ingestor_payload(
    *, article_id: int, article_data: Any
) -> list[dict[str, Any]]:
    """Normalize news-ingestor raw.stock_candidates into related-symbol rows."""
    best_by_key: dict[tuple[str, str, str], dict[str, Any]] = {}
    default_market = getattr(article_data, "market", None)
    for ordinal, candidate in enumerate(
        _iter_raw_stock_candidates(getattr(article_data, "raw", None)), start=1
    ):
        row = _related_symbol_row_from_candidate(
            article_id=article_id,
            candidate=candidate,
            default_market=default_market,
            ordinal=ordinal,
        )
        if row is None:
            continue
        key = (row["market"], row["symbol"], row["source"])
        existing = best_by_key.get(key)
        if _prefer_related_symbol_row(existing=existing, candidate=row):
            best_by_key[key] = row
    return sorted(
        best_by_key.values(),
        key=lambda row: (
            row["rank"] if row["rank"] is not None else 10_000,
            row["symbol"],
        ),
    )
