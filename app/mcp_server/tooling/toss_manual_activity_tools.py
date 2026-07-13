"""ROB-866 — MCP tool: detect Toss manual (unbooked) trading activity.

Toss has no execution websocket, so operator app-side manual trades are invisible
until reported. ``toss_detect_manual_activity`` diffs Toss GET /orders against the
ledger + proposal rungs to surface manual orders. dry_run (default) returns findings
only (zero writes); dry_run=False sends a Telegram alert + hands off to
session_context and records an idempotency marker so the same order is not
re-alerted. This is detection + alert only — no fill/journal bookkeeping (stage 2).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.config import validate_toss_api_config
from app.services.toss_manual_activity import run_manual_activity_sweep

if TYPE_CHECKING:
    from fastmcp import FastMCP

TOSS_MANUAL_ACTIVITY_TOOL_NAMES = frozenset({"toss_detect_manual_activity"})

_MIN_WINDOW_HOURS = 1
_MAX_WINDOW_HOURS = 168  # 7 days — bounds the CLOSED pagination cost.


async def toss_detect_manual_activity(
    window_hours: int = 24, dry_run: bool = True
) -> dict[str, Any]:
    """Surface Toss orders absent from the ledger + proposal rungs.

    Args:
        window_hours: lookback window for CLOSED orders (1–168h, default 24).
        dry_run: when True (default), returns findings only — no Telegram, no
            session_context, no marker writes. When False, alerts and records.
    """

    missing = validate_toss_api_config()
    if missing:
        return {
            "success": False,
            "source": "toss",
            "error": (
                "Toss Open API is disabled or missing required configuration: "
                + ", ".join(missing)
            ),
            "missing_env": missing,
        }

    try:
        window = int(window_hours)
    except (TypeError, ValueError):
        window = 24
    window = max(_MIN_WINDOW_HOURS, min(window, _MAX_WINDOW_HOURS))

    return await run_manual_activity_sweep(window_hours=window, dry_run=bool(dry_run))


def register_toss_manual_activity_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="toss_detect_manual_activity",
        description=(
            "Detect Toss manual (app-side) trades the system has not booked. Diffs "
            "Toss GET /orders (CLOSED + OPEN) over a lookback window against the "
            "toss_live_order_ledger and proposal rungs. dry_run=True (default) "
            "returns findings only (zero writes). dry_run=False sends a Telegram "
            "alert + session_context handoff and records an idempotency marker so "
            "the same order is not re-alerted. Read-only against the broker; no fill "
            "bookkeeping (that is a separate stage)."
        ),
    )(toss_detect_manual_activity)
