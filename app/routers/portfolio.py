"""
Portfolio Router

통합 포트폴리오 API 엔드포인트
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.manual_holdings import MarketType
from app.schemas.manual_holdings import (
    MergedHoldingResponse,
    MergedPortfolioResponse,
    ReferencePricesResponse,
)
from app.services.merged_portfolio_service import MergedPortfolioService
from app.services.kis import KISClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/portfolio", tags=["Portfolio"])


@router.get("/api/merged", response_model=MergedPortfolioResponse)
async def get_merged_portfolio(
    request: Request,
    market_type: Optional[MarketType] = None,
    db: AsyncSession = Depends(get_db),
):
    """통합 포트폴리오 조회

    KIS 보유 종목 + 수동 등록 종목을 통합하여 반환

    Args:
        market_type: 시장 타입 필터 (KR: 국내, US: 해외)
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")

    service = MergedPortfolioService(db)
    kis_client = KISClient()

    holdings = []
    krw_balance = None
    usd_balance = None

    try:
        if market_type is None or market_type == MarketType.KR:
            domestic = await service.get_merged_portfolio_domestic(
                user.id, kis_client
            )
            holdings.extend(domestic)

            # KRW 잔고 조회
            try:
                margin = await kis_client.inquire_integrated_margin()
                krw_balance = float(margin.get("dnca_tot_amt", 0))
            except Exception as e:
                logger.warning(f"Failed to get KRW balance: {e}")

        if market_type is None or market_type == MarketType.US:
            overseas = await service.get_merged_portfolio_overseas(
                user.id, kis_client
            )
            holdings.extend(overseas)

            # USD 잔고 조회
            try:
                usd_info = await kis_client.inquire_overseas_balance()
                usd_balance = float(usd_info.get("frcr_evlu_tota", 0))
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
    request: Request,
    ticker: str,
    market_type: MarketType,
    db: AsyncSession = Depends(get_db),
):
    """특정 종목의 통합 보유 정보 조회

    Args:
        ticker: 종목 코드
        market_type: 시장 타입 (KR: 국내, US: 해외)
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")

    service = MergedPortfolioService(db)
    kis_client = KISClient()

    try:
        if market_type == MarketType.KR:
            holdings = await service.get_merged_portfolio_domestic(
                user.id, kis_client
            )
        else:
            holdings = await service.get_merged_portfolio_overseas(
                user.id, kis_client
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
    request: Request,
    ticker: str,
    market_type: MarketType,
    db: AsyncSession = Depends(get_db),
):
    """특정 종목의 참조 평단가 조회

    Args:
        ticker: 종목 코드
        market_type: 시장 타입 (KR: 국내, US: 해외)
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")

    service = MergedPortfolioService(db)
    kis_client = KISClient()

    # KIS 보유 정보 조회
    kis_holdings = None
    try:
        if market_type == MarketType.KR:
            stocks = await kis_client.fetch_my_stocks()
            for s in stocks:
                if s.get("pdno") == ticker.upper():
                    kis_holdings = {
                        "quantity": int(s.get("hldg_qty", 0)),
                        "avg_price": float(s.get("pchs_avg_pric", 0)),
                    }
                    break
        else:
            stocks = await kis_client.fetch_overseas_stocks()
            for s in stocks:
                if s.get("ovrs_pdno") == ticker.upper():
                    kis_holdings = {
                        "quantity": int(float(s.get("ovrs_cblc_qty", 0))),
                        "avg_price": float(s.get("pchs_avg_pric", 0)),
                    }
                    break
    except Exception as e:
        logger.warning(f"Failed to fetch KIS holdings: {e}")

    ref = await service.get_reference_prices(
        user.id, ticker, market_type, kis_holdings
    )

    return ReferencePricesResponse(**ref.to_dict())
