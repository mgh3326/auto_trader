"""ROB-439 MVP (PR3): snapshot-backed screener MCP tool.

`screen_stocks` is the generic tvscreener/KIS candidate-discovery path. This tool
serves the /invest/screener *snapshot* data and lets a caller adjust/add AND-filters
over a preset's base snapshot (the "필터를 추가/조정" model), reusing the same
ScreenerFilterDefinition catalog + build_screener_results path the web screener uses.

Read-only: build_screener_results never mutates broker/order/watch state. Filters
currently thread through the consecutive_gainers loader (ROB-439 PR2); other presets
return their default snapshot results (and say so), expanding as more presets get wired.

ROB-1309: `screen_stocks_snapshot_impl` is DB-only by contract — it makes zero
external HTTP calls for any (market, preset) combination. The row-building /
filtering / pagination logic it shares with the live-enrichment tool lives in
`_build_snapshot_page` below (imported by `screener_enrich_tool.py`). Sector
lazy-fill, analyst-consensus fetching, and any other provider network calls
were moved out to the explicit `screen_stocks_enrich` MCP tool
(`app/mcp_server/tooling/screener_enrich_tool.py`) — see that module's
docstring and the root `CLAUDE.md` "screen_stocks_snapshot / screen_stocks_enrich
MCP 도구 분리 (ROB-1309)" section for the full before/after contract. This
tool's only network-adjacent call is the KIS-live
holdings lookup used for `isHeld` marking (`_collect_kis_positions`), which
predates ROB-1309, is bounded (one call, not a per-row fan-out), and is
explicitly out of this issue's scope (W2 — holdings/portfolio code).
"""

from __future__ import annotations

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
# ROB-1309: analyst-enrichment row cap moved to screener_enrich_tool (the only
# consumer of analyst-count filtering now). Re-exported here for backward
# compatibility with anything importing it from this module.
_MAX_ANALYST_ENRICHMENT_ROWS = 200


async def _build_snapshot_page(
    *,
    preset: str | None,
    presets: list[str] | None,
    market: str,
    filters: list[dict[str, Any]] | None,
    exclude_watched: bool,
    exclude_held: bool,
    exclude_symbols: list[str] | None,
    min_market_cap: float | None,
    min_market_cap_eok: float | None,
    max_market_cap_eok: float | None,
    sort: Literal["matched_presets_desc"] | None,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Build + filter + paginate a snapshot page — the DB-only core shared by
    ``screen_stocks_snapshot_impl`` (no enrichment) and ``screen_stocks_enrich_impl``
    (enrichment layered on top of this function's ``page``/``all_results``).

    Makes ZERO external HTTP calls: DB reads (invest_screener_snapshots via
    build_screener_results) plus one bounded KIS-live holdings lookup for
    isHeld marking (pre-existing, out of ROB-1309 scope — see module docstring).

    Returns a dict with keys: ``payload`` (the response dict sans results/
    pagination/enrichment), ``page`` (the paginated row slice), ``all_results``
    (full filtered/sorted set, pre-pagination — enrichment callers need this to
    run analyst-count filtering before paginating), ``eff_offset``, ``eff_limit``,
    ``total_available``, ``error`` (short-circuit dict to return verbatim, or
    None).
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
        return {
            "error": {"error": "preset or presets must not be empty", "results": []}
        }

    if len(preset_ids) > _MAX_PRESET_SWEEP_COUNT:
        return {
            "error": {
                "error": (
                    "too many presets for screen_stocks_snapshot sweep; "
                    f"maximum is {_MAX_PRESET_SWEEP_COUNT}"
                ),
                "preset": preset,
                "presets": preset_ids,
                "results": [],
            }
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
                "error": {
                    "error": f"invalid filter entry {entry!r}: {exc}",
                    "preset": preset,
                    "availableFilters": available,
                    "results": [],
                }
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
            "error": {
                "error": str(exc),
                "preset": preset,
                "availableFilters": available,
                "results": [],
            }
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

    return {
        "error": None,
        "payload": payload,
        "page": page,
        "all_results": all_results,
        "eff_offset": eff_offset,
        "eff_limit": eff_limit,
        "total_available": total_available,
        "preset": preset,
        "preset_ids": preset_ids,
        "market": market,
    }


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

    ROB-1309: this is the DB-only path — it makes ZERO external HTTP calls
    (sector lazy-fill, analyst-consensus fetch, quoteSummary/timeseries/crumb,
    etc). Results carry only what's already persisted in the snapshot tables;
    there is no ``analysisContext``/``analystLabel`` and no sector-lazy-fill
    ``category`` backfill. For live enrichment (analyst consensus, sector
    labels, ``min_analyst_count``/``min_analyst_buy_count`` filtering), call
    the separate ``screen_stocks_enrich`` MCP tool
    (``app/mcp_server/tooling/screener_enrich_tool.py::screen_stocks_enrich_impl``)
    on the page this tool returns.

    filters: list of {"field", "operator" (gte|lte|eq), "value"} conditions applied
    on top of the preset's starting set (adjust same field, add new). Returns the
    screener payload plus availableFilters (the adjustable catalog) and appliedFilters.

    exclude_watched/held: ROB-515 discovery workflow — hide already-processed symbols.
    exclude_symbols: explicit symbols to remove after dedupe.
    min_market_cap: raw numeric marketCapValue threshold (KRW for KR, USD for US).
    min/max_market_cap_eok: ROB-515 size filter — unit is 1억원 (KRW).

    min_analyst_count / min_analyst_buy_count: NOT supported here (ROB-1309) —
    passing either returns a fail-closed error pointing at ``screen_stocks_enrich``
    rather than silently ignoring the filter or making a network call.

    sort: "matched_presets_desc" ranks multi-preset intersections first.

    limit/offset: ROB-465 — results are capped (default 40, max 200) and paginated
    at the tool boundary to keep responses inside the MCP token budget. The full
    match count and a next_offset cursor are reported under ``pagination``.
    """
    if min_analyst_count is not None or min_analyst_buy_count is not None:
        return {
            "error": (
                "min_analyst_count/min_analyst_buy_count require live analyst-"
                "consensus enrichment and are no longer supported by "
                "screen_stocks_snapshot (ROB-1309: DB-only, zero external HTTP "
                "by default). Call screen_stocks_enrich with the same filters "
                "instead."
            ),
            "redirectTool": "screen_stocks_enrich",
            "preset": preset,
            "presets": presets,
            "results": [],
        }

    built = await _build_snapshot_page(
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
    payload["discoveryFilters"]["min_analyst_count"] = None
    payload["discoveryFilters"]["min_analyst_buy_count"] = None
    # ROB-1309: explicit, non-silent contract marker — this tool never enriches.
    payload["enrichment"] = {
        "applied": False,
        "tool": "screen_stocks_enrich",
        "reason": (
            "screen_stocks_snapshot is DB-only by contract (ROB-1309); call "
            "screen_stocks_enrich for sector labels / analyst consensus / "
            "min_analyst_* filtering."
        ),
    }
    return payload
