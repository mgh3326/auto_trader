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

from app.services.invest_momentum_events.query_service import RankingRow


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

