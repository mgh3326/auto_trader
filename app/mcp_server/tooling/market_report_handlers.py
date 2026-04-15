from __future__ import annotations

from typing import TYPE_CHECKING, Any

from app.services.market_report_service import (
    get_latest_market_brief,
    get_market_reports,
)

if TYPE_CHECKING:
    from fastmcp import FastMCP

MARKET_REPORT_TOOL_NAMES = ["get_market_reports", "get_latest_market_brief"]


async def _get_market_reports_impl(
    report_type: str | None = None,
    market: str | None = None,
    days: int | None = 7,
    limit: int | None = 10,
) -> dict[str, Any]:
    days = days or 7
    limit = limit or 10

    reports = await get_market_reports(
        report_type=report_type,
        market=market,
        days=days,
        limit=limit,
    )

    return {
        "count": len(reports),
        "reports": reports,
    }


async def _get_latest_market_brief_impl(
    market: str | None = "all",
) -> dict[str, Any]:
    market = market or "all"
    report = await get_latest_market_brief(market=market)

    if not report:
        return {"found": False, "report": None}

    return {"found": True, "report": report}


def _register_market_report_tools_impl(mcp: FastMCP) -> None:
    @mcp.tool(
        name="get_market_reports",
        description=(
            "과거 마켓 리포트 조회. report_type으로 필터 가능: "
            "'daily_brief' (일일 종합 브리프), 'kr_morning' (한국 주식 모닝 리포트), "
            "'crypto_scan' (크립토 스캔). market으로 필터 가능: 'kr', 'us', 'crypto', 'all'. "
            "days로 조회 기간 설정 (기본 7일)."
        ),
    )
    async def get_market_reports_tool(
        report_type: str | None = None,
        market: str | None = None,
        days: int = 7,
        limit: int = 10,
    ) -> dict[str, Any]:
        return await _get_market_reports_impl(
            report_type=report_type,
            market=market,
            days=days,
            limit=limit,
        )

    @mcp.tool(
        name="get_latest_market_brief",
        description=(
            "최신 daily_brief 마켓 브리프 조회. "
            "market='all'이면 전체 시장 브리프, 'kr'/'us'/'crypto'로 필터 가능."
        ),
    )
    async def get_latest_market_brief_tool(
        market: str = "all",
    ) -> dict[str, Any]:
        return await _get_latest_market_brief_impl(market=market)
