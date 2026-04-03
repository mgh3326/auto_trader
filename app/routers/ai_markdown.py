"""AI Markdown Export Router

포트폴리오 및 종목 데이터를 AI 질문용 Markdown으로 변환하는 API
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.ai_markdown import (
    MarkdownResponse,
    PortfolioMarkdownRequest,
    PresetType,
    StockMarkdownRequest,
)
from app.services.ai_markdown_service import AIMarkdownService
from app.services.portfolio_dashboard_service import PortfolioDashboardService
from app.services.portfolio_overview_service import PortfolioOverviewService
from app.services.portfolio_position_detail_service import (
    PortfolioPositionDetailNotFoundError,
    PortfolioPositionDetailService,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/api/ai-markdown", tags=["AI Markdown"])


def get_ai_markdown_service() -> AIMarkdownService:
    return AIMarkdownService()


def get_portfolio_overview_service(
    db: AsyncSession = Depends(get_db),
) -> PortfolioOverviewService:
    return PortfolioOverviewService(db)


def get_portfolio_dashboard_service(
    db: AsyncSession = Depends(get_db),
) -> PortfolioDashboardService:
    return PortfolioDashboardService(db)


def get_position_detail_service(
    overview_service: PortfolioOverviewService = Depends(
        get_portfolio_overview_service
    ),
    dashboard_service: PortfolioDashboardService = Depends(
        get_portfolio_dashboard_service
    ),
) -> PortfolioPositionDetailService:
    return PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )


@router.post("/portfolio", response_model=MarkdownResponse)
async def generate_portfolio_markdown(
    request: PortfolioMarkdownRequest,
    current_user: User = Depends(get_authenticated_user),
    overview_service: PortfolioOverviewService = Depends(
        get_portfolio_overview_service
    ),
    markdown_service: AIMarkdownService = Depends(get_ai_markdown_service),
):
    """포트폴리오 전체 스탠스용 Markdown 생성"""
    try:
        # 포트폴리오 데이터 조회
        portfolio_data = await overview_service.get_overview(
            user_id=current_user.id,
            market=request.include_market,
        )

        if not portfolio_data.get("success"):
            return MarkdownResponse(
                success=False,
                preset=request.preset,
                title="",
                content="",
                filename="",
                error="Failed to fetch portfolio data",
            )

        # Markdown 생성
        result = markdown_service.generate_portfolio_stance_markdown(portfolio_data)

        return MarkdownResponse(
            success=True,
            preset=request.preset,
            title=result["title"],
            content=result["content"],
            filename=result["filename"],
            metadata=result["metadata"],
        )

    except Exception as e:
        logger.error(f"Error generating portfolio markdown: {e}", exc_info=True)
        return MarkdownResponse(
            success=False,
            preset=request.preset,
            title="",
            content="",
            filename="",
            error=str(e),
        )


@router.post("/stock", response_model=MarkdownResponse)
async def generate_stock_markdown(
    request: StockMarkdownRequest,
    current_user: User = Depends(get_authenticated_user),
    detail_service: PortfolioPositionDetailService = Depends(
        get_position_detail_service
    ),
    markdown_service: AIMarkdownService = Depends(get_ai_markdown_service),
):
    """종목 상세 스탠스용 Markdown 생성"""
    try:
        # 종목 데이터 조회
        stock_data = await detail_service.get_page_payload(
            user_id=current_user.id,
            market_type=request.market_type,
            symbol=request.symbol,
        )

        # 프리셋에 따른 Markdown 생성
        if request.preset == PresetType.STOCK_STANCE:
            result = markdown_service.generate_stock_stance_markdown(stock_data)
        elif request.preset == PresetType.STOCK_ADD_OR_HOLD:
            result = markdown_service.generate_stock_add_or_hold_markdown(stock_data)
        else:
            raise HTTPException(
                status_code=400,
                detail=f"Invalid preset for stock markdown: {request.preset}",
            )

        return MarkdownResponse(
            success=True,
            preset=request.preset,
            title=result["title"],
            content=result["content"],
            filename=result["filename"],
            metadata=result["metadata"],
        )

    except PortfolioPositionDetailNotFoundError:
        return MarkdownResponse(
            success=False,
            preset=request.preset,
            title="",
            content="",
            filename="",
            error=f"Position not found: {request.symbol}",
        )
    except Exception as e:
        logger.error(f"Error generating stock markdown: {e}", exc_info=True)
        return MarkdownResponse(
            success=False,
            preset=request.preset,
            title="",
            content="",
            filename="",
            error=str(e),
        )
