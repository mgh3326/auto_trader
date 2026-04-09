"""Portfolio rotation plan service.

Classifies crypto positions into sell/locked/ignored buckets based on
strategy signals and trade journal context, then fetches screener-based
buy candidates.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

LOCKED_STRATEGIES: frozenset[str] = frozenset({
    "coinmoogi_dca",
    "staking_hold",
    "index_dca",
})
DUST_THRESHOLD_KRW: float = 5_000
PARTIAL_REDUCE_PCT: int = 30


class PortfolioRotationService:
    """Build rotation plans for crypto portfolios."""

    async def build_rotation_plan(
        self,
        *,
        market: str = "crypto",
        account: str | None = None,
    ) -> dict[str, Any]:
        if market != "crypto":
            return {
                "supported": False,
                "market": market,
                "warning": "Rotation plan is currently supported for crypto only.",
            }

        return {
            "supported": True,
            "market": "crypto",
            "account": account or "upbit",
            "generated_at": None,
            "summary": {
                "total_positions": 0,
                "actionable_positions": 0,
                "locked_positions": 0,
                "ignored_positions": 0,
                "buy_candidates": 0,
            },
            "sell_candidates": [],
            "buy_candidates": [],
            "locked_positions": [],
            "ignored_positions": [],
            "warnings": [],
        }
