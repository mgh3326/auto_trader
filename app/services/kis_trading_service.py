import asyncio
import logging
from typing import Dict, Any, List, Optional, Tuple
from app.services.kis import KISClient
from app.models.analysis import StockAnalysisResult
from app.core.config import settings

logger = logging.getLogger(__name__)

# KIS 매수 설정 (주식은 설정이 없으면 매수하지 않음)
KIS_MIN_BALANCE = 100000  # 최소 예수금

# 메시지 상수
MSG_NO_SETTINGS = "종목 설정 없음 - 매수 건너뜀"

async def process_kis_domestic_buy_orders_with_analysis(
    kis_client: KISClient,
    symbol: str,
    current_price: float,
    avg_buy_price: float
) -> Dict[str, Any]:
    """분석 결과를 기반으로 KIS 국내 주식 매수 주문 처리"""
    from app.core.db import AsyncSessionLocal
    from app.services.stock_info_service import StockAnalysisService
    from app.services.symbol_trade_settings_service import (
        get_buy_quantity_for_symbol,
        SymbolTradeSettingsService,
    )

    async with AsyncSessionLocal() as db:
        service = StockAnalysisService(db)
        analysis = await service.get_latest_analysis_by_symbol(symbol)

        # 1. 기본 조건: 현재가가 평균 매수가보다 1% 낮아야 함 (평단가가 0이면 신규 진입으로 간주하여 통과)
        if avg_buy_price > 0:
            target_price = avg_buy_price * 0.99
            if current_price >= target_price:
                return {
                    'success': False,
                    'message': f"1% 매수 조건 미충족: 현재가 {current_price} >= 목표가 {target_price}",
                    'orders_placed': 0
                }

        # 2. 분석 결과 확인
        if not analysis:
            return {
                'success': False,
                'message': "분석 결과 없음",
                'orders_placed': 0
            }

        # 2.5 종목 설정 확인 및 buy_price_levels 가져오기
        settings_service = SymbolTradeSettingsService(db)
        settings = await settings_service.get_by_symbol(symbol)
        if not settings or not settings.is_active:
            logger.info(f"[{symbol}] {MSG_NO_SETTINGS}")
            return {
                'success': False,
                'message': MSG_NO_SETTINGS,
                'orders_placed': 0
            }

        buy_price_levels = settings.buy_price_levels  # 1~4 (주문할 가격대 수)

        # 3. 가격 정보 확인 (낮은 가격 순서: appropriate_buy_min -> max -> buy_hope_min -> max)
        all_buy_prices = []
        if analysis.appropriate_buy_min is not None:
            all_buy_prices.append(("appropriate_buy_min", analysis.appropriate_buy_min))
        if analysis.appropriate_buy_max is not None:
            all_buy_prices.append(("appropriate_buy_max", analysis.appropriate_buy_max))
        if analysis.buy_hope_min is not None:
            all_buy_prices.append(("buy_hope_min", analysis.buy_hope_min))
        if analysis.buy_hope_max is not None:
            all_buy_prices.append(("buy_hope_max", analysis.buy_hope_max))

        if not all_buy_prices:
            return {
                'success': False,
                'message': "분석 결과에 매수 가격 정보 없음",
                'orders_placed': 0
            }

        # buy_price_levels 설정에 따라 사용할 가격대 제한
        buy_prices = all_buy_prices[:buy_price_levels]

        # 4. 조건에 맞는 가격 필터링
        # 평균 매수가의 99%보다 낮고(평단가 있을시), 현재가보다 낮은 가격
        threshold_price = avg_buy_price * 0.99 if avg_buy_price > 0 else float('inf')

        valid_prices = []
        for name, price in buy_prices:
            if price < threshold_price and price < current_price:
                valid_prices.append((name, price))

        if not valid_prices:
            return {
                'success': False,
                'message': f"조건에 맞는 매수 가격 없음 ({buy_price_levels}개 가격대 중 유효 없음)",
                'orders_placed': 0
            }

        # 5. 수량 확인
        quantity = int(settings.buy_quantity_per_order)
        if quantity < 1:
            return {
                'success': False,
                'message': "설정된 수량이 1 미만",
                'orders_placed': 0
            }

        # 6. 주문 실행
        success_count = 0
        ordered_prices: List[float] = []
        ordered_quantities: List[int] = []
        total_amount = 0.0

        for name, price in valid_prices:
            res = await kis_client.order_korea_stock(
                stock_code=symbol,
                order_type="buy",
                quantity=quantity,
                price=int(price)
            )

            if res and res.get('odno'):
                success_count += 1
                ordered_prices.append(price)
                ordered_quantities.append(quantity)
                total_amount += price * quantity

            await asyncio.sleep(0.2)

        return {
            'success': success_count > 0,
            'message': f"{success_count}개 주문 성공 (설정: {buy_price_levels}개 가격대)",
            'orders_placed': success_count,
            'prices': ordered_prices,
            'quantities': ordered_quantities,
            'total_amount': total_amount
        }

async def process_kis_overseas_buy_orders_with_analysis(
    kis_client: KISClient,
    symbol: str,
    current_price: float,
    avg_buy_price: float,
    exchange_code: str = "NASD"
) -> Dict[str, Any]:
    """분석 결과를 기반으로 KIS 해외 주식 매수 주문 처리"""
    from app.core.db import AsyncSessionLocal
    from app.services.stock_info_service import StockAnalysisService
    from app.services.symbol_trade_settings_service import SymbolTradeSettingsService

    async with AsyncSessionLocal() as db:
        service = StockAnalysisService(db)
        analysis = await service.get_latest_analysis_by_symbol(symbol)

        if avg_buy_price > 0:
            target_price = avg_buy_price * 0.99
            if current_price >= target_price:
                return {'success': False, 'message': "1% 매수 조건 미충족", 'orders_placed': 0}

        if not analysis:
            return {'success': False, 'message': "분석 결과 없음", 'orders_placed': 0}

        # 종목 설정 확인 및 buy_price_levels 가져오기
        settings_service = SymbolTradeSettingsService(db)
        settings = await settings_service.get_by_symbol(symbol)
        if not settings or not settings.is_active:
            logger.info(f"[{symbol}] {MSG_NO_SETTINGS}")
            return {
                'success': False,
                'message': MSG_NO_SETTINGS,
                'orders_placed': 0
            }

        buy_price_levels = settings.buy_price_levels  # 1~4 (주문할 가격대 수)
        # settings에 exchange_code가 설정되어 있으면 그것을 사용
        actual_exchange_code = settings.exchange_code or exchange_code

        # 가격 정보 확인 (낮은 가격 순서: appropriate_buy_min -> max -> buy_hope_min -> max)
        all_buy_prices = []
        if analysis.appropriate_buy_min:
            all_buy_prices.append(analysis.appropriate_buy_min)
        if analysis.appropriate_buy_max:
            all_buy_prices.append(analysis.appropriate_buy_max)
        if analysis.buy_hope_min:
            all_buy_prices.append(analysis.buy_hope_min)
        if analysis.buy_hope_max:
            all_buy_prices.append(analysis.buy_hope_max)

        if not all_buy_prices:
            return {'success': False, 'message': "분석 결과에 매수 가격 정보 없음", 'orders_placed': 0}

        # buy_price_levels 설정에 따라 사용할 가격대 제한
        buy_prices = all_buy_prices[:buy_price_levels]

        threshold_price = avg_buy_price * 0.99 if avg_buy_price > 0 else float('inf')
        valid_prices = [p for p in buy_prices if p < threshold_price and p < current_price]

        if not valid_prices:
            return {
                'success': False,
                'message': f"조건에 맞는 매수 가격 없음 ({buy_price_levels}개 가격대 중 유효 없음)",
                'orders_placed': 0
            }

        # 수량 확인
        quantity = int(settings.buy_quantity_per_order)
        if quantity < 1:
            return {
                'success': False,
                'message': "설정된 수량이 1 미만",
                'orders_placed': 0
            }

        success_count = 0
        ordered_prices: List[float] = []
        ordered_quantities: List[int] = []
        total_amount = 0.0

        for price in valid_prices:
            res = await kis_client.order_overseas_stock(
                symbol=symbol,
                exchange_code=actual_exchange_code,
                order_type="buy",
                quantity=quantity,
                price=price
            )
            if res and res.get('odno'):
                success_count += 1
                ordered_prices.append(price)
                ordered_quantities.append(quantity)
                total_amount += price * quantity

            await asyncio.sleep(0.2)

        return {
            'success': success_count > 0,
            'message': f"{success_count}개 주문 성공 (설정: {buy_price_levels}개 가격대)",
            'orders_placed': success_count,
            'prices': ordered_prices,
            'quantities': ordered_quantities,
            'total_amount': total_amount
        }


async def process_kis_domestic_sell_orders_with_analysis(
    kis_client: KISClient,
    symbol: str,
    current_price: float,
    avg_buy_price: float,
    balance_qty: int
) -> Dict[str, Any]:
    """분석 결과를 기반으로 KIS 국내 주식 매도 주문 처리"""
    from app.core.db import AsyncSessionLocal
    from app.services.stock_info_service import StockAnalysisService

    async with AsyncSessionLocal() as db:
        service = StockAnalysisService(db)
        analysis = await service.get_latest_analysis_by_symbol(symbol)

        if not analysis:
            return {'success': False, 'message': "분석 결과 없음", 'orders_placed': 0}

        sell_prices = []
        if analysis.appropriate_sell_min:
            sell_prices.append(analysis.appropriate_sell_min)
        if analysis.appropriate_sell_max:
            sell_prices.append(analysis.appropriate_sell_max)
        if analysis.sell_target_min:
            sell_prices.append(analysis.sell_target_min)
        if analysis.sell_target_max:
            sell_prices.append(analysis.sell_target_max)

        if not sell_prices:
            return {'success': False, 'message': "매도 가격 정보 없음", 'orders_placed': 0}

        min_sell_price = avg_buy_price * 1.01
        valid_prices = [p for p in sell_prices if p >= min_sell_price and p >= current_price]
        valid_prices.sort()

        if not valid_prices:
            if current_price >= min_sell_price:
                 res = await kis_client.order_korea_stock(
                     stock_code=symbol,
                     order_type="sell",
                     quantity=balance_qty,
                     price=int(current_price)
                 )
                 if res and res.get('odno'):
                     return {
                         'success': True,
                         'message': "목표가 도달로 전량 매도",
                         'orders_placed': 1,
                         'prices': [current_price],
                         'quantities': [balance_qty],
                         'total_volume': balance_qty,
                         'expected_amount': current_price * balance_qty
                     }
                 else:
                     return {'success': False, 'message': "매도 주문 실패", 'orders_placed': 0}

            return {'success': False, 'message': "매도 조건 미충족", 'orders_placed': 0}

        split_count = len(valid_prices)
        qty_per_order = balance_qty // split_count

        if qty_per_order < 1:
            target_price = valid_prices[0]
            res = await kis_client.order_korea_stock(
                stock_code=symbol,
                order_type="sell",
                quantity=balance_qty,
                price=int(target_price)
            )
            if res and res.get('odno'):
                return {
                    'success': True,
                    'message': "전량 매도 주문 (분할 불가)",
                    'orders_placed': 1,
                    'prices': [target_price],
                    'quantities': [balance_qty],
                    'total_volume': balance_qty,
                    'expected_amount': target_price * balance_qty
                }
            return {'success': False, 'message': "매도 주문 실패", 'orders_placed': 0}

        success_count = 0
        remaining_qty = balance_qty
        ordered_prices: List[float] = []
        ordered_quantities: List[int] = []
        total_volume = 0
        expected_amount = 0.0

        for i, price in enumerate(valid_prices):
            is_last = (i == len(valid_prices) - 1)
            qty = remaining_qty if is_last else qty_per_order

            if qty < 1:
                continue

            res = await kis_client.order_korea_stock(
                stock_code=symbol,
                order_type="sell",
                quantity=qty,
                price=int(price)
            )
            if res and res.get('odno'):
                success_count += 1
                remaining_qty -= qty
                ordered_prices.append(price)
                ordered_quantities.append(qty)
                total_volume += qty
                expected_amount += price * qty

            await asyncio.sleep(0.2)

        return {
            'success': success_count > 0,
            'message': f"{success_count}건 분할 매도 주문 완료",
            'orders_placed': success_count,
            'prices': ordered_prices,
            'quantities': ordered_quantities,
            'total_volume': total_volume,
            'expected_amount': expected_amount
        }


async def process_kis_overseas_sell_orders_with_analysis(
    kis_client: KISClient,
    symbol: str,
    current_price: float,
    avg_buy_price: float,
    balance_qty: int,
    exchange_code: str = "NASD"
) -> Dict[str, Any]:
    """분석 결과를 기반으로 KIS 해외 주식 매도 주문 처리"""
    from app.core.db import AsyncSessionLocal
    from app.services.stock_info_service import StockAnalysisService
    from app.services.symbol_trade_settings_service import SymbolTradeSettingsService

    async with AsyncSessionLocal() as db:
        service = StockAnalysisService(db)
        analysis = await service.get_latest_analysis_by_symbol(symbol)

        if not analysis:
            return {'success': False, 'message': "분석 결과 없음", 'orders_placed': 0}

        # settings에 exchange_code가 설정되어 있으면 그것을 사용
        settings_service = SymbolTradeSettingsService(db)
        settings = await settings_service.get_by_symbol(symbol)
        actual_exchange_code = (settings.exchange_code if settings and settings.exchange_code else exchange_code)

        # KIS 계좌의 실제 주문가능수량 조회 (토스 등 수동 잔고 제외)
        my_stocks = await kis_client.fetch_my_overseas_stocks()
        target_stock = next((s for s in my_stocks if s.get('ovrs_pdno') == symbol), None)
        if target_stock:
            # ord_psbl_qty가 있으면 사용, 없으면 ovrs_cblc_qty 사용
            actual_qty = int(float(target_stock.get('ord_psbl_qty', target_stock.get('ovrs_cblc_qty', 0))))
            if actual_qty < balance_qty:
                logger.info(f"[{symbol}] 주문가능수량 조정: {balance_qty} -> {actual_qty} (KIS 계좌 기준)")
                balance_qty = actual_qty

        if balance_qty <= 0:
            return {'success': False, 'message': "주문가능수량 없음", 'orders_placed': 0}

        sell_prices = []
        if analysis.appropriate_sell_min:
            sell_prices.append(analysis.appropriate_sell_min)
        if analysis.appropriate_sell_max:
            sell_prices.append(analysis.appropriate_sell_max)
        if analysis.sell_target_min:
            sell_prices.append(analysis.sell_target_min)
        if analysis.sell_target_max:
            sell_prices.append(analysis.sell_target_max)

        if not sell_prices:
            return {'success': False, 'message': "매도 가격 정보 없음", 'orders_placed': 0}

        min_sell_price = avg_buy_price * 1.01
        valid_prices = [p for p in sell_prices if p >= min_sell_price and p >= current_price]
        valid_prices.sort()

        if not valid_prices:
            if current_price >= min_sell_price:
                 res = await kis_client.order_overseas_stock(
                     symbol=symbol,
                     exchange_code=actual_exchange_code,
                     order_type="sell",
                     quantity=balance_qty,
                     price=current_price
                 )
                 if res and res.get('odno'):
                     return {
                         'success': True,
                         'message': "목표가 도달로 전량 매도",
                         'orders_placed': 1,
                         'prices': [current_price],
                         'quantities': [balance_qty],
                         'total_volume': balance_qty,
                         'expected_amount': current_price * balance_qty
                     }
                 else:
                     return {'success': False, 'message': "매도 주문 실패", 'orders_placed': 0}
            return {'success': False, 'message': "매도 조건 미충족", 'orders_placed': 0}

        split_count = len(valid_prices)
        qty_per_order = balance_qty // split_count

        if qty_per_order < 1:
            target_price = valid_prices[0]
            res = await kis_client.order_overseas_stock(
                symbol=symbol,
                exchange_code=actual_exchange_code,
                order_type="sell",
                quantity=balance_qty,
                price=target_price
            )
            if res and res.get('odno'):
                return {
                    'success': True,
                    'message': "전량 매도 주문",
                    'orders_placed': 1,
                    'prices': [target_price],
                    'quantities': [balance_qty],
                    'total_volume': balance_qty,
                    'expected_amount': target_price * balance_qty
                }
            return {'success': False, 'message': "매도 주문 실패", 'orders_placed': 0}

        success_count = 0
        remaining_qty = balance_qty
        ordered_prices: List[float] = []
        ordered_quantities: List[int] = []
        total_volume = 0
        expected_amount = 0.0

        for i, price in enumerate(valid_prices):
            is_last = (i == len(valid_prices) - 1)
            qty = remaining_qty if is_last else qty_per_order

            if qty < 1:
                continue

            res = await kis_client.order_overseas_stock(
                symbol=symbol,
                exchange_code=actual_exchange_code,
                order_type="sell",
                quantity=qty,
                price=price
            )
            if res and res.get('odno'):
                success_count += 1
                remaining_qty -= qty
                ordered_prices.append(price)
                ordered_quantities.append(qty)
                total_volume += qty
                expected_amount += price * qty

            await asyncio.sleep(0.2)

        return {
            'success': success_count > 0,
            'message': f"{success_count}건 분할 매도 주문 완료",
            'orders_placed': success_count,
            'prices': ordered_prices,
            'quantities': ordered_quantities,
            'total_volume': total_volume,
            'expected_amount': expected_amount
        }
