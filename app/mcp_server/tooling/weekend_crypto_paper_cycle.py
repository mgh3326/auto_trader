"""MCP tool: weekend_crypto_paper_cycle_run (ROB-94)."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.services.weekend_crypto_paper_cycle_runner import (
    CycleGateError,
    WeekendCryptoPaperCycleRunner,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

__all__ = [
    "WEEKEND_CRYPTO_PAPER_CYCLE_TOOL_NAMES",
    "register_weekend_crypto_paper_cycle_tools",
    "weekend_crypto_paper_cycle_run",
]

WEEKEND_CRYPTO_PAPER_CYCLE_TOOL_NAMES: set[str] = {"weekend_crypto_paper_cycle_run"}


async def weekend_crypto_paper_cycle_run(
    *,
    dry_run: bool = True,
    confirm: bool = False,
    max_candidates: int = 3,
    symbols: list[str] | None = None,
    approval_tokens: dict[str, str] | None = None,
    operator_token: str | None = None,
) -> dict[str, Any]:
    """Run the weekend crypto Alpaca Paper cycle; dry-run is the default."""
    try:
        runner = WeekendCryptoPaperCycleRunner()
        report = await runner.run_cycle(
            dry_run=dry_run,
            confirm=confirm,
            max_candidates=max_candidates,
            symbols=symbols,
            approval_tokens=approval_tokens,
            operator_token=operator_token,
        )
        return report.to_dict()
    except CycleGateError as exc:
        return {
            "status": "gate_refused",
            "error": str(exc),
            "dry_run": dry_run,
            "confirm": confirm,
        }
    except Exception as exc:
        return {
            "status": "error",
            "error": type(exc).__name__,
            "detail": str(exc),
            "dry_run": dry_run,
        }


def register_weekend_crypto_paper_cycle_tools(mcp: FastMCP) -> None:
    """Register weekend_crypto_paper_cycle_run with FastMCP."""
    mcp.tool(
        name="weekend_crypto_paper_cycle_run",
        description=(
            "Run the weekend crypto Alpaca Paper buy/sell cycle (dry-run by default). "
            "Execute mode requires dry_run=False, confirm=True, operator_token, and "
            "per-candidate approval_tokens. Hard caps: max 3 candidates, $10 notional, "
            "BTC/USD|ETH/USD|SOL/USD only, limit orders only, Alpaca Paper only."
        ),
    )(weekend_crypto_paper_cycle_run)
