"""
Trading Router

매수/매도 주문 API 엔드포인트
"""
import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.manual_holdings import MarketType
from app.schemas.manual_holdings import (
    BuyOrderRequest,
    SellOrderRequest,
    OrderSimulationResponse,
    ReferencePricesResponse,
    ExpectedProfitResponse,
)
from app.services.merged_portfolio_service import MergedPortfolioService
from app.services.trading_price_service import TradingPriceService, PriceStrategy
from app.services.kis import KISClient

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/trading", tags=["Trading"])


async def _get_kis_holdings(
    kis_client: KISClient,
    ticker: str,
    market_type: MarketType,
) -> dict:
    """KIS 보유 정보 조회"""
    try:
        if market_type == MarketType.KR:
            stocks = await kis_client.fetch_my_stocks()
            for s in stocks:
                if s.get("pdno") == ticker:
                    return {
                        "quantity": int(s.get("hldg_qty", 0)),
                        "avg_price": float(s.get("pchs_avg_pric", 0)),
                        "current_price": float(s.get("prpr", 0)),
                    }
        else:
            stocks = await kis_client.fetch_overseas_stocks()
            for s in stocks:
                if s.get("ovrs_pdno") == ticker:
                    return {
                        "quantity": int(float(s.get("ovrs_cblc_qty", 0))),
                        "avg_price": float(s.get("pchs_avg_pric", 0)),
                        "current_price": float(s.get("now_pric2", 0)),
                    }
    except Exception as e:
        logger.warning(f"Failed to fetch KIS holdings: {e}")

    return {"quantity": 0, "avg_price": 0, "current_price": 0}


async def _get_current_price(
    kis_client: KISClient,
    ticker: str,
    market_type: MarketType,
) -> float:
    """현재가 조회"""
    try:
        if market_type == MarketType.KR:
            price_info = await kis_client.get_price(ticker)
            return float(price_info.get("stck_prpr", 0))
        else:
            price_info = await kis_client.get_overseas_price(ticker)
            return float(price_info.get("last", 0))
    except Exception as e:
        logger.warning(f"Failed to fetch current price: {e}")
        return 0


@router.post("/api/buy", response_model=OrderSimulationResponse)
async def buy_order(
    request: Request,
    data: BuyOrderRequest,
    db: AsyncSession = Depends(get_db),
):
    """매수 주문

    dry_run=True: 시뮬레이션 (기본값)
    dry_run=False: 실제 주문 실행

    Args:
        data: 매수 주문 요청
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")

    kis_client = KISClient()
    portfolio_service = MergedPortfolioService(db)
    price_service = TradingPriceService()

    ticker = data.ticker.upper()

    # 1. KIS 보유 정보 조회
    kis_info = await _get_kis_holdings(kis_client, ticker, data.market_type)

    # 2. 현재가 조회
    current_price = kis_info.get("current_price", 0)
    if current_price <= 0:
        current_price = await _get_current_price(kis_client, ticker, data.market_type)
    if current_price <= 0:
        raise HTTPException(status_code=400, detail="현재가를 조회할 수 없습니다")

    # 3. 참조 평단가 조회
    ref = await portfolio_service.get_reference_prices(
        user.id, ticker, data.market_type,
        kis_holdings=kis_info if kis_info.get("quantity", 0) > 0 else None,
    )

    # 4. 매수 가격 계산
    try:
        result = price_service.calculate_buy_price(
            reference_prices=ref,
            current_price=current_price,
            strategy=data.price_strategy,
            discount_percent=data.discount_percent,
            manual_price=data.manual_price,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    order_price = result.price

    # 5. 시뮬레이션 또는 실제 주문
    if data.dry_run:
        return OrderSimulationResponse(
            status="simulated",
            order_price=order_price,
            price_source=result.price_source,
            current_price=current_price,
            reference_prices=ReferencePricesResponse(**ref.to_dict()),
        )

    # 실제 주문 실행
    try:
        if data.market_type == MarketType.KR:
            order_result = await kis_client.order_korea_stock(
                stock_code=ticker,
                order_type="buy",
                quantity=data.quantity,
                price=int(order_price),
            )
        else:
            order_result = await kis_client.order_overseas_stock(
                symbol=ticker,
                exchange_code="NASD",  # TODO: 거래소 코드 동적 처리
                order_type="buy",
                quantity=data.quantity,
                price=order_price,
            )

        if order_result and order_result.get("rt_cd") == "0":
            return OrderSimulationResponse(
                status="submitted",
                order_price=order_price,
                price_source=result.price_source,
                current_price=current_price,
                reference_prices=ReferencePricesResponse(**ref.to_dict()),
                order_id=order_result.get("odno"),
                order_time=order_result.get("ord_tmd"),
            )
        else:
            error_msg = order_result.get("msg1", "주문 실패") if order_result else "주문 실패"
            return OrderSimulationResponse(
                status="failed",
                order_price=order_price,
                price_source=result.price_source,
                current_price=current_price,
                reference_prices=ReferencePricesResponse(**ref.to_dict()),
                error=error_msg,
            )

    except Exception as e:
        logger.error(f"Order execution failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e


@router.post("/api/sell", response_model=OrderSimulationResponse)
async def sell_order(
    request: Request,
    data: SellOrderRequest,
    db: AsyncSession = Depends(get_db),
):
    """매도 주문

    dry_run=True: 시뮬레이션 (기본값)
    dry_run=False: 실제 주문 실행

    주의: 매도는 KIS 보유분 내에서만 가능합니다.

    Args:
        data: 매도 주문 요청
    """
    user = getattr(request.state, "user", None)
    if not user:
        raise HTTPException(status_code=401, detail="로그인이 필요합니다")

    kis_client = KISClient()
    portfolio_service = MergedPortfolioService(db)
    price_service = TradingPriceService()

    ticker = data.ticker.upper()

    # 1. KIS 보유 정보 조회
    kis_info = await _get_kis_holdings(kis_client, ticker, data.market_type)
    kis_quantity = kis_info.get("quantity", 0)

    # 2. 매도 수량 검증
    is_valid, warning = price_service.validate_sell_quantity(
        kis_quantity, data.quantity
    )
    if not is_valid:
        raise HTTPException(status_code=400, detail=warning)

    # 3. 현재가 조회
    current_price = kis_info.get("current_price", 0)
    if current_price <= 0:
        current_price = await _get_current_price(kis_client, ticker, data.market_type)
    if current_price <= 0:
        raise HTTPException(status_code=400, detail="현재가를 조회할 수 없습니다")

    # 4. 참조 평단가 조회
    ref = await portfolio_service.get_reference_prices(
        user.id, ticker, data.market_type,
        kis_holdings=kis_info if kis_quantity > 0 else None,
    )

    # 5. 매도 가격 계산
    try:
        result = price_service.calculate_sell_price(
            reference_prices=ref,
            current_price=current_price,
            strategy=data.price_strategy,
            profit_percent=data.profit_percent,
            manual_price=data.manual_price,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e)) from e

    order_price = result.price

    # 6. 예상 수익 계산
    expected_profit = price_service.calculate_expected_profit(
        data.quantity, order_price, ref
    )
    expected_profit_response = {
        k: ExpectedProfitResponse(**v.to_dict())
        for k, v in expected_profit.items()
    }

    # 7. 경고 메시지
    warning_msg = None
    if data.quantity < kis_quantity:
        warning_msg = f"KIS 보유 수량({kis_quantity}주) 중 {data.quantity}주만 매도합니다"

    # 8. 시뮬레이션 또는 실제 주문
    if data.dry_run:
        return OrderSimulationResponse(
            status="simulated",
            order_price=order_price,
            price_source=result.price_source,
            current_price=current_price,
            reference_prices=ReferencePricesResponse(**ref.to_dict()),
            expected_profit=expected_profit_response,
            warning=warning_msg,
        )

    # 실제 주문 실행
    try:
        if data.market_type == MarketType.KR:
            order_result = await kis_client.order_korea_stock(
                stock_code=ticker,
                order_type="sell",
                quantity=data.quantity,
                price=int(order_price),
            )
        else:
            order_result = await kis_client.order_overseas_stock(
                symbol=ticker,
                exchange_code="NASD",  # TODO: 거래소 코드 동적 처리
                order_type="sell",
                quantity=data.quantity,
                price=order_price,
            )

        if order_result and order_result.get("rt_cd") == "0":
            return OrderSimulationResponse(
                status="submitted",
                order_price=order_price,
                price_source=result.price_source,
                current_price=current_price,
                reference_prices=ReferencePricesResponse(**ref.to_dict()),
                expected_profit=expected_profit_response,
                warning=warning_msg,
                order_id=order_result.get("odno"),
                order_time=order_result.get("ord_tmd"),
            )
        else:
            error_msg = order_result.get("msg1", "주문 실패") if order_result else "주문 실패"
            return OrderSimulationResponse(
                status="failed",
                order_price=order_price,
                price_source=result.price_source,
                current_price=current_price,
                reference_prices=ReferencePricesResponse(**ref.to_dict()),
                expected_profit=expected_profit_response,
                warning=warning_msg,
                error=error_msg,
            )

    except Exception as e:
        logger.error(f"Order execution failed: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail=str(e)) from e
