"""ROB-388: screen_stocks(kr) snapshot-primary adapter.

Reads the kr_market_ranking durable read-model (InvestMomentumEvent snapshots via
MomentumRankingQueryService) and adapts it to the screen_stocks row/response shape,
with honest freshness. Self-acquires its DB session (mirrors screener_snapshot_tool)
and is fail-open: any error / ineligible sort_by / zero rows -> None, so the caller
falls through to the live (tvscreener -> legacy KRX) path.
"""

from __future__ import annotations

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


from typing import Any

from app.services.invest_momentum_events.query_service import Freshness, RankingRow


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


