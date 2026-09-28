"""Hermes-only, operator-audited KIS mock ledger DAY expiry tool.

The handler is DB-only. It must not import or construct a KIS client, read a
broker endpoint, or issue any broker mutation. All ledger writes remain in
KISMockLifecycleService.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Annotated, Any

from pydantic import Field

from app.core.config import settings, validate_kis_mock_config
from app.core.db import AsyncSessionLocal
from app.services.kis_mock_lifecycle_service import KISMockLifecycleService
from app.services.kis_mock_terminal_expiry import (
    MAX_LEDGER_IDS,
    RULE_VERSION,
    validate_request,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

TOOL_NAME = "kis_mock_ledger_expire_day_orders"
ExactLedgerId = Annotated[int, Field(strict=True, gt=0, le=2**63 - 1)]
LedgerIds = Annotated[
    list[ExactLedgerId], Field(min_length=1, max_length=MAX_LEDGER_IDS)
]


def register_kis_mock_terminal_tools(mcp: FastMCP) -> None:
    """Register only from the hermes-paper-kis profile branch."""

    @mcp.tool(
        name=TOOL_NAME,
        description=(
            "Audit and optionally expire explicit old KIS mock KR DAY ledger rows "
            "using only persisted row and XKRX calendar evidence. No broker read. "
            "dry_run defaults true; writes require confirm=true and an operator "
            "decision reference plus the expected strategy."
        ),
    )
    async def kis_mock_ledger_expire_day_orders(
        ledger_ids: LedgerIds,
        operator_decision_ref: str,
        expected_strategy: str,
        dry_run: bool = True,
        confirm: bool = False,
    ) -> dict[str, Any]:
        invalid = validate_request(ledger_ids, operator_decision_ref, expected_strategy)
        if invalid is not None:
            return {"success": False, "error": invalid, "rule_version": RULE_VERSION}
        if type(dry_run) is not bool or type(confirm) is not bool:
            return {
                "success": False,
                "error": "boolean_gate_invalid",
                "rule_version": RULE_VERSION,
            }
        if not dry_run and confirm is not True:
            return {
                "success": False,
                "error": "confirm_required",
                "rule_version": RULE_VERSION,
            }
        missing = validate_kis_mock_config()
        if missing:
            return {
                "success": False,
                "error": "kis_mock_config_unavailable",
                "missing_env_keys": missing,
                "rule_version": RULE_VERSION,
            }
        try:
            async with AsyncSessionLocal() as db:
                rows = await KISMockLifecycleService(db).expire_legacy_day_orders(
                    ledger_ids=ledger_ids,
                    operator_decision_ref=operator_decision_ref,
                    expected_strategy=expected_strategy,
                    min_sessions=settings.kis_mock_terminal_min_sessions,
                    dry_run=dry_run,
                    confirm=confirm,
                )
        except Exception:  # noqa: BLE001 - never return DB or secret-bearing exception text
            return {
                "success": False,
                "error": "terminal_review_unavailable",
                "rule_version": RULE_VERSION,
            }
        incomplete = any(row["decision"] in {"error", "not_processed"} for row in rows)
        return {
            "success": not incomplete,
            **({"error": "terminal_review_incomplete"} if incomplete else {}),
            "account_mode": "kis_mock",
            "broker": "kis",
            "dry_run": dry_run,
            "rule_version": RULE_VERSION,
            "operator_decision_ref": operator_decision_ref.strip(),
            "rows": rows,
        }


__all__ = ["TOOL_NAME", "register_kis_mock_terminal_tools"]
