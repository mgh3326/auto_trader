"""ROB-1309: live-enrichment MCP tool for the snapshot screener.

`screen_stocks_snapshot` (`screener_snapshot_tool.py`) became DB-only — zero
external HTTP by default — after a Sentry investigation found it was making
~214 HTTP calls per invocation (p50 38.11s / p95 54.73s, 8 timeouts over a
120s budget) fanning out sector lazy-fill + analyst-consensus + live-price
fetches for every returned row, on every call, whether or not the caller
asked for that data.

This tool is the explicit, opt-in home for that live enrichment. It:

1. Reuses `_build_snapshot_page` (DB-only row build/filter/paginate — the
   exact same logic `screen_stocks_snapshot` runs) so the two tools never
   drift on preset/filter/holdings/pagination semantics.
2. Optionally applies `min_analyst_count` / `min_analyst_buy_count` — which
   the DB-only tool now rejects outright — by resolving analyst-consensus
   COUNTS via the existing Redis cache-aside
   (`analyst_consensus_cache.resolve_consensus_counts`) BEFORE paginating.
3. Always enriches the final returned page (sector label lazy-fill +
   analyst consensus + RSI) via `screener_analysis_enrichment.enrich_snapshot_page`
   — same as the old inline `screen_stocks_snapshot` behavior.
4. Wraps the sector-fetch and consensus-fetch provider callables with a
   bounded, TTL'd negative cache (`enrichment_negative_cache.py`) so a
   symbol that just failed (delisted, renamed, parse-miss, rate-limited)
   is not re-fetched — and re-failed — on every subsequent call within the
   TTL window. Every skip/failure is reported under
   `meta.enrichment_excluded` (never a silent drop): the row itself always
   stays in `results`, only its sector/consensus enrichment is missing.
   Chronic failures (>=3 consecutive) are additionally surfaced under
   `meta.chronic_failure_candidates` as *advisory* universe-cleanup
   candidates — this tool never mutates `kr_symbol_universe` /
   `us_symbol_universe` / `symbol_sectors` itself beyond the pre-existing
   `symbol_sectors_service` lazy-fill write path (ROB-512), and it makes
   zero writes to `invest_screener_snapshots` (the only writer of that
   table remains `InvestScreenerSnapshotsRepository.upsert`, used by the
   offline snapshot-building job/flows, not by this MCP tool).

Read-only wrt broker/order/watch state, same as `screen_stocks_snapshot`.
"""

from __future__ import annotations

import logging
from typing import Any, Literal

from app.mcp_server.tooling import screener_snapshot_tool as _snapshot_tool
from app.services.invest_view_model import (
    analyst_consensus_cache,
)
from app.services.invest_view_model import (
    enrichment_negative_cache as negcache,
)

logger = logging.getLogger(__name__)


def _sector_fetcher_with_negative_cache(
    *, kind: str, market: str, redis_client: Any, excluded: list[dict[str, Any]]
) -> Any:
    """Build a fetch_kr_sector/fetch_us_sector-shaped callable that consults +
    updates the negative cache around the real fetcher. `kind` selects which
    real fetcher (`_fetch_kr_sector`/`_fetch_us_sector`) is wrapped."""
    from app.services.invest_view_model.screener_analysis_enrichment import (
        _fetch_kr_sector,
        _fetch_us_sector,
    )

    real_fetcher = _fetch_kr_sector if kind == "kr_sector" else _fetch_us_sector

    async def _wrapped(symbol: str) -> tuple[str | None, str | None]:
        entry = await negcache.should_skip(
            redis_client, kind=kind, market=market, symbol=symbol
        )
        if entry is not None:
            excluded.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "kind": kind,
                    "reason": "negative_cache_hit",
                    "errorClass": entry.error_class,
                    "consecutiveFailures": entry.consecutive_failures,
                }
            )
            return None, None
        try:
            result = await real_fetcher(symbol)
        except Exception as exc:  # noqa: BLE001 — classify + report, re-raise for caller's own fail-open
            recorded = await negcache.record_failure(
                redis_client, kind=kind, market=market, symbol=symbol, exc=exc
            )
            excluded.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "kind": kind,
                    "reason": "fetch_failed",
                    "errorClass": recorded.error_class,
                    "consecutiveFailures": recorded.consecutive_failures,
                }
            )
            raise
        await negcache.record_success(
            redis_client, kind=kind, market=market, symbol=symbol
        )
        return result

    return _wrapped


def _opinion_provider_with_negative_cache(
    *,
    market: str,
    redis_client: Any,
    memo: dict[str, Any],
    excluded: list[dict[str, Any]],
) -> Any:
    kind = "consensus"

    async def _wrapped(*, symbol: str, market: str, limit: int = 10) -> dict[str, Any]:
        entry = await negcache.should_skip(
            redis_client, kind=kind, market=market, symbol=symbol
        )
        if entry is not None:
            excluded.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "kind": kind,
                    "reason": "negative_cache_hit",
                    "errorClass": entry.error_class,
                    "consecutiveFailures": entry.consecutive_failures,
                }
            )
            return {"error": "analyst_consensus_unavailable"}
        result = await analyst_consensus_cache.cached_opinion_provider(
            symbol=symbol,
            market=market,
            limit=limit,
            redis_client=redis_client,
            memo=memo,
        )
        if isinstance(result, dict) and result.get("error"):
            recorded = await negcache.record_failure(
                redis_client,
                kind=kind,
                market=market,
                symbol=symbol,
                exc=RuntimeError(str(result.get("error"))),
            )
            excluded.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "kind": kind,
                    "reason": "fetch_failed",
                    "errorClass": recorded.error_class,
                    "consecutiveFailures": recorded.consecutive_failures,
                }
            )
        else:
            await negcache.record_success(
                redis_client, kind=kind, market=market, symbol=symbol
            )
        return result

    return _wrapped


async def screen_stocks_enrich_impl(
    *,
    preset: str | None = None,
    presets: list[str] | None = None,
    market: str = "kr",
    filters: list[dict[str, Any]] | None = None,
    exclude_watched: bool = False,
    exclude_held: bool = False,
    exclude_symbols: list[str] | None = None,
    min_analyst_count: int | None = None,
    min_analyst_buy_count: int | None = None,
    min_market_cap: float | None = None,
    min_market_cap_eok: float | None = None,
    max_market_cap_eok: float | None = None,
    sort: Literal["matched_presets_desc"] | None = None,
    limit: int = 40,
    offset: int = 0,
) -> dict[str, Any]:
    """Live-enrichment counterpart to `screen_stocks_snapshot` (ROB-1309).

    Runs the identical preset/filter/discovery/pagination pipeline as
    `screen_stocks_snapshot`, then makes external calls: sector lazy-fill
    (KR Naver / US yfinance, persisted via the pre-existing
    `symbol_sectors_service`), analyst-consensus fetch (KR Redis
    cache-aside, US live), and — only for the final returned page — a fresh
    current-price fetch for target-upside recomputation.

    min_analyst_count / min_analyst_buy_count: filters on consensus COUNTS,
    resolved BEFORE pagination (bounded to `_MAX_ANALYST_ENRICHMENT_ROWS`
    merged rows) — same semantics as the pre-ROB-1309 screen_stocks_snapshot.

    Negative caching: a symbol whose sector/consensus fetch just failed is
    not retried within the TTL window; every skip/failure is reported under
    `meta.enrichment_excluded` (rows are never silently dropped — only the
    specific enrichment field is missing for that symbol). See module
    docstring for the full contract, including the explicit non-mutation of
    `kr_symbol_universe`/`us_symbol_universe`/`symbol_sectors` beyond the
    pre-existing lazy-fill write path.
    """
    built = await _snapshot_tool._build_snapshot_page(
        preset=preset,
        presets=presets,
        market=market,
        filters=filters,
        exclude_watched=exclude_watched,
        exclude_held=exclude_held,
        exclude_symbols=exclude_symbols,
        min_market_cap=min_market_cap,
        min_market_cap_eok=min_market_cap_eok,
        max_market_cap_eok=max_market_cap_eok,
        sort=sort,
        limit=limit,
        offset=offset,
    )
    if built.get("error") is not None:
        return built["error"]

    payload = built["payload"]
    payload["discoveryFilters"]["min_analyst_count"] = min_analyst_count
    payload["discoveryFilters"]["min_analyst_buy_count"] = min_analyst_buy_count

    all_results: list[dict[str, Any]] = built["all_results"]
    eff_offset: int = built["eff_offset"]
    eff_limit: int = built["eff_limit"]

    from app.core import analyze_cache

    redis_client = await analyze_cache._get_redis_client()
    memo: dict[str, Any] = {}
    enrichment_excluded: list[dict[str, Any]] = []

    if min_analyst_count is not None or min_analyst_buy_count is not None:
        if len(all_results) > _snapshot_tool._MAX_ANALYST_ENRICHMENT_ROWS:
            return {
                "error": (
                    "analyst enrichment row cap exceeded; narrow presets, "
                    "market-cap filters, or exclude_symbols before applying analyst filters"
                ),
                "preset": preset,
                "presets": built["preset_ids"],
                "results": [],
                "pagination": {
                    "total_available": len(all_results),
                    "returned_count": 0,
                    "offset": eff_offset,
                    "limit": eff_limit,
                    "has_more": False,
                    "next_offset": None,
                },
            }

        matched_symbols = [
            str(r.get("symbol") or "").strip() for r in all_results if r.get("symbol")
        ]
        counts = await analyst_consensus_cache.resolve_consensus_counts(
            symbols=matched_symbols,
            market=market,
            redis_client=redis_client,
            memo=memo,
        )

        def _passes(row: dict[str, Any]) -> bool:
            c = counts.get(str(row.get("symbol") or "").strip())
            if c is None:
                return False
            if min_analyst_count is not None and (c.get("totalCount") or 0) < int(
                min_analyst_count
            ):
                return False
            if min_analyst_buy_count is not None and (c.get("buyCount") or 0) < int(
                min_analyst_buy_count
            ):
                return False
            return True

        all_results = [r for r in all_results if _passes(r)]
        total_available = len(all_results)
        page = all_results[eff_offset : eff_offset + eff_limit]
        next_offset = eff_offset + len(page)
        payload["pagination"] = {
            "total_available": total_available,
            "returned_count": len(page),
            "offset": eff_offset,
            "limit": eff_limit,
            "has_more": next_offset < total_available,
            "next_offset": next_offset if next_offset < total_available else None,
        }
    else:
        page = built["page"]

    fetch_kr_sector = _sector_fetcher_with_negative_cache(
        kind="kr_sector",
        market=market,
        redis_client=redis_client,
        excluded=enrichment_excluded,
    )
    fetch_us_sector = _sector_fetcher_with_negative_cache(
        kind="us_sector",
        market=market,
        redis_client=redis_client,
        excluded=enrichment_excluded,
    )
    opinion_provider = _opinion_provider_with_negative_cache(
        market=market,
        redis_client=redis_client,
        memo=memo,
        excluded=enrichment_excluded,
    )

    from app.services.invest_view_model.screener_analysis_enrichment import (
        enrich_snapshot_page,
    )

    enrichment = await enrich_snapshot_page(
        rows=page,
        market=market,
        session_factory=_snapshot_tool._session_factory(),
        opinion_provider=opinion_provider,
        fetch_kr_sector=fetch_kr_sector,
        fetch_us_sector=fetch_us_sector,
    )
    payload["results"] = enrichment["results"]
    payload["analysisEnrichment"] = enrichment["summary"]

    chronic = [e for e in enrichment_excluded if e.get("consecutiveFailures", 0) >= 3]
    payload["meta"] = {
        "enrichment_excluded": enrichment_excluded,
        "chronic_failure_candidates": chronic,
    }
    payload["enrichment"] = {
        "applied": True,
        "tool": "screen_stocks_enrich",
    }
    return payload
