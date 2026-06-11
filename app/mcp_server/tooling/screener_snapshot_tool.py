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

import logging
from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal

logger = logging.getLogger(__name__)


class _HeldResolver:
    """Held-aware resolver for MCP.マーク rows as held via KIS live positions."""

    def __init__(self, held_symbols: set[str]) -> None:
        self._h = held_symbols

    def relation(self, market: str, symbol: str) -> str:  # noqa: ARG002
        return "held" if symbol.upper() in self._h else "none"


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
# Mirrors the two `if filter_overrides:` dispatch branches in screener_service:
#   - consecutive_gainers (~1789): consecutive_gainers loader thresholds — ANY market
#     (the branch has no market gate; it lives inside `if preset_id == "consecutive_gainers"`)
#   - crypto presets (~1966): apply_filter_conditions — ONLY when market == "crypto"
# Every other preset (incl. high_yield_value, which HAS a snapshot_kind but no dispatch
# branch) silently drops filters → must warn. The old `snapshot_kind is None` guard missed
# exactly high_yield_value (it has a kind), which is the ROB-445 silent no-op.
_THREADED_ANY_MARKET: frozenset[str] = frozenset({"consecutive_gainers"})


def _filters_are_threaded(preset: str, market: str) -> bool:
    """True iff build_screener_results actually threads filter_overrides for this
    (preset, market). Source of truth: the two filter_overrides dispatch branches in
    screener_service. Used to emit an honest '필터 미적용' warning for every other preset."""
    if preset in _THREADED_ANY_MARKET:
        return True
    from app.services.invest_view_model.screener_filters import _CRYPTO_PRESET_IDS

    return (market or "").strip().lower() == "crypto" and preset in _CRYPTO_PRESET_IDS


def _normalize_preset_ids(raw_preset: str) -> list[str]:
    """Split comma-separated preset IDs and strip whitespace."""
    return [p.strip() for p in raw_preset.split(",") if p.strip()]


def _merge_rows(
    existing: list[dict[str, Any]], new_rows: list[dict[str, Any]], preset_id: str
) -> list[dict[str, Any]]:
    """Dedupe by symbol and merge matchedPresets.

    The first preset to see a symbol 'wins' on display fields (rank, name, labels).
    Subsequent matches only append to matchedPresets.
    """
    merged = list(existing)
    seen_symbols = {str(r.get("symbol")).upper(): r for r in merged}

    for row in new_rows:
        symbol = str(row.get("symbol")).upper()
        if symbol in seen_symbols:
            existing_row = seen_symbols[symbol]
            matched = list(existing_row.get("matchedPresets") or [])
            if preset_id not in matched:
                matched.append(preset_id)
            existing_row["matchedPresets"] = matched
        else:
            row["matchedPresets"] = [preset_id]
            merged.append(row)
            seen_symbols[symbol] = row

    return merged


_DEFAULT_RESULT_LIMIT = 40
_MAX_RESULT_LIMIT = 200


async def screen_stocks_snapshot_impl(
    *,
    preset: str,
    market: str = "kr",
    filters: list[dict[str, Any]] | None = None,
    exclude_watched: bool = False,
    exclude_held: bool = False,
    min_analyst_buy_count: int | None = None,
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
    min_analyst_buy_count: ROB-515 quality filter — consensus-buy count threshold.
    min/max_market_cap_eok: ROB-515 size filter — unit is 1억원 (KRW).

    sort: "matched_presets_desc" ranks multi-preset intersections first.

    limit/offset: ROB-465 — results are capped (default 40, max 200) and paginated
    at the tool boundary to keep responses inside the MCP token budget. The full
    match count and a next_offset cursor are reported under ``pagination``.
    """
    from app.services.invest_view_model.screener_filters import (
        ScreenerFilterCondition,
        ScreenerFilterError,
        snapshot_kind_for_preset,
    )
    from app.services.invest_view_model.screener_service import build_screener_results
    from app.services.screener_service import ScreenerService
    from app.mcp_server.tooling.portfolio_holdings import _collect_kis_positions

    preset_ids = _normalize_preset_ids(preset)
    if not preset_ids:
        return {"error": "preset must not be empty", "results": []}

    # ROB-515: mark 'held' rows in screener results via KIS live positions.
    # (Watchlist personalization is still omitted for discovery MCP).
    held_symbols: set[str] = set()
    holdings_meta = {"source": "kis_live", "status": "ok", "held_count": 0}
    if market == "kr":
        try:
            # MCP always runs live (is_mock=False)
            pos, _w = await _collect_kis_positions("equity_kr", is_mock=False)
            held_symbols = {str(p.get("symbol")).upper() for p in pos if p.get("symbol")}
            holdings_meta["held_count"] = len(held_symbols)
        except Exception as exc:  # noqa: BLE001
            holdings_meta["status"] = "error"
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
        merged_results = [r for r in merged_results if not r.get("isWatched")]
    if exclude_held:
        merged_results = [r for r in merged_results if not r.get("isHeld")]

    # Discovery filters (market cap) — unit is 1억원
    if min_market_cap_eok is not None:
        min_val = float(min_market_cap_eok) * 100_000_000
        merged_results = [
            r
            for r in merged_results
            if (r.get("marketCapValue") or 0) >= min_val
        ]
    if max_market_cap_eok is not None:
        max_val = float(max_market_cap_eok) * 100_000_000
        merged_results = [
            r
            for r in merged_results
            if (r.get("marketCapValue") or 0) <= max_val
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
        "presetId": preset,
        "results": merged_results,
        "warnings": combined_warnings,
        "availableFilters": available,
        "appliedFilters": [
            {"field": c.field, "operator": c.operator, "value": c.value}
            for c in conditions
        ],
        "snapshotKind": snapshot_kind,
        "holdings": holdings_meta,
    }
    if holdings_meta["status"] == "error":
        combined_warnings.append(
            "KIS live 보유종목 확인 실패 — 보유 여부가 표시되지 않을 수 있습니다."
        )

    # ROB-465: cap + paginate at the tool boundary so large snapshots (e.g.
    # high_yield_value ~161 rows / ~84k chars) don't blow the MCP token budget.
    all_results = payload.get("results") or []
    total_available = len(all_results)
    eff_limit = max(1, min(int(limit), _MAX_RESULT_LIMIT))
    eff_offset = max(0, int(offset))

    # ROB-515: if analyst filtering is requested, we must enrich BEFORE pagination
    # so we can filter on the enriched buyCount across the whole set.
    if min_analyst_buy_count is not None:
        from app.services.invest_view_model.screener_analysis_enrichment import (
            enrich_snapshot_page,
        )

        enrichment = await enrich_snapshot_page(
            rows=all_results,
            market=market,
            session_factory=_session_factory(),
        )
        all_results = enrichment["results"]
        # Update match total after analyst filter
        min_buy = int(min_analyst_buy_count)
        all_results = [
            r
            for r in all_results
            if (
                r.get("analysisContext", {})
                .get("consensus", {})
                .get("buyCount")
                 or 0
            ) >= min_buy
        ]
        total_available = len(all_results)
        page = all_results[eff_offset : eff_offset + eff_limit]
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

    if min_analyst_buy_count is None:
        from app.services.invest_view_model.screener_analysis_enrichment import (
            enrich_snapshot_page,
        )

        enrichment = await enrich_snapshot_page(
            rows=page,
            market=market,
            session_factory=_session_factory(),
        )
        payload["results"] = enrichment["results"]
        payload["analysisEnrichment"] = enrichment["summary"]

    return payload
