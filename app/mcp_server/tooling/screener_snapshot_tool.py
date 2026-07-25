"""ROB-439 MVP (PR3): snapshot-backed screener MCP tool.

`screen_stocks` is the generic tvscreener/KIS candidate-discovery path. This tool
serves the /invest/screener *snapshot* data and lets a caller adjust/add AND-filters
over a preset's base snapshot (the "필터를 추가/조정" model), reusing the same
ScreenerFilterDefinition catalog + build_screener_results path the web screener uses.

Read-only: build_screener_results never mutates broker/order/watch state. Filters
currently thread through the consecutive_gainers loader (ROB-439 PR2); other presets
return their default snapshot results (and say so), expanding as more presets get wired.
"""

from __future__ import annotations

import functools
import logging
from typing import Any, Literal, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal

logger = logging.getLogger(__name__)


class _HeldResolver:
    """Held-aware resolver for MCP rows backed by KIS live positions."""

    def __init__(self, held_symbols: set[tuple[str, str]]) -> None:
        self._h = held_symbols

    def relation(self, market: str, symbol: str) -> str:
        key = ((market or "").strip().lower(), _normalize_symbol_key(symbol))
        return "held" if key in self._h else "none"


def _session_factory() -> async_sessionmaker[AsyncSession]:
    return cast(async_sessionmaker[AsyncSession], cast(object, AsyncSessionLocal))


def _available_filters(preset: str) -> dict[str, dict[str, Any]]:
    """The adjustable filter catalog for a preset's base snapshot (empty if none)."""
    from app.services.invest_view_model.screener_filters import (
        SNAPSHOT_FILTER_FIELDS,
        snapshot_kind_for_preset,
    )

    kind = snapshot_kind_for_preset(preset)
    if not kind or kind not in SNAPSHOT_FILTER_FIELDS:
        return {}
    return {
        field: {
            "label": d.label,
            "operator": d.operator,
            "valueType": d.value_type,
            "default": d.default,
            "min": d.min_bound,
            "max": d.max_bound,
            "step": d.step,
            "unit": d.unit,
        }
        for field, d in SNAPSHOT_FILTER_FIELDS[kind].items()
    }


# ROB-445: presets whose filter_overrides build_screener_results actually CONSUMES.
# Mirrors the `if filter_overrides:` dispatch branches in screener_service:
#   - consecutive_gainers: consecutive_gainers loader thresholds — ANY market
#     (the branch has no market gate; it lives inside `if preset_id == "consecutive_gainers"`)
#   - oversold_recovery (ROB-543): an `rsi <= N` override tightens max_rsi — ANY market
#   - crypto presets: apply_filter_conditions — ONLY when market == "crypto"
# Every other preset (incl. high_yield_value, which HAS a snapshot_kind but no dispatch
# branch) silently drops filters → must warn. The old `snapshot_kind is None` guard missed
# exactly high_yield_value (it has a kind), which is the ROB-445 silent no-op.
_THREADED_ANY_MARKET: frozenset[str] = frozenset(
    {"consecutive_gainers", "oversold_recovery"}
)


def _filters_are_threaded(preset: str, market: str) -> bool:
    """True iff build_screener_results actually threads filter_overrides for this
    (preset, market). Source of truth: the two filter_overrides dispatch branches in
    screener_service. Used to emit an honest '필터 미적용' warning for every other preset."""
    if preset in _THREADED_ANY_MARKET:
        return True
    from app.services.invest_view_model.screener_filters import _CRYPTO_PRESET_IDS

    return (market or "").strip().lower() == "crypto" and preset in _CRYPTO_PRESET_IDS


def _normalize_symbol_key(symbol: object) -> str:
    from app.core.symbol import to_db_symbol

    raw = str(symbol or "").strip().upper()
    try:
        return to_db_symbol(raw).upper()
    except Exception:
        return raw


def _normalize_preset_ids(
    raw_preset: str | None, presets: list[str] | None = None
) -> list[str]:
    """Split preset inputs, preserving order while deduping."""
    raw: list[str] = []
    if raw_preset:
        raw.extend(str(raw_preset).split(","))
    for item in presets or []:
        raw.extend(str(item).split(","))

    out: list[str] = []
    seen: set[str] = set()
    for value in raw:
        preset_id = value.strip()
        if not preset_id or preset_id in seen:
            continue
        seen.add(preset_id)
        out.append(preset_id)
    return out


def _merge_rows(
    existing: list[dict[str, Any]], new_rows: list[dict[str, Any]], preset_id: str
) -> list[dict[str, Any]]:
    """Dedupe by symbol and merge matchedPresets.

    The first preset to see a symbol 'wins' on display fields (rank, name, labels).
    Subsequent matches only append to matchedPresets.
    """
    merged = list(existing)
    seen_symbols = {
        (
            str(r.get("market") or "").strip().lower(),
            _normalize_symbol_key(r.get("symbol")),
        ): r
        for r in merged
    }

    for row in new_rows:
        key = (
            str(row.get("market") or "").strip().lower(),
            _normalize_symbol_key(row.get("symbol")),
        )
        if key in seen_symbols:
            existing_row = seen_symbols[key]
            matched = list(existing_row.get("matchedPresets") or [])
            if preset_id not in matched:
                matched.append(preset_id)
            existing_row["matchedPresets"] = matched
        else:
            row["matchedPresets"] = [preset_id]
            merged.append(row)
            seen_symbols[key] = row

    return merged


def _holding_market_filter(market: str) -> str | None:
    normalized = (market or "").strip().lower()
    if normalized == "kr":
        return "equity_kr"
    if normalized == "us":
        return "equity_us"
    return None


def _filter_min_market_cap_with_warning(
    rows: list[dict[str, Any]], min_val: float
) -> tuple[list[dict[str, Any]], int]:
    kept: list[dict[str, Any]] = []
    missing_count = 0
    for row in rows:
        raw = row.get("marketCapValue")
        if raw is None:
            missing_count += 1
            continue
        try:
            if float(raw) >= min_val:
                kept.append(row)
        except (TypeError, ValueError):
            missing_count += 1
    return kept, missing_count


_DEFAULT_RESULT_LIMIT = 40
_MAX_RESULT_LIMIT = 200
_MAX_PRESET_SWEEP_COUNT = 5
_MAX_ANALYST_ENRICHMENT_ROWS = 200


async def screen_stocks_snapshot_impl(
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
    limit: int = _DEFAULT_RESULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Run a screener preset over its base snapshot with adjustable AND-filters.

    filters: list of {"field", "operator" (gte|lte|eq), "value"} conditions applied
    on top of the preset's starting set (adjust same field, add new). Returns the
    screener payload plus availableFilters (the adjustable catalog) and appliedFilters.

    exclude_watched/held: ROB-515 discovery workflow — hide already-processed symbols.
    exclude_symbols: explicit symbols to remove after dedupe.
    min_analyst_count: ROB-515 quality filter — consensus total coverage threshold.
    min_analyst_buy_count: backward-compatible buy-count threshold.
    min_market_cap: raw numeric marketCapValue threshold (KRW for KR, USD for US).
    min/max_market_cap_eok: ROB-515 size filter — unit is 1억원 (KRW).

    sort: "matched_presets_desc" ranks multi-preset intersections first.

    limit/offset: ROB-465 — results are capped (default 40, max 200) and paginated
    at the tool boundary to keep responses inside the MCP token budget. The full
    match count and a next_offset cursor are reported under ``pagination``.
    """
    from app.mcp_server.tooling.portfolio_holdings import _collect_kis_positions
    from app.services.invest_view_model.screener_filters import (
        ScreenerFilterCondition,
        ScreenerFilterError,
        snapshot_kind_for_preset,
    )
    from app.services.invest_view_model.screener_service import build_screener_results
    from app.services.screener_service import ScreenerService

    preset_ids = _normalize_preset_ids(preset, presets)
    if not preset_ids:
        return {"error": "preset or presets must not be empty", "results": []}

    if len(preset_ids) > _MAX_PRESET_SWEEP_COUNT:
        return {
            "error": (
                "too many presets for screen_stocks_snapshot sweep; "
                f"maximum is {_MAX_PRESET_SWEEP_COUNT}"
            ),
            "preset": preset,
            "presets": preset_ids,
            "results": [],
        }

    # ROB-515: mark 'held' rows in screener results via KIS live positions.
    # (Watchlist personalization is still omitted for discovery MCP).
    held_symbols: set[tuple[str, str]] = set()
    holdings_meta = {"source": "kis_live", "status": "ok", "held_count": 0}
    holdings_market_filter = _holding_market_filter(market)
    if holdings_market_filter is not None:
        try:
            # MCP always runs live (is_mock=False)
            pos, holdings_warnings = await _collect_kis_positions(
                holdings_market_filter, is_mock=False
            )
            if holdings_warnings:
                holdings_meta["status"] = "error" if not pos else "partial"
                holdings_meta["warning_count"] = len(holdings_warnings)
                logger.warning(
                    "screener_snapshot: kis holdings returned warnings: %s",
                    holdings_warnings,
                )
            else:
                holdings_meta["warning_count"] = 0

            held_symbols = {
                (
                    str(p.get("market") or market).strip().lower(),
                    _normalize_symbol_key(p.get("symbol")),
                )
                for p in pos
                if p.get("symbol")
            }
            holdings_meta["held_count"] = len(held_symbols)
        except Exception as exc:  # noqa: BLE001
            holdings_meta["status"] = "error"
            holdings_meta["warning_count"] = 1
            # surface as a non-fatal warning so results still return
            # (build_screener_results takes resolver as non-optional, so fall back to noop)
            logger.warning("screener_snapshot: kis holdings failed: %s", exc)

    # Use the first preset for adjustable filter catalog metadata
    main_preset_id = preset_ids[0]
    available = _available_filters(main_preset_id)
    snapshot_kind = snapshot_kind_for_preset(main_preset_id)

    conditions: list[ScreenerFilterCondition] = []
    for entry in filters or []:
        try:
            conditions.append(
                ScreenerFilterCondition(
                    field=str(entry["field"]),
                    operator=str(entry["operator"]),
                    value=entry["value"],
                )
            )
        except (KeyError, TypeError) as exc:
            return {
                "error": f"invalid filter entry {entry!r}: {exc}",
                "preset": preset,
                "availableFilters": available,
                "results": [],
            }

    merged_results: list[dict[str, Any]] = []
    combined_warnings: list[str] = []
    threaded_warned_presets: set[str] = set()

    try:
        async with _session_factory()() as db:
            for pid in preset_ids:
                resp = await build_screener_results(
                    preset_id=pid,
                    screening_service=ScreenerService(),
                    resolver=_HeldResolver(held_symbols),
                    market=market,
                    session=db,
                    filter_overrides=conditions or None,
                )
                raw_payload = resp.model_dump(mode="json")
                merged_results = _merge_rows(
                    merged_results, raw_payload.get("results") or [], pid
                )
                for w in raw_payload.get("warnings") or []:
                    if w not in combined_warnings:
                        combined_warnings.append(w)

                if conditions and not _filters_are_threaded(pid, market):
                    threaded_warned_presets.add(pid)
    except ScreenerFilterError as exc:
        return {
            "error": str(exc),
            "preset": preset,
            "availableFilters": available,
            "results": [],
        }

    # Discovery filters (exclude)
    if exclude_watched:
        combined_warnings.append(
            "exclude_watched는 MCP snapshot 도구에서 아직 사용자 watchlist를 "
            "배선하지 않아 지원하지 않습니다 (필터 미적용)."
        )
        merged_results = [r for r in merged_results if not r.get("isWatched")]
    # ROB-543 Slice B: surface how many held rows exclude_held removed so the
    # caller can reason about coverage (0 when the filter is off).
    excluded_held_count = 0
    if exclude_held:
        excluded_held_count = sum(1 for r in merged_results if r.get("isHeld"))
        merged_results = [r for r in merged_results if not r.get("isHeld")]
    if exclude_symbols:
        excluded_symbols = {_normalize_symbol_key(s) for s in exclude_symbols}
        merged_results = [
            r
            for r in merged_results
            if _normalize_symbol_key(r.get("symbol")) not in excluded_symbols
        ]

    # Discovery filters (market cap)
    if min_market_cap is not None:
        min_val = float(min_market_cap)
        merged_results, missing_count = _filter_min_market_cap_with_warning(
            merged_results, min_val
        )
        if missing_count:
            combined_warnings.append(
                f"min_market_cap 적용 중 marketCapValue 결측 {missing_count}개 행을 제외했습니다."
            )
    if min_market_cap_eok is not None:
        min_val = float(min_market_cap_eok) * 100_000_000
        merged_results, missing_count = _filter_min_market_cap_with_warning(
            merged_results, min_val
        )
        if missing_count:
            combined_warnings.append(
                f"min_market_cap_eok 적용 중 marketCapValue 결측 {missing_count}개 행을 제외했습니다."
            )
    if max_market_cap_eok is not None:
        max_val = float(max_market_cap_eok) * 100_000_000
        merged_results = [
            r for r in merged_results if (r.get("marketCapValue") or 0) <= max_val
        ]

    # Intersection sort
    if sort == "matched_presets_desc":
        merged_results.sort(
            key=lambda r: len(r.get("matchedPresets") or []), reverse=True
        )

    # ROB-445: warn whenever filters were passed but NOT actually threaded for the
    # resolved (preset, market) — REGARDLESS of snapshotKind.
    if threaded_warned_presets:
        p_list = ", ".join(sorted(threaded_warned_presets))
        combined_warnings.append(
            f"'{p_list}' 프리셋은 아직 스냅샷 위 필터 조정이 배선되지 않아 "
            "기본 결과를 반환했습니다 (필터 미적용)."
        )

    payload: dict[str, Any] = {
        "presetId": preset_ids[0] if len(preset_ids) == 1 else "multi",
        "presets": preset_ids,
        "results": merged_results,
        "warnings": combined_warnings,
        "availableFilters": available,
        "appliedFilters": [
            {"field": c.field, "operator": c.operator, "value": c.value}
            for c in conditions
        ],
        "snapshotKind": snapshot_kind,
        "holdings": holdings_meta,
        # ROB-543 Slice B: number of held rows exclude_held dropped (0 when off).
        "excluded_held_count": excluded_held_count,
        "discoveryFilters": {
            "exclude_watched": exclude_watched,
            "exclude_held": exclude_held,
            "exclude_symbols": [
                _normalize_symbol_key(s) for s in (exclude_symbols or [])
            ],
            "min_analyst_count": min_analyst_count,
            "min_analyst_buy_count": min_analyst_buy_count,
            "min_market_cap": min_market_cap,
            "min_market_cap_eok": min_market_cap_eok,
            "max_market_cap_eok": max_market_cap_eok,
            "sort": sort,
        },
    }
    if holdings_meta["status"] != "ok":
        combined_warnings.append(
            "KIS live 보유종목 확인 실패 — 보유 여부가 표시되지 않을 수 있습니다."
        )

    # ROB-465: cap + paginate at the tool boundary so large snapshots (e.g.
    # high_yield_value ~161 rows / ~84k chars) don't blow the MCP token budget.
    all_results = payload.get("results") or []
    total_available = len(all_results)
    eff_limit = max(1, min(int(limit), _MAX_RESULT_LIMIT))
    eff_offset = max(0, int(offset))

    # ROB-686: per-call memo shared by both the counts resolver and the page
    # enrichment provider so a symbol resolved once (cache/live) is never
    # re-fetched within the same tool call.
    memo: dict[str, Any] = {}

    # ROB-686: min_analyst_* now resolves consensus COUNTS once via the KR
    # Redis cache-aside (bounded by _MAX_ANALYST_ENRICHMENT_ROWS), filters,
    # paginates, and only THEN full-enriches the returned page — replacing the
    # old ROB-515 behavior of live-enriching the entire matched set (up to 200
    # rows) before pagination.
    if min_analyst_count is not None or min_analyst_buy_count is not None:
        if len(all_results) > _MAX_ANALYST_ENRICHMENT_ROWS:
            return {
                "error": (
                    "analyst enrichment row cap exceeded; narrow presets, "
                    "market-cap filters, or exclude_symbols before applying analyst filters"
                ),
                "preset": preset,
                "presets": preset_ids,
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

        from app.core import analyze_cache
        from app.services.invest_view_model import analyst_consensus_cache

        redis_client = await analyze_cache._get_redis_client()
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

        from app.services.invest_view_model.screener_analysis_enrichment import (
            enrich_snapshot_page,
        )

        enrichment = await enrich_snapshot_page(
            rows=page,
            market=market,
            session_factory=_session_factory(),
            opinion_provider=functools.partial(
                analyst_consensus_cache.cached_opinion_provider,
                redis_client=redis_client,
                memo=memo,
            ),
        )
        page = enrichment["results"]
        payload["analysisEnrichment"] = enrichment["summary"]
    else:
        page = all_results[eff_offset : eff_offset + eff_limit]

    next_offset = eff_offset + len(page)
    payload["results"] = page
    payload["pagination"] = {
        "total_available": total_available,
        "returned_count": len(page),
        "offset": eff_offset,
        "limit": eff_limit,
        "has_more": next_offset < total_available,
        "next_offset": next_offset if next_offset < total_available else None,
    }

    if min_analyst_count is None and min_analyst_buy_count is None:
        from app.core import analyze_cache
        from app.services.invest_view_model import analyst_consensus_cache
        from app.services.invest_view_model.screener_analysis_enrichment import (
            enrich_snapshot_page,
        )

        redis_client = await analyze_cache._get_redis_client()
        enrichment = await enrich_snapshot_page(
            rows=page,
            market=market,
            session_factory=_session_factory(),
            opinion_provider=functools.partial(
                analyst_consensus_cache.cached_opinion_provider,
                redis_client=redis_client,
                memo=memo,
            ),
        )
        payload["results"] = enrichment["results"]
        payload["analysisEnrichment"] = enrichment["summary"]

    return payload
