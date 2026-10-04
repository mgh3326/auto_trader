"""Task #963 — thin MCP-layer attach for ``get_holdings(include_ledger_lots=True)``.

All logic lives in ``app.services.execution_ledger.kis_lots``; this module only
selects the KIS live KR and (task #1173) KIS live US positions already present
in a ``get_holdings`` response, hands their broker quantity/price to the
service, and writes the returned block onto each position as ``ledger_lots``.
No broker call. It never raises: any failure leaves ``ledger_state="unknown"``
blocks instead of failing get_holdings. KR and US are read in separate sessions
so a failure on one market never degrades the other's blocks.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Awaitable, Callable, Sequence
from typing import Any

from app.core.db import AsyncSessionLocal
from app.services.execution_ledger.kis_lots import (
    COST_METHOD,
    UNKNOWN_LOAD_FAILED,
    MarketCode,
    PositionRef,
    load_kis_live_kr_lot_blocks,
    load_kis_live_us_lot_blocks,
    to_decimal,
    unknown_block,
)

logger = logging.getLogger(__name__)

LEDGER_LOTS_SCOPE = "kis_live_kr_us_positions"
_MARKETS: tuple[MarketCode, ...] = ("kr", "us")


def _eligible_positions(
    response: dict[str, Any], market: MarketCode
) -> list[dict[str, Any]]:
    """KIS live broker positions of one market (never Toss, manual, mock or crypto)."""
    eligible: list[dict[str, Any]] = []
    for group in response.get("accounts") or []:
        if group.get("broker") != "kis" or group.get("account_mode") != "kis_live":
            continue
        for position in group.get("positions") or []:
            if position.get("source") == "kis_api" and position.get("market") == market:
                eligible.append(position)
    return eligible


def _loader(
    market: MarketCode,
) -> Callable[[Any, Sequence[PositionRef]], Awaitable[dict[str, dict[str, Any]]]]:
    # Resolved at call time so tests can monkeypatch the module attributes.
    if market == "us":
        return load_kis_live_us_lot_blocks
    return load_kis_live_kr_lot_blocks


async def _attach_market(positions: list[dict[str, Any]], market: MarketCode) -> None:
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
            blocks = await _loader(market)(db, refs)
    except Exception:  # noqa: BLE001 — the block must never fail get_holdings
        logger.warning("ledger lots read failed market=%s", market, exc_info=True)
    for position in positions:
        symbol = str(position.get("symbol") or "")
        position["ledger_lots"] = blocks.get(symbol) or _unknown(symbol, market)


def _unknown(symbol: str, market: MarketCode) -> dict[str, Any]:
    # The KR call keeps the pre-#1173 signature so the KR block is unchanged.
    if market == "us":
        return unknown_block(symbol, UNKNOWN_LOAD_FAILED, market="us")
    return unknown_block(symbol, UNKNOWN_LOAD_FAILED)


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
            "positions_covered_by_market": dict.fromkeys(_MARKETS, 0),
        }
        response["ledger_lots"] = summary
        if not kis_live_routing:
            summary["reason"] = "kis_live_only"
            return
        by_market = {
            market: _eligible_positions(response, market) for market in _MARKETS
        }
        summary["applied"] = True
        summary["positions_covered"] = sum(len(p) for p in by_market.values())
        summary["positions_covered_by_market"] = {
            market: len(positions) for market, positions in by_market.items()
        }
        for market, positions in by_market.items():
            if positions:
                await _attach_market(positions, market)
    except Exception:  # noqa: BLE001 — never fail the holdings response
        logger.warning("ledger lots attach failed", exc_info=True)
        response["ledger_lots"] = {
            "requested": True,
            "scope": LEDGER_LOTS_SCOPE,
            "cost_method": COST_METHOD,
            "external_orders_verifiable": False,
            "applied": False,
            "positions_covered": 0,
            "positions_covered_by_market": dict.fromkeys(_MARKETS, 0),
            "reason": UNKNOWN_LOAD_FAILED,
        }
        with contextlib.suppress(Exception):
            for market in _MARKETS:
                for position in _eligible_positions(response, market):
                    position.setdefault(
                        "ledger_lots",
                        _unknown(str(position.get("symbol") or ""), market),
                    )
