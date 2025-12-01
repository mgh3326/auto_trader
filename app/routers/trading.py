"""
Trading Router

매수/매도 주문 API 엔드포인트
"""
import logging

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.manual_holdings import MarketType
from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.manual_holdings import (
    BuyOrderRequest,
    SellOrderRequest,
    OrderSimulationResponse,
    ReferencePricesResponse,
    ExpectedProfitResponse,
)
from app.services.merged_portfolio_service import MergedPortfolioService
from app.services.trading_price_service import TradingPriceService
from app.services.kis import KISClient
from app.services.kis_holdings_service import get_kis_holding_for_ticker

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/trading", tags=["Trading"])
PRICE_FETCH_ERROR = "현재가를 조회할 수 없습니다"


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
    data: BuyOrderRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
):
    """매수 주문

    dry_run=True: 시뮬레이션 (기본값)
    dry_run=False: 실제 주문 실행

    Args:
        data: 매수 주문 요청
    """
    kis_client = KISClient()
    portfolio_service = MergedPortfolioService(db)
    price_service = TradingPriceService()

    ticker = data.ticker.upper()

    # 1. KIS 보유 정보 조회
    kis_info = await get_kis_holding_for_ticker(
        kis_client, ticker, data.market_type
    )

    # 2. 현재가 조회
    current_price = kis_info.get("current_price", 0)
    if current_price <= 0:
        current_price = await _get_current_price(kis_client, ticker, data.market_type)
    if current_price <= 0:
        raise HTTPException(status_code=400, detail=PRICE_FETCH_ERROR)

    # 3. 참조 평단가 조회
    ref = await portfolio_service.get_reference_prices(
        current_user.id, ticker, data.market_type,
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
            # 거래소 코드 조회
            from app.services.stock_info_service import StockInfoService
            stock_service = StockInfoService(db)
            stock_info = await stock_service.get_stock_info_by_symbol(ticker)
            
            exchange_code = "NASD"  # 기본값
            if stock_info and stock_info.exchange:
                exchange_code = stock_info.exchange
            else:
                # 하드코딩된 매핑 (fallback)
                EXCHANGE_MAP = {
                    "BRK.B": "NYSE",
                    "TSM": "NYSE",
                    "JPM": "NYSE",
                    "V": "NYSE",
                    "JNJ": "NYSE",
                    "WMT": "NYSE",
                    "PG": "NYSE",
                    "MA": "NYSE",
                    "HD": "NYSE",
                    "CVX": "NYSE",
                    "MRK": "NYSE",
                    "KO": "NYSE",
                    "PEP": "NYSE",
                    "BAC": "NYSE",
                    "ABBV": "NYSE",
                    "TMO": "NYSE",
                    "DIS": "NYSE",
                    "MCD": "NYSE",
                    "CSCO": "NASD",
                    "INTC": "NASD",
                    "CMCSA": "NASD",
                    "PFE": "NYSE",
                    "NFLX": "NASD",
                    "ADBE": "NASD",
                    "NKE": "NYSE",
                    "LLY": "NYSE",
                    "UNH": "NYSE",
                    "XOM": "NYSE",
                    "ORCL": "NYSE",
                    "CRM": "NYSE",
                    "ACN": "NYSE",
                    "LIN": "NYSE",
                    "ABT": "NYSE",
                    "DHR": "NYSE",
                    "VZ": "NYSE",
                    "NEE": "NYSE",
                    "TXN": "NASD",
                    "PM": "NYSE",
                    "RTX": "NYSE",
                    "UPS": "NYSE",
                    "MS": "NYSE",
                    "HON": "NASD",
                    "BMY": "NYSE",
                    "BA": "NYSE",
                    "AMGN": "NASD",
                    "LOW": "NYSE",
                    "CAT": "NYSE",
                    "GS": "NYSE",
                    "IBM": "NYSE",
                    "GE": "NYSE",
                    "INTU": "NASD",
                    "DE": "NYSE",
                    "SPGI": "NYSE",
                    "PLD": "NYSE",
                    "AXP": "NYSE",
                    "BLK": "NYSE",
                    "SYK": "NYSE",
                    "AMT": "NYSE",
                    "C": "NYSE",
                    "GILD": "NASD",
                    "MDLZ": "NASD",
                    "ADP": "NASD",
                    "TJX": "NYSE",
                    "ISRG": "NASD",
                    "MMC": "NYSE",
                    "BKNG": "NASD",
                    "ADI": "NASD",
                    "LMT": "NYSE",
                    "CVS": "NYSE",
                    "VRTX": "NASD",
                    "UBER": "NYSE",
                    "REGN": "NASD",
                    "ZTS": "NYSE",
                    "CI": "NYSE",
                    "BSX": "NYSE",
                    "SLB": "NYSE",
                    "BDX": "NYSE",
                    "FI": "NYSE",
                    "PGR": "NYSE",
                    "EOG": "NYSE",
                    "SO": "NYSE",
                    "EQIX": "NASD",
                    "PANW": "NASD",
                    "SNPS": "NASD",
                    "KLAC": "NASD",
                    "CDNS": "NASD",
                    "MELI": "NASD",
                    "MAR": "NASD",
                    "CSX": "NASD",
                    "ORLY": "NASD",
                    "MNST": "NASD",
                    "ROP": "NASD",
                    "CTAS": "NASD",
                    "ODFL": "NASD",
                    "PAYX": "NASD",
                    "PCAR": "NASD",
                    "KDP": "NASD",
                    "MCHP": "NASD",
                    "AEP": "NASD",
                    "LRCX": "NASD",
                    "MRNA": "NASD",
                    "IDXX": "NASD",
                    "FTNT": "NASD",
                    "DXCM": "NASD",
                    "EXC": "NASD",
                    "KHC": "NASD",
                    "XEL": "NASD",
                    "EA": "NASD",
                    "WBD": "NASD",
                    "BKR": "NASD",
                    "FANG": "NASD",
                    "FAST": "NASD",
                    "BIIB": "NASD",
                    "ROST": "NASD",
                    "DLTR": "NASD",
                    "APTV": "NYSE",
                    "ANET": "NYSE",
                    "ALGN": "NASD",
                    "ILMN": "NASD",
                    "TEAM": "NASD",
                    "WDAY": "NASD",
                    "LCID": "NASD",
                    "RIVN": "NASD",
                    "ZM": "NASD",
                    "DDOG": "NASD",
                    "CRWD": "NASD",
                    "ZS": "NASD",
                    "NET": "NYSE",
                    "PLTR": "NYSE",
                    "COIN": "NASD",
                    "HOOD": "NASD",
                    "DKNG": "NASD",
                    "RBLX": "NYSE",
                    "U": "NYSE",
                    "AFRM": "NASD",
                    "OPEN": "NASD",
                    "SOFI": "NASD",
                    "UPST": "NASD",
                    "MSTR": "NASD",
                    "SQ": "NYSE",
                    "PYPL": "NASD",
                    "SHOP": "NYSE",
                    "SE": "NYSE",
                    "SPOT": "NYSE",
                    "SNAP": "NYSE",
                    "PINS": "NYSE",
                    "TWLO": "NYSE",
                    "ROKU": "NASD",
                    "DOCU": "NASD",
                    "OKTA": "NASD",
                    "MDB": "NASD",
                    "TTD": "NASD",
                    "HUBS": "NYSE",
                    "BILL": "NYSE",
                    "NET": "NYSE",
                    "SNOW": "NYSE",
                    "U": "NYSE",
                    "PATH": "NYSE",
                    "GTLB": "NASD",
                    "HCP": "NASD",
                    "CFLT": "NASD",
                    "AMPL": "NYSE",
                    "S": "NYSE",
                    "IOT": "NYSE",
                    "AI": "NYSE",
                    "PLTR": "NYSE",
                }
                exchange_code = EXCHANGE_MAP.get(ticker, "NASD")

            order_result = await kis_client.order_overseas_stock(
                symbol=ticker,
                exchange_code=exchange_code,
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
    data: SellOrderRequest,
    db: AsyncSession = Depends(get_db),
    current_user: User = Depends(get_authenticated_user),
):
    """매도 주문

    dry_run=True: 시뮬레이션 (기본값)
    dry_run=False: 실제 주문 실행

    주의: 매도는 KIS 보유분 내에서만 가능합니다.

    Args:
        data: 매도 주문 요청
    """
    kis_client = KISClient()
    portfolio_service = MergedPortfolioService(db)
    price_service = TradingPriceService()

    ticker = data.ticker.upper()

    # 1. KIS 보유 정보 조회
    kis_info = await get_kis_holding_for_ticker(
        kis_client, ticker, data.market_type
    )
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
        raise HTTPException(status_code=400, detail=PRICE_FETCH_ERROR)

    # 4. 참조 평단가 조회
    ref = await portfolio_service.get_reference_prices(
        current_user.id, ticker, data.market_type,
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
            # 거래소 코드 조회
            from app.services.stock_info_service import StockInfoService
            stock_service = StockInfoService(db)
            stock_info = await stock_service.get_stock_info_by_symbol(ticker)
            
            exchange_code = "NASD"  # 기본값
            if stock_info and stock_info.exchange:
                exchange_code = stock_info.exchange
            else:
                # 하드코딩된 매핑 (fallback)
                EXCHANGE_MAP = {
                    "BRK.B": "NYSE",
                    "TSM": "NYSE",
                    "JPM": "NYSE",
                    "V": "NYSE",
                    "JNJ": "NYSE",
                    "WMT": "NYSE",
                    "PG": "NYSE",
                    "MA": "NYSE",
                    "HD": "NYSE",
                    "CVX": "NYSE",
                    "MRK": "NYSE",
                    "KO": "NYSE",
                    "PEP": "NYSE",
                    "BAC": "NYSE",
                    "ABBV": "NYSE",
                    "TMO": "NYSE",
                    "DIS": "NYSE",
                    "MCD": "NYSE",
                    "CSCO": "NASD",
                    "INTC": "NASD",
                    "CMCSA": "NASD",
                    "PFE": "NYSE",
                    "NFLX": "NASD",
                    "ADBE": "NASD",
                    "NKE": "NYSE",
                    "LLY": "NYSE",
                    "UNH": "NYSE",
                    "XOM": "NYSE",
                    "ORCL": "NYSE",
                    "CRM": "NYSE",
                    "ACN": "NYSE",
                    "LIN": "NYSE",
                    "ABT": "NYSE",
                    "DHR": "NYSE",
                    "VZ": "NYSE",
                    "NEE": "NYSE",
                    "TXN": "NASD",
                    "PM": "NYSE",
                    "RTX": "NYSE",
                    "UPS": "NYSE",
                    "MS": "NYSE",
                    "HON": "NASD",
                    "BMY": "NYSE",
                    "BA": "NYSE",
                    "AMGN": "NASD",
                    "LOW": "NYSE",
                    "CAT": "NYSE",
                    "GS": "NYSE",
                    "IBM": "NYSE",
                    "GE": "NYSE",
                    "INTU": "NASD",
                    "DE": "NYSE",
                    "SPGI": "NYSE",
                    "PLD": "NYSE",
                    "AXP": "NYSE",
                    "BLK": "NYSE",
                    "SYK": "NYSE",
                    "AMT": "NYSE",
                    "C": "NYSE",
                    "GILD": "NASD",
                    "MDLZ": "NASD",
                    "ADP": "NASD",
                    "TJX": "NYSE",
                    "ISRG": "NASD",
                    "MMC": "NYSE",
                    "BKNG": "NASD",
                    "ADI": "NASD",
                    "LMT": "NYSE",
                    "CVS": "NYSE",
                    "VRTX": "NASD",
                    "UBER": "NYSE",
                    "REGN": "NASD",
                    "ZTS": "NYSE",
                    "CI": "NYSE",
                    "BSX": "NYSE",
                    "SLB": "NYSE",
                    "BDX": "NYSE",
                    "FI": "NYSE",
                    "PGR": "NYSE",
                    "EOG": "NYSE",
                    "SO": "NYSE",
                    "EQIX": "NASD",
                    "PANW": "NASD",
                    "SNPS": "NASD",
                    "KLAC": "NASD",
                    "CDNS": "NASD",
                    "MELI": "NASD",
                    "MAR": "NASD",
                    "CSX": "NASD",
                    "ORLY": "NASD",
                    "MNST": "NASD",
                    "ROP": "NASD",
                    "CTAS": "NASD",
                    "ODFL": "NASD",
                    "PAYX": "NASD",
                    "PCAR": "NASD",
                    "KDP": "NASD",
                    "MCHP": "NASD",
                    "AEP": "NASD",
                    "LRCX": "NASD",
                    "MRNA": "NASD",
                    "IDXX": "NASD",
                    "FTNT": "NASD",
                    "DXCM": "NASD",
                    "EXC": "NASD",
                    "KHC": "NASD",
                    "XEL": "NASD",
                    "EA": "NASD",
                    "WBD": "NASD",
                    "BKR": "NASD",
                    "FANG": "NASD",
                    "FAST": "NASD",
                    "BIIB": "NASD",
                    "ROST": "NASD",
                    "DLTR": "NASD",
                    "APTV": "NYSE",
                    "ANET": "NYSE",
                    "ALGN": "NASD",
                    "ILMN": "NASD",
                    "TEAM": "NASD",
                    "WDAY": "NASD",
                    "LCID": "NASD",
                    "RIVN": "NASD",
                    "ZM": "NASD",
                    "DDOG": "NASD",
                    "CRWD": "NASD",
                    "ZS": "NASD",
                    "NET": "NYSE",
                    "PLTR": "NYSE",
                    "COIN": "NASD",
                    "HOOD": "NASD",
                    "DKNG": "NASD",
                    "RBLX": "NYSE",
                    "U": "NYSE",
                    "AFRM": "NASD",
                    "OPEN": "NASD",
                    "SOFI": "NASD",
                    "UPST": "NASD",
                    "MSTR": "NASD",
                    "SQ": "NYSE",
                    "PYPL": "NASD",
                    "SHOP": "NYSE",
                    "SE": "NYSE",
                    "SPOT": "NYSE",
                    "SNAP": "NYSE",
                    "PINS": "NYSE",
                    "TWLO": "NYSE",
                    "ROKU": "NASD",
                    "DOCU": "NASD",
                    "OKTA": "NASD",
                    "MDB": "NASD",
                    "TTD": "NASD",
                    "HUBS": "NYSE",
                    "BILL": "NYSE",
                    "NET": "NYSE",
                    "SNOW": "NYSE",
                    "U": "NYSE",
                    "PATH": "NYSE",
                    "GTLB": "NASD",
                    "HCP": "NASD",
                    "CFLT": "NASD",
                    "AMPL": "NYSE",
                    "S": "NYSE",
                    "IOT": "NYSE",
                    "AI": "NYSE",
                    "PLTR": "NYSE",
                }
                exchange_code = EXCHANGE_MAP.get(ticker, "NASD")

            order_result = await kis_client.order_overseas_stock(
                symbol=ticker,
                exchange_code=exchange_code,
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
