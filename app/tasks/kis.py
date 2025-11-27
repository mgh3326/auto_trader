import asyncio
from typing import Callable, Dict, List, Optional

from celery import shared_task

from app.analysis.service_analyzers import KISAnalyzer
from app.monitoring.trade_notifier import get_trade_notifier
from app.services.kis import KISClient
from app.services.kis_trading_service import (
    process_kis_domestic_buy_orders_with_analysis,
    process_kis_domestic_sell_orders_with_analysis,
    process_kis_overseas_buy_orders_with_analysis,
    process_kis_overseas_sell_orders_with_analysis
)

ProgressCallback = Optional[Callable[[Dict[str, str]], None]]


# --- Domestic Stocks Tasks ---

async def _analyze_domestic_stock_async(code: str, progress_cb: ProgressCallback = None) -> Dict[str, object]:
    """단일 국내 주식 분석 비동기 헬퍼"""
    if not code:
        return {"status": "failed", "error": "종목 코드가 필요합니다."}

    kis = KISClient()
    analyzer = KISAnalyzer()
    
    try:
        # 기본 정보 조회로 종목명 확인
        info = await kis.fetch_fundamental_info(code)
        name = info.get("종목명", code)
        
        if progress_cb:
            progress_cb({
                "status": f"{name}({code}) 분석 중...",
                "symbol": code,
                "step": "analysis",
            })

        result, model = await analyzer.analyze_stock_json(name)

        if result is None:
            return {
                "status": "failed",
                "symbol": code,
                "name": name,
                "error": "분석 결과를 가져올 수 없습니다."
            }

        # Telegram notification
        if hasattr(result, 'decision'):
            try:
                notifier = get_trade_notifier()
                await notifier.notify_analysis_complete(
                    symbol=code,
                    korean_name=name,
                    decision=result.decision,
                    confidence=float(result.confidence) if result.confidence else 0.0,
                    reasons=result.reasons if hasattr(result, 'reasons') and result.reasons else [],
                    market_type="국내주식",
                )
            except Exception as notify_error:
                print(f"⚠️ 텔레그램 알림 전송 실패: {notify_error}")

        return {
            "status": "completed",
            "symbol": code,
            "name": name,
            "message": f"{name} 분석이 완료되었습니다."
        }
    except Exception as exc:
        return {
            "status": "failed",
            "symbol": code,
            "error": str(exc)
        }
    finally:
        await analyzer.close()


@shared_task(name="kis.run_analysis_for_my_domestic_stocks", bind=True)
def run_analysis_for_my_domestic_stocks(self) -> dict:
    """보유 국내 주식 AI 분석 실행"""
    async def _run() -> dict:
        kis = KISClient()
        analyzer = KISAnalyzer()
        
        try:
            self.update_state(state='PROGRESS', meta={'status': '보유 주식 조회 중...', 'current': 0, 'total': 0})
            
            my_stocks = await kis.fetch_my_stocks()
            if not my_stocks:
                return {'status': 'completed', 'analyzed_count': 0, 'total_count': 0, 'message': '보유 중인 국내 주식이 없습니다.', 'results': []}

            total_count = len(my_stocks)
            results = []

            for index, stock in enumerate(my_stocks, 1):
                code = stock.get('pdno')
                name = stock.get('prdt_name')
                
                self.update_state(
                    state='PROGRESS',
                    meta={
                        'current': index,
                        'total': total_count,
                        'status': f'{name} 분석 중... ({index}/{total_count})',
                        'current_stock': name,
                        'percentage': int((index / total_count) * 100)
                    }
                )
                
                try:
                    _, model = await analyzer.analyze_stock_json(name)
                    results.append({'name': name, 'code': code, 'success': True, 'model': model})
                except Exception as e:
                    results.append({'name': name, 'code': code, 'success': False, 'error': str(e)})

            success_count = sum(1 for r in results if r['success'])
            return {
                'status': 'completed',
                'analyzed_count': success_count,
                'total_count': total_count,
                'message': f'{success_count}/{total_count}개 종목 분석 완료',
                'results': results
            }
        except Exception as e:
            return {'status': 'failed', 'error': str(e)}
        finally:
            await analyzer.close()

    return asyncio.run(_run())


@shared_task(name="kis.execute_domestic_buy_orders", bind=True)
def execute_domestic_buy_orders(self) -> dict:
    """국내 주식 자동 매수 주문 실행"""
    async def _run() -> dict:
        kis = KISClient()
        try:
            self.update_state(state='PROGRESS', meta={'status': '보유 주식 조회 중...', 'current': 0, 'total': 0})
            
            # 보유 주식 조회 (평단가 확인용)
            my_stocks = await kis.fetch_my_stocks()
            stock_map = {s['pdno']: float(s['pchs_avg_pric']) for s in my_stocks}
            
            # 분석된 종목이 있어야 매수 가능 (DB에서 최근 분석 조회 필요)
            # 여기서는 보유 종목에 대해서만 매수 시도 (추가 매수)
            # 신규 매수는 별도 로직 필요 (관심 종목 등). 현재는 보유 종목 추가 매수만 구현.
            
            if not my_stocks:
                return {'status': 'completed', 'success_count': 0, 'total_count': 0, 'message': '보유 중인 국내 주식이 없습니다.', 'results': []}

            total_count = len(my_stocks)
            results = []

            for index, stock in enumerate(my_stocks, 1):
                code = stock.get('pdno')
                name = stock.get('prdt_name')
                avg_price = float(stock.get('pchs_avg_pric', 0))
                current_price = float(stock.get('prpr', 0)) # 현재가 (fetch_my_stocks에서 가져옴)
                
                self.update_state(
                    state='PROGRESS',
                    meta={
                        'current': index,
                        'total': total_count,
                        'status': f'{name} 매수 주문 처리 중... ({index}/{total_count})',
                        'current_stock': name,
                        'percentage': int((index / total_count) * 100)
                    }
                )
                
                try:
                    res = await process_kis_domestic_buy_orders_with_analysis(kis, code, current_price, avg_price)
                    results.append({'name': name, 'code': code, 'success': res['success'], 'message': res['message']})
                except Exception as e:
                    results.append({'name': name, 'code': code, 'success': False, 'error': str(e)})

            success_count = sum(1 for r in results if r['success'])
            return {
                'status': 'completed',
                'success_count': success_count,
                'total_count': total_count,
                'message': f'{success_count}/{total_count}개 종목 매수 주문 완료',
                'results': results
            }
        except Exception as e:
            return {'status': 'failed', 'error': str(e)}

    return asyncio.run(_run())


@shared_task(name="kis.execute_domestic_sell_orders", bind=True)
def execute_domestic_sell_orders(self) -> dict:
    """국내 주식 자동 매도 주문 실행"""
    async def _run() -> dict:
        kis = KISClient()
        try:
            self.update_state(state='PROGRESS', meta={'status': '보유 주식 조회 중...', 'current': 0, 'total': 0})
            
            my_stocks = await kis.fetch_my_stocks()
            if not my_stocks:
                return {'status': 'completed', 'success_count': 0, 'total_count': 0, 'message': '보유 중인 국내 주식이 없습니다.', 'results': []}

            total_count = len(my_stocks)
            results = []

            for index, stock in enumerate(my_stocks, 1):
                code = stock.get('pdno')
                name = stock.get('prdt_name')
                avg_price = float(stock.get('pchs_avg_pric', 0))
                current_price = float(stock.get('prpr', 0))
                qty = int(stock.get('hldg_qty', 0))
                
                self.update_state(
                    state='PROGRESS',
                    meta={
                        'current': index,
                        'total': total_count,
                        'status': f'{name} 매도 주문 처리 중... ({index}/{total_count})',
                        'current_stock': name,
                        'percentage': int((index / total_count) * 100)
                    }
                )
                
                try:
                    res = await process_kis_domestic_sell_orders_with_analysis(kis, code, current_price, avg_price, qty)
                    results.append({'name': name, 'code': code, 'success': res['success'], 'message': res['message']})
                except Exception as e:
                    results.append({'name': name, 'code': code, 'success': False, 'error': str(e)})

            success_count = sum(1 for r in results if r['success'])
            return {
                'status': 'completed',
                'success_count': success_count,
                'total_count': total_count,
                'message': f'{success_count}/{total_count}개 종목 매도 주문 완료',
                'results': results
            }
        except Exception as e:
            return {'status': 'failed', 'error': str(e)}

    return asyncio.run(_run())


@shared_task(name="kis.run_per_domestic_stock_automation", bind=True)
def run_per_domestic_stock_automation(self) -> dict:
    """국내 주식 종목별 자동 실행 (분석 -> 매수 -> 매도)"""
    async def _run() -> dict:
        kis = KISClient()
        analyzer = KISAnalyzer() # For analysis step
        
        try:
            self.update_state(state='PROGRESS', meta={'status': '보유 주식 조회 중...', 'current': 0, 'total': 0})
            
            my_stocks = await kis.fetch_my_stocks()
            if not my_stocks:
                return {'status': 'completed', 'message': '보유 중인 국내 주식이 없습니다.', 'results': []}

            total_count = len(my_stocks)
            results = []

            for index, stock in enumerate(my_stocks, 1):
                code = stock.get('pdno')
                name = stock.get('prdt_name')
                avg_price = float(stock.get('pchs_avg_pric', 0))
                current_price = float(stock.get('prpr', 0))
                qty = int(stock.get('hldg_qty', 0))
                
                stock_steps = []
                
                # 1. 분석
                self.update_state(state='PROGRESS', meta={'status': f'{name} 분석 중...', 'current': index, 'total': total_count, 'percentage': int((index / total_count) * 100)})
                try:
                    _, model = await analyzer.analyze_stock_json(name)
                    stock_steps.append({'step': '분석', 'result': {'success': True, 'message': '분석 완료'}})
                except Exception as e:
                    stock_steps.append({'step': '분석', 'result': {'success': False, 'error': str(e)}})
                    results.append({'name': name, 'code': code, 'steps': stock_steps})
                    continue # 분석 실패시 매수/매도 건너뜀

                # 2. 매수
                self.update_state(state='PROGRESS', meta={'status': f'{name} 매수 주문 중...', 'current': index, 'total': total_count, 'percentage': int((index / total_count) * 100)})
                try:
                    res = await process_kis_domestic_buy_orders_with_analysis(kis, code, current_price, avg_price)
                    stock_steps.append({'step': '매수', 'result': res})
                except Exception as e:
                    stock_steps.append({'step': '매수', 'result': {'success': False, 'error': str(e)}})

                # 3. 매도
                self.update_state(state='PROGRESS', meta={'status': f'{name} 매도 주문 중...', 'current': index, 'total': total_count, 'percentage': int((index / total_count) * 100)})
                try:
                    # 매수 후 잔고가 변했을 수 있으므로 다시 조회하거나, 기존 수량 사용 (여기선 기존 수량 사용)
                    # 실제로는 매수 체결 확인이 필요하지만, 비동기라 즉시 반영 안될 수 있음.
                    res = await process_kis_domestic_sell_orders_with_analysis(kis, code, current_price, avg_price, qty)
                    stock_steps.append({'step': '매도', 'result': res})
                except Exception as e:
                    stock_steps.append({'step': '매도', 'result': {'success': False, 'error': str(e)}})
                
                results.append({'name': name, 'code': code, 'steps': stock_steps})

            return {
                'status': 'completed',
                'message': '종목별 자동 실행 완료',
                'results': results
            }
        except Exception as e:
            return {'status': 'failed', 'error': str(e)}
        finally:
            await analyzer.close()

    return asyncio.run(_run())


@shared_task(name="kis.analyze_overseas_stock_task", bind=True)
def analyze_overseas_stock_task(self, symbol: str) -> dict:
    """단일 해외 주식 분석 실행"""
    return asyncio.run(_analyze_overseas_stock_async(symbol))


@shared_task(name="kis.execute_overseas_buy_order_task", bind=True)
def execute_overseas_buy_order_task(self, symbol: str) -> dict:
    """단일 해외 주식 매수 주문 실행"""
    async def _run() -> dict:
        kis = KISClient()
        try:
            my_stocks = await kis.fetch_my_overseas_stocks()
            target_stock = next((s for s in my_stocks if s['ovrs_pdno'] == symbol), None)
            
            if target_stock:
                avg_price = float(target_stock['pchs_avg_pric'])
                current_price = float(target_stock['now_pric2'])
            else:
                # 보유 중이 아니면 현재가 조회 필요 (구현 필요)
                # 여기서는 간단히 에러 처리 혹은 0으로 가정
                # 해외 주식 현재가 조회 메서드가 필요함.
                # kis.fetch_overseas_price(symbol) 구현 안됨.
                return {'success': False, 'message': '보유 중이 아닌 종목의 매수는 현재 지원되지 않습니다.'}
            
            res = await process_kis_overseas_buy_orders_with_analysis(kis, symbol, current_price, avg_price)
            return res
        except Exception as e:
            return {'success': False, 'error': str(e)}

    return asyncio.run(_run())


@shared_task(name="kis.execute_overseas_sell_order_task", bind=True)
def execute_overseas_sell_order_task(self, symbol: str) -> dict:
    """단일 해외 주식 매도 주문 실행"""
    async def _run() -> dict:
        kis = KISClient()
        try:
            my_stocks = await kis.fetch_my_overseas_stocks()
            target_stock = next((s for s in my_stocks if s['ovrs_pdno'] == symbol), None)
            
            if not target_stock:
                return {'success': False, 'message': '보유 중인 주식이 아닙니다.'}
                
            avg_price = float(target_stock['pchs_avg_pric'])
            current_price = float(target_stock['now_pric2'])
            qty = int(float(target_stock['ovrs_cblc_qty']))
            
            res = await process_kis_overseas_sell_orders_with_analysis(kis, symbol, current_price, avg_price, qty)
            return res
        except Exception as e:
            return {'success': False, 'error': str(e)}

    return asyncio.run(_run())


# --- Overseas Stocks Tasks ---

async def _analyze_overseas_stock_async(symbol: str, progress_cb: ProgressCallback = None) -> Dict[str, object]:
    """단일 해외 주식 분석 비동기 헬퍼"""
    if not symbol:
        return {"status": "failed", "error": "심볼이 필요합니다."}

    from app.analysis.service_analyzers import YahooAnalyzer
    analyzer = YahooAnalyzer()  # 해외 주식은 YahooAnalyzer 사용 (또는 KISAnalyzer 확장 필요 시 변경)
    
    try:
        if progress_cb:
            progress_cb({
                "status": f"{symbol} 분석 중...",
                "symbol": symbol,
                "step": "analysis",
            })

        result, model = await analyzer.analyze_stock_json(symbol)

        if result is None:
            return {
                "status": "failed",
                "symbol": symbol,
                "error": "분석 결과를 가져올 수 없습니다."
            }

        # Telegram notification
        if hasattr(result, 'decision'):
            try:
                notifier = get_trade_notifier()
                await notifier.notify_analysis_complete(
                    symbol=symbol,
                    korean_name=symbol, # 해외주식은 한글명이 없을 수 있음
                    decision=result.decision,
                    confidence=float(result.confidence) if result.confidence else 0.0,
                    reasons=result.reasons if hasattr(result, 'reasons') and result.reasons else [],
                    market_type="해외주식",
                )
            except Exception as notify_error:
                print(f"⚠️ 텔레그램 알림 전송 실패: {notify_error}")

        return {
            "status": "completed",
            "symbol": symbol,
            "message": f"{symbol} 분석이 완료되었습니다."
        }
    except Exception as exc:
        return {
            "status": "failed",
            "symbol": symbol,
            "error": str(exc)
        }
    finally:
        await analyzer.close()


@shared_task(name="kis.run_analysis_for_my_overseas_stocks", bind=True)
def run_analysis_for_my_overseas_stocks(self) -> dict:
    """보유 해외 주식 AI 분석 실행"""
    async def _run() -> dict:
        kis = KISClient()
        from app.analysis.service_analyzers import YahooAnalyzer
        analyzer = YahooAnalyzer()
        
        try:
            self.update_state(state='PROGRESS', meta={'status': '보유 주식 조회 중...', 'current': 0, 'total': 0})
            
            my_stocks = await kis.fetch_my_overseas_stocks()
            if not my_stocks:
                return {'status': 'completed', 'analyzed_count': 0, 'total_count': 0, 'message': '보유 중인 해외 주식이 없습니다.', 'results': []}

            total_count = len(my_stocks)
            results = []

            for index, stock in enumerate(my_stocks, 1):
                symbol = stock.get('ovrs_pdno') # 심볼
                name = stock.get('ovrs_item_name')
                
                self.update_state(
                    state='PROGRESS',
                    meta={
                        'current': index,
                        'total': total_count,
                        'status': f'{symbol} 분석 중... ({index}/{total_count})',
                        'current_stock': symbol,
                        'percentage': int((index / total_count) * 100)
                    }
                )
                
                try:
                    # 해외주식은 심볼로 분석
                    _, model = await analyzer.analyze_stock_json(symbol)
                    results.append({'name': name, 'symbol': symbol, 'success': True, 'model': model})
                except Exception as e:
                    results.append({'name': name, 'symbol': symbol, 'success': False, 'error': str(e)})

            success_count = sum(1 for r in results if r['success'])
            return {
                'status': 'completed',
                'analyzed_count': success_count,
                'total_count': total_count,
                'message': f'{success_count}/{total_count}개 종목 분석 완료',
                'results': results
            }
        except Exception as e:
            return {'status': 'failed', 'error': str(e)}
        finally:
            await analyzer.close()

    return asyncio.run(_run())


@shared_task(name="kis.execute_overseas_buy_orders", bind=True)
def execute_overseas_buy_orders(self) -> dict:
    """해외 주식 자동 매수 주문 실행"""
    async def _run() -> dict:
        kis = KISClient()
        try:
            self.update_state(state='PROGRESS', meta={'status': '보유 주식 조회 중...', 'current': 0, 'total': 0})
            
            my_stocks = await kis.fetch_my_overseas_stocks()
            if not my_stocks:
                return {'status': 'completed', 'success_count': 0, 'total_count': 0, 'message': '보유 중인 해외 주식이 없습니다.', 'results': []}

            total_count = len(my_stocks)
            results = []

            for index, stock in enumerate(my_stocks, 1):
                symbol = stock.get('ovrs_pdno')
                name = stock.get('ovrs_item_name')
                avg_price = float(stock.get('pchs_avg_pric', 0))
                current_price = float(stock.get('now_pric2', 0))
                
                self.update_state(
                    state='PROGRESS',
                    meta={
                        'current': index,
                        'total': total_count,
                        'status': f'{symbol} 매수 주문 처리 중... ({index}/{total_count})',
                        'current_stock': symbol,
                        'percentage': int((index / total_count) * 100)
                    }
                )
                
                try:
                    res = await process_kis_overseas_buy_orders_with_analysis(kis, symbol, current_price, avg_price)
                    results.append({'name': name, 'symbol': symbol, 'success': res['success'], 'message': res['message']})
                except Exception as e:
                    results.append({'name': name, 'symbol': symbol, 'success': False, 'error': str(e)})

            success_count = sum(1 for r in results if r['success'])
            return {
                'status': 'completed',
                'success_count': success_count,
                'total_count': total_count,
                'message': f'{success_count}/{total_count}개 종목 매수 주문 완료',
                'results': results
            }
        except Exception as e:
            return {'status': 'failed', 'error': str(e)}

    return asyncio.run(_run())


@shared_task(name="kis.execute_overseas_sell_orders", bind=True)
def execute_overseas_sell_orders(self) -> dict:
    """해외 주식 자동 매도 주문 실행"""
    async def _run() -> dict:
        kis = KISClient()
        try:
            self.update_state(state='PROGRESS', meta={'status': '보유 주식 조회 중...', 'current': 0, 'total': 0})
            
            my_stocks = await kis.fetch_my_overseas_stocks()
            if not my_stocks:
                return {'status': 'completed', 'success_count': 0, 'total_count': 0, 'message': '보유 중인 해외 주식이 없습니다.', 'results': []}

            total_count = len(my_stocks)
            results = []

            for index, stock in enumerate(my_stocks, 1):
                symbol = stock.get('ovrs_pdno')
                name = stock.get('ovrs_item_name')
                avg_price = float(stock.get('pchs_avg_pric', 0))
                current_price = float(stock.get('now_pric2', 0))
                qty = int(float(stock.get('ovrs_cblc_qty', 0)))
                
                self.update_state(
                    state='PROGRESS',
                    meta={
                        'current': index,
                        'total': total_count,
                        'status': f'{symbol} 매도 주문 처리 중... ({index}/{total_count})',
                        'current_stock': symbol,
                        'percentage': int((index / total_count) * 100)
                    }
                )
                
                try:
                    res = await process_kis_overseas_sell_orders_with_analysis(kis, symbol, current_price, avg_price, qty)
                    results.append({'name': name, 'symbol': symbol, 'success': res['success'], 'message': res['message']})
                except Exception as e:
                    results.append({'name': name, 'symbol': symbol, 'success': False, 'error': str(e)})

            success_count = sum(1 for r in results if r['success'])
            return {
                'status': 'completed',
                'success_count': success_count,
                'total_count': total_count,
                'message': f'{success_count}/{total_count}개 종목 매도 주문 완료',
                'results': results
            }
        except Exception as e:
            return {'status': 'failed', 'error': str(e)}

    return asyncio.run(_run())


@shared_task(name="kis.run_per_overseas_stock_automation", bind=True)
def run_per_overseas_stock_automation(self) -> dict:
    """해외 주식 종목별 자동 실행 (분석 -> 매수 -> 매도)"""
    async def _run() -> dict:
        kis = KISClient()
        from app.analysis.service_analyzers import YahooAnalyzer
        analyzer = YahooAnalyzer()
        
        try:
            self.update_state(state='PROGRESS', meta={'status': '보유 주식 조회 중...', 'current': 0, 'total': 0})
            
            my_stocks = await kis.fetch_my_overseas_stocks()
            if not my_stocks:
                return {'status': 'completed', 'message': '보유 중인 해외 주식이 없습니다.', 'results': []}

            total_count = len(my_stocks)
            results = []

            for index, stock in enumerate(my_stocks, 1):
                symbol = stock.get('ovrs_pdno')
                name = stock.get('ovrs_item_name')
                avg_price = float(stock.get('pchs_avg_pric', 0))
                current_price = float(stock.get('now_pric2', 0))
                qty = int(float(stock.get('ovrs_cblc_qty', 0)))
                
                stock_steps = []
                
                # 1. 분석
                self.update_state(state='PROGRESS', meta={'status': f'{symbol} 분석 중...', 'current': index, 'total': total_count, 'percentage': int((index / total_count) * 100)})
                try:
                    _, model = await analyzer.analyze_stock_json(symbol)
                    stock_steps.append({'step': '분석', 'result': {'success': True, 'message': '분석 완료'}})
                except Exception as e:
                    stock_steps.append({'step': '분석', 'result': {'success': False, 'error': str(e)}})
                    results.append({'name': name, 'symbol': symbol, 'steps': stock_steps})
                    continue

                # 2. 매수
                self.update_state(state='PROGRESS', meta={'status': f'{symbol} 매수 주문 중...', 'current': index, 'total': total_count, 'percentage': int((index / total_count) * 100)})
                try:
                    res = await process_kis_overseas_buy_orders_with_analysis(kis, symbol, current_price, avg_price)
                    stock_steps.append({'step': '매수', 'result': res})
                except Exception as e:
                    stock_steps.append({'step': '매수', 'result': {'success': False, 'error': str(e)}})

                # 3. 매도
                self.update_state(state='PROGRESS', meta={'status': f'{symbol} 매도 주문 중...', 'current': index, 'total': total_count, 'percentage': int((index / total_count) * 100)})
                try:
                    res = await process_kis_overseas_sell_orders_with_analysis(kis, symbol, current_price, avg_price, qty)
                    stock_steps.append({'step': '매도', 'result': res})
                except Exception as e:
                    stock_steps.append({'step': '매도', 'result': {'success': False, 'error': str(e)}})
                
                results.append({'name': name, 'symbol': symbol, 'steps': stock_steps})

            return {
                'status': 'completed',
                'message': '종목별 자동 실행 완료',
                'results': results
            }
        except Exception as e:
            return {'status': 'failed', 'error': str(e)}
        finally:
            await analyzer.close()

    return asyncio.run(_run())
