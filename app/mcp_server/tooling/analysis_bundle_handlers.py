"""Gated MCP handlers for frozen analysis snapshot bundles."""

from __future__ import annotations

import uuid
from functools import partial
from typing import TYPE_CHECKING, Any

from app.core.db import AsyncSessionLocal
from app.mcp_server.tooling.analysis_tool_handlers import analyze_stock_batch_impl
from app.schemas.analysis_snapshot_bundle import AnalysisBundleCreateRequest
from app.services.action_report.snapshot_backed.collectors.registry import (
    production_collector_registry,
)
from app.services.analysis_snapshot_bundle import AnalysisBundleCaptureService
from app.services.analysis_snapshot_bundle.read import (
    AnalysisBundleIntegrityError,
    AnalysisBundleNotFound,
    AnalysisBundleReadService,
    UnknownAnalysisBundleSection,
)
from app.services.decision_history import build_decision_context
from app.services.investment_snapshots.repository import InvestmentSnapshotsRepository

if TYPE_CHECKING:
    from fastmcp import FastMCP


ANALYSIS_BUNDLE_TOOL_NAMES: set[str] = {
    "analysis_bundle_create",
    "analysis_bundle_get",
}


async def analysis_bundle_create_impl(
    market: str,
    account_scope: str | None,
    symbols: list[str],
    user_id: int | None = None,
    market_session: str | None = None,
) -> dict[str, Any]:
    """Capture one append-only frozen analysis evidence bundle."""
    async with AsyncSessionLocal() as db:

        async def decision_history(
            symbol: str, decision_market: str, decision_account_scope: str | None
        ) -> dict[str, Any] | None:
            return await build_decision_context(
                db,
                symbol,
                decision_market,
                account_mode=decision_account_scope,
            )

        service = AnalysisBundleCaptureService(
            db,
            collectors=production_collector_registry(db),
            analysis_fn=partial(
                analyze_stock_batch_impl,
                quick=False,
                include_position=False,
                refresh=False,
            ),
            decision_history_fn=decision_history,
        )
        response = await service.capture(
            AnalysisBundleCreateRequest(
                market=market,
                account_scope=account_scope,
                symbols=symbols,
                user_id=user_id,
                market_session=market_session,
            )
        )
        await db.commit()
        return {"success": True, **response.model_dump(mode="json")}


async def analysis_bundle_get_impl(
    bundle_id: str,
    sections: list[str] | None = None,
) -> dict[str, Any]:
    """Read one frozen bundle without invoking capture or provider services."""
    try:
        parsed_bundle_id = uuid.UUID(bundle_id)
    except (TypeError, ValueError, AttributeError):
        return {
            "success": False,
            "error": "invalid_bundle_id",
            "bundle_id": bundle_id,
        }

    async with AsyncSessionLocal() as db:
        service = AnalysisBundleReadService(InvestmentSnapshotsRepository(db))
        try:
            response = await service.get(parsed_bundle_id, sections=sections)  # type: ignore[arg-type]
        except AnalysisBundleNotFound:
            error = "analysis_bundle_not_found"
        except AnalysisBundleIntegrityError:
            error = "analysis_bundle_integrity_error"
        except UnknownAnalysisBundleSection:
            error = "unknown_analysis_bundle_section"
        else:
            return {"success": True, **response.model_dump(mode="json")}
    return {"success": False, "error": error, "bundle_id": bundle_id}


def register_analysis_bundle_tools(mcp: FastMCP, *, allow_create: bool = True) -> None:
    """Register frozen bundle tools, optionally omitting capture physically."""
    if allow_create:
        mcp.tool(
            name="analysis_bundle_create",
            description=(
                "Capture analysis inputs as append-only evidence and return its id. "
                "Evidence append only; no order/proposal mutation."
            ),
        )(analysis_bundle_create_impl)
    mcp.tool(
        name="analysis_bundle_get",
        description=(
            "Return the stored payload verbatim from a frozen analysis bundle; "
            "zero provider calls/recomputation on get; SHA-256 verified. "
            "Evidence append only; no order/proposal mutation."
        ),
    )(analysis_bundle_get_impl)


__all__ = [
    "ANALYSIS_BUNDLE_TOOL_NAMES",
    "analysis_bundle_create_impl",
    "analysis_bundle_get_impl",
    "register_analysis_bundle_tools",
]
