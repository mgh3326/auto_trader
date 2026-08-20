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
4. Wraps EVERY provider-fetch call this tool makes — sector fetch, the
   per-page consensus fetch used by `enrich_snapshot_page`, AND the
   consensus-COUNT resolver used by `min_analyst_count`/`min_analyst_buy_count`
   filtering (`resolve_consensus_counts`, step 2 above) — with a bounded,
   TTL'd negative cache (`enrichment_negative_cache.py`) so a symbol that
   just failed (delisted, renamed, parse-miss, rate-limited) — INCLUDING a
   "no data" non-exception return like `(None, None)` from the sector
   fetchers, not just a raised exception — is not re-fetched, and re-failed,
   on every subsequent call within the retry-suppression window. Every
   skip/failure is reported under `meta.enrichment_excluded` (never a
   silent drop of the ROW: `results` never has a row vanish without a
   corresponding report). Symbols with >=3 consecutive failures (tracked
   across retry windows via a longer-lived history TTL — see
   `enrichment_negative_cache.NEGATIVE_CACHE_HISTORY_TTL_SECONDS`) ARE
   additionally excluded from `results` on THIS call — the
   `meta.halted_suspect_excluded`-style pattern the ROB-1309 ticket named
   explicitly: per-call candidate-pool cleanup + non-silent reporting under
   `meta.chronic_failure_candidates`, not a permanent hide (the exclusion
   self-heals the moment the fetch succeeds again — `record_success` clears
   the entry). This tool never mutates `kr_symbol_universe` /
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
        # ROB-1309 checkpoint fix: _fetch_kr_sector/_fetch_us_sector return
        # (None, None) — NOT an exception — for "no sector data found" (the
        # exact delisted/unrecognized-symbol case this cache exists for).
        # Treating that as success would mean a permanently-no-data symbol
        # is retried forever; it must be recorded as a failure instead.
        if result == (None, None):
            recorded = await negcache.record_failure(
                redis_client,
                kind=kind,
                market=market,
                symbol=symbol,
                exc=RuntimeError("sector data not found"),
            )
            excluded.append(
                {
                    "symbol": symbol,
                    "market": market,
                    "kind": kind,
                    "reason": "no_data",
                    "errorClass": recorded.error_class,
                    "consecutiveFailures": recorded.consecutive_failures,
                }
            )
            return result
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


def _raw_opinion_fetcher_with_negative_cache(
    *,
    market: str,
    redis_client: Any,
    excluded: list[dict[str, Any]],
) -> Any:
    """Negative-cache-aware wrapper for the RAW (uncached-by-itself)
    opinion_fetcher signature `resolve_consensus`/`resolve_consensus_counts`
    expect — checkpoint fix (ROB-1309 addendum): the min_analyst_count/
    min_analyst_buy_count path called `resolve_consensus_counts` with the
    default (unwrapped) `handle_get_investment_opinions`, so a symbol that
    just failed there was never recorded and got retried on every single
    call, unlike the per-page sector/consensus fetchers used by
    `enrich_snapshot_page`. This wraps the SAME kind="consensus" negative
    cache bucket as `_opinion_provider_with_negative_cache` — a failure
    recorded by one path is honored by the other.

    Round-3 fix: `handle_get_investment_opinions` is awaited inside a
    try/except HERE, not left bare — a raise must be caught at this
    boundary, because `resolve_consensus` (the caller of this
    `opinion_fetcher`) has its own outer try/except that would otherwise
    swallow the exception before this wrapper's negcache bookkeeping
    (record_failure / excluded.append) ever runs, silently disabling the
    negative cache for exactly the timeout/rate-limit case it exists for."""
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

        from app.mcp_server.tooling.fundamentals._valuation import (
            handle_get_investment_opinions,
        )

        try:
            result = await handle_get_investment_opinions(
                symbol=symbol, market=market, limit=limit
            )
        except Exception as exc:  # noqa: BLE001 — round-3 fix: a raise here must
            # be caught HERE, not left to propagate into
            # analyst_consensus_cache.resolve_consensus's own outer
            # try/except, which would swallow it before this wrapper's
            # negcache bookkeeping (record_failure/excluded.append) runs.
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
            return {"error": str(exc) or "analyst_consensus_unavailable"}
        consensus = (
            (result or {}).get("consensus") if isinstance(result, dict) else None
        )
        if not (
            isinstance(consensus, dict)
            and analyst_consensus_cache._is_meaningful_consensus(consensus)
        ):
            recorded = await negcache.record_failure(
                redis_client,
                kind=kind,
                market=market,
                symbol=symbol,
                exc=RuntimeError(
                    str((result or {}).get("error") or "analyst_consensus_unavailable")
                ),
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
    `symbol_sectors_service`), analyst-consensus fetch (KR AND US both via
    the Redis cache-aside in `analyst_consensus_cache.py` — a cache hit
    skips the provider on either market), a live KIS holdings lookup for
    `isHeld`/`exclude_held` (the one call `screen_stocks_snapshot` no
    longer makes), and — only for the final returned page — a fresh
    lightweight current-price fetch for target-upside recomputation.

    min_analyst_count / min_analyst_buy_count: filters on consensus COUNTS,
    resolved BEFORE pagination (bounded to `_MAX_ANALYST_ENRICHMENT_ROWS`
    merged rows) — same semantics as the pre-ROB-1309 screen_stocks_snapshot.

    Negative caching: a symbol whose sector/consensus fetch just failed is
    not retried within the retry-block TTL (`NEGATIVE_CACHE_TTL_SECONDS`,
    30 min); every skip/failure is reported under `meta.enrichment_excluded`.
    Below the chronic threshold this only costs the specific enrichment
    field (row stays in `results`). At/above `_CHRONIC_FAILURE_THRESHOLD`
    (3 consecutive failures, tracked for `NEGATIVE_CACHE_HISTORY_TTL_SECONDS`
    = 24h) the row IS removed from THIS call's `results` — but always
    non-silently via `meta.chronic_failure_candidates`/`chronic_excluded_count`,
    never a DB/permanent mutation, and self-healing: the very next
    `record_success` (or either TTL lapsing) clears the negative-cache entry
    and the symbol is included again on the next call. See module docstring
    for the full contract, including the explicit non-mutation of
    `kr_symbol_universe`/`us_symbol_universe`/`symbol_sectors` beyond the
    pre-existing lazy-fill write path.
    """
    held_symbols, holdings_meta = await _snapshot_tool._collect_holdings_for_market(
        market
    )
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
        held_symbols=held_symbols,
        holdings_meta=holdings_meta,
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
            opinion_fetcher=_raw_opinion_fetcher_with_negative_cache(
                market=market, redis_client=redis_client, excluded=enrichment_excluded
            ),
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
    enriched_results = enrichment["results"]

    # ROB-1309 checkpoint fix ("negative cache + universe cleanup", read as
    # the same per-call candidate-pool-cleanup pattern the ticket named
    # explicitly via meta.halted_suspect_excluded — not a DB mutation, see
    # module docstring): a symbol with >= chronic threshold consecutive
    # failures is removed from THIS call's results, non-silently, via
    # meta.chronic_failure_candidates. It is never permanently hidden — the
    # very next successful fetch (record_success) clears the negative-cache
    # entry and the symbol is included again on the next call.
    chronic = [e for e in enrichment_excluded if e.get("consecutiveFailures", 0) >= 3]
    chronic_symbols = {(e["market"], e["symbol"].upper()) for e in chronic}
    if chronic_symbols:
        kept_results = [
            row
            for row in enriched_results
            if (
                (str(row.get("market") or market).strip().lower()),
                str(row.get("symbol") or "").strip().upper(),
            )
            not in chronic_symbols
        ]
    else:
        kept_results = enriched_results
    chronic_excluded_count = len(enriched_results) - len(kept_results)

    payload["results"] = kept_results
    payload["analysisEnrichment"] = enrichment["summary"]
    if payload.get("pagination") is not None and chronic_excluded_count:
        payload["pagination"]["returned_count"] = len(kept_results)

    payload["meta"] = {
        "enrichment_excluded": enrichment_excluded,
        "chronic_failure_candidates": chronic,
        "chronic_excluded_count": chronic_excluded_count,
    }
    payload["enrichment"] = {
        "applied": True,
        "tool": "screen_stocks_enrich",
    }
    return payload
