"""
Portfolio Router

통합 포트폴리오 API 엔드포인트
"""

import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.core.templates import templates
from app.models.manual_holdings import MarketType
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.ai_advisor import (
    AiAdviceRequest,
    AiAdviceResponse,
    AiProvidersResponse,
    ProviderInfo,
)
from app.schemas.manual_holdings import (
    MergedHoldingResponse,
    MergedPortfolioResponse,
    ReferencePricesResponse,
)
from app.schemas.portfolio_decision import PortfolioDecisionSlateResponse
from app.schemas.portfolio_position_detail import (
    PositionIndicatorsResponse,
    PositionNewsResponse,
    PositionOpinionsResponse,
    PositionOrdersResponse,
)
from app.services.ai_advisor_service import AiAdvisorService
from app.services.ai_markdown_service import AIMarkdownService
from app.services.ai_providers.base import AiProviderError
from app.services.brokers.kis.client import KISClient
from app.services.kis_holdings_service import get_kis_holding_for_ticker
from app.services.merged_portfolio_service import MergedPortfolioService
from app.services.portfolio_dashboard_service import PortfolioDashboardService
from app.services.portfolio_decision_service import PortfolioDecisionService
from app.services.portfolio_overview_service import PortfolioOverviewService
from app.services.portfolio_position_detail_service import (
    PortfolioPositionDetailNotFoundError,
    PortfolioPositionDetailService,
)

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/portfolio", tags=["Portfolio"])


def get_portfolio_overview_service(
    db: AsyncSession = Depends(get_db),
) -> PortfolioOverviewService:
    return PortfolioOverviewService(db)


def get_portfolio_dashboard_service(
    db: AsyncSession = Depends(get_db),
) -> PortfolioDashboardService:
    return PortfolioDashboardService(db)


def get_portfolio_decision_service(
    overview_service: PortfolioOverviewService = Depends(
        get_portfolio_overview_service
    ),
    dashboard_service: PortfolioDashboardService = Depends(
        get_portfolio_dashboard_service
    ),
) -> PortfolioDecisionService:
    return PortfolioDecisionService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )


@router.get("/decision", response_class=HTMLResponse)
async def portfolio_decision_page(request: Request):
    user = getattr(request.state, "user", None)
    return templates.TemplateResponse(
        request,
        "portfolio_decision_desk.html",
        {
            "request": request,
            "user": user,
        },
    )


@router.get("/api/decision-slate", response_model=PortfolioDecisionSlateResponse)
async def get_portfolio_decision_slate(
    market: Literal["ALL", "KR", "US", "CRYPTO"] = "ALL",
    account_keys: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query(min_length=1)] = None,
    current_user: User = Depends(get_authenticated_user),
    decision_service: PortfolioDecisionService = Depends(
        get_portfolio_decision_service
    ),
):
    try:
        return await decision_service.build_decision_slate(
            user_id=current_user.id,
            market=market,
            account_keys=account_keys,
            q=q,
        )
    except Exception as e:
        logger.error("Error building decision slate: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/", response_class=HTMLResponse)
async def portfolio_dashboard_page(request: Request):
    user = getattr(request.state, "user", None)
    return templates.TemplateResponse(
        request,
        "portfolio_dashboard.html",
        {
            "request": request,
            "user": user,
        },
    )


class EnrichSymbol(BaseModel):
    symbol: str
    market_type: Literal["KR", "US", "CRYPTO"]


class EnrichRequest(BaseModel):
    symbols: list[EnrichSymbol]


def _merge_journal_snapshots(
    payload: dict[str, object],
    journal_map: dict[str, dict[str, object]],
) -> dict[str, object]:
    raw_positions = payload.get("positions")
    positions = raw_positions if isinstance(raw_positions, list) else []
    for position in positions:
        position["journal"] = journal_map.get(position.get("symbol"))
    return payload


@router.get("/api/overview")
async def get_portfolio_overview(
    market: Literal["ALL", "KR", "US", "CRYPTO"] = "ALL",
    account_keys: Annotated[list[str] | None, Query()] = None,
    q: Annotated[str | None, Query(min_length=1)] = None,
    skip_missing_prices: bool = False,
    current_user: User = Depends(get_authenticated_user),
    overview_service: PortfolioOverviewService = Depends(
        get_portfolio_overview_service
    ),
    dashboard_service: PortfolioDashboardService = Depends(
        get_portfolio_dashboard_service
    ),
):
    try:
        overview = await overview_service.get_overview(
            user_id=current_user.id,
            market=market,
            account_keys=account_keys,
            q=q,
            skip_missing_prices=skip_missing_prices,
        )

        if overview.get("success") and overview.get("positions"):
            journal_map = await dashboard_service.get_journals_batch(
                [position["symbol"] for position in overview["positions"]],
                current_prices={
                    position["symbol"]: position.get("current_price")
                    for position in overview["positions"]
                },
            )
            overview = _merge_journal_snapshots(overview, journal_map)

        return overview
    except Exception as e:
        logger.error("Error fetching portfolio overview: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/overview/enrich")
async def enrich_portfolio_overview(
    request: EnrichRequest,
    current_user: User = Depends(get_authenticated_user),
    overview_service: PortfolioOverviewService = Depends(
        get_portfolio_overview_service
    ),
    dashboard_service: PortfolioDashboardService = Depends(
        get_portfolio_dashboard_service
    ),
):
    try:
        targets = [
            {"symbol": t.symbol, "market_type": t.market_type} for t in request.symbols
        ]
        payload = await overview_service.enrich_manual_positions(
            user_id=current_user.id,
            targets=targets,
        )
        if payload.get("success") and payload.get("positions"):
            journal_map = await dashboard_service.get_journals_batch(
                [position["symbol"] for position in payload["positions"]],
                current_prices={
                    position["symbol"]: position.get("current_price")
                    for position in payload["positions"]
                },
            )
            payload = _merge_journal_snapshots(payload, journal_map)
        return payload
    except Exception as e:
        logger.error("Error enriching portfolio overview: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.get("/api/merged", response_model=MergedPortfolioResponse)
async def get_merged_portfolio(
    market_type: MarketType | None = None,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """통합 포트폴리오 조회

    KIS 보유 종목 + 수동 등록 종목을 통합하여 반환

    Args:
        market_type: 시장 타입 필터 (KR: 국내, US: 해외)
    """
    service = MergedPortfolioService(db)
    kis_client = KISClient()

    holdings = []
    krw_balance = None
    usd_balance = None

    try:
        if market_type is None or market_type == MarketType.KR:
            domestic = await service.get_merged_portfolio_domestic(
                current_user.id, kis_client
            )
            holdings.extend(domestic)

            # KRW 잔고 조회
            try:
                balance_data = await kis_client.inquire_domestic_cash_balance()
                krw_balance = float(balance_data.get("dnca_tot_amt") or 0)
            except Exception as e:
                logger.warning(f"Failed to get KRW balance: {e}")

        if market_type is None or market_type == MarketType.US:
            overseas = await service.get_merged_portfolio_overseas(
                current_user.id, kis_client
            )
            holdings.extend(overseas)

            # USD 잔고 조회
            try:
                overseas_margin_data = await kis_client.inquire_overseas_margin()
                usd_row = next(
                    (
                        row
                        for row in overseas_margin_data
                        if str(row.get("currency") or "").upper() == "USD"
                    ),
                    None,
                )
                if usd_row is not None:
                    usd_balance = float(
                        usd_row.get("frcr_dncl_amt1")
                        or usd_row.get("frcr_dncl_amt_2")
                        or 0
                    )
            except Exception as e:
                logger.warning(f"Failed to get USD balance: {e}")

    except Exception as e:
        logger.error(f"Error fetching merged portfolio: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e

    # 합계 계산
    total_evaluation = sum(h.evaluation for h in holdings)
    total_profit_loss = sum(h.profit_loss for h in holdings)

    return MergedPortfolioResponse(
        success=True,
        total_holdings=len(holdings),
        krw_balance=krw_balance,
        usd_balance=usd_balance,
        total_evaluation=total_evaluation,
        total_profit_loss=total_profit_loss,
        holdings=[MergedHoldingResponse(**h.to_dict()) for h in holdings],
    )


@router.get("/api/merged/{ticker}", response_model=MergedHoldingResponse)
async def get_merged_holding_detail(
    ticker: str,
    market_type: MarketType,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """특정 종목의 통합 보유 정보 조회

    Args:
        ticker: 종목 코드
        market_type: 시장 타입 (KR: 국내, US: 해외)
    """
    service = MergedPortfolioService(db)
    kis_client = KISClient()

    try:
        if market_type == MarketType.KR:
            holdings = await service.get_merged_portfolio_domestic(
                current_user.id, kis_client
            )
        else:
            holdings = await service.get_merged_portfolio_overseas(
                current_user.id, kis_client
            )
    except Exception as e:
        logger.error(f"Error fetching holding detail: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e

    # 해당 종목 찾기
    ticker = ticker.upper()
    for h in holdings:
        if h.ticker == ticker:
            return MergedHoldingResponse(**h.to_dict())

    raise HTTPException(status_code=404, detail=f"종목을 찾을 수 없습니다: {ticker}")


@router.get("/api/reference-prices/{ticker}", response_model=ReferencePricesResponse)
async def get_reference_prices(
    ticker: str,
    market_type: MarketType,
    current_user: User = Depends(get_authenticated_user),
    db: AsyncSession = Depends(get_db),
):
    """특정 종목의 참조 평단가 조회

    Args:
        ticker: 종목 코드
        market_type: 시장 타입 (KR: 국내, US: 해외)
    """
    service = MergedPortfolioService(db)
    kis_client = KISClient()

    kis_holdings = await get_kis_holding_for_ticker(kis_client, ticker, market_type)
    if not kis_holdings.get("quantity"):
        kis_holdings = None

    ref = await service.get_reference_prices(
        current_user.id, ticker, market_type, kis_holdings
    )

    return ReferencePricesResponse(**ref.to_dict())


@router.get("/api/journal/{symbol}")
async def get_portfolio_journal(
    symbol: str,
    current_price: float | None = None,
    _current_user: User = Depends(get_authenticated_user),
    dashboard_service: PortfolioDashboardService = Depends(
        get_portfolio_dashboard_service
    ),
):
    journal = await dashboard_service.get_latest_journal_snapshot(
        symbol,
        current_price=current_price,
    )
    if journal is None:
        raise HTTPException(status_code=404, detail="Trade journal not found")
    return journal


@router.get("/api/cash")
async def get_portfolio_cash(
    _current_user: User = Depends(get_authenticated_user),
    dashboard_service: PortfolioDashboardService = Depends(
        get_portfolio_dashboard_service
    ),
):
    return await dashboard_service.get_cash_snapshot()


@router.get("/api/rotation-plan")
async def get_rotation_plan(
    _current_user: User = Depends(get_authenticated_user),
):
    from app.services.portfolio_rotation_service import PortfolioRotationService

    service = PortfolioRotationService()
    return await service.build_rotation_plan(market="crypto")


class PositionDetailNotFoundHTTPError(HTTPException):
    def __init__(self, symbol: str):
        super().__init__(status_code=404, detail=f"Position not found: {symbol}")


def get_portfolio_position_detail_service(
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


@router.get("/positions/{market_type}/{symbol}", response_class=HTMLResponse)
async def portfolio_position_detail_page(
    request: Request,
    market_type: str,
    symbol: str,
    current_user: User = Depends(get_authenticated_user),
    detail_service: PortfolioPositionDetailService = Depends(
        get_portfolio_position_detail_service
    ),
):
    try:
        payload = await detail_service.get_page_payload(
            user_id=current_user.id,
            market_type=market_type,
            symbol=symbol,
        )
    except PortfolioPositionDetailNotFoundError as exc:
        raise HTTPException(
            status_code=404, detail=f"Position not found: {symbol}"
        ) from exc

    return templates.TemplateResponse(
        request,
        "portfolio_position_detail.html",
        {
            "user": current_user,
            "page_payload": payload,
        },
    )


@router.get(
    "/api/positions/{market_type}/{symbol}/indicators",
    response_model=PositionIndicatorsResponse,
)
async def get_position_indicators(
    market_type: str,
    symbol: str,
    detail_service: PortfolioPositionDetailService = Depends(
        get_portfolio_position_detail_service
    ),
):
    return await detail_service.get_indicators_payload(
        market_type=market_type, symbol=symbol
    )


@router.get(
    "/api/positions/{market_type}/{symbol}/news",
    response_model=PositionNewsResponse,
)
async def get_position_news(
    market_type: str,
    symbol: str,
    detail_service: PortfolioPositionDetailService = Depends(
        get_portfolio_position_detail_service
    ),
):
    return await detail_service.get_news_payload(market_type=market_type, symbol=symbol)


@router.get(
    "/api/positions/{market_type}/{symbol}/orders",
    response_model=PositionOrdersResponse,
)
async def get_position_orders(
    market_type: str,
    symbol: str,
    detail_service: PortfolioPositionDetailService = Depends(
        get_portfolio_position_detail_service
    ),
):
    return await detail_service.get_orders_payload(
        market_type=market_type,
        symbol=symbol,
    )


@router.get(
    "/api/positions/{market_type}/{symbol}/opinions",
    response_model=PositionOpinionsResponse,
)
async def get_position_opinions(
    market_type: str,
    symbol: str,
    detail_service: PortfolioPositionDetailService = Depends(
        get_portfolio_position_detail_service
    ),
):
    return await detail_service.get_opinions_payload(
        market_type=market_type, symbol=symbol
    )


# --- AI Advisor ---


def get_ai_advisor_service(
    db: AsyncSession = Depends(get_db),
) -> AiAdvisorService:
    overview_service = PortfolioOverviewService(db)
    dashboard_service = PortfolioDashboardService(db)
    detail_service = PortfolioPositionDetailService(
        overview_service=overview_service,
        dashboard_service=dashboard_service,
    )
    return AiAdvisorService(
        markdown_service=AIMarkdownService(),
        overview_service=overview_service,
        detail_service=detail_service,
    )


@router.get("/api/ai-advice/providers", response_model=AiProvidersResponse)
async def get_ai_providers(
    current_user: User = Depends(get_authenticated_user),
    advisor_service: AiAdvisorService = Depends(get_ai_advisor_service),
):
    from app.core.config import settings

    providers = [
        ProviderInfo(name=p["name"], default_model=p["default_model"])
        for p in advisor_service.available_providers()
    ]
    return AiProvidersResponse(
        providers=providers,
        default_provider=settings.ai_advisor_default_provider,
    )


@router.post("/api/ai-advice", response_model=AiAdviceResponse)
async def post_ai_advice(
    request: AiAdviceRequest,
    current_user: User = Depends(get_authenticated_user),
    advisor_service: AiAdvisorService = Depends(get_ai_advisor_service),
):
    import time

    start = time.monotonic()

    # Validate provider exists
    available = {p["name"] for p in advisor_service.available_providers()}
    if request.provider not in available:
        raise HTTPException(
            status_code=400,
            detail=f"Provider '{request.provider}' is not available. "
            f"Available: {sorted(available) or 'none (no API keys configured)'}",
        )

    # Validate position scope has required fields
    if request.scope == "position":
        if not request.market_type or not request.symbol:
            raise HTTPException(
                status_code=400,
                detail="market_type and symbol are required for position scope",
            )

    try:
        return await advisor_service.ask(
            user_id=current_user.id,
            scope=request.scope,
            preset=request.preset,
            provider=request.provider,
            question=request.question,
            model=request.model,
            market_type=request.market_type,
            symbol=request.symbol,
            include_market=request.include_market,
        )
    except AiProviderError as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.warning("AI advisor provider error: %s | %s", e.user_message, e.detail)
        return AiAdviceResponse(
            success=False,
            answer="",
            provider=request.provider,
            model=request.model or "",
            elapsed_ms=elapsed_ms,
            error=e.user_message,
        )
    except PortfolioPositionDetailNotFoundError:
        raise HTTPException(status_code=404, detail="Position not found")
    except Exception as e:
        elapsed_ms = int((time.monotonic() - start) * 1000)
        logger.error("AI advisor unexpected error: %s", e, exc_info=True)
        return AiAdviceResponse(
            success=False,
            answer="",
            provider=request.provider,
            model=request.model or "",
            elapsed_ms=elapsed_ms,
            error="AI 응답 생성 실패. 잠시 후 다시 시도해주세요.",
        )
