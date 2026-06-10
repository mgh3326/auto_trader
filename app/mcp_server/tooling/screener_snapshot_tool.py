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

from typing import Any, cast

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from app.core.db import AsyncSessionLocal


class _NoopResolver:
    """MCP candidate-discovery has no user context, so nothing is 'watched'.

    build_screener_results only needs ``relation(market, symbol) -> str``; returning
    "none" makes every row isWatched=False (no user personalization in MCP)."""

    def relation(self, market: str, symbol: str) -> str:  # noqa: ARG002
        return "none"


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


_DEFAULT_RESULT_LIMIT = 40
_MAX_RESULT_LIMIT = 200


async def screen_stocks_snapshot_impl(
    *,
    preset: str,
    market: str = "kr",
    filters: list[dict[str, Any]] | None = None,
    limit: int = _DEFAULT_RESULT_LIMIT,
    offset: int = 0,
) -> dict[str, Any]:
    """Run a screener preset over its base snapshot with adjustable AND-filters.

    filters: list of {"field", "operator" (gte|lte|eq), "value"} conditions applied
    on top of the preset's starting set (adjust same field, add new). Returns the
    screener payload plus availableFilters (the adjustable catalog) and appliedFilters.

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

    available = _available_filters(preset)
    snapshot_kind = snapshot_kind_for_preset(preset)

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

    try:
        async with _session_factory()() as db:
            resp = await build_screener_results(
                preset_id=preset,
                screening_service=ScreenerService(),
                resolver=_NoopResolver(),
                market=market,
                session=db,
                filter_overrides=conditions or None,
            )
    except ScreenerFilterError as exc:
        return {
            "error": str(exc),
            "preset": preset,
            "availableFilters": available,
            "results": [],
        }

    payload: dict[str, Any] = resp.model_dump(mode="json")
    payload["availableFilters"] = available
    payload["appliedFilters"] = [
        {"field": c.field, "operator": c.operator, "value": c.value} for c in conditions
    ]
    payload["snapshotKind"] = snapshot_kind
    # ROB-445: warn whenever filters were passed but NOT actually threaded for the
    # resolved (preset, market) — REGARDLESS of snapshotKind. The old `snapshot_kind
    # is None` predicate let high_yield_value (kind=market_valuation_snapshots, but
    # no dispatch branch) echo appliedFilters while silently returning the unfiltered
    # snapshot (silent no-op). Now the unfiltered fact is reported honestly.
    if conditions and not _filters_are_threaded(preset, market):
        warnings = list(payload.get("warnings") or [])
        warnings.append(
            f"'{preset}' 프리셋은 아직 스냅샷 위 필터 조정이 배선되지 않아 "
            "기본 결과를 반환했습니다 (필터 미적용)."
        )
        payload["warnings"] = warnings

    # ROB-465: cap + paginate at the tool boundary so large snapshots (e.g.
    # high_yield_value ~161 rows / ~84k chars) don't blow the MCP token budget.
    # The web screener path (build_screener_results) is left untouched.
    all_results = payload.get("results") or []
    total_available = len(all_results)
    eff_limit = max(1, min(int(limit), _MAX_RESULT_LIMIT))
    eff_offset = max(0, int(offset))
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
