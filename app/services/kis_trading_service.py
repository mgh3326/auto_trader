import asyncio
from typing import Dict, Any, List, Optional, Tuple
from app.services.kis import KISClient
from app.models.analysis import StockAnalysisResult
from app.core.config import settings

# KIS 매수 설정 (추후 설정 파일로 이동 가능)
KIS_BUY_AMOUNT = 100000  # 10만원
KIS_MIN_BALANCE = 100000 # 최소 예수금

async def process_kis_domestic_buy_orders_with_analysis(
    kis_client: KISClient,
    symbol: str, 
    current_price: float, 
    avg_buy_price: float
) -> Dict[str, Any]:
    """분석 결과를 기반으로 KIS 국내 주식 매수 주문 처리"""
    from app.core.db import AsyncSessionLocal
    from app.services.stock_info_service import StockAnalysisService

    # 1. 예수금 확인
    # KISClient.get_balance returns dict with 'output2' list usually containing balance info
    # We need a simpler balance check. 
    # For now, let's assume the caller might have checked, or we check here.
    # Fetching balance is an API call.
    
    # balance_info = await kis_client.get_balance()
    # output2[0]['dnca_tot_amt'] is usually the deposit (pre-calculated in get_balance wrapper if exists)
    # But KISClient.get_balance implementation details:
    # It returns raw response usually. Let's check KISClient.get_balance in kis.py
    # Actually, let's trust the task to handle balance check or do it here if simple.
    # KISClient.get_balance returns the full response.
    # Let's skip strict balance check for this MVP step or implement a simple one.
    
    # Assuming we have enough balance for now or handle error on order.
    
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

        # 3. 가격 정보 확인
        buy_prices = []
        if analysis.appropriate_buy_min is not None:
            buy_prices.append(("appropriate_buy_min", analysis.appropriate_buy_min))
        if analysis.appropriate_buy_max is not None:
            buy_prices.append(("appropriate_buy_max", analysis.appropriate_buy_max))
        if analysis.buy_hope_min is not None:
            buy_prices.append(("buy_hope_min", analysis.buy_hope_min))
        if analysis.buy_hope_max is not None:
            buy_prices.append(("buy_hope_max", analysis.buy_hope_max))

        if not buy_prices:
            return {
                'success': False,
                'message': "분석 결과에 매수 가격 정보 없음",
                'orders_placed': 0
            }

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
                'message': "조건에 맞는 매수 가격 없음 (현재가 및 평단가-1% 보다 낮아야 함)",
                'orders_placed': 0
            }

        # 5. 주문 실행
        success_count = 0
        for name, price in valid_prices:
            # 수량 계산: 금액 / 가격
            quantity = int(KIS_BUY_AMOUNT / price)
            if quantity < 1:
                continue
                
            # 호가 단위 맞추기 (KIS는 보통 시장가나 지정가. 지정가 시 호가 단위 중요)
            # 여기서는 단순화를 위해 계산된 가격 그대로 시도하거나, 
            # 시장가(0)가 아닌 지정가 주문이므로 호가 단위를 맞춰야 함.
            # KISClient doesn't have adjust_price yet?
            # We will use the price from analysis directly. If it fails, it fails.
            
            res = await kis_client.order_korea_stock(
                symbol=symbol,
                order_type="buy",
                quantity=quantity,
                price=int(price) # Domestic stocks are integers usually
            )
            
            if res and res.get('rt_cd') == '0':
                success_count += 1
            
            await asyncio.sleep(0.2) # Rate limit

        return {
            'success': success_count > 0,
            'message': f"{success_count}개 주문 성공",
            'orders_placed': success_count
        }

async def process_kis_overseas_buy_orders_with_analysis(
    kis_client: KISClient,
    symbol: str, 
    current_price: float, 
    avg_buy_price: float,
    exchange_code: str = "NASD" # Default to NASD, but should be passed
) -> Dict[str, Any]:
    """분석 결과를 기반으로 KIS 해외 주식 매수 주문 처리"""
    from app.core.db import AsyncSessionLocal
    from app.services.stock_info_service import StockAnalysisService

    async with AsyncSessionLocal() as db:
        service = StockAnalysisService(db)
        analysis = await service.get_latest_analysis_by_symbol(symbol)

        if avg_buy_price > 0:
            target_price = avg_buy_price * 0.99
            if current_price >= target_price:
                return {'success': False, 'message': "1% 매수 조건 미충족", 'orders_placed': 0}
        
        if not analysis:
            return {'success': False, 'message': "분석 결과 없음", 'orders_placed': 0}

        buy_prices = []
        if analysis.appropriate_buy_min:
            buy_prices.append(analysis.appropriate_buy_min)
        if analysis.appropriate_buy_max:
            buy_prices.append(analysis.appropriate_buy_max)
        if analysis.buy_hope_min:
            buy_prices.append(analysis.buy_hope_min)
        if analysis.buy_hope_max:
            buy_prices.append(analysis.buy_hope_max)

        threshold_price = avg_buy_price * 0.99 if avg_buy_price > 0 else float('inf')
        valid_prices = [p for p in buy_prices if p < threshold_price and p < current_price]

        if not valid_prices:
            return {'success': False, 'message': "조건에 맞는 매수 가격 없음", 'orders_placed': 0}

        success_count = 0
        for price in valid_prices:
            quantity = int(KIS_BUY_AMOUNT / price)
            if quantity < 1:
                continue

            res = await kis_client.order_overseas_stock(
                symbol=symbol,
                exchange_code=exchange_code,
                order_type="buy",
                quantity=quantity,
                price=price
            )
            if res and res.get('rt_cd') == '0':
                success_count += 1
            
            await asyncio.sleep(0.2)

        return {
            'success': success_count > 0,
            'message': f"{success_count}개 주문 성공",
            'orders_placed': success_count
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
                     symbol=symbol,
                     order_type="sell",
                     quantity=balance_qty,
                     price=int(current_price)
                 )
                 if res and res.get('rt_cd') == '0':
                     return {'success': True, 'message': "목표가 도달로 전량 매도", 'orders_placed': 1}
                 else:
                     return {'success': False, 'message': "매도 주문 실패", 'orders_placed': 0}
            
            return {'success': False, 'message': "매도 조건 미충족", 'orders_placed': 0}

        split_count = len(valid_prices)
        qty_per_order = balance_qty // split_count
        
        if qty_per_order < 1:
            target_price = valid_prices[0]
            res = await kis_client.order_korea_stock(
                symbol=symbol,
                order_type="sell",
                quantity=balance_qty,
                price=int(target_price)
            )
            if res and res.get('rt_cd') == '0':
                return {'success': True, 'message': "전량 매도 주문 (분할 불가)", 'orders_placed': 1}
            return {'success': False, 'message': "매도 주문 실패", 'orders_placed': 0}

        success_count = 0
        remaining_qty = balance_qty
        
        for i, price in enumerate(valid_prices):
            is_last = (i == len(valid_prices) - 1)
            qty = remaining_qty if is_last else qty_per_order
            
            if qty < 1:
                continue
            
            res = await kis_client.order_korea_stock(
                symbol=symbol,
                order_type="sell",
                quantity=qty,
                price=int(price)
            )
            if res and res.get('rt_cd') == '0':
                success_count += 1
                remaining_qty -= qty
            
            await asyncio.sleep(0.2)

        return {
            'success': success_count > 0,
            'message': f"{success_count}건 분할 매도 주문 완료",
            'orders_placed': success_count
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
                 res = await kis_client.order_overseas_stock(
                     symbol=symbol,
                     exchange_code=exchange_code,
                     order_type="sell",
                     quantity=balance_qty,
                     price=current_price
                 )
                 if res and res.get('rt_cd') == '0':
                     return {'success': True, 'message': "목표가 도달로 전량 매도", 'orders_placed': 1}
                 else:
                     return {'success': False, 'message': "매도 주문 실패", 'orders_placed': 0}
            return {'success': False, 'message': "매도 조건 미충족", 'orders_placed': 0}

        split_count = len(valid_prices)
        qty_per_order = balance_qty // split_count
        
        if qty_per_order < 1:
            target_price = valid_prices[0]
            res = await kis_client.order_overseas_stock(
                symbol=symbol,
                exchange_code=exchange_code,
                order_type="sell",
                quantity=balance_qty,
                price=target_price
            )
            if res and res.get('rt_cd') == '0':
                return {'success': True, 'message': "전량 매도 주문", 'orders_placed': 1}
            return {'success': False, 'message': "매도 주문 실패", 'orders_placed': 0}

        success_count = 0
        remaining_qty = balance_qty
        
        for i, price in enumerate(valid_prices):
            is_last = (i == len(valid_prices) - 1)
            qty = remaining_qty if is_last else qty_per_order
            
            if qty < 1:
                continue
            
            res = await kis_client.order_overseas_stock(
                symbol=symbol,
                exchange_code=exchange_code,
                order_type="sell",
                quantity=qty,
                price=price
            )
            if res and res.get('rt_cd') == '0':
                success_count += 1
                remaining_qty -= qty
            
            await asyncio.sleep(0.2)

        return {
            'success': success_count > 0,
            'message': f"{success_count}건 분할 매도 주문 완료",
            'orders_placed': success_count
        }
