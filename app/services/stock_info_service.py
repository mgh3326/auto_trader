from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select, update, delete, func, desc
from typing import List, Optional, Dict, Any, Tuple
from app.models.analysis import StockInfo, StockAnalysisResult
from app.core.db import get_db
from app.analysis.prompt import format_decimal


class StockInfoService:
    """주식 정보 관리 서비스"""
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    async def create_stock_info(self, stock_data: Dict[str, Any]) -> StockInfo:
        """새로운 주식 정보 생성"""
        stock_info = StockInfo(**stock_data)
        self.db.add(stock_info)
        await self.db.commit()
        await self.db.refresh(stock_info)
        return stock_info
    
    async def get_stock_info_by_symbol(self, symbol: str) -> Optional[StockInfo]:
        """심볼로 주식 정보 조회"""
        result = await self.db.execute(
            select(StockInfo).where(StockInfo.symbol == symbol)
        )
        return result.scalar_one_or_none()
    
    async def get_stock_info_by_id(self, stock_info_id: int) -> Optional[StockInfo]:
        """ID로 주식 정보 조회"""
        result = await self.db.execute(
            select(StockInfo).where(StockInfo.id == stock_info_id)
        )
        return result.scalar_one_or_none()
    
    async def get_all_active_stocks(self) -> List[StockInfo]:
        """활성화된 모든 주식 정보 조회"""
        result = await self.db.execute(
            select(StockInfo).where(StockInfo.is_active == True)
        )
        return result.scalars().all()
    
    async def get_stocks_by_type(self, instrument_type: str) -> List[StockInfo]:
        """상품 타입별 주식 정보 조회"""
        result = await self.db.execute(
            select(StockInfo).where(
                StockInfo.instrument_type == instrument_type,
                StockInfo.is_active == True
            )
        )
        return result.scalars().all()
    
    async def update_stock_info(self, stock_info_id: int, update_data: Dict[str, Any]) -> Optional[StockInfo]:
        """주식 정보 업데이트"""
        await self.db.execute(
            update(StockInfo)
            .where(StockInfo.id == stock_info_id)
            .values(**update_data)
        )
        await self.db.commit()
        return await self.get_stock_info_by_id(stock_info_id)
    
    async def deactivate_stock(self, stock_info_id: int) -> bool:
        """주식 비활성화"""
        await self.db.execute(
            update(StockInfo)
            .where(StockInfo.id == stock_info_id)
            .values(is_active=False)
        )
        await self.db.commit()
        return True
    
    async def activate_stock(self, stock_info_id: int) -> bool:
        """주식 활성화"""
        await self.db.execute(
            update(StockInfo)
            .where(StockInfo.id == stock_info_id)
            .values(is_active=True)
        )
        await self.db.commit()
        return True
    
    async def delete_stock_info(self, stock_info_id: int) -> bool:
        """주식 정보 삭제 (실제로는 비활성화 권장)"""
        await self.db.execute(
            delete(StockInfo).where(StockInfo.id == stock_info_id)
        )
        await self.db.commit()
        return True
    
    async def search_stocks(self, query: str, limit: int = 50) -> List[StockInfo]:
        """주식 검색 (심볼 또는 이름으로)"""
        result = await self.db.execute(
            select(StockInfo)
            .where(
                StockInfo.is_active == True,
                (StockInfo.symbol.ilike(f"%{query}%") | 
                 StockInfo.name.ilike(f"%{query}%"))
            )
            .limit(limit)
        )
        return result.scalars().all()
    
    async def get_stock_count_by_type(self) -> Dict[str, int]:
        """상품 타입별 주식 개수 조회"""
        result = await self.db.execute(
            select(StockInfo.instrument_type, func.count(StockInfo.id))
            .where(StockInfo.is_active == True)
            .group_by(StockInfo.instrument_type)
        )
        return {row[0]: row[1] for row in result.fetchall()}
    
    async def bulk_create_stocks(self, stocks_data: List[Dict[str, Any]]) -> List[StockInfo]:
        """여러 주식 정보 일괄 생성"""
        stock_infos = []
        for stock_data in stocks_data:
            stock_info = StockInfo(**stock_data)
            stock_infos.append(stock_info)
        
        self.db.add_all(stock_infos)
        await self.db.commit()
        
        # 생성된 객체들을 새로고침
        for stock_info in stock_infos:
            await self.db.refresh(stock_info)
        
        return stock_infos


# 편의 함수들
async def create_stock_if_not_exists(symbol: str, name: str, instrument_type: str, **kwargs) -> StockInfo:
    """주식이 존재하지 않으면 생성하고, 존재하면 반환"""
    from app.core.db import AsyncSessionLocal
    
    async with AsyncSessionLocal() as db:
        service = StockInfoService(db)
        
        existing_stock = await service.get_stock_info_by_symbol(symbol)
        if existing_stock:
            return existing_stock
        
        stock_data = {
            "symbol": symbol,
            "name": name,
            "instrument_type": instrument_type,
            **kwargs
        }
        
        return await service.create_stock_info(stock_data)


class StockAnalysisService:
    """주식 분석 결과 관리 서비스"""
    
    def __init__(self, db: AsyncSession):
        self.db = db
    
    async def get_latest_analysis_by_symbol(self, symbol: str) -> Optional[StockAnalysisResult]:
        """심볼로 최신 분석 결과 조회"""
        result = await self.db.execute(
            select(StockAnalysisResult)
            .join(StockInfo)
            .where(StockInfo.symbol == symbol)
            .order_by(desc(StockAnalysisResult.created_at))
            .limit(1)
        )
        return result.scalar_one_or_none()
    
    async def get_sell_price_range_by_symbol(self, symbol: str) -> Optional[Tuple[float, float]]:
        """심볼로 최신 분석 결과의 매도 가격 범위 조회
        
        Returns
        -------
        Optional[Tuple[float, float]]
            (appropriate_sell_min, appropriate_sell_max) 또는 None
        """
        analysis = await self.get_latest_analysis_by_symbol(symbol)
        if not analysis:
            return None
        
        # appropriate_sell 범위가 있으면 우선 사용
        if analysis.appropriate_sell_min is not None and analysis.appropriate_sell_max is not None:
            return (analysis.appropriate_sell_min, analysis.appropriate_sell_max)
        
        # appropriate_sell 범위가 없으면 sell_target 범위 사용
        if analysis.sell_target_min is not None and analysis.sell_target_max is not None:
            return (analysis.sell_target_min, analysis.sell_target_max)
        
        return None
    
    async def get_sell_price_by_symbol(self, symbol: str, use_min: bool = False) -> Optional[float]:
        """심볼로 최신 분석 결과의 매도 가격 조회
        
        Parameters
        ----------
        symbol : str
            종목 심볼
        use_min : bool, default False
            True면 최소값, False면 최대값 반환
        
        Returns
        -------
        Optional[float]
            매도 가격 또는 None
        """
        price_range = await self.get_sell_price_range_by_symbol(symbol)
        if not price_range:
            return None
        
        return price_range[0] if use_min else price_range[1]
    
    async def get_latest_analysis_results_for_coins(self, coin_symbols: List[str]) -> Dict[str, Optional[StockAnalysisResult]]:
        """여러 코인의 최신 분석 결과를 한 번에 조회"""
        results = {}
        
        for symbol in coin_symbols:
            analysis = await self.get_latest_analysis_by_symbol(symbol)
            results[symbol] = analysis
        
        return results


# 편의 함수들
async def get_coin_sell_price(symbol: str, use_min: bool = False) -> Optional[float]:
    """코인의 매도 가격을 조회하는 편의 함수"""
    from app.core.db import AsyncSessionLocal
    
    async with AsyncSessionLocal() as db:
        service = StockAnalysisService(db)
        return await service.get_sell_price_by_symbol(symbol, use_min=use_min)


async def get_coin_sell_price_range(symbol: str) -> Optional[Tuple[float, float]]:
    """코인의 매도 가격 범위를 조회하는 편의 함수"""
    from app.core.db import AsyncSessionLocal
    
    async with AsyncSessionLocal() as db:
        service = StockAnalysisService(db)
        return await service.get_sell_price_range_by_symbol(symbol)


async def get_coin_buy_price_ranges(symbol: str) -> Dict[str, Optional[Tuple[float, float]]]:
    """코인의 매수 가격 범위들을 조회하는 편의 함수
    
    Returns
    -------
    Dict[str, Optional[Tuple[float, float]]]
        {
            'appropriate_buy': (min, max) or None,
            'buy_hope': (min, max) or None
        }
    """
    from app.core.db import AsyncSessionLocal
    
    async with AsyncSessionLocal() as db:
        service = StockAnalysisService(db)
        analysis = await service.get_latest_analysis_by_symbol(symbol)
        
        if not analysis:
            return {'appropriate_buy': None, 'buy_hope': None}
        
        result = {}
        
        # appropriate_buy 범위
        if analysis.appropriate_buy_min is not None and analysis.appropriate_buy_max is not None:
            result['appropriate_buy'] = (analysis.appropriate_buy_min, analysis.appropriate_buy_max)
        else:
            result['appropriate_buy'] = None
        
        # buy_hope 범위
        if analysis.buy_hope_min is not None and analysis.buy_hope_max is not None:
            result['buy_hope'] = (analysis.buy_hope_min, analysis.buy_hope_max)
        else:
            result['buy_hope'] = None
        
        return result


async def check_buy_condition_with_analysis(symbol: str, current_price: float, avg_buy_price: float) -> Tuple[bool, str]:
    """분석 결과와 1% 룰을 모두 확인하여 매수 조건을 판단
    
    Parameters
    ----------
    symbol : str
        종목 심볼
    current_price : float
        현재가
    avg_buy_price : float
        평균 매수가
    
    Returns
    -------
    Tuple[bool, str]
        (매수 가능 여부, 판단 근거)
    """
    from app.core.db import AsyncSessionLocal
    
    async with AsyncSessionLocal() as db:
        service = StockAnalysisService(db)
        analysis = await service.get_latest_analysis_by_symbol(symbol)
        
        # 1. 기본 조건: 현재가가 평균 매수가보다 1% 낮아야 함
        target_price = avg_buy_price * 0.99
        if current_price >= target_price:
            return False, f"현재가 {format_decimal(current_price, '₩')}원이 목표가 {format_decimal(target_price, '₩')}원보다 높음"
        
        # 2. 분석 결과가 없으면 1% 룰만으로 판단
        if not analysis:
            return True, "분석 결과 없음, 1% 룰만 적용하여 매수 가능"
        
        # 3. 분석 결과에서 매수 가격 범위 확인
        buy_ranges = []
        range_info = []
        
        # appropriate_buy 범위 확인
        if analysis.appropriate_buy_min is not None and analysis.appropriate_buy_max is not None:
            buy_ranges.append((analysis.appropriate_buy_min, analysis.appropriate_buy_max))
            range_info.append(f"적절매수: {format_decimal(analysis.appropriate_buy_min, '₩')}~{format_decimal(analysis.appropriate_buy_max, '₩')}원")
        
        # buy_hope 범위 확인
        if analysis.buy_hope_min is not None and analysis.buy_hope_max is not None:
            buy_ranges.append((analysis.buy_hope_min, analysis.buy_hope_max))
            range_info.append(f"희망매수: {format_decimal(analysis.buy_hope_min, '₩')}~{format_decimal(analysis.buy_hope_max, '₩')}원")
        
        # 분석 결과에 매수 범위가 없으면 1% 룰만 적용
        if not buy_ranges:
            return True, f"분석 결과에 매수 범위 없음, 1% 룰로 매수 가능 ({', '.join(range_info)})"
        
        # 4. 현재가가 매수 범위 중 하나라도 포함되는지 확인
        for min_price, max_price in buy_ranges:
            if min_price <= current_price <= max_price:
                return True, f"현재가 {format_decimal(current_price, '₩')}원이 매수 범위에 포함됨 ({', '.join(range_info)})"
        
        # 5. 매수 범위에 포함되지 않음
        return False, f"현재가 {format_decimal(current_price, '₩')}원이 매수 범위에 포함되지 않음 ({', '.join(range_info)})"


async def process_buy_orders_with_analysis(symbol: str, current_price: float, avg_buy_price: float) -> Dict[str, Any]:
    """분석 결과를 기반으로 조건 확인 후 매수 주문을 처리합니다.

    Returns
    -------
    Dict[str, Any]
        {
            'success': bool,
            'message': str,
            'orders_placed': int,
            'total_amount': float,
            'failure_reasons': List[str] (optional, failures only)
        }
    """
    from app.core.db import AsyncSessionLocal
    from app.services import upbit
    from app.core.config import settings

    # 1. KRW 잔고 먼저 확인
    print(f"💰 KRW 잔고 확인 중...")
    is_sufficient, krw_balance = await upbit.check_krw_balance_sufficient(settings.upbit_min_krw_balance)

    print(f"현재 KRW 잔고: {format_decimal(krw_balance, '₩')}원")
    print(f"최소 필요 잔고: {format_decimal(settings.upbit_min_krw_balance, '₩')}원")

    if not is_sufficient:
        message = f"KRW 잔고 부족: 최소 {format_decimal(settings.upbit_min_krw_balance, '₩')}원 필요"
        print(f"❌ {message}")
        return {
            'success': False,
            'message': message,
            'orders_placed': 0,
            'total_amount': 0.0,
            'insufficient_balance': True,
            'failure_reasons': [message],
        }

    print(f"✅ KRW 잔고 충분: 매수 가능")

    async with AsyncSessionLocal() as db:
        service = StockAnalysisService(db)
        analysis = await service.get_latest_analysis_by_symbol(symbol)

        # 1. 기본 조건: 현재가가 평균 매수가보다 1% 낮아야 함
        target_price = avg_buy_price * 0.99
        if current_price >= target_price:
            message = (
                "1% 매수 조건을 충족하지 않습니다: "
                f"현재가 {format_decimal(current_price, '₩')}원, "
                f"목표가 {format_decimal(target_price, '₩')}원"
            )
            print(f"❌ {message}")
            return {
                'success': False,
                'message': message,
                'orders_placed': 0,
                'total_amount': 0.0
            }

        # 2. 분석 결과가 없으면 1% 룰만으로 판단
        if not analysis:
            message = "분석 결과 없음: 매수를 건너뜁니다"
            print(f"✅ 매수 조건 충족: 분석 결과 없음, 1% 룰만 적용")
            print(f"  ⚠️ {message}")
            return {
                'success': False,
                'message': message,
                'orders_placed': 0,
                'total_amount': 0.0
            }

        # 3. 분석 결과 확인 (4개 가격 값이 있는지만 확인)
        price_count = 0
        if analysis.appropriate_buy_min is not None:
            price_count += 1
        if analysis.appropriate_buy_max is not None:
            price_count += 1
        if analysis.buy_hope_min is not None:
            price_count += 1
        if analysis.buy_hope_max is not None:
            price_count += 1

        if price_count == 0:
            message = "분석 결과에 가격 정보 없음: 매수를 건너뜁니다"
            print(f"✅ 기본 매수 조건 충족: 분석 결과에 가격 정보 없음, 1% 룰만 적용")
            print(f"  ⚠️ {message}")
            return {
                'success': False,
                'message': message,
                'orders_placed': 0,
                'total_amount': 0.0
            }

        print(f"✅ 기본 매수 조건 충족: 1% 룰 통과, 분석 결과 {price_count}개 가격 확인 예정")

        # 5. 4개 가격 값 중 평균 매수가보다 1% 낮고 현재가보다 낮은 것들을 찾아서 각각 10만원씩 매수
        return await _place_multiple_buy_orders_by_analysis(symbol, current_price, avg_buy_price, analysis)


async def _place_multiple_buy_orders_by_analysis(market: str, current_price: float, avg_buy_price: float, analysis) -> Dict[str, Any]:
    """분석 결과의 4개 가격 값 중 평균 매수가보다 1% 낮고 현재가보다 낮은 것들을 각각 설정된 금액/수량씩 매수합니다.

    암호화폐의 경우:
    - 종목별 설정이 있으면 설정된 금액 사용
    - 설정이 없으면 사용자 기본 설정의 crypto_default_buy_amount 사용 (기본 10,000원)

    Returns
    -------
    Dict[str, Any]
        {
            'success': bool,
            'message': str,
            'orders_placed': int,
            'total_amount': float
        }
    """
    from app.services import upbit
    from app.core.config import settings
    from app.core.db import AsyncSessionLocal
    from app.services.symbol_trade_settings_service import (
        SymbolTradeSettingsService,
        UserTradeDefaultsService,
        get_buy_amount_for_crypto,
    )

    # 코인 코드 추출 (KRW-BTC -> BTC)
    currency = market.replace("KRW-", "")

    # 종목 설정 및 사용자 기본 설정 조회
    async with AsyncSessionLocal() as db:
        settings_service = SymbolTradeSettingsService(db)
        symbol_settings = await settings_service.get_by_symbol(currency)

        # 종목별 설정이 있으면 그 금액 사용, 없으면 사용자 기본값 또는 시스템 기본값(10,000원) 사용
        if symbol_settings and symbol_settings.is_active:
            buy_amount = float(symbol_settings.buy_quantity_per_order)
            use_settings_mode = True
        else:
            # 암호화폐는 설정이 없어도 기본 금액으로 매수
            buy_amount = await get_buy_amount_for_crypto(db, currency, default_amount=10000)
            use_settings_mode = False

    print(f"📊 {market} 분석 기반 다중 매수 주문 처리")
    print(f"현재가: {format_decimal(current_price, '₩')}원")
    print(f"평균 매수가: {format_decimal(avg_buy_price, '₩')}원")
    if use_settings_mode:
        print(f"매수 금액: {format_decimal(buy_amount, '₩')}원 (종목 설정)")
    else:
        print(f"매수 금액: {format_decimal(buy_amount, '₩')}원 (기본값)")

    # 1% 룰 기준가 계산
    threshold_price = avg_buy_price * 0.99
    print(f"매수 기준가 (99%): {format_decimal(threshold_price, '₩')}원")

    # 4개 가격 값 추출
    buy_prices = []

    if analysis.appropriate_buy_min is not None:
        buy_prices.append(("appropriate_buy_min", analysis.appropriate_buy_min))
    if analysis.appropriate_buy_max is not None:
        buy_prices.append(("appropriate_buy_max", analysis.appropriate_buy_max))
    if analysis.buy_hope_min is not None:
        buy_prices.append(("buy_hope_min", analysis.buy_hope_min))
    if analysis.buy_hope_max is not None:
        buy_prices.append(("buy_hope_max", analysis.buy_hope_max))

    # 범위 정보 출력
    if analysis.appropriate_buy_min is not None and analysis.appropriate_buy_max is not None:
        print(f"적절한 매수 범위: {format_decimal(analysis.appropriate_buy_min, '₩')}원 ~ {format_decimal(analysis.appropriate_buy_max, '₩')}원")
    if analysis.buy_hope_min is not None and analysis.buy_hope_max is not None:
        print(f"희망 매수 범위: {format_decimal(analysis.buy_hope_min, '₩')}원 ~ {format_decimal(analysis.buy_hope_max, '₩')}원")

    if not buy_prices:
        message = "분석 결과에 매수 가격 정보가 없습니다"
        print(f"❌ {message}")
        return {
            'success': False,
            'message': message,
            'orders_placed': 0,
            'total_amount': 0.0
        }

    # 조건에 맞는 가격들 필터링 (평균 매수가의 99%보다 낮고 현재가보다 낮아야 함)
    valid_prices = []
    for price_name, price_value in buy_prices:
        is_below_threshold = price_value < threshold_price
        is_below_current = price_value < current_price

        if is_below_threshold and is_below_current:
            valid_prices.append((price_name, price_value))
            threshold_diff = ((threshold_price - price_value) / threshold_price * 100)
            current_diff = ((current_price - price_value) / current_price * 100)
            print(f"✅ {price_name}: {format_decimal(price_value, '₩')}원 (기준가보다 {threshold_diff:.1f}% 낮음, 현재가보다 {current_diff:.1f}% 낮음)")
        else:
            reasons = []
            if not is_below_threshold:
                reasons.append("기준가보다 높음")
            if not is_below_current:
                reasons.append("현재가보다 높음")
            print(f"❌ {price_name}: {format_decimal(price_value, '₩')}원 ({', '.join(reasons)})")

    if not valid_prices:
        message = "조건에 맞는 매수 가격이 없습니다 (기준가보다 낮고 현재가보다 낮아야 함)"
        print(f"⚠️ {message}")
        return {
            'success': False,
            'message': message,
            'orders_placed': 0,
            'total_amount': 0.0
        }

    print(f"\n🎯 총 {len(valid_prices)}개 가격에서 매수 주문 실행:")

    # 각 가격별로 금액 기반 매수 주문 실행
    success_count = 0
    total_orders = len(valid_prices)
    total_amount_placed = 0.0
    failure_reasons: List[str] = []

    for i, (price_name, buy_price) in enumerate(valid_prices, 1):
        print(f"\n[{i}/{total_orders}] {price_name} - {format_decimal(buy_price, '₩')}원")

        # 금액 기반 매수 (암호화폐)
        result = await _place_single_buy_order(
            market,
            buy_amount,
            buy_price,
            price_name,
            failure_reasons=failure_reasons,
        )
        if result:
            success_count += 1
            total_amount_placed += buy_amount

        # 주문 간 약간의 지연 (API 제한 고려)
        if i < total_orders:
            import asyncio
            await asyncio.sleep(0.5)

    print(f"\n📈 매수 주문 완료: {success_count}/{total_orders}개 성공")

    if success_count > 0:
        return {
            'success': True,
            'message': f"{success_count}개 매수 주문 성공",
            'orders_placed': success_count,
            'total_amount': total_amount_placed
        }
    else:
        unique_reasons = list(dict.fromkeys(failure_reasons))
        failure_message = "모든 매수 주문 실패"
        if unique_reasons:
            failure_message = f"{failure_message}: {unique_reasons[0]}"

        return {
            'success': False,
            'message': failure_message,
            'orders_placed': 0,
            'total_amount': 0.0,
            'failure_reasons': unique_reasons,
        }


async def _place_single_buy_order(
    market: str,
    amount: int,
    buy_price: float,
    price_name: str,
    failure_reasons: Optional[List[str]] = None,
):
    """단일 가격으로 매수 주문을 실행합니다."""
    from app.services import upbit
    
    try:
        # 매수 수량 계산 (수수료 고려)
        fee_rate = 0.0005  # 업비트 수수료 0.05%
        effective_amount = amount * (1 - fee_rate)
        volume = effective_amount / buy_price
        
        # 업비트 가격 단위에 맞게 조정
        adjusted_price = upbit.adjust_price_to_upbit_unit(buy_price)
        
        print(f"  💰 {amount:,}원 지정가 매수 주문")
        print(f"    - 원본 가격: {buy_price:,.2f}원")
        print(f"    - 조정 가격: {adjusted_price:,.5f}원 (업비트 단위)")
        print(f"    - 주문 수량: {volume:.8f}")
        
        # 지정가 매수 주문
        order_result = await upbit.place_buy_order(
            market=market,
            price=str(adjusted_price),
            volume=str(volume),
            ord_type="limit"
        )
        
        print(f"    ✅ 주문 성공:")
        print(f"      - 주문 ID: {order_result.get('uuid')}")
        print(f"      - 실제 주문가: {adjusted_price:,.5f}원")
        print(f"      - 예상 금액: {format_decimal(adjusted_price * volume, '₩')}원")
        print(f"      - 주문 시간: {order_result.get('created_at')}")
        
        return order_result
        
    except Exception as e:
        print(f"    ❌ {price_name} 매수 주문 실패: {e}")
        if failure_reasons is not None:
            failure_reasons.append(str(e))
        return None


async def _place_single_buy_order_by_quantity(market: str, quantity: float, buy_price: float, price_name: str):
    """수량 기반으로 단일 가격 매수 주문을 실행합니다."""
    from app.services import upbit

    try:
        # 업비트 가격 단위에 맞게 조정
        adjusted_price = upbit.adjust_price_to_upbit_unit(buy_price)
        estimated_amount = adjusted_price * quantity

        print(f"  💰 {quantity} 개 지정가 매수 주문")
        print(f"    - 원본 가격: {buy_price:,.2f}원")
        print(f"    - 조정 가격: {adjusted_price:,.5f}원 (업비트 단위)")
        print(f"    - 주문 수량: {quantity:.8f}")
        print(f"    - 예상 금액: {format_decimal(estimated_amount, '₩')}원")

        # 지정가 매수 주문
        order_result = await upbit.place_buy_order(
            market=market,
            price=str(adjusted_price),
            volume=str(quantity),
            ord_type="limit"
        )

        print(f"    ✅ 주문 성공:")
        print(f"      - 주문 ID: {order_result.get('uuid')}")
        print(f"      - 실제 주문가: {adjusted_price:,.5f}원")
        print(f"      - 예상 금액: {format_decimal(estimated_amount, '₩')}원")
        print(f"      - 주문 시간: {order_result.get('created_at')}")

        return order_result

    except Exception as e:
        print(f"    ❌ {price_name} 매수 주문 실패: {e}")
        return None


# 가격 조정 함수는 upbit.adjust_price_to_upbit_unit() 사용
