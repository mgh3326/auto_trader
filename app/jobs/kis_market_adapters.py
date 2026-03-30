import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

AutomationResult = dict[str, object]
StepResults = list[dict[str, object]]

logger = logging.getLogger(__name__)


class SupportsMarketAutomation(Protocol):
    market: str

    async def execute(self) -> AutomationResult: ...


@dataclass(slots=True)
class DomesticAutomationAdapter:
    kis_client_factory: Callable[[], Any]
    async_session_factory: Callable[[], Any]
    manual_holdings_service_factory: Callable[[Any], Any]
    manual_market_type: Any
    buy_handler: Callable[[Any, str, float, float], Awaitable[dict[str, Any]]]
    sell_handler: Callable[[Any, str, float, float, int], Awaitable[dict[str, Any]]]
    cancel_pending_orders: Callable[
        [Any, str, str, list[dict[str, Any]]], Awaitable[dict[str, Any]]
    ]
    send_toss_recommendation: Callable[..., Awaitable[None]]
    notifier_factory: Callable[[], Any]
    no_stocks_message: str
    market: str = "domestic"
    market_type_label: str = "국내주식"
    result_symbol_key: str = "code"
    refresh_holdings_after_sell_cancel: bool = True

    def analysis_target(self, *, name: str | None, symbol: str | None) -> str:
        return name or symbol or ""

    def build_result_entry(
        self,
        *,
        name: str | None,
        symbol: str | None,
        steps: StepResults,
    ) -> AutomationResult:
        resolved_name = name or symbol or ""
        resolved_symbol = symbol or ""
        return {
            "name": resolved_name,
            self.result_symbol_key: resolved_symbol,
            "steps": steps,
        }

    async def execute(self) -> AutomationResult:
        kis = self.kis_client_factory()

        try:
            my_stocks = await kis.fetch_my_stocks()

            async with self.async_session_factory() as db:
                manual_service = self.manual_holdings_service_factory(db)
                manual_holdings = await manual_service.get_holdings_by_user(
                    user_id=1,
                    market_type=self.manual_market_type,
                )

            for holding in manual_holdings:
                ticker = holding.ticker
                if any(stock.get("pdno") == ticker for stock in my_stocks):
                    continue

                qty_str = str(holding.quantity)
                my_stocks.append(
                    {
                        "pdno": ticker,
                        "prdt_name": holding.display_name or ticker,
                        "hldg_qty": qty_str,
                        "ord_psbl_qty": qty_str,
                        "pchs_avg_pric": str(holding.avg_price),
                        "prpr": str(holding.avg_price),
                        "_is_manual": True,
                    }
                )

            if not my_stocks:
                return {
                    "status": "completed",
                    "message": self.no_stocks_message,
                    "results": [],
                }

            results: list[AutomationResult] = []
            all_open_orders = await kis.inquire_korea_orders(is_mock=False)
            logger.info("국내주식 미체결 주문 조회 완료: %s건", len(all_open_orders))

            for stock in my_stocks:
                code = stock.get("pdno")
                name = stock.get("prdt_name")
                avg_price = float(stock.get("pchs_avg_pric", 0))
                current_price = float(stock.get("prpr", 0))
                qty = int(float(stock.get("ord_psbl_qty", stock.get("hldg_qty", 0))))
                is_manual = stock.get("_is_manual", False)

                if is_manual:
                    try:
                        price_info = await kis.fetch_fundamental_info(code)
                        current_price = float(price_info.get("현재가", current_price))
                        logger.info(
                            "[수동잔고] %s(%s) 현재가 조회: %s원",
                            name,
                            code,
                            f"{current_price:,}",
                        )
                    except Exception as exc:
                        logger.warning(
                            "[수동잔고] %s(%s) 현재가 조회 실패, 평단가 사용: %s",
                            name,
                            code,
                            exc,
                        )

                stock_steps: StepResults = []

                # Analysis step skipped (Gemini removed, OpenClaw replacement pending)
                stock_steps.append(
                    {
                        "step": "분석",
                        "result": {"success": True, "message": "분석 스킵 (대체 분석기 준비 중)"},
                    }
                )

                try:
                    cancel_result = await self.cancel_pending_orders(
                        kis,
                        code,
                        "buy",
                        all_open_orders,
                    )
                    if cancel_result["total"] > 0:
                        logger.info(
                            "%s 미체결 매수 주문 취소: %s/%s건",
                            name,
                            cancel_result["cancelled"],
                            cancel_result["total"],
                        )
                        stock_steps.append(
                            {
                                "step": "매수취소",
                                "result": {"success": True, **cancel_result},
                            }
                        )
                        await asyncio.sleep(0.5)
                except Exception as exc:
                    logger.warning("%s 미체결 매수 주문 취소 실패: %s", name, exc)
                    stock_steps.append(
                        {
                            "step": "매수취소",
                            "result": {"success": False, "error": str(exc)},
                        }
                    )

                try:
                    buy_result = await self.buy_handler(
                        kis, code, current_price, avg_price
                    )
                    stock_steps.append({"step": "매수", "result": buy_result})
                    if buy_result.get("error"):
                        logger.error(
                            "[매수 에러] %s(%s): %s",
                            name,
                            code,
                            buy_result["error"],
                            extra={"task": "kis.run_per_domestic_stock_automation"},
                        )
                    elif (
                        buy_result.get("success")
                        and buy_result.get("orders_placed", 0) > 0
                    ):
                        try:
                            notifier = self.notifier_factory()
                            await notifier.notify_buy_order(
                                symbol=code,
                                korean_name=name,
                                order_count=buy_result.get("orders_placed", 0),
                                total_amount=buy_result.get("total_amount", 0.0),
                                prices=buy_result.get("prices", []),
                                volumes=buy_result.get("quantities", []),
                                market_type=self.market_type_label,
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
                except Exception as exc:
                    error_msg = str(exc)
                    stock_steps.append(
                        {
                            "step": "매수",
                            "result": {"success": False, "error": error_msg},
                        }
                    )
                    logger.error(
                        "[매수 실패] %s(%s): %s",
                        name,
                        code,
                        error_msg,
                        extra={"task": "kis.run_per_domestic_stock_automation"},
                    )

                refreshed_qty = qty
                refreshed_avg_price = avg_price
                refreshed_current_price = current_price
                try:
                    latest_holdings = await kis.fetch_my_stocks()
                    latest = next(
                        (item for item in latest_holdings if item.get("pdno") == code),
                        None,
                    )
                    if latest:
                        refreshed_qty = int(
                            latest.get(
                                "ord_psbl_qty", latest.get("hldg_qty", refreshed_qty)
                            )
                        )
                        refreshed_avg_price = float(
                            latest.get("pchs_avg_pric", refreshed_avg_price)
                        )
                        refreshed_current_price = float(
                            latest.get("prpr", refreshed_current_price)
                        )
                except Exception as refresh_error:
                    logger.warning(
                        "잔고 재조회 실패 - 기존 수량 사용 (%s)",
                        refresh_error,
                    )

                if is_manual:
                    logger.info(
                        "[수동잔고] %s(%s) - KIS 매도 불가, 토스 추천 알림 발송",
                        name,
                        code,
                    )
                    try:
                        await self.send_toss_recommendation(
                            code=code,
                            name=name,
                            current_price=refreshed_current_price,
                            toss_quantity=refreshed_qty,
                            toss_avg_price=avg_price,
                        )
                        stock_steps.append(
                            {
                                "step": "매도",
                                "result": {
                                    "success": True,
                                    "message": "수동잔고 - 토스 추천 알림 발송",
                                    "orders_placed": 0,
                                },
                            }
                        )
                    except Exception as exc:
                        logger.warning(
                            "[수동잔고] %s(%s) 토스 추천 알림 발송 실패: %s",
                            name,
                            code,
                            exc,
                        )
                        stock_steps.append(
                            {
                                "step": "매도",
                                "result": {
                                    "success": True,
                                    "message": "수동잔고 - 매도 스킵",
                                    "orders_placed": 0,
                                },
                            }
                        )
                    results.append(
                        self.build_result_entry(
                            name=name, symbol=code, steps=stock_steps
                        )
                    )
                    continue

                sell_orders_cancelled = False
                try:
                    cancel_result = await self.cancel_pending_orders(
                        kis,
                        code,
                        "sell",
                        all_open_orders,
                    )
                    if cancel_result["total"] > 0:
                        logger.info(
                            "%s 미체결 매도 주문 취소: %s/%s건",
                            name,
                            cancel_result["cancelled"],
                            cancel_result["total"],
                        )
                        stock_steps.append(
                            {
                                "step": "매도취소",
                                "result": {"success": True, **cancel_result},
                            }
                        )
                        sell_orders_cancelled = cancel_result["cancelled"] > 0
                        await asyncio.sleep(0.5)
                except Exception as exc:
                    logger.warning("%s 미체결 매도 주문 취소 실패: %s", name, exc)
                    stock_steps.append(
                        {
                            "step": "매도취소",
                            "result": {"success": False, "error": str(exc)},
                        }
                    )

                if sell_orders_cancelled and self.refresh_holdings_after_sell_cancel:
                    try:
                        latest_holdings = await kis.fetch_my_stocks()
                        latest = next(
                            (
                                item
                                for item in latest_holdings
                                if item.get("pdno") == code
                            ),
                            None,
                        )
                        if latest:
                            refreshed_qty = int(
                                latest.get(
                                    "ord_psbl_qty",
                                    latest.get("hldg_qty", refreshed_qty),
                                )
                            )
                            refreshed_current_price = float(
                                latest.get("prpr", refreshed_current_price)
                            )
                            logger.info(
                                "%s 매도 취소 후 잔고 재조회: ord_psbl_qty=%s",
                                name,
                                refreshed_qty,
                            )
                    except Exception as refresh_error:
                        logger.warning(
                            "%s 매도 취소 후 잔고 재조회 실패 - 기존 수량 사용: %s",
                            name,
                            refresh_error,
                        )

                try:
                    sell_result = await self.sell_handler(
                        kis,
                        code,
                        refreshed_current_price,
                        refreshed_avg_price,
                        refreshed_qty,
                    )
                    stock_steps.append({"step": "매도", "result": sell_result})
                    if sell_result.get("error"):
                        logger.error(
                            "[매도 에러] %s(%s): %s",
                            name,
                            code,
                            sell_result["error"],
                            extra={"task": "kis.run_per_domestic_stock_automation"},
                        )
                    elif (
                        sell_result.get("success")
                        and sell_result.get("orders_placed", 0) > 0
                    ):
                        try:
                            notifier = self.notifier_factory()
                            await notifier.notify_sell_order(
                                symbol=code,
                                korean_name=name,
                                order_count=sell_result.get("orders_placed", 0),
                                total_volume=sell_result.get("total_volume", 0),
                                prices=sell_result.get("prices", []),
                                volumes=sell_result.get("quantities", []),
                                expected_amount=sell_result.get("expected_amount", 0.0),
                                market_type=self.market_type_label,
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
                except Exception as exc:
                    error_msg = str(exc)
                    stock_steps.append(
                        {
                            "step": "매도",
                            "result": {"success": False, "error": error_msg},
                        }
                    )
                    logger.error(
                        "[매도 실패] %s(%s): %s",
                        name,
                        code,
                        error_msg,
                        extra={"task": "kis.run_per_domestic_stock_automation"},
                    )

                results.append(
                    self.build_result_entry(name=name, symbol=code, steps=stock_steps)
                )

            return {
                "status": "completed",
                "message": "종목별 자동 실행 완료",
                "results": results,
            }
        except Exception as exc:
            logger.error(
                "[태스크 실패] kis.run_per_domestic_stock_automation: %s",
                exc,
                exc_info=True,
            )
            return {"status": "failed", "error": str(exc)}


@dataclass(slots=True)
class OverseasAutomationAdapter:
    kis_client_factory: Callable[[], Any]
    async_session_factory: Callable[[], Any]
    manual_holdings_service_factory: Callable[[Any], Any]
    manual_market_type: Any
    buy_handler: Callable[[Any, str, float, float, str], Awaitable[dict[str, Any]]]
    sell_handler: Callable[
        [Any, str, float, float, int, str], Awaitable[dict[str, Any]]
    ]
    resolve_exchange_code: Callable[[str, str | None], Awaitable[str]]
    load_open_orders: Callable[[Any], Awaitable[list[dict[str, Any]]]]
    cancel_pending_orders: Callable[
        [Any, str, str, str, list[dict[str, Any]]], Awaitable[dict[str, Any]]
    ]
    send_toss_recommendation: Callable[..., Awaitable[None]]
    notifier_factory: Callable[[], Any]
    normalize_symbol: Callable[[str], str]
    no_stocks_message: str
    market: str = "overseas"
    market_type_label: str = "해외주식"
    result_symbol_key: str = "symbol"
    refresh_holdings_after_sell_cancel: bool = False

    def analysis_target(self, *, name: str | None, symbol: str | None) -> str:
        return symbol or name or ""

    def build_result_entry(
        self,
        *,
        name: str | None,
        symbol: str | None,
        steps: StepResults,
    ) -> AutomationResult:
        resolved_name = name or symbol or ""
        resolved_symbol = symbol or ""
        return {
            "name": resolved_name,
            self.result_symbol_key: resolved_symbol,
            "steps": steps,
        }

    async def execute(self) -> AutomationResult:
        kis = self.kis_client_factory()

        try:
            my_stocks = await kis.fetch_my_overseas_stocks()

            async with self.async_session_factory() as db:
                manual_service = self.manual_holdings_service_factory(db)
                manual_holdings = await manual_service.get_holdings_by_user(
                    user_id=1,
                    market_type=self.manual_market_type,
                )

            for holding in manual_holdings:
                ticker = holding.ticker
                if any(
                    self.normalize_symbol(stock.get("ovrs_pdno", "")) == ticker
                    for stock in my_stocks
                ):
                    continue

                qty_str = str(holding.quantity)
                my_stocks.append(
                    {
                        "ovrs_pdno": ticker,
                        "ovrs_item_name": holding.display_name or ticker,
                        "ovrs_cblc_qty": qty_str,
                        "ord_psbl_qty": qty_str,
                        "pchs_avg_pric": str(holding.avg_price),
                        "now_pric2": "0",
                        "_is_manual": True,
                    }
                )

            if not my_stocks:
                return {
                    "status": "completed",
                    "message": self.no_stocks_message,
                    "results": [],
                }

            results: list[AutomationResult] = []
            all_open_orders = await self.load_open_orders(kis)
            logger.info("미체결 주문 조회 완료: %s건", len(all_open_orders))

            for stock in my_stocks:
                symbol = stock.get("ovrs_pdno")
                name = stock.get("ovrs_item_name")
                avg_price = float(stock.get("pchs_avg_pric", 0))
                current_price = float(stock.get("now_pric2", 0))
                qty = int(
                    float(stock.get("ord_psbl_qty", stock.get("ovrs_cblc_qty", 0)))
                )
                exchange_code = await self.resolve_exchange_code(
                    symbol,
                    stock.get("ovrs_excg_cd"),
                )
                is_manual = stock.get("_is_manual", False)

                if is_manual:
                    try:
                        price_df = await kis.inquire_overseas_price(symbol)
                        if not price_df.empty:
                            current_price = float(price_df.iloc[0]["close"])
                            logger.info(
                                "[수동잔고] %s(%s) 현재가 조회: $%.2f",
                                name,
                                symbol,
                                current_price,
                            )
                    except Exception as exc:
                        logger.warning(
                            "[수동잔고] %s(%s) 현재가 조회 실패, 평단가 사용: %s",
                            name,
                            symbol,
                            exc,
                        )
                        current_price = avg_price

                stock_steps: StepResults = []

                # Analysis step skipped (Gemini removed, OpenClaw replacement pending)
                stock_steps.append(
                    {
                        "step": "분석",
                        "result": {"success": True, "message": "분석 스킵 (대체 분석기 준비 중)"},
                    }
                )

                try:
                    cancel_result = await self.cancel_pending_orders(
                        kis,
                        symbol,
                        exchange_code,
                        "buy",
                        all_open_orders,
                    )
                    if cancel_result["total"] > 0:
                        logger.info(
                            "%s 미체결 매수 주문 취소: %s/%s건",
                            symbol,
                            cancel_result["cancelled"],
                            cancel_result["total"],
                        )
                        stock_steps.append(
                            {
                                "step": "매수취소",
                                "result": {"success": True, **cancel_result},
                            }
                        )
                        await asyncio.sleep(0.5)
                except Exception as exc:
                    logger.warning("%s 미체결 매수 주문 취소 실패: %s", symbol, exc)
                    stock_steps.append(
                        {
                            "step": "매수취소",
                            "result": {"success": False, "error": str(exc)},
                        }
                    )

                try:
                    buy_result = await self.buy_handler(
                        kis,
                        symbol,
                        current_price,
                        avg_price,
                        exchange_code,
                    )
                    stock_steps.append({"step": "매수", "result": buy_result})
                    if (
                        buy_result.get("success")
                        and buy_result.get("orders_placed", 0) > 0
                    ):
                        try:
                            notifier = self.notifier_factory()
                            await notifier.notify_buy_order(
                                symbol=symbol,
                                korean_name=name or symbol,
                                order_count=buy_result.get("orders_placed", 0),
                                total_amount=buy_result.get("total_amount", 0.0),
                                prices=buy_result.get("prices", []),
                                volumes=buy_result.get("quantities", []),
                                market_type=self.market_type_label,
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
                except Exception as exc:
                    stock_steps.append(
                        {
                            "step": "매수",
                            "result": {"success": False, "error": str(exc)},
                        }
                    )
                    try:
                        notifier = self.notifier_factory()
                        await notifier.notify_trade_failure(
                            symbol=symbol,
                            korean_name=name or symbol,
                            reason=f"매수 주문 실패: {str(exc)}",
                            market_type=self.market_type_label,
                        )
                    except Exception as notify_error:
                        logger.warning("텔레그램 알림 전송 실패: %s", notify_error)

                if is_manual:
                    logger.info(
                        "[수동잔고] %s(%s) - KIS 매도 불가, 토스 추천 알림 발송",
                        name,
                        symbol,
                    )
                    try:
                        await self.send_toss_recommendation(
                            code=symbol,
                            name=name or symbol,
                            current_price=current_price,
                            toss_quantity=qty,
                            toss_avg_price=avg_price,
                        )
                        stock_steps.append(
                            {
                                "step": "매도",
                                "result": {
                                    "success": True,
                                    "message": "수동잔고 - 토스 추천 알림 발송",
                                    "orders_placed": 0,
                                },
                            }
                        )
                    except Exception as exc:
                        logger.warning(
                            "[수동잔고] %s(%s) 토스 추천 알림 발송 실패: %s",
                            name,
                            symbol,
                            exc,
                        )
                        stock_steps.append(
                            {
                                "step": "매도",
                                "result": {
                                    "success": True,
                                    "message": "수동잔고 - 매도 스킵",
                                    "orders_placed": 0,
                                },
                            }
                        )
                    results.append(
                        self.build_result_entry(
                            name=name, symbol=symbol, steps=stock_steps
                        )
                    )
                    continue

                sell_orders_cancelled = False
                try:
                    cancel_result = await self.cancel_pending_orders(
                        kis,
                        symbol,
                        exchange_code,
                        "sell",
                        all_open_orders,
                    )
                    if cancel_result["total"] > 0:
                        logger.info(
                            "%s 미체결 매도 주문 취소: %s/%s건",
                            symbol,
                            cancel_result["cancelled"],
                            cancel_result["total"],
                        )
                        stock_steps.append(
                            {
                                "step": "매도취소",
                                "result": {"success": True, **cancel_result},
                            }
                        )
                        sell_orders_cancelled = cancel_result["cancelled"] > 0
                        await asyncio.sleep(0.5)
                except Exception as exc:
                    logger.warning("%s 미체결 매도 주문 취소 실패: %s", symbol, exc)
                    stock_steps.append(
                        {
                            "step": "매도취소",
                            "result": {"success": False, "error": str(exc)},
                        }
                    )

                if sell_orders_cancelled and self.refresh_holdings_after_sell_cancel:
                    logger.info("%s 매도 취소 후 잔고 재조회 활성화", symbol)

                try:
                    sell_result = await self.sell_handler(
                        kis,
                        symbol,
                        current_price,
                        avg_price,
                        qty,
                        exchange_code,
                    )
                    stock_steps.append({"step": "매도", "result": sell_result})
                    if (
                        sell_result.get("success")
                        and sell_result.get("orders_placed", 0) > 0
                    ):
                        try:
                            notifier = self.notifier_factory()
                            await notifier.notify_sell_order(
                                symbol=symbol,
                                korean_name=name or symbol,
                                order_count=sell_result.get("orders_placed", 0),
                                total_volume=sell_result.get("total_volume", 0),
                                prices=sell_result.get("prices", []),
                                volumes=sell_result.get("quantities", []),
                                expected_amount=sell_result.get("expected_amount", 0.0),
                                market_type=self.market_type_label,
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
                except Exception as exc:
                    stock_steps.append(
                        {
                            "step": "매도",
                            "result": {"success": False, "error": str(exc)},
                        }
                    )
                    try:
                        notifier = self.notifier_factory()
                        await notifier.notify_trade_failure(
                            symbol=symbol,
                            korean_name=name or symbol,
                            reason=f"매도 주문 실패: {str(exc)}",
                            market_type=self.market_type_label,
                        )
                    except Exception as notify_error:
                        logger.warning("텔레그램 알림 전송 실패: %s", notify_error)

                results.append(
                    self.build_result_entry(name=name, symbol=symbol, steps=stock_steps)
                )

            return {
                "status": "completed",
                "message": "종목별 자동 실행 완료",
                "results": results,
            }
        except Exception as exc:
            return {"status": "failed", "error": str(exc)}
