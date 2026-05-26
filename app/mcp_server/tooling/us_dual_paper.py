"""Read-only US dual-paper MCP tools (ROB-326). No submit/cancel/modify surface."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.schemas.us_dual_paper import DualPaperBrokerStatus
from app.services.us_dual_paper.adapters.alpaca import AlpacaPaperAdapter
from app.services.us_dual_paper.adapters.base import BrokerPreviewAdapter
from app.services.us_dual_paper.adapters.kis_mock import KisMockUsAdapter
from app.services.us_dual_paper.capability_matrix import get_capability_matrix
from app.services.us_dual_paper.packet import build_packet

if TYPE_CHECKING:
    from fastmcp import FastMCP

US_DUAL_PAPER_TOOL_NAMES: set[str] = {
    "us_dual_paper_capability_matrix",
    "us_dual_paper_account_states",
    "us_dual_paper_preview",
}


def _adapters() -> list[BrokerPreviewAdapter]:
    return [KisMockUsAdapter(), AlpacaPaperAdapter()]


async def us_dual_paper_capability_matrix() -> dict[str, Any]:
    """Return the read-only capability matrix for kis_mock + alpaca_paper (US)."""
    return {"submit_enabled": False, "matrix": get_capability_matrix()}


async def us_dual_paper_account_states() -> dict[str, Any]:
    """Read-only account states for both paper brokers. Counts/numbers only — no secrets."""
    out: dict[str, Any] = {"submit_enabled": False, "brokers": {}}
    for adapter in _adapters():
        scope = adapter.account_scope
        if not adapter.is_enabled():
            out["brokers"][scope] = {
                "status": DualPaperBrokerStatus.UNSUPPORTED.value,
                "missing_env_keys": adapter.missing_env_keys(),
            }
            continue
        try:
            summary = await adapter.read_account_state()
            out["brokers"][scope] = {
                "status": "ok",
                "account_state": summary.model_dump(),
            }
        except Exception as exc:  # isolate per broker
            out["brokers"][scope] = {
                "status": DualPaperBrokerStatus.ERROR.value,
                "reason": type(exc).__name__,
            }
    return out


async def us_dual_paper_preview(
    symbol: str,
    quantity: float,
    limit_price_usd: float,
    notional_cap_usd: float = 50.0,
    reference_price_usd: float | None = None,
    limit_price_source: str = "operator_input",
) -> dict[str, Any]:
    """Generate a dual-broker (kis_mock + alpaca_paper) BUY/LIMIT preview packet.

    Read-only. submit_enabled is always False. Each broker reported independently
    as previewed/blocked/unsupported/error. Never submits, cancels, or modifies.
    """
    packet = await build_packet(
        symbol=symbol,
        quantity=quantity,
        limit_price_usd=limit_price_usd,
        notional_cap_usd=notional_cap_usd,
        limit_price_source=limit_price_source,
        reference_price_usd=reference_price_usd,
    )
    return packet.model_dump(mode="json")


def register_us_dual_paper_tools(mcp: FastMCP) -> None:
    _ = mcp.tool(
        name="us_dual_paper_capability_matrix",
        description="Read-only US dual-paper (kis_mock + alpaca_paper) capability matrix. No submit.",
    )(us_dual_paper_capability_matrix)
    _ = mcp.tool(
        name="us_dual_paper_account_states",
        description=(
            "Read-only account states (cash/buying-power/position counts) for KIS mock US "
            "and Alpaca Paper. Counts/numbers only, no secrets. No submit/cancel/modify."
        ),
    )(us_dual_paper_account_states)
    _ = mcp.tool(
        name="us_dual_paper_preview",
        description=(
            "Dual-broker BUY/LIMIT preview packet for KIS mock US + Alpaca Paper. "
            "Read-only, submit_enabled always False; each broker reported independently "
            "(previewed/blocked/unsupported/error). No submit/cancel/modify."
        ),
    )(us_dual_paper_preview)


__all__ = [
    "US_DUAL_PAPER_TOOL_NAMES",
    "register_us_dual_paper_tools",
    "us_dual_paper_account_states",
    "us_dual_paper_capability_matrix",
    "us_dual_paper_preview",
]

