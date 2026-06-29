# app/mcp_server/tooling/fundamentals/_news.py
"""Handler for get_news tool (routes through symbol_news_service, ROB-423)."""

from __future__ import annotations

import asyncio
from typing import Any

from app.mcp_server.tooling.fundamentals._helpers import normalize_market_with_crypto
from app.mcp_server.tooling.shared import (
    error_payload as _error_payload,
)
from app.mcp_server.tooling.shared import (
    is_crypto_market as _is_crypto_market,
)
from app.mcp_server.tooling.shared import (
    is_korean_equity_code as _is_korean_equity_code,
)
from app.mcp_server.tooling.shared import (
    normalize_symbol_input as _normalize_symbol_input,
)
from app.services import symbol_news_service

_INSTRUMENT_BY_MARKET = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}


async def handle_get_news(
    symbol: str | int,
    market: str | None = None,
    limit: int = 10,
) -> dict[str, Any]:
    symbol = _normalize_symbol_input(symbol, market)
    if not symbol:
        raise ValueError("symbol is required")

    if market is None:
        if _is_korean_equity_code(symbol):
            market = "kr"
        elif _is_crypto_market(symbol):
            market = "crypto"
        else:
            market = "us"

    normalized_market = normalize_market_with_crypto(market)
    capped_limit = min(max(limit, 1), 50)
    instrument_type = _INSTRUMENT_BY_MARKET.get(normalized_market, "equity_us")

    result = await symbol_news_service.fetch_symbol_news(
        symbol, normalized_market, instrument_type, limit=capped_limit
    )

    if result.status in ("error", "unavailable"):
        return _error_payload(
            source=result.provider,
            message=result.error_code or "news_unavailable",
            symbol=symbol,
            instrument_type=instrument_type,
        )

    news = []
    for article in result.articles:
        source_item = article.provider_metadata.get("source_item", {})
        item = dict(source_item) if isinstance(source_item, dict) else {}
        if relevance := article.provider_metadata.get("relevance"):
            item["relevance"] = relevance
        news.append(item)
    payload: dict[str, Any] = {
        "symbol": symbol,
        "market": normalized_market,
        "source": result.provider,
        "count": len(news),
        "excluded_count": result.excluded_count,
        "news": news,
    }
    if result.degraded:
        payload["degraded"] = True
        payload["fetch_error"] = result.fetch_error
    return payload


# ---------------------------------------------------------------------------
# get_holdings_news — cross-market catalyst-headline sweep (ROB-628 P2)
# ---------------------------------------------------------------------------

# Bound the basket so a large portfolio can't explode the fan-out, and bound
# concurrency so the per-symbol fetches don't stall the MCP event loop.
HOLDINGS_NEWS_MAX_SYMBOLS = 30
HOLDINGS_NEWS_CONCURRENCY = 4


def _lean_holdings_news_item(
    article: symbol_news_service.SymbolNewsArticle,
) -> dict[str, Any]:
    """Project a normalized article down to the lean sweep shape."""
    published_at = article.published_at
    return {
        "title": article.title,
        "url": article.canonical_url,
        "source": article.source_name,
        "published_at": published_at.isoformat() if published_at else None,
        "relevance": article.provider_metadata.get("relevance"),
    }


def _infer_holdings_news_market(symbol: str) -> str:
    """Infer 'kr'|'us'|'crypto' for a passed-through symbol (mirrors get_news)."""
    if _is_korean_equity_code(symbol):
        return "kr"
    if _is_crypto_market(symbol):
        return "crypto"
    return "us"


async def _resolve_holdings_news_candidates(
    symbols: list[str] | None,
) -> tuple[list[dict[str, Any]], str | None]:
    """Resolve the (symbol, market, name) candidates to sweep.

    Explicit ``symbols`` are normalized and passed through (market inferred per
    symbol, name unknown -> None). When ``symbols`` is omitted, current
    cross-market holdings are resolved through the canonical aggregation entry
    point ``_collect_portfolio_positions`` (KIS KR/US, Upbit, manual, Toss).
    De-dupes on (symbol, market) preserving first occurrence. Never raises.
    Returns ``(candidates, degraded_reason)``.
    """
    seen: set[tuple[str, str]] = set()
    candidates: list[dict[str, Any]] = []

    if symbols is not None:
        for raw in symbols:
            symbol = _normalize_symbol_input(raw, None)
            if not symbol:
                continue
            market = normalize_market_with_crypto(_infer_holdings_news_market(symbol))
            key = (symbol, market)
            if key in seen:
                continue
            seen.add(key)
            candidates.append({"symbol": symbol, "market": market, "name": None})
        return candidates, None

    # Holdings mode. Lazy import keeps the portfolio_holdings dependency (and its
    # broker clients) out of the plain fundamentals import path and avoids any
    # import cycle — mirrors _collect_portfolio_positions' own lazy imports.
    from app.mcp_server.tooling.portfolio_holdings import _collect_portfolio_positions

    try:
        positions, errors, _, _ = await _collect_portfolio_positions(
            account=None,
            market=None,
            include_current_price=False,
        )
    except Exception as exc:  # noqa: BLE001 — sweep must stay fail-soft
        return [], f"holdings_resolution_failed: {type(exc).__name__}"

    degraded_reason = (
        f"holdings resolution partial ({len(errors)} source error(s))"
        if errors
        else None
    )

    for position in positions:
        symbol = str(position.get("symbol") or "").strip()
        market = str(position.get("market") or "").strip().lower()
        if not symbol or market not in _INSTRUMENT_BY_MARKET:
            continue
        key = (symbol, market)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(
            {"symbol": symbol, "market": market, "name": position.get("name")}
        )

    return candidates, degraded_reason


async def _get_holdings_news_impl(
    symbols: list[str] | None = None,
    limit_per_symbol: int = 5,
) -> dict[str, Any]:
    """Sweep recent catalyst headlines for a basket of symbols in one call.

    ``symbols`` passed -> swept as-is (cross-market, market inferred per symbol);
    omitted -> current cross-market holdings are resolved and swept. Capped at
    HOLDINGS_NEWS_MAX_SYMBOLS; per-symbol fetch failures are isolated to that
    row (never abort the sweep). Lean rows mirror get_news but trimmed.
    """
    capped_limit = min(max(int(limit_per_symbol), 1), 50)

    candidates, resolution_degraded = await _resolve_holdings_news_candidates(symbols)

    symbols_requested = [entry["symbol"] for entry in candidates]
    degraded_reasons: list[str] = []
    if resolution_degraded:
        degraded_reasons.append(resolution_degraded)

    if len(candidates) > HOLDINGS_NEWS_MAX_SYMBOLS:
        degraded_reasons.append(
            f"resolved {len(candidates)} symbols; capped to "
            f"{HOLDINGS_NEWS_MAX_SYMBOLS} — pass a narrower symbols list to widen"
        )
        candidates = candidates[:HOLDINGS_NEWS_MAX_SYMBOLS]

    symbols_resolved = [entry["symbol"] for entry in candidates]

    semaphore = asyncio.Semaphore(HOLDINGS_NEWS_CONCURRENCY)

    async def _fetch_one(entry: dict[str, Any]) -> dict[str, Any]:
        symbol = entry["symbol"]
        market = entry["market"]
        instrument_type = _INSTRUMENT_BY_MARKET.get(market, "equity_us")
        row: dict[str, Any] = {
            "symbol": symbol,
            "name": entry["name"],
            "market": market,
            "status": "error",
            "news": [],
        }
        async with semaphore:
            try:
                result = await symbol_news_service.fetch_symbol_news(
                    symbol, market, instrument_type, limit=capped_limit
                )
            except Exception as exc:  # noqa: BLE001 — one bad symbol mustn't kill the sweep
                row["degraded_reason"] = type(exc).__name__
                return row
        row["status"] = result.status
        row["news"] = [_lean_holdings_news_item(a) for a in result.articles]
        if result.status in ("error", "unavailable"):
            row["degraded_reason"] = result.error_code or "news_unavailable"
        elif result.degraded:
            row["degraded_reason"] = result.fetch_error or "degraded"
        return row

    results = await asyncio.gather(*[_fetch_one(entry) for entry in candidates])

    payload: dict[str, Any] = {
        "symbols_requested": symbols_requested,
        "symbols_resolved": symbols_resolved,
        "count": len(results),
        "results": list(results),
    }
    if degraded_reasons:
        payload["degraded_reason"] = "; ".join(degraded_reasons)
    return payload
