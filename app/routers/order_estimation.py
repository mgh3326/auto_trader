"""Order Estimation Router — 주문 비용 추정 API"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import InstrumentType
from app.routers.dependencies import get_user_from_request
from app.services.order_estimation_service import (
    calculate_estimated_order_cost,
    extract_buy_prices_from_analysis,
    fetch_pending_crypto_buy_cost,
    fetch_pending_domestic_buy_cost,
    fetch_pending_overseas_buy_cost,
)
from app.services.stock_info_service import StockAnalysisService
from app.services.symbol_trade_settings_service import (
    SymbolTradeSettingsService,
    UserTradeDefaultsService,
)
from app.services.upbit_symbol_universe_service import get_active_upbit_base_currencies

router = APIRouter(prefix="/api/symbol-settings", tags=["symbol-settings"])


class EstimatedCostResponse(BaseModel):
    """예상 비용 응답"""

    symbol: str
    quantity_per_order: float
    buy_prices: list[dict]
    total_orders: int
    total_quantity: float
    total_cost: float
    currency: str


class AllEstimatedCostResponse(BaseModel):
    """전체 예상 비용 응답"""

    symbols: list[EstimatedCostResponse]
    grand_total_cost: float
    total_symbols: int
    pending_buy_orders_cost: float = 0.0
    net_estimated_cost: float = 0.0


async def _estimate_costs_for_settings(
    settings_list,
    analysis_service: StockAnalysisService,
    currency: str,
) -> tuple[list[EstimatedCostResponse], float]:
    """설정 목록에 대한 비용 추정 공통 루프

    Returns:
        (results, grand_total)
    """
    results = []
    grand_total = 0.0

    for settings_obj in settings_list:
        analysis = await analysis_service.get_latest_analysis_by_symbol(
            settings_obj.symbol
        )
        if not analysis:
            continue

        buy_prices = extract_buy_prices_from_analysis(analysis)
        if not buy_prices:
            continue

        limited_buy_prices = buy_prices[: settings_obj.buy_price_levels]

        result = calculate_estimated_order_cost(
            symbol=settings_obj.symbol,
            buy_prices=limited_buy_prices,
            quantity_per_order=float(settings_obj.buy_quantity_per_order),
            currency=currency,
        )

        results.append(EstimatedCostResponse(**result))
        grand_total += result["total_cost"]

    return results, grand_total


# NOTE: 고정 경로는 반드시 경로 파라미터({symbol}) 라우트보다 먼저 정의해야 함
@router.get("/symbols/domestic/estimated-cost", response_model=AllEstimatedCostResponse)
async def get_domestic_estimated_costs(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """국내 주식 예상 매수 비용 합계 (미체결 매수 주문 금액 차감)

    설정된 국내 주식 종목에 대해 예상 비용을 계산하고,
    기존 미체결 매수 주문 금액을 차감한 순 비용을 반환합니다.
    """
    user = await get_user_from_request(request, db)
    settings_service = SymbolTradeSettingsService(db)
    analysis_service = StockAnalysisService(db)

    all_settings = await settings_service.get_all(user.id, active_only=True)
    domestic_settings = [
        s for s in all_settings if s.instrument_type == InstrumentType.equity_kr
    ]

    results, grand_total = await _estimate_costs_for_settings(
        domestic_settings, analysis_service, currency="KRW"
    )

    pending_buy_cost = await fetch_pending_domestic_buy_cost()
    net_cost = max(0.0, grand_total - pending_buy_cost)

    return AllEstimatedCostResponse(
        symbols=results,
        grand_total_cost=grand_total,
        total_symbols=len(results),
        pending_buy_orders_cost=pending_buy_cost,
        net_estimated_cost=net_cost,
    )


@router.get("/symbols/overseas/estimated-cost", response_model=AllEstimatedCostResponse)
async def get_overseas_estimated_costs(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """해외 주식 예상 매수 비용 합계 (미체결 매수 주문 금액 차감)

    설정된 해외 주식 종목에 대해 예상 비용을 계산하고,
    기존 미체결 매수 주문 금액을 차감한 순 비용을 반환합니다.
    """
    user = await get_user_from_request(request, db)
    settings_service = SymbolTradeSettingsService(db)
    analysis_service = StockAnalysisService(db)

    all_settings = await settings_service.get_all(user.id, active_only=True)
    overseas_settings = [
        s for s in all_settings if s.instrument_type == InstrumentType.equity_us
    ]

    results, grand_total = await _estimate_costs_for_settings(
        overseas_settings, analysis_service, currency="USD"
    )

    pending_buy_cost = await fetch_pending_overseas_buy_cost()
    net_cost = max(0.0, grand_total - pending_buy_cost)

    return AllEstimatedCostResponse(
        symbols=results,
        grand_total_cost=grand_total,
        total_symbols=len(results),
        pending_buy_orders_cost=pending_buy_cost,
        net_estimated_cost=net_cost,
    )


@router.get("/symbols/all/estimated-cost", response_model=AllEstimatedCostResponse)
async def get_all_estimated_costs(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 모든 활성 종목 예상 매수 비용 합계

    설정된 모든 종목에 대해 예상 비용을 계산하고 합계를 반환합니다.
    """
    user = await get_user_from_request(request, db)
    settings_service = SymbolTradeSettingsService(db)
    analysis_service = StockAnalysisService(db)

    all_settings = await settings_service.get_all(user.id, active_only=True)

    results = []
    grand_total = 0.0

    for settings_obj in all_settings:
        analysis = await analysis_service.get_latest_analysis_by_symbol(
            settings_obj.symbol
        )
        if not analysis:
            continue

        buy_prices = extract_buy_prices_from_analysis(analysis)
        if not buy_prices:
            continue

        currency = (
            "USD" if settings_obj.instrument_type == InstrumentType.equity_us else "KRW"
        )

        result = calculate_estimated_order_cost(
            symbol=settings_obj.symbol,
            buy_prices=buy_prices,
            quantity_per_order=float(settings_obj.buy_quantity_per_order),
            currency=currency,
        )

        results.append(EstimatedCostResponse(**result))
        grand_total += result["total_cost"]

    return AllEstimatedCostResponse(
        symbols=results,
        grand_total_cost=grand_total,
        total_symbols=len(results),
        pending_buy_orders_cost=0.0,
        net_estimated_cost=grand_total,
    )


@router.get("/symbols/crypto/estimated-cost", response_model=AllEstimatedCostResponse)
async def get_crypto_estimated_costs(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """암호화폐 예상 매수 비용 합계 (미체결 매수 주문 금액 차감)

    보유 코인 전체에 대해 예상 비용을 계산합니다.
    - 종목 설정이 있으면 설정된 금액 사용
    - 종목 설정이 없으면 사용자 기본 설정(crypto_default_buy_amount) 또는 10,000원 사용
    기존 미체결 매수 주문 금액을 차감한 순 비용을 반환합니다.
    """
    import app.services.brokers.upbit.client as upbit

    user = await get_user_from_request(request, db)
    settings_service = SymbolTradeSettingsService(db)
    analysis_service = StockAnalysisService(db)
    defaults_service = UserTradeDefaultsService(db)

    # 사용자 기본 설정에서 기본 매수 금액 조회
    user_defaults = await defaults_service.get_or_create(user.id)
    default_buy_amount = (
        float(user_defaults.crypto_default_buy_amount) if user_defaults else 10000.0
    )

    # 보유 코인 조회
    my_coins = await upbit.fetch_my_coins()
    tradable_currencies = await get_active_upbit_base_currencies(
        quote_currency="KRW",
        db=db,
    )

    # 거래 가능한 코인만 필터링
    MIN_TRADE_THRESHOLD = 1000
    tradable_coins = [
        coin
        for coin in my_coins
        if str(coin.get("currency") or "").upper() != "KRW"
        and (
            (float(coin.get("balance", 0)) + float(coin.get("locked", 0)))
            * float(coin.get("avg_buy_price", 0))
        )
        >= MIN_TRADE_THRESHOLD
        and str(coin.get("currency") or "").upper() in tradable_currencies
    ]

    # 종목별 설정 조회
    all_settings = await settings_service.get_all(user.id, active_only=True)
    settings_map = {
        s.symbol: s for s in all_settings if s.instrument_type == InstrumentType.crypto
    }

    results = []
    grand_total = 0.0

    for coin in tradable_coins:
        currency = coin.get("currency")
        market = f"KRW-{currency}"

        analysis = await analysis_service.get_latest_analysis_by_symbol(market)
        if not analysis:
            continue

        buy_prices = extract_buy_prices_from_analysis(analysis)
        if not buy_prices:
            continue

        # 설정 조회 (없으면 기본값 사용)
        settings_obj = settings_map.get(market)
        if settings_obj:
            buy_amount = float(settings_obj.buy_quantity_per_order)
            buy_price_levels = settings_obj.buy_price_levels
        else:
            buy_amount = default_buy_amount
            buy_price_levels = 4

        limited_buy_prices = buy_prices[:buy_price_levels]

        # 암호화폐는 금액 기반 매수 → amount_based=True
        result = calculate_estimated_order_cost(
            symbol=market,
            buy_prices=limited_buy_prices,
            quantity_per_order=buy_amount,
            currency="KRW",
            amount_based=True,
        )

        results.append(EstimatedCostResponse(**result))
        grand_total += result["total_cost"]

    pending_buy_cost = await fetch_pending_crypto_buy_cost()
    net_cost = max(0.0, grand_total - pending_buy_cost)

    return AllEstimatedCostResponse(
        symbols=results,
        grand_total_cost=grand_total,
        total_symbols=len(results),
        pending_buy_orders_cost=pending_buy_cost,
        net_estimated_cost=net_cost,
    )


@router.get("/symbols/{symbol}/estimated-cost", response_model=EstimatedCostResponse)
async def get_estimated_cost(
    symbol: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """특정 종목의 예상 매수 비용 계산

    AI 분석 결과의 4개 매수 가격을 기반으로 예상 비용을 계산합니다.
    """
    user = await get_user_from_request(request, db)
    settings_service = SymbolTradeSettingsService(db)
    analysis_service = StockAnalysisService(db)

    settings_obj = await settings_service.get_by_symbol(symbol, user.id)
    if not settings_obj or not settings_obj.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Active settings not found for symbol: {symbol}",
        )

    analysis = await analysis_service.get_latest_analysis_by_symbol(symbol)
    if not analysis:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No analysis found for symbol: {symbol}",
        )

    buy_prices = extract_buy_prices_from_analysis(analysis)
    if not buy_prices:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No buy prices in analysis for symbol: {symbol}",
        )

    currency = (
        "USD" if settings_obj.instrument_type == InstrumentType.equity_us else "KRW"
    )

    result = calculate_estimated_order_cost(
        symbol=symbol,
        buy_prices=buy_prices,
        quantity_per_order=float(settings_obj.buy_quantity_per_order),
        currency=currency,
    )

    return EstimatedCostResponse(**result)
