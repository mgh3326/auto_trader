"""ROB-388: screen_stocks(kr) snapshot-primary adapter.

Reads the kr_market_ranking durable read-model (InvestMomentumEvent snapshots via
MomentumRankingQueryService) and adapts it to the screen_stocks row/response shape,
with honest freshness. Self-acquires its DB session (mirrors screener_snapshot_tool)
and is fail-open: any error / ineligible sort_by / zero rows -> None, so the caller
falls through to the live (tvscreener -> legacy KRX) path.
"""

from __future__ import annotations

import datetime as dt
import logging
from dataclasses import dataclass, field
from typing import Any

from app.services.invest_momentum_events.query_service import Freshness, RankingRow

logger = logging.getLogger(__name__)

# sort_by values the kr_market_ranking read-model can serve. Everything else
# (dividend_yield, week_change_rate, rsi, score, ...) must go to the live path.
SNAPSHOT_ELIGIBLE_SORTS: frozenset[str] = frozenset(
    {"change_rate", "volume", "trade_amount", "market_cap"}
)

# Momentum read-model is bucketed by order_type. "up" = 상승(change_rate),
# "quantTop" = 거래량(volume). trade_amount / market_cap have no native bucket, so we
# union the two default-collected buckets and re-sort by the requested field.
_ORDER_TYPE_BY_SORT: dict[str, tuple[str, ...]] = {
    "change_rate": ("up",),
    "volume": ("quantTop",),
    "trade_amount": ("up", "quantTop"),
    "market_cap": ("up", "quantTop"),
}


def is_snapshot_eligible_sort(sort_by: str) -> bool:
    return sort_by in SNAPSHOT_ELIGIBLE_SORTS


def order_types_for_sort(sort_by: str) -> tuple[str, ...]:
    return _ORDER_TYPE_BY_SORT.get(sort_by, ())


def snapshot_path_applicable(
    *,
    market: str,
    asset_type: str | None,
    category: str | None,
    sector: str | None,
    min_market_cap: float | None,
    max_per: float | None,
    max_pbr: float | None,
    min_dividend_yield: float | None,
    min_analyst_buy: float | None,
    max_rsi: float | None,
    adv_krw_min: int | None,
    market_cap_min_krw: int | None,
    market_cap_max_krw: int | None,
    instrument_types: list[str] | None,
    exclude_sectors: list[str] | None,
) -> bool:
    """True iff this is a plain KR-wide ranking query the kr_market_ranking read-model
    can faithfully serve.

    The read-model is KR-wide (KOSPI+KOSDAQ) common stocks with no quality fields, so:
    - a market sub-filter (kospi/kosdaq/konex) would mislabel KR-wide rows,
    - an ETF/category/sector constraint or any quality filter cannot be honored.
    In those cases this returns False so the caller uses the live path (which honors
    them) instead of silently returning unfiltered / mislabeled snapshot rows.
    """
    if market not in {"kr", "all"}:
        return False
    if asset_type not in (None, "stock"):
        return False
    if category is not None or sector is not None:
        return False
    if any(
        v is not None
        for v in (
            min_market_cap,
            max_per,
            max_pbr,
            min_dividend_yield,
            min_analyst_buy,
            max_rsi,
            adv_krw_min,
            market_cap_min_krw,
            market_cap_max_krw,
        )
    ):
        return False
    if instrument_types or exclude_sectors:
        return False
    return True


def _opt_float(value: float | int | None) -> float | None:
    return float(value) if value is not None else None


def ranking_row_to_screen_row(row: RankingRow) -> dict[str, Any]:
    """Map one RankingRow to the screen_stocks result-row shape. Pure; null-safe;
    never fabricates valuation fields (per/pbr/dividend_yield default to None and
    are filled best-effort later by enrichment)."""
    code = row.symbol
    return {
        "symbol": code,
        "short_code": code,
        "code": code,
        "name": row.name or code,
        "price": _opt_float(row.price),
        "change_rate": _opt_float(row.change_rate),
        "volume": _opt_float(row.volume),
        "trade_amount": _opt_float(row.trade_value),
        "market_cap": _opt_float(row.market_cap),
        "market": "kr",
        "per": None,
        "pbr": None,
        "dividend_yield": None,
        "instrument_type": "stock",
    }


def dedupe_and_sort_rows(
    rows: list[dict[str, Any]], *, sort_by: str, sort_order: str
) -> list[dict[str, Any]]:
    """Dedupe by symbol (first wins) and sort by the requested field. None sorts last
    regardless of order. Used for trade_amount / market_cap which have no native
    momentum bucket (we union 'up'+'quantTop' then re-rank)."""
    seen: set[str] = set()
    deduped: list[dict[str, Any]] = []
    for r in rows:
        sym = r.get("symbol")
        if sym in seen:
            continue
        seen.add(sym)
        deduped.append(r)

    reverse = sort_order != "asc"

    def key(r: dict[str, Any]) -> tuple[int, float]:
        v = r.get(sort_by)
        if v is None:
            # None always last: sort to the extreme opposite of the active direction
            return (1, float("-inf") if reverse else float("inf"))
        return (0, float(v))

    return sorted(deduped, key=key, reverse=reverse)


def freshness_to_meta(
    freshness: Freshness, *, row_count: int
) -> tuple[str, dict[str, Any], list[str]]:
    """Map momentum Freshness to (data_state, meta_fields, warnings) for screen_stocks.
    'unavailable' is handled by the caller (returns None -> live), so only fresh/stale
    produce a response here."""
    data_state = freshness.overall
    meta: dict[str, Any] = {
        "data_state": data_state,
        "source": "kr_market_ranking",
        "latest_snapshot_at": (
            freshness.latest_snapshot_at.isoformat()
            if freshness.latest_snapshot_at is not None
            else None
        ),
    }
    warnings: list[str] = [
        f"모멘텀 랭킹 상위 {row_count}종목 기반 — 전체 KRX 스캔이 아닙니다."
    ]
    if data_state == "stale":
        meta["stale_reason"] = freshness.stale_reason
        meta["retryable"] = False
        meta["reason"] = "kr_market_ranking_stale"
        warnings.append(
            "모멘텀 랭킹 스냅샷이 오래되었습니다"
            f"({freshness.stale_reason}) — 신규 후보 발굴에 주의하세요."
        )
    return data_state, meta, warnings


@dataclass(frozen=True)
class KrRankingSnapshotResult:
    rows: list[dict[str, Any]]
    total_count: int
    data_state: str  # "fresh" | "stale"
    source: str
    latest_snapshot_at: str | None
    warnings: list[str] = field(default_factory=list)
    meta_fields: dict[str, Any] = field(default_factory=dict)


def _build_query_service(session: Any) -> Any:
    from app.services.invest_momentum_events.query_service import (
        MomentumRankingQueryService,
    )
    from app.services.invest_momentum_events.repository import (
        InvestMomentumEventSnapshotsRepository,
    )

    return MomentumRankingQueryService(InvestMomentumEventSnapshotsRepository(session))


async def _load_universe_by_code() -> dict[str, dict[str, Any]]:
    from app.services.krx import fetch_stock_all_cached

    out: dict[str, dict[str, Any]] = {}
    for mkt in ("STK", "KSQ"):
        for item in await fetch_stock_all_cached(market=mkt):
            code = str(item.get("short_code") or item.get("code") or "").strip()
            if code:
                out[code] = item
    return out


async def _load_valuation_by_code() -> dict[str, dict[str, Any]]:
    from app.services.krx import fetch_valuation_all_cached

    return await fetch_valuation_all_cached(market="ALL")


async def _enrich_rows(
    rows: list[dict[str, Any]],
    *,
    universe_by_code: dict[str, dict[str, Any]] | None = None,
    valuation_by_code: dict[str, dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Best-effort enrich snapshot rows with code/sector/instrument_type + per/pbr/
    dividend_yield from KRX universe/valuation caches. Fail-open and no fabrication:
    on any cache error or missing key, leave fields as-is (null)."""
    from app.mcp_server.tooling.screening.instrument_type import classify_kr_instrument

    try:
        if universe_by_code is None:
            universe_by_code = await _load_universe_by_code()
        if valuation_by_code is None:
            valuation_by_code = await _load_valuation_by_code()
    except Exception:  # noqa: BLE001 — enrichment is best-effort
        return rows

    for r in rows:
        code = r.get("symbol") or ""
        base = universe_by_code.get(code) or {}
        val = valuation_by_code.get(code) or {}
        if base.get("code"):
            r["code"] = base["code"]
        if base.get("sector") and not r.get("sector"):
            r["sector"] = base["sector"]
        r["instrument_type"] = classify_kr_instrument(
            code, r.get("name"), base.get("subtype")
        )
        if r.get("per") is None:
            r["per"] = val.get("per")
        if r.get("pbr") is None:
            r["pbr"] = val.get("pbr")
        if r.get("dividend_yield") is None:
            r["dividend_yield"] = val.get("dividend_yield")
    return rows


async def load_kr_ranking_snapshot(
    *,
    sort_by: str,
    sort_order: str,
    limit: int,
    now: dt.datetime | None = None,
    query_service: Any | None = None,
    enrich: bool = True,
) -> KrRankingSnapshotResult | None:
    """Primary KR discovery source for screen_stocks. Returns a result with rows
    (fresh OR stale, honestly labeled) or None (ineligible sort_by / zero rows /
    any error) so the caller falls through to the live path. Fail-open by design."""
    if not is_snapshot_eligible_sort(sort_by):
        return None
    if now is None:
        now = dt.datetime.now(dt.UTC)

    order_types = order_types_for_sort(sort_by)
    try:
        if query_service is not None:
            return await _run(
                query_service,
                sort_by,
                sort_order,
                limit,
                now,
                order_types,
                enrich,
                None,
            )
        from app.core.db import AsyncSessionLocal

        async with AsyncSessionLocal() as session:
            qs = _build_query_service(session)
            return await _run(
                qs, sort_by, sort_order, limit, now, order_types, enrich, session
            )
    except Exception as exc:  # fail-open: never break screen_stocks
        logger.debug("kr_market_ranking snapshot unavailable, falling back: %s", exc)
        return None


async def _run(
    qs: Any,
    sort_by: str,
    sort_order: str,
    limit: int,
    now: dt.datetime,
    order_types: tuple[str, ...],
    enrich: bool,
    session: Any | None,
) -> KrRankingSnapshotResult | None:
    collected: list[dict[str, Any]] = []
    freshnesses: list[Freshness] = []
    for ot in order_types:
        ranking = await qs.get_ranking(
            order_type=ot, market="kr", limit=max(limit, 50), now=now
        )
        freshnesses.append(ranking.freshness)
        collected.extend(ranking_row_to_screen_row(r) for r in ranking.rows)

    if not collected:
        return None  # unavailable -> live fallthrough

    rows = dedupe_and_sort_rows(collected, sort_by=sort_by, sort_order=sort_order)
    rows = rows[:limit]

    # Worst freshness wins (stale beats fresh when buckets disagree).
    overall = "stale" if any(f.overall == "stale" for f in freshnesses) else "fresh"
    base = next((f for f in freshnesses if f.overall == overall), freshnesses[0])

    if enrich and session is not None:
        rows = await _enrich_rows(rows)  # defined in Task 5

    data_state, meta, warnings = freshness_to_meta(base, row_count=len(rows))
    return KrRankingSnapshotResult(
        rows=rows,
        total_count=len(rows),
        data_state=data_state,
        source="kr_market_ranking",
        latest_snapshot_at=meta.get("latest_snapshot_at"),
        warnings=warnings,
        meta_fields=meta,
    )
