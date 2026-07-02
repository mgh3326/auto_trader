"""Read-only get_trading_policy MCP tool (ROB-646).

Echoes market x lane judgment thresholds plus the policy version stamp
({version, content_hash}). Consumers cite the stamp so a verdict record can
recover "what criteria did we judge under?". Operator edits via PR only —
there is no write tool."""

from __future__ import annotations

from typing import Any

from app.services.trading_policy_service import (
    TradingPolicyKeyError,
    get_policy_for,
)


async def get_trading_policy(market: str, lane: str) -> dict[str, Any]:
    """Return trading-policy thresholds for a market x lane, plus the version stamp."""
    try:
        view = get_policy_for(market, lane)
    except TradingPolicyKeyError as exc:
        return {"success": False, "error": "unknown_key", "detail": str(exc)}
    return {"success": True, **view}
