import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Protocol

from app.core.symbol import to_db_symbol

AutomationResult = dict[str, object]
StepResults = list[dict[str, object]]


@dataclass(slots=True)
class StockContext:
    """Per-stock context for automation workflows."""

    symbol: str
    name: str
    avg_price: float
    current_price: float
    qty: int
    is_manual: bool
    exchange_code: str | None  # None for domestic


def extract_domestic_stock_info(stock: dict[str, Any]) -> StockContext:
    return StockContext(
        symbol=stock.get("pdno", ""),
        name=stock.get("prdt_name", ""),
        avg_price=float(stock.get("pchs_avg_pric", 0)),
        current_price=float(stock.get("prpr", 0)),
        qty=int(float(stock.get("ord_psbl_qty", stock.get("hldg_qty", 0)))),
        is_manual=stock.get("_is_manual", False),
        exchange_code=None,
    )


def extract_overseas_stock_info(stock: dict[str, Any]) -> StockContext:
    return StockContext(
        symbol=stock.get("ovrs_pdno", ""),
        name=stock.get("ovrs_item_name", ""),
        avg_price=float(stock.get("pchs_avg_pric", 0)),
        current_price=float(stock.get("now_pric2", 0)),
        qty=int(float(stock.get("ord_psbl_qty", stock.get("ovrs_cblc_qty", 0)))),
        is_manual=stock.get("_is_manual", False),
        exchange_code=stock.get("ovrs_excg_cd"),  # raw, resolved later
    )


def match_domestic_stock(
    stocks: list[dict[str, Any]], symbol: str
) -> dict[str, Any] | None:
    return next((s for s in stocks if s.get("pdno") == symbol), None)


def match_overseas_stock(
    stocks: list[dict[str, Any]], symbol: str
) -> dict[str, Any] | None:
    normalized = to_db_symbol(symbol)
    return next(
        (s for s in stocks if to_db_symbol(s.get("ovrs_pdno", "")) == normalized),
        None,
    )


logger = logging.getLogger(__name__)


class SupportsMarketAutomation(Protocol):
    market: str

    async def execute(self) -> AutomationResult: ...


@dataclass(slots=True)
class BaseAutomationAdapter:
    """Common per-stock automation workflow. Market-specific behavior via hook methods."""

    # Injected dependencies
    kis_client_factory: Callable[[], Any]
    async_session_factory: Callable[[], Any]
    manual_holdings_service_factory: Callable[[Any], Any]
    manual_market_type: Any
    buy_handler: Callable[..., Awaitable[dict[str, Any]]]
    sell_handler: Callable[..., Awaitable[dict[str, Any]]]
    send_toss_recommendation: Callable[..., Awaitable[None]]
    notifier_factory: Callable[[], Any]
    no_stocks_message: str

    # Market attributes (subclass sets defaults)
    market: str = ""
    market_type_label: str = ""
    result_symbol_key: str = ""
    toss_market_type: str = ""
    toss_currency: str = ""
    refresh_holdings_after_sell_cancel: bool = False

    # --- Hook methods: subclass MUST override ---

    async def fetch_holdings(self, kis: Any) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def fetch_open_orders(self, kis: Any) -> list[dict[str, Any]]:
        raise NotImplementedError

    def extract_stock_info(self, stock: dict[str, Any]) -> StockContext:
        raise NotImplementedError

    def build_manual_entry(self, holding: Any) -> dict[str, Any]:
        raise NotImplementedError

    def is_same_symbol(self, stock: dict[str, Any], ticker: str) -> bool:
        raise NotImplementedError

    async def fetch_manual_price(self, kis: Any, symbol: str) -> float:
        raise NotImplementedError

    async def cancel_pending(
        self,
        kis: Any,
        symbol: str,
        order_type: str,
        all_open_orders: list[dict[str, Any]],
        *,
        exchange_code: str | None = None,
    ) -> dict[str, Any]:
        """Cancel pending orders matching symbol and type."""
        target_code = "02" if order_type == "buy" else "01"
        target_orders = self._filter_pending_orders(
            all_open_orders, symbol, target_code
        )
        if not target_orders:
            return {"cancelled": 0, "failed": 0, "total": 0}

        cancelled = 0
        failed = 0
        for order in target_orders:
            order_number = self._extract_order_number(order)
            if not order_number:
                logger.warning("주문번호 없음 (%s): order=%s", symbol, order)
                failed += 1
                continue
            try:
                await self._cancel_single_order(
                    kis,
                    symbol,
                    order,
                    order_number,
                    order_type,
                    exchange_code=exchange_code,
                )
                cancelled += 1
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.warning(
                    "주문 취소 실패 (%s, %s): %s",
                    symbol,
                    order_number,
                    e,
                )
                failed += 1
        return {
            "cancelled": cancelled,
            "failed": failed,
            "total": len(target_orders),
        }

    @staticmethod
    def _extract_order_number(order: dict[str, Any]) -> str | None:
        return (
            order.get("odno")
            or order.get("ODNO")
            or order.get("ord_no")
            or order.get("ORD_NO")
        )

    def _filter_pending_orders(
        self,
        orders: list[dict[str, Any]],
        symbol: str,
        target_code: str,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def _cancel_single_order(
        self,
        kis: Any,
        symbol: str,
        order: dict[str, Any],
        order_number: str,
        order_type: str,
        *,
        exchange_code: str | None = None,
    ) -> None:
        raise NotImplementedError

    # --- Hook methods: subclass MAY override (have defaults) ---

    async def resolve_exchange(self, symbol: str, stock: dict[str, Any]) -> str | None:
        return None

    async def refresh_after_buy(
        self,
        kis: Any,
        symbol: str,
        qty: int,
        avg_price: float,
        current_price: float,
    ) -> tuple[int, float, float]:
        return qty, avg_price, current_price

    async def refresh_after_sell_cancel(
        self, kis: Any, symbol: str, qty: int, current_price: float
    ) -> tuple[int, float]:
        return qty, current_price

    async def on_buy_error_result(
        self, name: str, symbol: str, result: dict[str, Any]
    ) -> None:
        pass

    async def on_trade_exception(
        self, symbol: str, name: str, exc: Exception, trade_type: str
    ) -> None:
        pass

    def analysis_target(self, *, name: str | None, symbol: str | None) -> str:
        raise NotImplementedError

    def build_result_entry(
        self, *, name: str | None, symbol: str | None, steps: StepResults
    ) -> AutomationResult:
        resolved_name = name or symbol or ""
        resolved_symbol = symbol or ""
        return {
            "name": resolved_name,
            self.result_symbol_key: resolved_symbol,
            "steps": steps,
        }

    # --- Main workflow ---

    async def _prepare_holdings(
        self, kis: Any
    ) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
        """Fetch KIS + manual holdings, merge, and fetch open orders.

        Returns (None, []) when no stocks are held.
        """
        my_stocks = await self.fetch_holdings(kis)

        async with self.async_session_factory() as db:
            manual_service = self.manual_holdings_service_factory(db)
            manual_holdings = await manual_service.get_holdings_by_user(
                user_id=1,
                market_type=self.manual_market_type,
            )

        for holding in manual_holdings:
            ticker = holding.ticker
            if any(self.is_same_symbol(stock, ticker) for stock in my_stocks):
                continue
            my_stocks.append(self.build_manual_entry(holding))

        if not my_stocks:
            return None, []

        all_open_orders = await self.fetch_open_orders(kis)
        logger.info(
            "%s 미체결 주문 조회 완료: %s건",
            self.market_type_label,
            len(all_open_orders),
        )
        return my_stocks, all_open_orders

    async def _resolve_manual_price(self, kis: Any, ctx: StockContext) -> None:
        """Fetch live price for manual holdings; fall back to avg_price on failure."""
        try:
            ctx.current_price = await self.fetch_manual_price(kis, ctx.symbol)
            logger.info(
                "[수동잔고] %s(%s) 현재가 조회: %s",
                ctx.name,
                ctx.symbol,
                ctx.current_price,
            )
        except Exception as exc:
            logger.warning(
                "[수동잔고] %s(%s) 현재가 조회 실패, 평단가 사용: %s",
                ctx.name,
                ctx.symbol,
                exc,
            )
            ctx.current_price = ctx.avg_price

    async def _execute_buy_orders(
        self,
        kis: Any,
        ctx: StockContext,
        all_open_orders: list[dict[str, Any]],
        stock_steps: StepResults,
    ) -> None:
        """Cancel pending buy orders, execute buy, notify, and refresh holdings."""
        # --- Cancel pending buy orders ---
        try:
            cancel_result = await self.cancel_pending(
                kis,
                ctx.symbol,
                "buy",
                all_open_orders,
                exchange_code=ctx.exchange_code,
            )
            if cancel_result["total"] > 0:
                logger.info(
                    "%s 미체결 매수 주문 취소: %s/%s건",
                    ctx.name or ctx.symbol,
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
            logger.warning(
                "%s 미체결 매수 주문 취소 실패: %s", ctx.name or ctx.symbol, exc
            )
            stock_steps.append(
                {
                    "step": "매수취소",
                    "result": {"success": False, "error": str(exc)},
                }
            )

        # --- Buy ---
        try:
            buy_result = await self.buy_handler(
                kis,
                ctx.symbol,
                ctx.current_price,
                ctx.avg_price,
                exchange_code=ctx.exchange_code,
            )
            stock_steps.append({"step": "매수", "result": buy_result})
            await self.on_buy_error_result(ctx.name, ctx.symbol, buy_result)
            if buy_result.get("success") and buy_result.get("orders_placed", 0) > 0:
                try:
                    notifier = self.notifier_factory()
                    await notifier.notify_buy_order(
                        symbol=ctx.symbol,
                        korean_name=ctx.name or ctx.symbol,
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
                ctx.name,
                ctx.symbol,
                error_msg,
            )
            await self.on_trade_exception(ctx.symbol, ctx.name, exc, "매수")

        # --- Refresh after buy ---
        (
            ctx.qty,
            ctx.avg_price,
            ctx.current_price,
        ) = await self.refresh_after_buy(
            kis,
            ctx.symbol,
            ctx.qty,
            ctx.avg_price,
            ctx.current_price,
        )

    async def _handle_manual_sell(
        self, kis: Any, ctx: StockContext, stock_steps: StepResults
    ) -> None:
        """Send toss recommendation for manual holdings instead of KIS sell."""
        logger.info(
            "[수동잔고] %s(%s) - KIS 매도 불가, 토스 추천 알림 발송",
            ctx.name,
            ctx.symbol,
        )
        try:
            await self.send_toss_recommendation(
                code=ctx.symbol,
                name=ctx.name,
                current_price=ctx.current_price,
                toss_quantity=ctx.qty,
                toss_avg_price=ctx.avg_price,
                market_type=self.toss_market_type,
                currency=self.toss_currency,
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
                ctx.name,
                ctx.symbol,
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

    async def _execute_sell_orders(
        self,
        kis: Any,
        ctx: StockContext,
        all_open_orders: list[dict[str, Any]],
        stock_steps: StepResults,
    ) -> None:
        """Cancel pending sell orders, refresh, and execute sell.

        For manual holdings, delegates to toss recommendation instead.
        """
        if ctx.is_manual:
            await self._handle_manual_sell(kis, ctx, stock_steps)
            return

        # --- Cancel pending sell orders ---
        sell_orders_cancelled = False
        try:
            cancel_result = await self.cancel_pending(
                kis,
                ctx.symbol,
                "sell",
                all_open_orders,
                exchange_code=ctx.exchange_code,
            )
            if cancel_result["total"] > 0:
                logger.info(
                    "%s 미체결 매도 주문 취소: %s/%s건",
                    ctx.name or ctx.symbol,
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
            logger.warning(
                "%s 미체결 매도 주문 취소 실패: %s", ctx.name or ctx.symbol, exc
            )
            stock_steps.append(
                {
                    "step": "매도취소",
                    "result": {"success": False, "error": str(exc)},
                }
            )

        # --- Refresh after sell cancel ---
        if sell_orders_cancelled and self.refresh_holdings_after_sell_cancel:
            ctx.qty, ctx.current_price = await self.refresh_after_sell_cancel(
                kis,
                ctx.symbol,
                ctx.qty,
                ctx.current_price,
            )

        # --- Sell ---
        try:
            sell_result = await self.sell_handler(
                kis,
                ctx.symbol,
                ctx.current_price,
                ctx.avg_price,
                ctx.qty,
                exchange_code=ctx.exchange_code,
            )
            stock_steps.append({"step": "매도", "result": sell_result})
            if sell_result.get("success") and sell_result.get("orders_placed", 0) > 0:
                try:
                    notifier = self.notifier_factory()
                    await notifier.notify_sell_order(
                        symbol=ctx.symbol,
                        korean_name=ctx.name or ctx.symbol,
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
                ctx.name,
                ctx.symbol,
                error_msg,
            )
            await self.on_trade_exception(ctx.symbol, ctx.name, exc, "매도")

    def _aggregate_results(self, results: list[AutomationResult]) -> AutomationResult:
        return {
            "status": "completed",
            "message": "종목별 자동 실행 완료",
            "results": results,
        }

    async def execute(self) -> AutomationResult:
        """Unified per-stock automation: cancel -> buy -> refresh -> sell."""
        kis = self.kis_client_factory()

        try:
            my_stocks, all_open_orders = await self._prepare_holdings(kis)
            if my_stocks is None:
                return {
                    "status": "completed",
                    "message": self.no_stocks_message,
                    "results": [],
                }

            results: list[AutomationResult] = []

            for stock in my_stocks:
                ctx = self.extract_stock_info(stock)
                ctx.exchange_code = await self.resolve_exchange(ctx.symbol, stock)

                if ctx.is_manual:
                    await self._resolve_manual_price(kis, ctx)

                stock_steps: StepResults = [
                    {
                        "step": "분석",
                        "result": {
                            "success": True,
                            "message": "분석 스킵 (대체 분석기 준비 중)",
                        },
                    },
                ]

                await self._execute_buy_orders(kis, ctx, all_open_orders, stock_steps)
                await self._execute_sell_orders(kis, ctx, all_open_orders, stock_steps)

                results.append(
                    self.build_result_entry(
                        name=ctx.name,
                        symbol=ctx.symbol,
                        steps=stock_steps,
                    )
                )

            return self._aggregate_results(results)
        except Exception as exc:
            logger.error(
                "[태스크 실패] %s: %s",
                self.market_type_label,
                exc,
                exc_info=True,
            )
            return {"status": "failed", "error": str(exc)}


@dataclass(slots=True)
class DomesticAutomationAdapter(BaseAutomationAdapter):
    market: str = "domestic"
    market_type_label: str = "국내주식"
    result_symbol_key: str = "code"
    toss_market_type: str = "kr"
    toss_currency: str = "원"
    refresh_holdings_after_sell_cancel: bool = True

    async def fetch_holdings(self, kis):
        return await kis.fetch_my_stocks()

    async def fetch_open_orders(self, kis):
        return await kis.inquire_korea_orders(is_mock=False)

    def extract_stock_info(self, stock):
        return extract_domestic_stock_info(stock)

    def build_manual_entry(self, holding):
        qty_str = str(holding.quantity)
        return {
            "pdno": holding.ticker,
            "prdt_name": holding.display_name or holding.ticker,
            "hldg_qty": qty_str,
            "ord_psbl_qty": qty_str,
            "pchs_avg_pric": str(holding.avg_price),
            "prpr": str(holding.avg_price),
            "_is_manual": True,
        }

    def is_same_symbol(self, stock, ticker):
        return stock.get("pdno") == ticker

    async def fetch_manual_price(self, kis, symbol):
        info = await kis.fetch_fundamental_info(symbol)
        return float(info.get("현재가", 0))

    def _filter_pending_orders(self, orders, symbol, target_code):
        return [
            order
            for order in orders
            if (order.get("pdno") or order.get("PDNO")) == symbol
            and (order.get("sll_buy_dvsn_cd") or order.get("SLL_BUY_DVSN_CD"))
            == target_code
        ]

    async def _cancel_single_order(
        self, kis, symbol, order, order_number, order_type, *, exchange_code=None
    ):
        order_qty = int(order.get("ord_qty") or order.get("ORD_QTY") or 0)
        order_price = int(float(order.get("ord_unpr") or order.get("ORD_UNPR") or 0))
        order_orgno = (
            order.get("ord_gno_brno")
            or order.get("ORD_GNO_BRNO")
            or order.get("krx_fwdg_ord_orgno")
            or order.get("KRX_FWDG_ORD_ORGNO")
        )
        await kis.cancel_korea_order(
            order_number=order_number,
            stock_code=symbol,
            quantity=order_qty,
            price=order_price,
            order_type=order_type,
            is_mock=False,
            krx_fwdg_ord_orgno=str(order_orgno).strip() if order_orgno else None,
        )

    def analysis_target(self, *, name=None, symbol=None):
        return name or symbol or ""

    async def refresh_after_buy(self, kis, symbol, qty, avg_price, current_price):
        try:
            latest = await kis.fetch_my_stocks()
            target = next((s for s in latest if s.get("pdno") == symbol), None)
            if target:
                return (
                    int(target.get("ord_psbl_qty", target.get("hldg_qty", qty))),
                    float(target.get("pchs_avg_pric", avg_price)),
                    float(target.get("prpr", current_price)),
                )
        except Exception:
            pass
        return qty, avg_price, current_price

    async def refresh_after_sell_cancel(self, kis, symbol, qty, current_price):
        try:
            latest = await kis.fetch_my_stocks()
            target = next((s for s in latest if s.get("pdno") == symbol), None)
            if target:
                return (
                    int(target.get("ord_psbl_qty", target.get("hldg_qty", qty))),
                    float(target.get("prpr", current_price)),
                )
        except Exception:
            pass
        return qty, current_price

    async def on_buy_error_result(self, name, symbol, result):
        if result.get("error"):
            logger.error(
                "[매수 에러] %s(%s): %s",
                name,
                symbol,
                result["error"],
                extra={"task": "kis.run_per_domestic_stock_automation"},
            )


@dataclass(slots=True)
class OverseasAutomationAdapter(BaseAutomationAdapter):
    market: str = "overseas"
    market_type_label: str = "해외주식"
    result_symbol_key: str = "symbol"
    toss_market_type: str = "us"
    toss_currency: str = "$"
    refresh_holdings_after_sell_cancel: bool = False

    async def fetch_holdings(self, kis):
        return await kis.fetch_my_overseas_stocks()

    async def fetch_open_orders(self, kis):
        orders_by_id: dict[str, dict] = {}
        anonymous: list[dict] = []
        for exchange in ("NASD", "NYSE", "AMEX"):
            try:
                open_orders = await kis.inquire_overseas_orders(
                    exchange_code=exchange,
                    is_mock=False,
                )
            except Exception as exc:
                logger.warning("미체결 주문 조회 실패 (exchange=%s): %s", exchange, exc)
                continue
            for order in open_orders:
                oid = self._extract_order_id(order)
                if oid:
                    orders_by_id[oid] = order
                else:
                    anonymous.append(order)
        return list(orders_by_id.values()) + anonymous

    @staticmethod
    def _extract_order_id(order: dict) -> str:
        for key in ("odno", "ODNO", "ord_no", "ORD_NO"):
            if v := order.get(key):
                return str(v).strip()
        return ""

    def extract_stock_info(self, stock):
        return extract_overseas_stock_info(stock)

    def build_manual_entry(self, holding):
        qty_str = str(holding.quantity)
        return {
            "ovrs_pdno": holding.ticker,
            "ovrs_item_name": holding.display_name or holding.ticker,
            "ovrs_cblc_qty": qty_str,
            "ord_psbl_qty": qty_str,
            "pchs_avg_pric": str(holding.avg_price),
            "now_pric2": "0",
            "_is_manual": True,
        }

    def is_same_symbol(self, stock, ticker):
        return to_db_symbol(stock.get("ovrs_pdno", "")) == to_db_symbol(ticker)

    async def fetch_manual_price(self, kis, symbol):
        df = await kis.inquire_overseas_price(symbol)
        if not df.empty:
            return float(df.iloc[0]["close"])
        return 0.0

    async def resolve_exchange(self, symbol, stock):
        from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

        preferred = stock.get("ovrs_excg_cd") if isinstance(stock, dict) else None
        normalized = str(preferred or "").strip().upper()
        if normalized:
            return normalized
        return await get_us_exchange_by_symbol(symbol)

    def _filter_pending_orders(self, orders, symbol, target_code):
        normalized_symbol = to_db_symbol(symbol)
        return [
            order
            for order in orders
            if to_db_symbol(order.get("pdno") or order.get("PDNO") or "")
            == normalized_symbol
            and (order.get("sll_buy_dvsn_cd") or order.get("SLL_BUY_DVSN_CD"))
            == target_code
        ]

    async def _cancel_single_order(
        self, kis, symbol, order, order_number, order_type, *, exchange_code=None
    ):
        order_qty = int(order.get("ft_ord_qty") or order.get("FT_ORD_QTY") or 0)
        await kis.cancel_overseas_order(
            order_number=order_number,
            symbol=symbol,
            exchange_code=exchange_code or "NASD",
            quantity=order_qty,
            is_mock=False,
        )

    def analysis_target(self, *, name=None, symbol=None):
        return symbol or name or ""

    async def on_trade_exception(self, symbol, name, exc, trade_type):
        try:
            notifier = self.notifier_factory()
            await notifier.notify_trade_failure(
                symbol=symbol,
                korean_name=name or symbol,
                reason=f"{trade_type} 주문 실패: {exc}",
                market_type=self.market_type_label,
            )
        except Exception as notify_error:
            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
