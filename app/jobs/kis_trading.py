import asyncio
import json
import logging

from app.analysis.service_analyzers import KISAnalyzer
from app.core.symbol import to_db_symbol
from app.monitoring.trade_notifier import get_trade_notifier
from app.services.brokers.kis.client import KISClient
from app.services.kis_trading_service import (
    process_kis_domestic_buy_orders_with_analysis,
    process_kis_domestic_sell_orders_with_analysis,
    process_kis_overseas_buy_orders_with_analysis,
    process_kis_overseas_sell_orders_with_analysis,
)
from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

logger = logging.getLogger(__name__)

STATUS_FETCHING_HOLDINGS = "보유 주식 조회 중..."
NO_DOMESTIC_STOCKS_MESSAGE = "보유 중인 국내 주식이 없습니다."
NO_OVERSEAS_STOCKS_MESSAGE = "보유 중인 해외 주식이 없습니다."


async def _resolve_overseas_exchange_code(
    symbol: str,
    preferred_exchange: str | None,
) -> str:
    normalized_preferred = str(preferred_exchange or "").strip().upper()
    if normalized_preferred:
        return normalized_preferred
    return await get_us_exchange_by_symbol(symbol)


def _extract_overseas_order_id(order: dict) -> str:
    for key in ("odno", "ODNO", "ord_no", "ORD_NO"):
        value = order.get(key)
        if value:
            return str(value).strip()
    return ""


async def _load_overseas_open_orders_all_exchanges(kis: KISClient) -> list[dict]:
    orders_by_id: dict[str, dict] = {}
    anonymous_orders: list[dict] = []
    for exchange_code in ("NASD", "NYSE", "AMEX"):
        try:
            open_orders = await kis.inquire_overseas_orders(
                exchange_code=exchange_code,
                is_mock=False,
            )
        except Exception as exc:
            logger.warning(
                "미체결 주문 조회 실패 (exchange=%s): %s",
                exchange_code,
                exc,
            )
            continue

        for order in open_orders:
            order_id = _extract_overseas_order_id(order)
            if order_id:
                orders_by_id[order_id] = order
            else:
                anonymous_orders.append(order)

    return list(orders_by_id.values()) + anonymous_orders


async def _send_toss_recommendation_async(
    code: str,
    name: str,
    current_price: float,
    toss_quantity: int,
    toss_avg_price: float,
    kis_quantity: int | None = None,
    kis_avg_price: float | None = None,
) -> None:
    """수동 잔고(토스) 종목에 대해 AI 분석 결과와 가격 제안 알림 발송.

    AI 결정(buy/hold/sell)과 무관하게 항상 가격 제안을 포함하여 알림을 발송합니다.
    """
    from app.core.db import AsyncSessionLocal
    from app.services.stock_info_service import StockAnalysisService

    notifier = get_trade_notifier()
    if not notifier._enabled:
        logger.debug(f"[토스추천] {name}({code}) - 알림 비활성화됨")
        return

    async with AsyncSessionLocal() as db:
        service = StockAnalysisService(db)
        analysis = await service.get_latest_analysis_by_symbol(code)

        if not analysis:
            logger.warning(f"[토스추천] {name}({code}) - 분석 결과 없음, 알림 스킵")
            return

        decision = analysis.decision.lower() if analysis.decision else "hold"
        confidence = analysis.confidence if analysis.confidence else 0
        raw_reasons = analysis.reasons
        if isinstance(raw_reasons, list):
            reasons = [str(r) for r in raw_reasons]
        elif isinstance(raw_reasons, str):
            try:
                parsed = json.loads(raw_reasons)
                reasons = (
                    [str(r) for r in parsed]
                    if isinstance(parsed, list)
                    else [str(parsed)]
                )
            except Exception as parse_error:
                logger.debug(
                    "Failed to parse analysis reasons for %s(%s): %s",
                    name,
                    code,
                    parse_error,
                )
                reasons = [raw_reasons]
        else:
            reasons = []

        # AI 결정과 무관하게 항상 가격 제안 알림 발송
        await notifier.notify_toss_price_recommendation(
            symbol=code,
            korean_name=name,
            current_price=current_price,
            toss_quantity=toss_quantity,
            toss_avg_price=toss_avg_price,
            decision=decision,
            confidence=confidence,
            reasons=reasons,
            appropriate_buy_min=analysis.appropriate_buy_min,
            appropriate_buy_max=analysis.appropriate_buy_max,
            appropriate_sell_min=analysis.appropriate_sell_min,
            appropriate_sell_max=analysis.appropriate_sell_max,
            buy_hope_min=analysis.buy_hope_min,
            buy_hope_max=analysis.buy_hope_max,
            sell_target_min=analysis.sell_target_min,
            sell_target_max=analysis.sell_target_max,
            currency="원",
        )
        logger.info(
            f"[토스추천] {name}({code}) - 가격 제안 알림 발송 (AI 판단: {decision}, 신뢰도: {confidence}%)"
        )


# --- Domestic Stocks Tasks ---


async def _analyze_domestic_stock_async(code: str) -> dict[str, object]:
    """단일 국내 주식 분석 비동기 헬퍼"""
    if not code:
        return {"status": "failed", "error": "종목 코드가 필요합니다."}

    kis = KISClient()
    analyzer = KISAnalyzer()

    try:
        # 기본 정보 조회로 종목명 확인
        info = await kis.fetch_fundamental_info(code)
        name = info.get("종목명", code)
        current_price = info.get("현재가", 0)

        result, _ = await analyzer.analyze_stock_json(name)

        if result is None:
            return {
                "status": "failed",
                "symbol": code,
                "name": name,
                "error": "분석 결과를 가져올 수 없습니다.",
            }

        # Telegram notification
        if hasattr(result, "decision"):
            try:
                notifier = get_trade_notifier()
                await notifier.notify_analysis_complete(
                    symbol=code,
                    korean_name=name,
                    decision=result.decision,
                    confidence=float(result.confidence) if result.confidence else 0.0,
                    reasons=result.reasons
                    if hasattr(result, "reasons") and result.reasons
                    else [],
                    market_type="국내주식",
                )
            except Exception as notify_error:
                logger.warning("⚠️ 텔레그램 알림 전송 실패: %s", notify_error)

            # 수동 잔고(토스 등) 알림 전송
            try:
                from app.core.db import AsyncSessionLocal
                from app.models.manual_holdings import MarketType
                from app.services.toss_notification_service import (
                    send_toss_notification_if_needed,
                )

                async with AsyncSessionLocal() as db:
                    # USER_ID는 현재 1로 고정 (추후 다중 사용자 지원 시 변경 필요)
                    user_id = 1

                    # 매수/매도 추천 가격 추출
                    recommended_buy_price = None
                    recommended_sell_price = None
                    recommended_quantity = 1

                    if result.decision == "buy" and hasattr(
                        result, "appropriate_buy_min"
                    ):
                        # 4개 구간 중 가장 적절한 매수가 (appropriate_buy_min)
                        recommended_buy_price = float(result.appropriate_buy_min)
                    elif result.decision == "sell" and hasattr(
                        result, "appropriate_sell_min"
                    ):
                        # 4개 구간 중 가장 적절한 매도가 (appropriate_sell_min)
                        recommended_sell_price = float(result.appropriate_sell_min)

                    await send_toss_notification_if_needed(
                        db=db,
                        user_id=user_id,
                        ticker=code,
                        name=name,
                        market_type=MarketType.KR,
                        decision=result.decision,
                        current_price=float(current_price) if current_price else 0.0,
                        recommended_buy_price=recommended_buy_price,
                        recommended_sell_price=recommended_sell_price,
                        recommended_quantity=recommended_quantity,
                    )
            except Exception as toss_error:
                logger.warning("⚠️ 토스 알림 전송 실패: %s", toss_error)

        return {
            "status": "completed",
            "symbol": code,
            "name": name,
            "message": f"{name} 분석이 완료되었습니다.",
        }
    except Exception as exc:
        return {"status": "failed", "symbol": code, "error": str(exc)}
    finally:
        await analyzer.close()


async def run_analysis_for_my_domestic_stocks() -> dict:
    """보유 국내 주식 AI 분석 실행"""

    async def _run() -> dict:
        kis = KISClient()
        analyzer = KISAnalyzer()

        try:
            my_stocks = await kis.fetch_my_stocks()
            if not my_stocks:
                return {
                    "status": "completed",
                    "analyzed_count": 0,
                    "total_count": 0,
                    "message": NO_DOMESTIC_STOCKS_MESSAGE,
                    "results": [],
                }

            total_count = len(my_stocks)
            results = []

            for _index, stock in enumerate(my_stocks, 1):
                code = stock.get("pdno")
                name = stock.get("prdt_name")

                try:
                    result, _ = await analyzer.analyze_stock_json(name)
                    results.append({"name": name, "code": code, "success": True})

                    # Send Telegram notification if analysis completed successfully
                    if result is not None and hasattr(result, "decision"):
                        try:
                            notifier = get_trade_notifier()
                            await notifier.notify_analysis_complete(
                                symbol=code,
                                korean_name=name,
                                decision=result.decision,
                                confidence=float(result.confidence)
                                if result.confidence
                                else 0.0,
                                reasons=result.reasons
                                if hasattr(result, "reasons") and result.reasons
                                else [],
                                market_type="국내주식",
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
                except Exception as e:
                    results.append(
                        {"name": name, "code": code, "success": False, "error": str(e)}
                    )

            success_count = sum(1 for r in results if r["success"])
            return {
                "status": "completed",
                "analyzed_count": success_count,
                "total_count": total_count,
                "message": f"{success_count}/{total_count}개 종목 분석 완료",
                "results": results,
            }
        except Exception as e:
            return {"status": "failed", "error": str(e)}
        finally:
            await analyzer.close()

    return await _run()


async def execute_domestic_buy_orders() -> dict:
    """국내 주식 자동 매수 주문 실행"""

    async def _run() -> dict:
        kis = KISClient()
        try:
            # 보유 주식 조회 (평단가 확인용)
            my_stocks = await kis.fetch_my_stocks()

            # 분석된 종목이 있어야 매수 가능 (DB에서 최근 분석 조회 필요)
            # 여기서는 보유 종목에 대해서만 매수 시도 (추가 매수)
            # 신규 매수는 별도 로직 필요 (관심 종목 등). 현재는 보유 종목 추가 매수만 구현.

            if not my_stocks:
                return {
                    "status": "completed",
                    "success_count": 0,
                    "total_count": 0,
                    "message": NO_DOMESTIC_STOCKS_MESSAGE,
                    "results": [],
                }

            total_count = len(my_stocks)
            results = []

            for _index, stock in enumerate(my_stocks, 1):
                code = stock.get("pdno")
                name = stock.get("prdt_name")
                avg_price = float(stock.get("pchs_avg_pric", 0))
                current_price = float(
                    stock.get("prpr", 0)
                )  # 현재가 (fetch_my_stocks에서 가져옴)

                try:
                    res = await process_kis_domestic_buy_orders_with_analysis(
                        kis, code, current_price, avg_price
                    )
                    results.append(
                        {
                            "name": name,
                            "code": code,
                            "success": res["success"],
                            "message": res["message"],
                        }
                    )
                except Exception as e:
                    results.append(
                        {"name": name, "code": code, "success": False, "error": str(e)}
                    )

            success_count = sum(1 for r in results if r["success"])
            return {
                "status": "completed",
                "success_count": success_count,
                "total_count": total_count,
                "message": f"{success_count}/{total_count}개 종목 매수 주문 완료",
                "results": results,
            }
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    return await _run()


async def execute_domestic_sell_orders() -> dict:
    """국내 주식 자동 매도 주문 실행"""

    async def _run() -> dict:
        kis = KISClient()
        try:
            my_stocks = await kis.fetch_my_stocks()
            if not my_stocks:
                return {
                    "status": "completed",
                    "success_count": 0,
                    "total_count": 0,
                    "message": NO_DOMESTIC_STOCKS_MESSAGE,
                    "results": [],
                }

            total_count = len(my_stocks)
            results = []

            for _index, stock in enumerate(my_stocks, 1):
                code = stock.get("pdno")
                name = stock.get("prdt_name")
                avg_price = float(stock.get("pchs_avg_pric", 0))
                current_price = float(stock.get("prpr", 0))
                qty = int(stock.get("hldg_qty", 0))

                try:
                    res = await process_kis_domestic_sell_orders_with_analysis(
                        kis, code, current_price, avg_price, qty
                    )
                    results.append(
                        {
                            "name": name,
                            "code": code,
                            "success": res["success"],
                            "message": res["message"],
                        }
                    )
                except Exception as e:
                    results.append(
                        {"name": name, "code": code, "success": False, "error": str(e)}
                    )

            success_count = sum(1 for r in results if r["success"])
            return {
                "status": "completed",
                "success_count": success_count,
                "total_count": total_count,
                "message": f"{success_count}/{total_count}개 종목 매도 주문 완료",
                "results": results,
            }
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    return await _run()


async def _cancel_domestic_pending_orders(
    kis: KISClient,
    stock_code: str,
    order_type: str,
    all_open_orders: list[dict],
) -> dict:
    """
    특정 종목의 기존 국내 미체결 주문들을 취소합니다.

    Args:
        kis: KIS 클라이언트
        stock_code: 종목코드
        order_type: "buy" 또는 "sell"
        all_open_orders: 미리 조회한 전체 미체결 주문 목록

    Returns:
        취소 결과 딕셔너리 {'cancelled': int, 'failed': int, 'total': int}
    """
    # sll_buy_dvsn_cd: 01=매도, 02=매수
    target_code = "02" if order_type == "buy" else "01"

    # 해당 종목의 주문만 필터링 (필드명 대소문자 모두 확인)
    target_orders = [
        order
        for order in all_open_orders
        if (order.get("pdno") or order.get("PDNO")) == stock_code
        and (order.get("sll_buy_dvsn_cd") or order.get("SLL_BUY_DVSN_CD"))
        == target_code
    ]

    if not target_orders:
        return {"cancelled": 0, "failed": 0, "total": 0}

    cancelled = 0
    failed = 0

    for order in target_orders:
        order_number: str | None = None
        try:
            # API 응답 필드명이 소문자 또는 대문자일 수 있음
            order_number = (
                order.get("odno")
                or order.get("ODNO")
                or order.get("ord_no")
                or order.get("ORD_NO")
            )
            order_qty = int(order.get("ord_qty") or order.get("ORD_QTY") or 0)
            order_price = int(
                float(order.get("ord_unpr") or order.get("ORD_UNPR") or 0)
            )
            order_orgno = (
                order.get("ord_gno_brno")
                or order.get("ORD_GNO_BRNO")
                or order.get("krx_fwdg_ord_orgno")
                or order.get("KRX_FWDG_ORD_ORGNO")
            )

            if not order_number:
                logger.warning(f"주문번호 없음 ({stock_code}): order={order}")
                failed += 1
                continue

            await kis.cancel_korea_order(
                order_number=order_number,
                stock_code=stock_code,
                quantity=order_qty,
                price=order_price,
                order_type=order_type,
                is_mock=False,
                krx_fwdg_ord_orgno=str(order_orgno).strip() if order_orgno else None,
            )
            cancelled += 1
            await asyncio.sleep(0.2)  # API 호출 제한 방지
        except Exception as e:
            logger.warning(
                "주문 취소 실패 (%s, %s): %s",
                stock_code,
                order_number or "unknown",
                e,
            )
            failed += 1

    return {"cancelled": cancelled, "failed": failed, "total": len(target_orders)}


async def run_per_domestic_stock_automation() -> dict:
    """국내 주식 종목별 자동 실행 (미체결취소 -> 분석 -> 매수 -> 매도)

    Refactored to use TradingOrchestrator with DomesticStrategy.
    Maintains backward compatibility with existing test expectations.
    """

    async def _run() -> dict:
        from app.jobs.kis_trading_orchestrator import (
            DomesticStrategy,
            TradingOrchestrator,
        )
        from app.jobs.kis_trading_steps import (
            AnalyzeStep,
            BuyStep,
            CancelBuyOrdersStep,
            CancelSellOrdersStep,
            RefreshStep,
            SellStep,
        )

        kis = KISClient()

        try:
            # Create strategy and steps with trading function dependencies
            # This allows tests to patch the module-level functions
            strategy = DomesticStrategy()

            # Create analyzer instance from module-level class (can be patched by tests)
            analyzer = KISAnalyzer()

            # Create steps with dependencies injected from this module
            # This ensures that monkeypatch patches work correctly in tests
            steps = [
                AnalyzeStep(analyzer=analyzer),
                CancelBuyOrdersStep(),
                BuyStep(
                    domestic_buy_func=process_kis_domestic_buy_orders_with_analysis,
                    overseas_buy_func=None,  # Domestic strategy doesn't use overseas
                ),
                RefreshStep(),
                CancelSellOrdersStep(),
                SellStep(
                    domestic_sell_func=process_kis_domestic_sell_orders_with_analysis,
                    overseas_sell_func=None,  # Domestic strategy doesn't use overseas
                ),
            ]

            # Create and run orchestrator
            orchestrator = TradingOrchestrator(strategy=strategy, steps=steps)
            result = await orchestrator.run(kis)

            # Transform result to match original format for backward compatibility
            # Original format: {"name", "code", "steps"} with Korean step names
            transformed_results = []
            for stock_result in result.get("results", []):
                # Transform step names from English to Korean for backward compatibility
                transformed_steps = []
                for step in stock_result.get("steps", []):
                    step_name = step.get("step", "")
                    # Map English step names to Korean
                    step_name_map = {
                        "analyze": "분석",
                        "cancel_buy_orders": "매수취소",
                        "buy": "매수",
                        "refresh": "리프레시",
                        "cancel_sell_orders": "매도취소",
                        "sell": "매도",
                    }
                    korean_name = step_name_map.get(step_name, step_name)
                    transformed_steps.append({
                        "step": korean_name,
                        "result": step.get("result", {}),
                    })

                transformed_results.append({
                    "name": stock_result.get("name", ""),
                    "code": stock_result.get("symbol", ""),
                    "steps": transformed_steps,
                })

            return {
                "status": result.get("status", "completed"),
                "message": "종목별 자동 실행 완료"
                if result.get("status") == "completed"
                else result.get("message", ""),
                "results": transformed_results,
            }

        except Exception as e:
            # 태스크 전체 실패 시 로깅
            logger.error(
                f"[태스크 실패] kis.run_per_domestic_stock_automation: {e}",
                exc_info=True,
            )
            return {"status": "failed", "error": str(e)}

    return await _run()


async def analyze_domestic_stock_task(symbol: str) -> dict:
    """단일 국내 주식 분석 실행"""
    return await _analyze_domestic_stock_async(symbol)


async def execute_domestic_buy_order_task(symbol: str) -> dict:
    """단일 국내 주식 매수 주문 실행"""

    async def _run() -> dict:
        kis = KISClient()
        try:
            # 현재가 및 평단가 조회
            my_stocks = await kis.fetch_my_stocks()
            target_stock = next((s for s in my_stocks if s["pdno"] == symbol), None)

            if target_stock:
                avg_price = float(target_stock["pchs_avg_pric"])
                current_price = float(target_stock["prpr"])
            else:
                # 보유 중이 아니면 현재가 조회 필요
                price_info = await kis.fetch_price(symbol)
                current_price = float(price_info["output"]["stck_prpr"])
                avg_price = 0  # 신규 매수

            res = await process_kis_domestic_buy_orders_with_analysis(
                kis, symbol, current_price, avg_price
            )
            return res
        except Exception as e:
            return {"success": False, "error": str(e)}

    return await _run()


async def execute_domestic_sell_order_task(symbol: str) -> dict:
    """단일 국내 주식 매도 주문 실행"""

    async def _run() -> dict:
        kis = KISClient()
        try:
            my_stocks = await kis.fetch_my_stocks()
            target_stock = next((s for s in my_stocks if s["pdno"] == symbol), None)

            if not target_stock:
                return {"success": False, "message": "보유 중인 주식이 아닙니다."}

            avg_price = float(target_stock["pchs_avg_pric"])
            current_price = float(target_stock["prpr"])
            qty = int(target_stock["hldg_qty"])

            res = await process_kis_domestic_sell_orders_with_analysis(
                kis, symbol, current_price, avg_price, qty
            )
            return res
        except Exception as e:
            return {"success": False, "error": str(e)}

    return await _run()


async def analyze_overseas_stock_task(symbol: str) -> dict:
    """단일 해외 주식 분석 실행"""
    return await _analyze_overseas_stock_async(symbol)


async def execute_overseas_buy_order_task(symbol: str) -> dict:
    """단일 해외 주식 매수 주문 실행"""

    async def _run() -> dict:
        kis = KISClient()
        try:
            my_stocks = await kis.fetch_my_overseas_stocks()
            # 심볼 형식 정규화하여 비교
            normalized_symbol = to_db_symbol(symbol)
            target_stock = next(
                (
                    s
                    for s in my_stocks
                    if to_db_symbol(s.get("ovrs_pdno", "")) == normalized_symbol
                ),
                None,
            )

            if target_stock:
                avg_price = float(target_stock["pchs_avg_pric"])
                current_price = float(target_stock["now_pric2"])
                exchange_code = await _resolve_overseas_exchange_code(
                    symbol,
                    target_stock.get("ovrs_excg_cd"),
                )
            else:
                try:
                    current_price = await kis.fetch_overseas_price(symbol)
                    avg_price = 0.0  # 신규 매수이므로 평단 없음
                except Exception as price_error:
                    return {
                        "success": False,
                        "message": f"현재가 조회 실패: {price_error}",
                    }
                exchange_code = await _resolve_overseas_exchange_code(symbol, None)

            res = await process_kis_overseas_buy_orders_with_analysis(
                kis, symbol, current_price, avg_price, exchange_code
            )
            return res
        except Exception as e:
            return {"success": False, "error": str(e)}

    return await _run()


async def execute_overseas_sell_order_task(symbol: str) -> dict:
    """단일 해외 주식 매도 주문 실행"""

    async def _run() -> dict:
        kis = KISClient()
        try:
            my_stocks = await kis.fetch_my_overseas_stocks()
            # 심볼 형식 정규화하여 비교
            normalized_symbol = to_db_symbol(symbol)
            target_stock = next(
                (
                    s
                    for s in my_stocks
                    if to_db_symbol(s.get("ovrs_pdno", "")) == normalized_symbol
                ),
                None,
            )

            if not target_stock:
                return {"success": False, "message": "보유 중인 주식이 아닙니다."}

            avg_price = float(target_stock["pchs_avg_pric"])
            current_price = float(target_stock["now_pric2"])
            # 매도 시 미체결 주문을 제외한 주문 가능 수량(ord_psbl_qty)을 사용
            qty = int(
                float(
                    target_stock.get(
                        "ord_psbl_qty", target_stock.get("ovrs_cblc_qty", 0)
                    )
                )
            )
            exchange_code = await _resolve_overseas_exchange_code(
                symbol,
                target_stock.get("ovrs_excg_cd"),
            )

            res = await process_kis_overseas_sell_orders_with_analysis(
                kis, symbol, current_price, avg_price, qty, exchange_code
            )
            return res
        except Exception as e:
            return {"success": False, "error": str(e)}

    return await _run()


# --- Overseas Stocks Tasks ---


async def _analyze_overseas_stock_async(symbol: str) -> dict[str, object]:
    """단일 해외 주식 분석 비동기 헬퍼"""
    if not symbol:
        return {"status": "failed", "error": "심볼이 필요합니다."}

    import app.services.brokers.yahoo.client as yahoo
    from app.analysis.service_analyzers import YahooAnalyzer

    analyzer = (
        YahooAnalyzer()
    )  # 해외 주식은 YahooAnalyzer 사용 (또는 KISAnalyzer 확장 필요 시 변경)

    try:
        result, _ = await analyzer.analyze_stock_json(symbol)

        if result is None:
            return {
                "status": "failed",
                "symbol": symbol,
                "error": "분석 결과를 가져올 수 없습니다.",
            }

        # Telegram notification
        if hasattr(result, "decision"):
            try:
                notifier = get_trade_notifier()
                await notifier.notify_analysis_complete(
                    symbol=symbol,
                    korean_name=symbol,  # 해외주식은 한글명이 없을 수 있음
                    decision=result.decision,
                    confidence=float(result.confidence) if result.confidence else 0.0,
                    reasons=result.reasons
                    if hasattr(result, "reasons") and result.reasons
                    else [],
                    market_type="해외주식",
                )
            except Exception as notify_error:
                logger.warning("⚠️ 텔레그램 알림 전송 실패: %s", notify_error)

            # 수동 잔고(토스 등) 알림 전송
            try:
                from app.core.db import AsyncSessionLocal
                from app.models.manual_holdings import MarketType
                from app.services.toss_notification_service import (
                    send_toss_notification_if_needed,
                )

                # 현재가 조회
                current_price = 0.0
                try:
                    price_df = await yahoo.fetch_price(symbol)
                    if not price_df.empty:
                        current_price = float(price_df.iloc[0]["close"])
                except Exception as price_error:
                    logger.warning(f"현재가 조회 실패 ({symbol}): {price_error}")

                async with AsyncSessionLocal() as db:
                    # USER_ID는 현재 1로 고정 (추후 다중 사용자 지원 시 변경 필요)
                    user_id = 1

                    # 매수/매도 추천 가격 추출
                    recommended_buy_price = None
                    recommended_sell_price = None
                    recommended_quantity = 1

                    if result.decision == "buy" and hasattr(
                        result, "appropriate_buy_min"
                    ):
                        # 4개 구간 중 가장 적절한 매수가 (appropriate_buy_min)
                        recommended_buy_price = float(result.appropriate_buy_min)
                    elif result.decision == "sell" and hasattr(
                        result, "appropriate_sell_min"
                    ):
                        # 4개 구간 중 가장 적절한 매도가 (appropriate_sell_min)
                        recommended_sell_price = float(result.appropriate_sell_min)

                    await send_toss_notification_if_needed(
                        db=db,
                        user_id=user_id,
                        ticker=symbol,
                        name=symbol,
                        market_type=MarketType.US,
                        decision=result.decision,
                        current_price=current_price,
                        recommended_buy_price=recommended_buy_price,
                        recommended_sell_price=recommended_sell_price,
                        recommended_quantity=recommended_quantity,
                    )
            except Exception as toss_error:
                logger.warning("⚠️ 토스 알림 전송 실패: %s", toss_error)

        return {
            "status": "completed",
            "symbol": symbol,
            "message": f"{symbol} 분석이 완료되었습니다.",
        }
    except Exception as exc:
        return {"status": "failed", "symbol": symbol, "error": str(exc)}
    finally:
        await analyzer.close()


async def run_analysis_for_my_overseas_stocks() -> dict:
    """보유 해외 주식 AI 분석 실행"""

    async def _run() -> dict:
        kis = KISClient()
        from app.analysis.service_analyzers import YahooAnalyzer

        analyzer = YahooAnalyzer()

        try:
            my_stocks = await kis.fetch_my_overseas_stocks()
            if not my_stocks:
                return {
                    "status": "completed",
                    "analyzed_count": 0,
                    "total_count": 0,
                    "message": NO_OVERSEAS_STOCKS_MESSAGE,
                    "results": [],
                }

            total_count = len(my_stocks)
            results = []

            for _index, stock in enumerate(my_stocks, 1):
                symbol = stock.get("ovrs_pdno")  # 심볼
                name = stock.get("ovrs_item_name")

                try:
                    # 해외주식은 심볼로 분석
                    result, _ = await analyzer.analyze_stock_json(symbol)
                    results.append({"name": name, "symbol": symbol, "success": True})

                    # Send Telegram notification if analysis completed successfully
                    if result is not None and hasattr(result, "decision"):
                        try:
                            notifier = get_trade_notifier()
                            await notifier.notify_analysis_complete(
                                symbol=symbol,
                                korean_name=name or symbol,
                                decision=result.decision,
                                confidence=float(result.confidence)
                                if result.confidence
                                else 0.0,
                                reasons=result.reasons
                                if hasattr(result, "reasons") and result.reasons
                                else [],
                                market_type="해외주식",
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
                except Exception as e:
                    results.append(
                        {
                            "name": name,
                            "symbol": symbol,
                            "success": False,
                            "error": str(e),
                        }
                    )

            success_count = sum(1 for r in results if r["success"])
            return {
                "status": "completed",
                "analyzed_count": success_count,
                "total_count": total_count,
                "message": f"{success_count}/{total_count}개 종목 분석 완료",
                "results": results,
            }
        except Exception as e:
            return {"status": "failed", "error": str(e)}
        finally:
            await analyzer.close()

    return await _run()


async def execute_overseas_buy_orders() -> dict:
    """해외 주식 자동 매수 주문 실행"""

    async def _run() -> dict:
        kis = KISClient()
        try:
            my_stocks = await kis.fetch_my_overseas_stocks()
            if not my_stocks:
                return {
                    "status": "completed",
                    "success_count": 0,
                    "total_count": 0,
                    "message": NO_OVERSEAS_STOCKS_MESSAGE,
                    "results": [],
                }

            total_count = len(my_stocks)
            results = []

            for _index, stock in enumerate(my_stocks, 1):
                symbol = stock.get("ovrs_pdno")
                name = stock.get("ovrs_item_name")
                avg_price = float(stock.get("pchs_avg_pric", 0))
                current_price = float(stock.get("now_pric2", 0))
                exchange_code = await _resolve_overseas_exchange_code(
                    symbol,
                    stock.get("ovrs_excg_cd"),
                )

                try:
                    res = await process_kis_overseas_buy_orders_with_analysis(
                        kis, symbol, current_price, avg_price, exchange_code
                    )
                    results.append(
                        {
                            "name": name,
                            "symbol": symbol,
                            "success": res["success"],
                            "message": res["message"],
                        }
                    )
                    # 매수 성공 시 텔레그램 알림
                    if res.get("success") and res.get("orders_placed", 0) > 0:
                        try:
                            notifier = get_trade_notifier()
                            await notifier.notify_buy_order(
                                symbol=symbol,
                                korean_name=name or symbol,
                                order_count=res.get("orders_placed", 0),
                                total_amount=res.get("total_amount", 0.0),
                                prices=res.get("prices", []),
                                volumes=res.get("quantities", []),
                                market_type="해외주식",
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
                except Exception as e:
                    results.append(
                        {
                            "name": name,
                            "symbol": symbol,
                            "success": False,
                            "error": str(e),
                        }
                    )
                    # 매수 실패 시 텔레그램 알림
                    try:
                        notifier = get_trade_notifier()
                        await notifier.notify_trade_failure(
                            symbol=symbol,
                            korean_name=name or symbol,
                            reason=f"매수 주문 실패: {str(e)}",
                            market_type="해외주식",
                        )
                    except Exception as notify_error:
                        logger.warning("텔레그램 알림 전송 실패: %s", notify_error)

            success_count = sum(1 for r in results if r["success"])
            return {
                "status": "completed",
                "success_count": success_count,
                "total_count": total_count,
                "message": f"{success_count}/{total_count}개 종목 매수 주문 완료",
                "results": results,
            }
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    return await _run()


async def execute_overseas_sell_orders() -> dict:
    """해외 주식 자동 매도 주문 실행"""

    async def _run() -> dict:
        kis = KISClient()
        try:
            my_stocks = await kis.fetch_my_overseas_stocks()
            if not my_stocks:
                return {
                    "status": "completed",
                    "success_count": 0,
                    "total_count": 0,
                    "message": NO_OVERSEAS_STOCKS_MESSAGE,
                    "results": [],
                }

            total_count = len(my_stocks)
            results = []

            for _index, stock in enumerate(my_stocks, 1):
                symbol = stock.get("ovrs_pdno")
                name = stock.get("ovrs_item_name")
                avg_price = float(stock.get("pchs_avg_pric", 0))
                current_price = float(stock.get("now_pric2", 0))
                # 매도 시 미체결 주문을 제외한 주문 가능 수량(ord_psbl_qty)을 사용
                # ord_psbl_qty가 없으면 ovrs_cblc_qty를 fallback으로 사용
                qty = int(
                    float(stock.get("ord_psbl_qty", stock.get("ovrs_cblc_qty", 0)))
                )
                exchange_code = await _resolve_overseas_exchange_code(
                    symbol,
                    stock.get("ovrs_excg_cd"),
                )

                try:
                    res = await process_kis_overseas_sell_orders_with_analysis(
                        kis, symbol, current_price, avg_price, qty, exchange_code
                    )
                    results.append(
                        {
                            "name": name,
                            "symbol": symbol,
                            "success": res["success"],
                            "message": res["message"],
                        }
                    )
                    # 매도 성공 시 텔레그램 알림
                    if res.get("success") and res.get("orders_placed", 0) > 0:
                        try:
                            notifier = get_trade_notifier()
                            await notifier.notify_sell_order(
                                symbol=symbol,
                                korean_name=name or symbol,
                                order_count=res.get("orders_placed", 0),
                                total_volume=res.get("total_volume", 0),
                                prices=res.get("prices", []),
                                volumes=res.get("quantities", []),
                                expected_amount=res.get("expected_amount", 0.0),
                                market_type="해외주식",
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
                except Exception as e:
                    results.append(
                        {
                            "name": name,
                            "symbol": symbol,
                            "success": False,
                            "error": str(e),
                        }
                    )
                    # 매도 실패 시 텔레그램 알림
                    try:
                        notifier = get_trade_notifier()
                        await notifier.notify_trade_failure(
                            symbol=symbol,
                            korean_name=name or symbol,
                            reason=f"매도 주문 실패: {str(e)}",
                            market_type="해외주식",
                        )
                    except Exception as notify_error:
                        logger.warning("텔레그램 알림 전송 실패: %s", notify_error)

            success_count = sum(1 for r in results if r["success"])
            return {
                "status": "completed",
                "success_count": success_count,
                "total_count": total_count,
                "message": f"{success_count}/{total_count}개 종목 매도 주문 완료",
                "results": results,
            }
        except Exception as e:
            return {"status": "failed", "error": str(e)}

    return await _run()


async def _cancel_overseas_pending_orders(
    kis: KISClient,
    symbol: str,
    exchange_code: str,
    order_type: str,
    all_open_orders: list[dict],
) -> dict:
    """
    특정 종목의 기존 미체결 주문들을 취소합니다.

    Args:
        kis: KIS 클라이언트
        symbol: 종목 심볼
        exchange_code: 거래소 코드
        order_type: "buy" 또는 "sell"
        all_open_orders: 미리 조회한 전체 미체결 주문 목록

    Returns:
        취소 결과 딕셔너리 {'cancelled': int, 'failed': int, 'total': int}
    """
    # sll_buy_dvsn_cd: 01=매도, 02=매수
    target_code = "02" if order_type == "buy" else "01"

    # 해당 종목의 주문만 필터링 (필드명 대소문자 모두 확인, 심볼 형식 정규화)
    normalized_symbol = to_db_symbol(symbol)
    target_orders = [
        order
        for order in all_open_orders
        if to_db_symbol(order.get("pdno") or order.get("PDNO") or "")
        == normalized_symbol
        and (order.get("sll_buy_dvsn_cd") or order.get("SLL_BUY_DVSN_CD"))
        == target_code
    ]

    if not target_orders:
        return {"cancelled": 0, "failed": 0, "total": 0}

    cancelled = 0
    failed = 0

    for order in target_orders:
        order_number: str | None = None
        try:
            # API 응답 필드명이 소문자 또는 대문자일 수 있음
            order_number = (
                order.get("odno")
                or order.get("ODNO")
                or order.get("ord_no")
                or order.get("ORD_NO")
            )
            order_qty = int(order.get("ft_ord_qty") or order.get("FT_ORD_QTY") or 0)

            if not order_number:
                logger.warning(f"주문번호 없음 ({symbol}): order={order}")
                failed += 1
                continue

            await kis.cancel_overseas_order(
                order_number=order_number,
                symbol=symbol,
                exchange_code=exchange_code,
                quantity=order_qty,
                is_mock=False,
            )
            cancelled += 1
            await asyncio.sleep(0.2)  # API 호출 제한 방지
        except Exception as e:
            logger.warning(
                "주문 취소 실패 (%s, %s): %s",
                symbol,
                order_number or "unknown",
                e,
            )
            failed += 1

    return {"cancelled": cancelled, "failed": failed, "total": len(target_orders)}


async def run_per_overseas_stock_automation() -> dict:
    """해외 주식 종목별 자동 실행 (미체결취소 -> 분석 -> 매수 -> 매도)

    Refactored to use TradingOrchestrator with OverseasStrategy.
    Maintains backward compatibility with existing test expectations.
    """

    async def _run() -> dict:
        from app.analysis.service_analyzers import YahooAnalyzer
        from app.jobs.kis_trading_orchestrator import (
            OverseasStrategy,
            TradingOrchestrator,
        )
        from app.jobs.kis_trading_steps import (
            AnalyzeStep,
            BuyStep,
            CancelBuyOrdersStep,
            CancelSellOrdersStep,
            RefreshStep,
            SellStep,
        )

        kis = KISClient()

        try:
            # Create strategy and steps with trading function dependencies
            # This allows tests to patch the module-level functions
            strategy = OverseasStrategy()

            # Create analyzer instance from module-level class (can be patched by tests)
            analyzer = YahooAnalyzer()

            # Create steps with dependencies injected from this module
            # This ensures that monkeypatch patches work correctly in tests
            steps = [
                AnalyzeStep(analyzer=analyzer),
                CancelBuyOrdersStep(),
                BuyStep(
                    domestic_buy_func=None,  # Overseas strategy doesn't use domestic
                    overseas_buy_func=process_kis_overseas_buy_orders_with_analysis,
                ),
                RefreshStep(),
                CancelSellOrdersStep(),
                SellStep(
                    domestic_sell_func=None,  # Overseas strategy doesn't use domestic
                    overseas_sell_func=process_kis_overseas_sell_orders_with_analysis,
                ),
            ]

            # Create and run orchestrator
            orchestrator = TradingOrchestrator(strategy=strategy, steps=steps)
            result = await orchestrator.run(kis)

            # Transform result to match original format for backward compatibility
            # Original format: {"name", "symbol", "steps"} with Korean step names
            transformed_results = []
            for stock_result in result.get("results", []):
                # Transform step names from English to Korean for backward compatibility
                transformed_steps = []
                for step in stock_result.get("steps", []):
                    step_name = step.get("step", "")
                    # Map English step names to Korean
                    step_name_map = {
                        "analyze": "분석",
                        "cancel_buy_orders": "매수취소",
                        "buy": "매수",
                        "refresh": "리프레시",
                        "cancel_sell_orders": "매도취소",
                        "sell": "매도",
                    }
                    korean_name = step_name_map.get(step_name, step_name)
                    transformed_steps.append({
                        "step": korean_name,
                        "result": step.get("result", {}),
                    })

                transformed_results.append({
                    "name": stock_result.get("name", ""),
                    "symbol": stock_result.get("symbol", ""),
                    "steps": transformed_steps,
                })

            return {
                "status": result.get("status", "completed"),
                "message": "종목별 자동 실행 완료"
                if result.get("status") == "completed"
                else result.get("message", ""),
                "results": transformed_results,
            }

        except Exception as e:
            # 태스크 전체 실패 시 로깅
            logger.error(
                f"[태스크 실패] kis.run_per_overseas_stock_automation: {e}",
                exc_info=True,
            )
            return {"status": "failed", "error": str(e)}
        finally:
            await analyzer.close()

    return await _run()
