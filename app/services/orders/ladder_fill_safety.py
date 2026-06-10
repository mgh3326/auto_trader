"""Pure fill-safety analysis for multi-rung sell limit ladders (ROB-477).

No broker calls, no I/O, no DB. Shared by:
- app/services/action_report/us/order_preview.py (ROB-244 preview gate)
- app/mcp_server/tooling/orders_registration.py (sell_ladder_fill_preview tool)

Semantics:
- ladder_all_above_market: every VALID rung is strictly above the anchor.
- ladder_missing_near_market_anchor: no VALID rung is marketable (<= anchor)
  NOR near-above-market (above anchor but within the near threshold).
- Rungs with limit_price <= 0 are invalid: excluded from every aggregate and
  never satisfy the anchor requirement.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

DEFAULT_NEAR_MARKET_ANCHOR_PCT = 0.3
DEFAULT_NEAR_MARKET_ANCHOR_ATR_MULTIPLE = 0.3

WARNING_ALL_ABOVE_MARKET = "ladder_all_above_market"
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
    anchor_source: str | None = None,
    atr: float | None = None,
    near_market_pct: float = DEFAULT_NEAR_MARKET_ANCHOR_PCT,
    near_market_atr_multiple: float = DEFAULT_NEAR_MARKET_ANCHOR_ATR_MULTIPLE,
) -> tuple[list[str], dict[str, Any] | None]:
    """Return (warnings, details) for a sell limit ladder.

    Returns ([], None) when there is nothing to analyze: no valid rungs or a
    missing/non-positive anchor. Detail keys stay camelCase for byte-compat
    with the PR #1228 fillSafety payload.
    """
    if anchor_price is None or anchor_price <= 0:
        return [], None

    valid_rungs = [rung for rung in rungs if rung.limit_price > 0]
    invalid_rung_count = len(rungs) - len(valid_rungs)
    if not valid_rungs:
        return [], None

    near_threshold_usd = anchor_price * near_market_pct / 100.0
    if atr is not None and atr > 0:
        near_threshold_usd = max(near_threshold_usd, atr * near_market_atr_multiple)

    rung_details: list[dict[str, Any]] = []
    all_above_market = True
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
        distance_usd = rung.limit_price - anchor_price
        distance_pct = distance_usd / anchor_price * 100.0
        above_market = rung.limit_price > anchor_price
        marketable_anchor = not above_market
        near_above_market = above_market and distance_usd <= near_threshold_usd
        is_near_market_anchor = marketable_anchor or near_above_market
        atr_multiple = distance_usd / atr if atr is not None and atr > 0 else None

        all_above_market = all_above_market and above_market
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
                "aboveMarket": above_market,
                "marketableAnchor": marketable_anchor,
                "nearAboveMarket": near_above_market,
                "nearMarketAnchor": is_near_market_anchor,
                "invalid": False,
            }
        )

    warnings: list[str] = []
    if all_above_market:
        warnings.append(WARNING_ALL_ABOVE_MARKET)
    if not has_near_market_anchor:
        warnings.append(WARNING_MISSING_NEAR_MARKET_ANCHOR)

    details: dict[str, Any] = {
        "anchorPriceUsd": _round_price(anchor_price),
        "anchorSource": anchor_source,
        "nearMarketThresholdPct": near_market_pct,
        "nearMarketThresholdUsd": _round_price(near_threshold_usd),
        "nearMarketAtrMultiple": near_market_atr_multiple,
        "atrUsd": _round_price(atr) if atr is not None else None,
        "allRungsAboveMarket": all_above_market,
        "hasMarketableAnchor": has_marketable_anchor,
        "hasNearMarketAnchor": has_near_market_anchor,
        "invalidRungCount": invalid_rung_count,
        "rungs": rung_details,
    }
    if warnings:
        details["suggestedAnchorRung"] = {
            "limitPriceUsd": _round_price(anchor_price),
            "rationale": (
                "place at least one sell rung at or near the anchor price "
                "(within the near-market threshold) to secure a partial fill"
            ),
        }

    return warnings, details
