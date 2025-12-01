import asyncio
import logging
from typing import Any, Callable, Dict, List, Optional

from celery import shared_task

from app.analysis.service_analyzers import KISAnalyzer
from app.monitoring.trade_notifier import get_trade_notifier
from app.monitoring.error_reporter import get_error_reporter
from app.services.kis import KISClient
from app.services.kis_trading_service import (
    process_kis_domestic_buy_orders_with_analysis,
    process_kis_domestic_sell_orders_with_analysis,
    process_kis_overseas_buy_orders_with_analysis,
    process_kis_overseas_sell_orders_with_analysis
)

ProgressCallback = Optional[Callable[[Dict[str, str]], None]]
logger = logging.getLogger(__name__)

STATUS_FETCHING_HOLDINGS = "보유 주식 조회 중..."
NO_DOMESTIC_STOCKS_MESSAGE = "보유 중인 국내 주식이 없습니다."
NO_OVERSEAS_STOCKS_MESSAGE = "보유 중인 해외 주식이 없습니다."


async def _report_step_error_async(
    task_name: str,
    stock_name: str,
    stock_code: str,
    step_name: str,
    error_message: str,
) -> None:
    """태스크 step 에러를 Telegram으로 알림 (비동기)."""
    error_reporter = get_error_reporter()
    if not error_reporter._enabled:
        return

    # 가상의 Exception 생성하여 ErrorReporter 형식 활용
    class StepError(Exception):
        pass

    error = StepError(error_message)

    additional_context = {
        "task_name": task_name,
        "stock": f"{stock_name} ({stock_code})",
        "step": step_name,
    }

    try:
        await error_reporter.send_error_to_telegram(
            error=error,
            additional_context=additional_context,
        )
        logger.info(f"Step error reported to Telegram: {task_name} - {step_name}")
    except Exception as e:
        logger.error(f"Failed to report step error to Telegram: {e}")


def _report_step_error(
    task_name: str,
    stock_name: str,
    stock_code: str,
    step_name: str,
    error_message: str,
) -> None:
    """태스크 step 에러를 Telegram으로 알림 (동기 wrapper)."""
    try:
        loop = asyncio.new_event_loop()
        try:
            loop.run_until_complete(
                _report_step_error_async(
                    task_name, stock_name, stock_code, step_name, error_message
                )
            )
        finally:
            loop.close()
    except Exception as e:
        logger.error(f"Failed to report step error: {e}")


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

        result, _ = await analyzer.analyze_stock_json(name)

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
                logger.warning("⚠️ 텔레그램 알림 전송 실패: %s", notify_error)

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
            self.update_state(state='PROGRESS', meta={'status': STATUS_FETCHING_HOLDINGS, 'current': 0, 'total': 0})
            
            my_stocks = await kis.fetch_my_stocks()
            if not my_stocks:
                return {'status': 'completed', 'analyzed_count': 0, 'total_count': 0, 'message': NO_DOMESTIC_STOCKS_MESSAGE, 'results': []}

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
                    result, _ = await analyzer.analyze_stock_json(name)
                    results.append({'name': name, 'code': code, 'success': True})

                    # Send Telegram notification if analysis completed successfully
                    if result is not None and hasattr(result, 'decision'):
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
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
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
            self.update_state(state='PROGRESS', meta={'status': STATUS_FETCHING_HOLDINGS, 'current': 0, 'total': 0})
            
            # 보유 주식 조회 (평단가 확인용)
            my_stocks = await kis.fetch_my_stocks()
            
            # 분석된 종목이 있어야 매수 가능 (DB에서 최근 분석 조회 필요)
            # 여기서는 보유 종목에 대해서만 매수 시도 (추가 매수)
            # 신규 매수는 별도 로직 필요 (관심 종목 등). 현재는 보유 종목 추가 매수만 구현.
            
            if not my_stocks:
                return {'status': 'completed', 'success_count': 0, 'total_count': 0, 'message': NO_DOMESTIC_STOCKS_MESSAGE, 'results': []}

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
            self.update_state(state='PROGRESS', meta={'status': STATUS_FETCHING_HOLDINGS, 'current': 0, 'total': 0})
            
            my_stocks = await kis.fetch_my_stocks()
            if not my_stocks:
                return {'status': 'completed', 'success_count': 0, 'total_count': 0, 'message': NO_DOMESTIC_STOCKS_MESSAGE, 'results': []}

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
            self.update_state(state='PROGRESS', meta={'status': STATUS_FETCHING_HOLDINGS, 'current': 0, 'total': 0})
            
            my_stocks = await kis.fetch_my_stocks()
            if not my_stocks:
                return {'status': 'completed', 'message': NO_DOMESTIC_STOCKS_MESSAGE, 'results': []}

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
                    await analyzer.analyze_stock_json(name)
                    stock_steps.append({'step': '분석', 'result': {'success': True, 'message': '분석 완료'}})
                except Exception as e:
                    error_msg = str(e)
                    stock_steps.append({'step': '분석', 'result': {'success': False, 'error': error_msg}})
                    await _report_step_error_async(
                        "kis.run_per_domestic_stock_automation", name, code, "분석", error_msg
                    )
                    results.append({'name': name, 'code': code, 'steps': stock_steps})
                    continue # 분석 실패시 매수/매도 건너뜀

                # 2. 매수
                self.update_state(state='PROGRESS', meta={'status': f'{name} 매수 주문 중...', 'current': index, 'total': total_count, 'percentage': int((index / total_count) * 100)})
                try:
                    res = await process_kis_domestic_buy_orders_with_analysis(kis, code, current_price, avg_price)
                    stock_steps.append({'step': '매수', 'result': res})
                    # 매수 결과에 error가 있으면 알림
                    if res.get('error'):
                        await _report_step_error_async(
                            "kis.run_per_domestic_stock_automation", name, code, "매수", res['error']
                        )
                    # 매수 성공 시 텔레그램 알림
                    elif res.get('success') and res.get('orders_placed', 0) > 0:
                        try:
                            notifier = get_trade_notifier()
                            await notifier.notify_buy_order(
                                symbol=code,
                                korean_name=name,
                                order_count=res.get('orders_placed', 0),
                                total_amount=res.get('total_amount', 0.0),
                                prices=res.get('prices', []),
                                volumes=res.get('quantities', []),
                                market_type="국내주식",
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
                except Exception as e:
                    error_msg = str(e)
                    stock_steps.append({'step': '매수', 'result': {'success': False, 'error': error_msg}})
                    await _report_step_error_async(
                        "kis.run_per_domestic_stock_automation", name, code, "매수", error_msg
                    )

                # 매수 후 잔고/평단가를 최신화하여 매도 단계에 반영
                refreshed_qty = qty
                refreshed_avg_price = avg_price
                refreshed_current_price = current_price
                try:
                    latest_holdings = await kis.fetch_my_stocks()
                    latest = next((s for s in latest_holdings if s.get('pdno') == code), None)
                    if latest:
                        refreshed_qty = int(latest.get('hldg_qty', refreshed_qty))
                        refreshed_avg_price = float(latest.get('pchs_avg_pric', refreshed_avg_price))
                        refreshed_current_price = float(latest.get('prpr', refreshed_current_price))
                except Exception as refresh_error:
                    logger.warning("잔고 재조회 실패 - 기존 수량 사용 (%s)", refresh_error)

                # 3. 매도
                self.update_state(state='PROGRESS', meta={'status': f'{name} 매도 주문 중...', 'current': index, 'total': total_count, 'percentage': int((index / total_count) * 100)})
                try:
                    res = await process_kis_domestic_sell_orders_with_analysis(
                        kis,
                        code,
                        refreshed_current_price,
                        refreshed_avg_price,
                        refreshed_qty,
                    )
                    stock_steps.append({'step': '매도', 'result': res})
                    # 매도 결과에 error가 있으면 알림
                    if res.get('error'):
                        await _report_step_error_async(
                            "kis.run_per_domestic_stock_automation", name, code, "매도", res['error']
                        )
                    # 매도 성공 시 텔레그램 알림
                    elif res.get('success') and res.get('orders_placed', 0) > 0:
                        try:
                            notifier = get_trade_notifier()
                            await notifier.notify_sell_order(
                                symbol=code,
                                korean_name=name,
                                order_count=res.get('orders_placed', 0),
                                total_volume=res.get('total_volume', 0),
                                prices=res.get('prices', []),
                                volumes=res.get('quantities', []),
                                expected_amount=res.get('expected_amount', 0.0),
                                market_type="국내주식",
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
                except Exception as e:
                    error_msg = str(e)
                    stock_steps.append({'step': '매도', 'result': {'success': False, 'error': error_msg}})
                    await _report_step_error_async(
                        "kis.run_per_domestic_stock_automation", name, code, "매도", error_msg
                    )

                results.append({'name': name, 'code': code, 'steps': stock_steps})

            return {
                'status': 'completed',
                'message': '종목별 자동 실행 완료',
                'results': results
            }
        except Exception as e:
            # 태스크 전체 실패 시에도 알림
            await _report_step_error_async(
                "kis.run_per_domestic_stock_automation", "전체", "-", "태스크", str(e)
            )
            return {'status': 'failed', 'error': str(e)}
        finally:
            await analyzer.close()

    return asyncio.run(_run())


@shared_task(name="kis.analyze_domestic_stock_task", bind=True)
def analyze_domestic_stock_task(self, symbol: str) -> dict:
    """단일 국내 주식 분석 실행"""
    return asyncio.run(_analyze_domestic_stock_async(symbol))


@shared_task(name="kis.execute_domestic_buy_order_task", bind=True)
def execute_domestic_buy_order_task(self, symbol: str) -> dict:
    """단일 국내 주식 매수 주문 실행"""
    async def _run() -> dict:
        kis = KISClient()
        try:
            # 현재가 및 평단가 조회
            my_stocks = await kis.fetch_my_stocks()
            target_stock = next((s for s in my_stocks if s['pdno'] == symbol), None)
            
            if target_stock:
                avg_price = float(target_stock['pchs_avg_pric'])
                current_price = float(target_stock['prpr'])
            else:
                # 보유 중이 아니면 현재가 조회 필요
                price_info = await kis.fetch_price(symbol)
                current_price = float(price_info['output']['stck_prpr'])
                avg_price = 0 # 신규 매수
            
            res = await process_kis_domestic_buy_orders_with_analysis(kis, symbol, current_price, avg_price)
            return res
        except Exception as e:
            return {'success': False, 'error': str(e)}

    return asyncio.run(_run())


@shared_task(name="kis.execute_domestic_sell_order_task", bind=True)
def execute_domestic_sell_order_task(self, symbol: str) -> dict:
    """단일 국내 주식 매도 주문 실행"""
    async def _run() -> dict:
        kis = KISClient()
        try:
            my_stocks = await kis.fetch_my_stocks()
            target_stock = next((s for s in my_stocks if s['pdno'] == symbol), None)
            
            if not target_stock:
                return {'success': False, 'message': '보유 중인 주식이 아닙니다.'}
                
            avg_price = float(target_stock['pchs_avg_pric'])
            current_price = float(target_stock['prpr'])
            qty = int(target_stock['hldg_qty'])
            
            res = await process_kis_domestic_sell_orders_with_analysis(kis, symbol, current_price, avg_price, qty)
            return res
        except Exception as e:
            return {'success': False, 'error': str(e)}

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
                try:
                    current_price = await kis.fetch_overseas_price(symbol)
                    avg_price = 0.0  # 신규 매수이므로 평단 없음
                except Exception as price_error:
                    return {'success': False, 'message': f'현재가 조회 실패: {price_error}'}
            
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

        result, _ = await analyzer.analyze_stock_json(symbol)

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
                logger.warning("⚠️ 텔레그램 알림 전송 실패: %s", notify_error)

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
            self.update_state(state='PROGRESS', meta={'status': STATUS_FETCHING_HOLDINGS, 'current': 0, 'total': 0})
            
            my_stocks = await kis.fetch_my_overseas_stocks()
            if not my_stocks:
                return {'status': 'completed', 'analyzed_count': 0, 'total_count': 0, 'message': NO_OVERSEAS_STOCKS_MESSAGE, 'results': []}

            total_count = len(my_stocks)
            results = []

            for index, stock in enumerate(my_stocks, 1):
                symbol = stock.get('ovrs_pdno')  # 심볼
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
                    result, _ = await analyzer.analyze_stock_json(symbol)
                    results.append({'name': name, 'symbol': symbol, 'success': True})

                    # Send Telegram notification if analysis completed successfully
                    if result is not None and hasattr(result, 'decision'):
                        try:
                            notifier = get_trade_notifier()
                            await notifier.notify_analysis_complete(
                                symbol=symbol,
                                korean_name=name or symbol,
                                decision=result.decision,
                                confidence=float(result.confidence) if result.confidence else 0.0,
                                reasons=result.reasons if hasattr(result, 'reasons') and result.reasons else [],
                                market_type="해외주식",
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
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
            self.update_state(state='PROGRESS', meta={'status': STATUS_FETCHING_HOLDINGS, 'current': 0, 'total': 0})
            
            my_stocks = await kis.fetch_my_overseas_stocks()
            if not my_stocks:
                return {'status': 'completed', 'success_count': 0, 'total_count': 0, 'message': NO_OVERSEAS_STOCKS_MESSAGE, 'results': []}

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
            self.update_state(state='PROGRESS', meta={'status': STATUS_FETCHING_HOLDINGS, 'current': 0, 'total': 0})
            
            my_stocks = await kis.fetch_my_overseas_stocks()
            if not my_stocks:
                return {'status': 'completed', 'success_count': 0, 'total_count': 0, 'message': NO_OVERSEAS_STOCKS_MESSAGE, 'results': []}

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
            self.update_state(state='PROGRESS', meta={'status': STATUS_FETCHING_HOLDINGS, 'current': 0, 'total': 0})
            
            my_stocks = await kis.fetch_my_overseas_stocks()
            if not my_stocks:
                return {'status': 'completed', 'message': NO_OVERSEAS_STOCKS_MESSAGE, 'results': []}

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
                    await analyzer.analyze_stock_json(symbol)
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
                    # 매수 성공 시 텔레그램 알림
                    if res.get('success') and res.get('orders_placed', 0) > 0:
                        try:
                            notifier = get_trade_notifier()
                            await notifier.notify_buy_order(
                                symbol=symbol,
                                korean_name=name or symbol,
                                order_count=res.get('orders_placed', 0),
                                total_amount=res.get('total_amount', 0.0),
                                prices=res.get('prices', []),
                                volumes=res.get('quantities', []),
                                market_type="해외주식",
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
                except Exception as e:
                    stock_steps.append({'step': '매수', 'result': {'success': False, 'error': str(e)}})

                # 3. 매도
                self.update_state(state='PROGRESS', meta={'status': f'{symbol} 매도 주문 중...', 'current': index, 'total': total_count, 'percentage': int((index / total_count) * 100)})
                try:
                    res = await process_kis_overseas_sell_orders_with_analysis(kis, symbol, current_price, avg_price, qty)
                    stock_steps.append({'step': '매도', 'result': res})
                    # 매도 성공 시 텔레그램 알림
                    if res.get('success') and res.get('orders_placed', 0) > 0:
                        try:
                            notifier = get_trade_notifier()
                            await notifier.notify_sell_order(
                                symbol=symbol,
                                korean_name=name or symbol,
                                order_count=res.get('orders_placed', 0),
                                total_volume=res.get('total_volume', 0),
                                prices=res.get('prices', []),
                                volumes=res.get('quantities', []),
                                expected_amount=res.get('expected_amount', 0.0),
                                market_type="해외주식",
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
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
