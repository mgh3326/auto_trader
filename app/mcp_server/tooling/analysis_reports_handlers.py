"""MCP handlers for ROB-257 analysis report artifacts."""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.core.db import AsyncSessionLocal
from app.schemas.analysis_reports import AnalysisReportCreateRequest
from app.services.analysis_report_service import AnalysisReportService

if TYPE_CHECKING:
    from fastmcp import FastMCP

ANALYSIS_REPORT_TOOL_NAMES: set[str] = {
    "analysis_report_create",
    "analysis_report_list",
    "analysis_report_get",
    "analysis_candidate_list",
    "analysis_candidate_get",
}


async def analysis_report_create_impl(**payload: Any) -> dict:
    request = AnalysisReportCreateRequest.model_validate(payload)
    async with AsyncSessionLocal() as db:
        service = AnalysisReportService(db)
        report = await service.create_report(request, created_by_profile="mcp")
        return {
            "success": True,
            "idempotent": bool(report.get("idempotent", False)),
            "report": report,
        }


async def analysis_report_list_impl(
    market: str | None = None,
    status: str | None = None,
    limit: int = 20,
) -> dict:
    capped = max(1, min(int(limit), 100))
    async with AsyncSessionLocal() as db:
        service = AnalysisReportService(db)
        result = await service.list_reports(market=market, status=status, limit=capped)
        return {"success": True, **result}


async def analysis_report_get_impl(report_uuid: str) -> dict:
    async with AsyncSessionLocal() as db:
        service = AnalysisReportService(db)
        report = await service.get_report(report_uuid)
        if report is None:
            return {"success": False, "error": "not_found"}
        return {"success": True, "report": report}


async def analysis_candidate_list_impl(
    market: str | None = None,
    symbol: str | None = None,
    approval_status: str | None = None,
    limit: int = 50,
) -> dict:
    capped = max(1, min(int(limit), 100))
    async with AsyncSessionLocal() as db:
        service = AnalysisReportService(db)
        result = await service.list_candidates(
            market=market,
            symbol=symbol,
            approval_status=approval_status,
            limit=capped,
        )
        return {"success": True, **result}


async def analysis_candidate_get_impl(candidate_uuid: str) -> dict:
    async with AsyncSessionLocal() as db:
        service = AnalysisReportService(db)
        candidate = await service.get_candidate(candidate_uuid)
        if candidate is None:
            return {"success": False, "error": "not_found"}
        return {"success": True, "candidate": candidate}


def register_analysis_report_tools(mcp: FastMCP) -> None:
    mcp.tool(
        name="analysis_report_create",
        description=(
            "Persist one ROB-257 analysis report decision artifact and candidates. "
            "No broker/order submission is performed."
        ),
    )(analysis_report_create_impl)
    mcp.tool(
        name="analysis_report_list",
        description="List analysis report artifacts (read-only, limit clamped to 1..100).",
    )(analysis_report_list_impl)
    mcp.tool(
        name="analysis_report_get",
        description="Fetch one analysis report artifact by report_uuid (read-only).",
    )(analysis_report_get_impl)
    mcp.tool(
        name="analysis_candidate_list",
        description="List analysis action-center candidates (read-only, limit clamped to 1..100).",
    )(analysis_candidate_list_impl)
    mcp.tool(
        name="analysis_candidate_get",
        description="Fetch one analysis action-center candidate by candidate_uuid (read-only).",
    )(analysis_candidate_get_impl)


__all__ = [
    "ANALYSIS_REPORT_TOOL_NAMES",
    "analysis_candidate_get_impl",
    "analysis_candidate_list_impl",
    "analysis_report_create_impl",
    "analysis_report_get_impl",
    "analysis_report_list_impl",
    "register_analysis_report_tools",
]
