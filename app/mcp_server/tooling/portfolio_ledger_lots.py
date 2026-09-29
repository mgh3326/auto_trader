"""Task #963 — thin MCP-layer attach for ``get_holdings(include_ledger_lots=True)``.

All logic lives in ``app.services.execution_ledger.kis_lots``; this module only
selects the KIS live KR positions already present in a ``get_holdings`` response,
hands their broker quantity/price to the service, and writes the returned block
onto each position as ``ledger_lots``. No broker call. It never raises: any
failure leaves ``ledger_state="unknown"`` blocks instead of failing get_holdings.
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.execution_ledger.kis_lots import (
    COST_METHOD,
    UNKNOWN_LOAD_FAILED,
    PositionRef,
    load_kis_live_kr_lot_blocks,
    to_decimal,
    unknown_block,
)

logger = logging.getLogger(__name__)

LEDGER_LOTS_SCOPE = "kis_live_kr_positions"


def _eligible_positions(response: dict[str, Any]) -> list[dict[str, Any]]:
    """KIS live KR broker positions (never Toss, manual, US, mock or crypto)."""
    eligible: list[dict[str, Any]] = []
    for group in response.get("accounts") or []:
        if group.get("broker") != "kis" or group.get("account_mode") != "kis_live":
            continue
        for position in group.get("positions") or []:
            if position.get("source") == "kis_api" and position.get("market") == "kr":
                eligible.append(position)
    return eligible


async def attach_ledger_lots(
    response: dict[str, Any], *, kis_live_routing: bool
) -> None:
    """Attach ``ledger_lots`` to eligible positions and a top-level summary."""
    try:
        summary: dict[str, Any] = {
            "requested": True,
            "scope": LEDGER_LOTS_SCOPE,
            "cost_method": COST_METHOD,
            "external_orders_verifiable": False,
            "applied": False,
            "positions_covered": 0,
        }
        response["ledger_lots"] = summary
        if not kis_live_routing:
            summary["reason"] = "kis_live_kr_only"
            return
        positions = _eligible_positions(response)
        summary["applied"] = True
        summary["positions_covered"] = len(positions)
        if not positions:
            return
        refs = [
            PositionRef(
                symbol=str(position.get("symbol") or ""),
                reference_quantity=to_decimal(position.get("quantity")),
                current_price=to_decimal(position.get("current_price")),
            )
            for position in positions
        ]
        blocks: dict[str, dict[str, Any]] = {}
        try:
            async with AsyncSessionLocal() as db:
                blocks = await load_kis_live_kr_lot_blocks(db, refs)
        except Exception:  # noqa: BLE001 — the block must never fail get_holdings
            logger.warning("ledger lots read failed", exc_info=True)
        for position in positions:
            symbol = str(position.get("symbol") or "")
            position["ledger_lots"] = blocks.get(symbol) or unknown_block(
                symbol, UNKNOWN_LOAD_FAILED
            )
    except Exception:  # noqa: BLE001 — never fail the holdings response
        logger.warning("ledger lots attach failed", exc_info=True)
        response["ledger_lots"] = {
            "requested": True,
            "scope": LEDGER_LOTS_SCOPE,
            "cost_method": COST_METHOD,
            "external_orders_verifiable": False,
            "applied": False,
            "positions_covered": 0,
            "reason": UNKNOWN_LOAD_FAILED,
        }
        with contextlib.suppress(Exception):
            for position in _eligible_positions(response):
                position.setdefault(
                    "ledger_lots",
                    unknown_block(
                        str(position.get("symbol") or ""), UNKNOWN_LOAD_FAILED
                    ),
                )
