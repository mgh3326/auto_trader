"""Pure fill-safety analysis for multi-rung limit ladders (ROB-477 / ROB-507).

No broker calls, no I/O, no DB. Shared by:
- app/services/action_report/us/order_preview.py (ROB-244 preview gate)
- app/mcp_server/tooling/orders_registration.py (sell_ladder_fill_preview /
  buy_ladder_fill_preview tools)

Semantics (side="sell", the ROB-477 original):
- ladder_all_above_market: every VALID rung is strictly above the anchor.
- ladder_missing_near_market_anchor: no VALID rung is marketable (<= anchor)
  NOR near-above-market (above anchor but within the near threshold).

Semantics (side="buy", the ROB-507 mirror):
- ladder_all_below_market: every VALID rung is strictly below the anchor —
  zero-fill tail risk in a rally.
- ladder_missing_near_market_anchor: no VALID rung is marketable (>= anchor)
  NOR near-below-market (below anchor but within the near threshold).

Rungs with limit_price <= 0 are invalid: excluded from every aggregate and
never satisfy the anchor requirement.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_NEAR_MARKET_ANCHOR_PCT = 0.3
DEFAULT_NEAR_MARKET_ANCHOR_ATR_MULTIPLE = 0.3

WARNING_ALL_ABOVE_MARKET = "ladder_all_above_market"
WARNING_ALL_BELOW_MARKET = "ladder_all_below_market"
WARNING_MISSING_NEAR_MARKET_ANCHOR = "ladder_missing_near_market_anchor"


@dataclass(frozen=True)
class LadderRung:
    limit_price: float
    quantity: float | None = None


def _round_price(value: float) -> float:
    return round(value, 4)


def evaluate_ladder_fill_safety(
    *,
    rungs: Sequence[LadderRung],
    anchor_price: float | None,
    side: str = "sell",
    anchor_source: str | None = None,
    atr: float | None = None,
    near_market_pct: float = DEFAULT_NEAR_MARKET_ANCHOR_PCT,
    near_market_atr_multiple: float = DEFAULT_NEAR_MARKET_ANCHOR_ATR_MULTIPLE,
) -> tuple[list[str], dict[str, Any] | None]:
    """Return (warnings, details) for a limit ladder on the given side.

    Returns ([], None) when there is nothing to analyze: no valid rungs or a
    missing/non-positive anchor. Detail keys stay camelCase; the side="sell"
    key set is byte-compatible with the PR #1228 fillSafety payload, while
    side="buy" uses the mirrored belowMarket/nearBelowMarket keys. The
    per-rung risk distance is signed away-from-market: rung - anchor for
    sells, anchor - rung for buys (positive = fill-miss direction).
    """
    if side not in ("sell", "buy"):
        raise ValueError(f"side must be 'sell' or 'buy', got {side!r}")
    if anchor_price is None or anchor_price <= 0:
        return [], None

    valid_rungs = [rung for rung in rungs if rung.limit_price > 0]
    invalid_rung_count = len(rungs) - len(valid_rungs)
    if not valid_rungs:
        return [], None

    near_threshold_usd = anchor_price * near_market_pct / 100.0
    if atr is not None and atr > 0:
        near_threshold_usd = max(near_threshold_usd, atr * near_market_atr_multiple)

    is_sell = side == "sell"
    away_key = "aboveMarket" if is_sell else "belowMarket"
    near_away_key = "nearAboveMarket" if is_sell else "nearBelowMarket"
    all_away_detail_key = "allRungsAboveMarket" if is_sell else "allRungsBelowMarket"
    all_away_warning = WARNING_ALL_ABOVE_MARKET if is_sell else WARNING_ALL_BELOW_MARKET

    rung_details: list[dict[str, Any]] = []
    all_away_from_market = True
    has_marketable_anchor = False
    has_near_market_anchor = False

    for index, rung in enumerate(rungs, start=1):
        if rung.limit_price <= 0:
            rung_details.append(
                {
                    "index": index,
                    "quantity": rung.quantity,
                    "limitPriceUsd": _round_price(rung.limit_price),
                    "invalid": True,
                }
            )
            continue
        if is_sell:
            distance_usd = rung.limit_price - anchor_price
        else:
            distance_usd = anchor_price - rung.limit_price
        distance_pct = distance_usd / anchor_price * 100.0
        away_from_market = distance_usd > 0
        marketable_anchor = not away_from_market
        near_away_from_market = away_from_market and distance_usd <= near_threshold_usd
        is_near_market_anchor = marketable_anchor or near_away_from_market
        atr_multiple = distance_usd / atr if atr is not None and atr > 0 else None

        all_away_from_market = all_away_from_market and away_from_market
        has_marketable_anchor = has_marketable_anchor or marketable_anchor
        has_near_market_anchor = has_near_market_anchor or is_near_market_anchor
        rung_details.append(
            {
                "index": index,
                "quantity": rung.quantity,
                "limitPriceUsd": _round_price(rung.limit_price),
                "distanceUsd": _round_price(distance_usd),
                "distancePct": round(distance_pct, 4),
                "atrMultiple": (
                    round(atr_multiple, 4) if atr_multiple is not None else None
                ),
                away_key: away_from_market,
                "marketableAnchor": marketable_anchor,
                near_away_key: near_away_from_market,
                "nearMarketAnchor": is_near_market_anchor,
                "invalid": False,
            }
        )

    warnings: list[str] = []
    if all_away_from_market:
        warnings.append(all_away_warning)
    if not has_near_market_anchor:
        warnings.append(WARNING_MISSING_NEAR_MARKET_ANCHOR)

    details: dict[str, Any] = {
        "anchorPriceUsd": _round_price(anchor_price),
        "anchorSource": anchor_source,
        "nearMarketThresholdPct": near_market_pct,
        "nearMarketThresholdUsd": _round_price(near_threshold_usd),
        "nearMarketAtrMultiple": near_market_atr_multiple,
        "atrUsd": _round_price(atr) if atr is not None else None,
        all_away_detail_key: all_away_from_market,
        "hasMarketableAnchor": has_marketable_anchor,
        "hasNearMarketAnchor": has_near_market_anchor,
        "invalidRungCount": invalid_rung_count,
        "rungs": rung_details,
    }
    if warnings:
        details["suggestedAnchorRung"] = {
            "limitPriceUsd": _round_price(anchor_price),
            "rationale": (
                f"place at least one {side} rung at or near the anchor price "
                "(within the near-market threshold) to secure a partial fill"
            ),
        }

    return warnings, details
