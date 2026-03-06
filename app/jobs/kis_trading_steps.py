"""
Trading step base class and concrete step implementations for KIS automation.

This module defines the TradingStep abstract base class that all trading
automation steps must implement. Each step represents a discrete action
in the trading automation workflow (analyze, buy, sell, etc.).

Steps are executed sequentially by the TradingOrchestrator, with explicit
failure policies and skip conditions.
"""

import logging
from abc import ABC, abstractmethod
from typing import Any

from app.jobs.kis_trading_types import (
    FailurePolicy,
    SkipCondition,
    StepOutcome,
    StepResult,
    TradingContext,
)

logger = logging.getLogger(__name__)


class TradingStep(ABC):
    """
    Abstract base class for trading automation steps.

    Each step represents a discrete action in the trading automation workflow.
    Steps are executed sequentially by the TradingOrchestrator.

    Subclasses must implement:
    - name: Property returning the step name
    - execute: Async method performing the step action

    Subclasses can optionally override:
    - failure_policy: What to do when this step fails (default: CONTINUE)
    - skip_conditions: List of conditions under which to skip this step
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """
        Return the name of this step.

        Used for logging and result aggregation.
        Example: "analyze", "buy", "sell"
        """
        ...

    @property
    def failure_policy(self) -> FailurePolicy:
        """
        What to do when this step fails.

        Default is CONTINUE (log error, continue to next step).
        Override this property to change the behavior.

        Returns:
            FailurePolicy enum value
        """
        return FailurePolicy.CONTINUE

    @property
    def skip_conditions(self) -> list[SkipCondition]:
        """
        Conditions under which this step should be skipped.

        Each condition is a callable that takes TradingContext and returns
        True if the step should be skipped.

        Returns:
            List of skip condition callables
        """
        return []

    @abstractmethod
    async def execute(self, context: TradingContext) -> StepOutcome:
        """
        Execute this step for the given stock context.

        This method must be implemented by subclasses to perform the
        actual step action.

        Args:
            context: TradingContext containing stock info, orders, and services

        Returns:
            StepOutcome indicating success/failure/skip and any data
        """
        ...

    def should_skip(self, context: TradingContext) -> bool:
        """
        Check if this step should be skipped for the given context.

        Args:
            context: TradingContext for the current stock

        Returns:
            True if any skip condition is met, False otherwise
        """
        return any(condition(context) for condition in self.skip_conditions)

    def _log_start(self, context: TradingContext) -> None:
        """Log the start of step execution."""
        logger.info(
            "[Step:%s] Starting for %s (%s)",
            self.name,
            context.name,
            context.symbol,
        )

    def _log_success(self, context: TradingContext, message: str = "완료") -> None:
        """Log successful step completion."""
        logger.info(
            "[Step:%s] Success for %s (%s): %s",
            self.name,
            context.name,
            context.symbol,
            message,
        )

    def _log_failure(
        self, context: TradingContext, error: Exception, message: str = ""
    ) -> None:
        """Log step failure."""
        logger.error(
            "[Step:%s] Failed for %s (%s): %s%s",
            self.name,
            context.name,
            context.symbol,
            f"{message}: " if message else "",
            error,
            exc_info=error,
        )

    def _log_skip(self, context: TradingContext, reason: str) -> None:
        """Log step skip."""
        logger.info(
            "[Step:%s] Skipped for %s (%s): %s",
            self.name,
            context.name,
            context.symbol,
            reason,
        )

    @staticmethod
    def _success(message: str, data: dict[str, Any] | None = None) -> StepOutcome:
        """Helper to create a success outcome."""
        return StepOutcome(
            result=StepResult.SUCCESS,
            message=message,
            data=data,
            should_continue=True,
        )

    @staticmethod
    def _failure(
        message: str,
        data: dict[str, Any] | None = None,
        should_continue: bool = True,
    ) -> StepOutcome:
        """Helper to create a failure outcome."""
        return StepOutcome(
            result=StepResult.FAILURE,
            message=message,
            data=data,
            should_continue=should_continue,
        )

    @staticmethod
    def _skip(message: str, data: dict[str, Any] | None = None) -> StepOutcome:
        """Helper to create a skip outcome."""
        return StepOutcome(
            result=StepResult.SKIP,
            message=message,
            data=data,
            should_continue=True,
        )


class AnalyzeStep(TradingStep):
    """
    Step that performs AI analysis on a stock.

    Uses KISAnalyzer to analyze the stock and stores the result in context.
    If analysis fails, processing for this stock is stopped (STOP_STOCK policy).
    """

    @property
    def name(self) -> str:
        return "analyze"

    @property
    def failure_policy(self) -> FailurePolicy:
        """Analysis failure stops processing for this stock."""
        return FailurePolicy.STOP_STOCK

    async def execute(self, context: TradingContext) -> StepOutcome:
        """Execute AI analysis for the stock."""
        self._log_start(context)

        # Skip manual holdings (토스 등) - they don't need AI analysis
        if context.is_manual:
            self._log_skip(context, "수동 잔고는 분석 스킵")
            return self._skip("수동 잔고 종목")

        if not context.name:
            self._log_skip(context, "종목명 없음")
            return self._skip("종목명을 찾을 수 없음")

        try:
            from app.analysis.service_analyzers import KISAnalyzer

            analyzer = KISAnalyzer()
            result, _ = await analyzer.analyze_stock_json(context.name)

            if result is None:
                self._log_failure(context, Exception("분석 결과 없음"))
                return self._failure(
                    "분석 결과를 가져올 수 없습니다.",
                    should_continue=False,  # STOP_STOCK
                )

            # Store analysis result in context for later steps
            analysis_data: dict[str, Any] = {}

            if hasattr(result, "decision"):
                analysis_data["decision"] = result.decision
                analysis_data["confidence"] = (
                    float(result.confidence) if result.confidence else 0.0
                )
                analysis_data["reasons"] = (
                    list(result.reasons)
                    if hasattr(result, "reasons") and result.reasons
                    else []
                )

                # Price ranges
                for attr in (
                    "appropriate_buy_min",
                    "appropriate_buy_max",
                    "appropriate_sell_min",
                    "appropriate_sell_max",
                    "buy_hope_min",
                    "buy_hope_max",
                    "sell_target_min",
                    "sell_target_max",
                ):
                    if hasattr(result, attr):
                        analysis_data[attr] = getattr(result, attr)

                # Store in context
                context.analysis_result = analysis_data

                decision = result.decision
                confidence = analysis_data.get("confidence", 0)
                self._log_success(
                    context, f"결정: {decision}, 신뢰도: {confidence}%"
                )
                return self._success(
                    f"분석 완료: {decision} ({confidence}%)",
                    data=analysis_data,
                )
            else:
                # Fallback: text result (no structured decision)
                analysis_data["raw_result"] = str(result)
                context.analysis_result = analysis_data
                self._log_success(context, "분석 완료 (텍스트 응답)")
                return self._success("분석 완료 (텍스트 응답)", data=analysis_data)

        except Exception as e:
            self._log_failure(context, e)
            return self._failure(
                f"분석 실패: {e}",
                should_continue=False,  # STOP_STOCK
            )


class CancelBuyOrdersStep(TradingStep):
    """
    Step that cancels pending buy orders for a stock.

    Cancels all pending buy orders before placing new buy orders.
    Uses CONTINUE failure policy - cancellation failures don't stop processing.
    """

    @property
    def name(self) -> str:
        return "cancel_buy_orders"

    @property
    def failure_policy(self) -> FailurePolicy:
        """Cancellation failures should not stop processing."""
        return FailurePolicy.CONTINUE

    async def execute(self, context: TradingContext) -> StepOutcome:
        """Cancel all pending buy orders for the stock."""
        self._log_start(context)

        if not context.open_orders:
            self._log_skip(context, "미체결 주문 없음")
            return self._skip("미체결 주문 없음")

        # Determine if domestic or overseas based on exchange_code
        is_domestic = not context.exchange_code

        # Filter buy orders (sll_buy_dvsn_cd: 01=매도, 02=매수)
        target_code = "02"  # buy
        target_orders = []

        for order in context.open_orders:
            dvsn_code = (
                order.get("sll_buy_dvsn_cd")
                or order.get("SLL_BUY_DVSN_CD")
                or order.get("ord_dvsn_cd")
                or order.get("ORD_DVSN_CD")
            )
            if not dvsn_code:
                continue

            # For domestic orders
            if is_domestic:
                stock_code = order.get("pdno") or order.get("PDNO")
                if stock_code == context.symbol and str(dvsn_code) == target_code:
                    target_orders.append(order)
            # For overseas orders
            else:
                order_symbol = order.get("ovrs_pdno") or order.get("OVRS_PDNO")
                # Normalize symbol for comparison (handle format differences)
                from app.core.symbol import to_db_symbol

                normalized_order_symbol = to_db_symbol(order_symbol or "")
                if (
                    normalized_order_symbol == context.symbol
                    and str(dvsn_code) == target_code
                ):
                    target_orders.append(order)

        if not target_orders:
            self._log_skip(context, "미체결 매수 주문 없음")
            return self._skip("미체결 매수 주문 없음")

        cancelled = 0
        failed = 0

        for order in target_orders:
            try:
                if is_domestic:
                    await self._cancel_domestic_order(context, order)
                else:
                    await self._cancel_overseas_order(context, order)
                cancelled += 1
            except Exception as e:
                logger.warning(
                    "[Step:%s] 주문 취소 실패 (%s): %s",
                    self.name,
                    context.symbol,
                    e,
                )
                failed += 1

        result_msg = f"{cancelled}/{len(target_orders)}건 취소 완료"
        if failed > 0:
            result_msg += f" ({failed}건 실패)"

        self._log_success(context, result_msg)
        return self._success(
            result_msg,
            data={"cancelled": cancelled, "failed": failed, "total": len(target_orders)},
        )

    async def _cancel_domestic_order(
        self, context: TradingContext, order: dict
    ) -> None:
        """Cancel a domestic buy order."""
        order_number = (
            order.get("odno")
            or order.get("ODNO")
            or order.get("orgn_odno")
            or order.get("ORGN_ODNO")
        )
        order_qty = int(
            float(order.get("ft_ord_qty") or order.get("FT_ORD_QTY") or 0)
        )
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
            raise ValueError("주문번호 없음")

        await context.kis.cancel_korea_order(
            order_number=order_number,
            stock_code=context.symbol,
            quantity=order_qty,
            price=order_price,
            order_type="buy",
            is_mock=False,
            krx_fwdg_ord_orgno=str(order_orgno).strip() if order_orgno else None,
        )

    async def _cancel_overseas_order(
        self, context: TradingContext, order: dict
    ) -> None:
        """Cancel an overseas buy order."""
        order_number = (
            order.get("odno")
            or order.get("ODNO")
            or order.get("ord_no")
            or order.get("ORD_NO")
        )
        order_qty = int(
            float(order.get("ft_ord_qty") or order.get("FT_ORD_QTY") or 0)
        )

        if not order_number:
            raise ValueError("주문번호 없음")

        await context.kis.cancel_overseas_order(
            order_number=order_number,
            symbol=context.symbol,
            exchange_code=context.exchange_code,
            quantity=order_qty,
            is_mock=False,
        )


class CancelSellOrdersStep(TradingStep):
    """
    Step that cancels pending sell orders for a stock.

    Cancels all pending sell orders before placing new sell orders.
    Uses CONTINUE failure policy - cancellation failures don't stop processing.
    """

    @property
    def name(self) -> str:
        return "cancel_sell_orders"

    @property
    def failure_policy(self) -> FailurePolicy:
        """Cancellation failures should not stop processing."""
        return FailurePolicy.CONTINUE

    async def execute(self, context: TradingContext) -> StepOutcome:
        """Cancel all pending sell orders for the stock."""
        self._log_start(context)

        if not context.open_orders:
            self._log_skip(context, "미체결 주문 없음")
            return self._skip("미체결 주문 없음")

        # Determine if domestic or overseas based on exchange_code
        is_domestic = not context.exchange_code

        # Filter sell orders (sll_buy_dvsn_cd: 01=매도, 02=매수)
        target_code = "01"  # sell
        target_orders = []

        for order in context.open_orders:
            dvsn_code = (
                order.get("sll_buy_dvsn_cd")
                or order.get("SLL_BUY_DVSN_CD")
                or order.get("ord_dvsn_cd")
                or order.get("ORD_DVSN_CD")
            )
            if not dvsn_code:
                continue

            # For domestic orders
            if is_domestic:
                stock_code = order.get("pdno") or order.get("PDNO")
                if stock_code == context.symbol and str(dvsn_code) == target_code:
                    target_orders.append(order)
            # For overseas orders
            else:
                order_symbol = order.get("ovrs_pdno") or order.get("OVRS_PDNO")
                # Normalize symbol for comparison (handle format differences)
                from app.core.symbol import to_db_symbol

                normalized_order_symbol = to_db_symbol(order_symbol or "")
                if (
                    normalized_order_symbol == context.symbol
                    and str(dvsn_code) == target_code
                ):
                    target_orders.append(order)

        if not target_orders:
            self._log_skip(context, "미체결 매도 주문 없음")
            return self._skip("미체결 매도 주문 없음")

        cancelled = 0
        failed = 0

        for order in target_orders:
            try:
                if is_domestic:
                    await self._cancel_domestic_order(context, order)
                else:
                    await self._cancel_overseas_order(context, order)
                cancelled += 1
            except Exception as e:
                logger.warning(
                    "[Step:%s] 주문 취소 실패 (%s): %s",
                    self.name,
                    context.symbol,
                    e,
                )
                failed += 1

        result_msg = f"{cancelled}/{len(target_orders)}건 취소 완료"
        if failed > 0:
            result_msg += f" ({failed}건 실패)"

        self._log_success(context, result_msg)
        return self._success(
            result_msg,
            data={"cancelled": cancelled, "failed": failed, "total": len(target_orders)},
        )

    async def _cancel_domestic_order(
        self, context: TradingContext, order: dict
    ) -> None:
        """Cancel a domestic sell order."""
        order_number = (
            order.get("odno")
            or order.get("ODNO")
            or order.get("orgn_odno")
            or order.get("ORGN_ODNO")
        )
        order_qty = int(
            float(order.get("ft_ord_qty") or order.get("FT_ORD_QTY") or 0)
        )
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
            raise ValueError("주문번호 없음")

        await context.kis.cancel_korea_order(
            order_number=order_number,
            stock_code=context.symbol,
            quantity=order_qty,
            price=order_price,
            order_type="sell",
            is_mock=False,
            krx_fwdg_ord_orgno=str(order_orgno).strip() if order_orgno else None,
        )

    async def _cancel_overseas_order(
        self, context: TradingContext, order: dict
    ) -> None:
        """Cancel an overseas sell order."""
        order_number = (
            order.get("odno")
            or order.get("ODNO")
            or order.get("ord_no")
            or order.get("ORD_NO")
        )
        order_qty = int(
            float(order.get("ft_ord_qty") or order.get("FT_ORD_QTY") or 0)
        )

        if not order_number:
            raise ValueError("주문번호 없음")

        await context.kis.cancel_overseas_order(
            order_number=order_number,
            symbol=context.symbol,
            exchange_code=context.exchange_code,
            quantity=order_qty,
            is_mock=False,
        )


class BuyStep(TradingStep):
    """
    Step that executes buy orders based on AI analysis.

    Uses the trading service to place buy orders according to analysis
    results and trade settings. Sends notifications on success or failure.
    Uses CONTINUE failure policy - buy failures don't stop processing.
    """

    @property
    def name(self) -> str:
        return "buy"

    @property
    def failure_policy(self) -> FailurePolicy:
        """Buy failures should not stop processing."""
        return FailurePolicy.CONTINUE

    async def execute(self, context: TradingContext) -> StepOutcome:
        """Execute buy orders for the stock."""
        self._log_start(context)

        # Skip manual holdings (토스 등) - they don't need KIS trading
        if context.is_manual:
            self._log_skip(context, "수동 잔고는 매수 스킵")
            return self._skip("수동 잔고 종목")

        # Determine if domestic or overseas based on exchange_code
        is_domestic = not context.exchange_code

        try:
            if is_domestic:
                result = await self._execute_domestic_buy(context)
            else:
                result = await self._execute_overseas_buy(context)

            # Handle notification
            await self._send_notification(context, result, is_domestic)

            if result.get("success"):
                orders_placed = result.get("orders_placed", 0)
                if orders_placed > 0:
                    self._log_success(
                        context,
                        f"{orders_placed}건 매수 주문 완료",
                    )
                    return self._success(
                        f"매수 주문 {orders_placed}건 성공",
                        data=result,
                    )
                else:
                    self._log_skip(context, result.get("message", "매수 조건 미충족"))
                    return self._skip(result.get("message", "매수 조건 미충족"))
            else:
                self._log_failure(
                    context,
                    Exception(result.get("message", "매수 실패")),
                )
                return self._failure(result.get("message", "매수 실패"), data=result)

        except Exception as e:
            self._log_failure(context, e, "매수 주문 실패")
            # Send failure notification
            try:
                from app.monitoring.trade_notifier import get_trade_notifier

                notifier = get_trade_notifier()
                await notifier.notify_trade_failure(
                    symbol=context.symbol,
                    korean_name=context.name or context.symbol,
                    trade_type="매수",
                    error_message=str(e),
                    market_type="국내주식" if is_domestic else "해외주식",
                )
            except Exception as notify_error:
                logger.warning("매수 실패 알림 전송 실패: %s", notify_error)

            return self._failure(f"매수 주문 실패: {e}")

    async def _execute_domestic_buy(
        self, context: TradingContext
    ) -> dict[str, Any]:
        """Execute domestic buy orders."""
        from app.services.kis_trading_service import (
            process_kis_domestic_buy_orders_with_analysis,
        )

        return await process_kis_domestic_buy_orders_with_analysis(
            kis_client=context.kis,
            symbol=context.symbol,
            current_price=context.current_price,
            avg_buy_price=context.avg_price,
        )

    async def _execute_overseas_buy(
        self, context: TradingContext
    ) -> dict[str, Any]:
        """Execute overseas buy orders."""
        from app.services.kis_trading_service import (
            process_kis_overseas_buy_orders_with_analysis,
        )

        return await process_kis_overseas_buy_orders_with_analysis(
            kis_client=context.kis,
            symbol=context.symbol,
            current_price=context.current_price,
            avg_buy_price=context.avg_price,
            exchange_code=context.exchange_code,
        )

    async def _send_notification(
        self,
        context: TradingContext,
        result: dict[str, Any],
        is_domestic: bool,
    ) -> None:
        """Send buy notification on success."""
        if not result.get("success") or result.get("orders_placed", 0) <= 0:
            return

        try:
            from app.monitoring.trade_notifier import get_trade_notifier

            notifier = get_trade_notifier()
            await notifier.notify_buy_order(
                symbol=context.symbol,
                korean_name=context.name or context.symbol,
                order_count=result.get("orders_placed", 0),
                total_amount=result.get("total_amount", 0.0),
                prices=result.get("prices", []),
                market_type="국내주식" if is_domestic else "해외주식",
            )
        except Exception as notify_error:
            logger.warning("매수 성공 알림 전송 실패: %s", notify_error)


class RefreshStep(TradingStep):
    """
    Step that refreshes holdings data after buy orders.

    Fetches the latest holdings from KIS API to update quantity, average price,
    and current price in the context. This ensures subsequent steps (like sell)
    use up-to-date data.

    Uses CONTINUE failure policy - refresh failures don't stop processing,
    and existing values are kept on error.
    """

    @property
    def name(self) -> str:
        return "refresh"

    @property
    def failure_policy(self) -> FailurePolicy:
        """Refresh failures should not stop processing."""
        return FailurePolicy.CONTINUE

    async def execute(self, context: TradingContext) -> StepOutcome:
        """Refresh holdings data for the stock."""
        self._log_start(context)

        # Skip manual holdings (토스 등) - they don't need KIS refresh
        if context.is_manual:
            self._log_skip(context, "수동 잔고는 리프레시 스킵")
            return self._skip("수동 잔고 종목")

        # Determine if domestic or overseas based on exchange_code
        is_domestic = not context.exchange_code

        try:
            if is_domestic:
                refreshed_data = await self._refresh_domestic_holdings(context)
            else:
                refreshed_data = await self._refresh_overseas_holdings(context)

            if refreshed_data:
                # Update context with refreshed values
                context.quantity = refreshed_data.get("quantity", context.quantity)
                context.avg_price = refreshed_data.get("avg_price", context.avg_price)
                context.current_price = refreshed_data.get(
                    "current_price", context.current_price
                )

                self._log_success(
                    context,
                    f"수량: {context.quantity}, 평단가: {context.avg_price}, "
                    f"현재가: {context.current_price}",
                )
                return self._success(
                    "잔고 정보 갱신 완료",
                    data=refreshed_data,
                )
            else:
                self._log_skip(context, "갱신된 잔고 정보 없음")
                return self._skip("갱신된 잔고 정보 없음")

        except Exception as e:
            # On failure, keep existing values and continue
            self._log_failure(context, e, "잔고 재조회 실패")
            logger.warning(
                "[Step:%s] 잔고 재조회 실패 - 기존 값 사용 (%s)",
                self.name,
                e,
            )
            return self._success(
                "잔고 재조회 실패, 기존 값 사용",
                data={
                    "quantity": context.quantity,
                    "avg_price": context.avg_price,
                    "current_price": context.current_price,
                    "refresh_failed": True,
                    "error": str(e),
                },
            )

    async def _refresh_domestic_holdings(
        self, context: TradingContext
    ) -> dict[str, Any] | None:
        """Refresh domestic holdings from KIS API."""
        latest_holdings = await context.kis.fetch_my_stocks()

        # Find the stock in the refreshed holdings
        latest = next(
            (s for s in latest_holdings if s.get("pdno") == context.symbol),
            None,
        )

        if not latest:
            return None

        # Use ord_psbl_qty (orderable quantity) for sell, fallback to hldg_qty
        quantity = int(
            float(
                latest.get("ord_psbl_qty")
                or latest.get("hldg_qty")
                or context.quantity
            )
        )
        avg_price = float(latest.get("pchs_avg_pric") or context.avg_price)
        current_price = float(latest.get("prpr") or context.current_price)

        return {
            "quantity": quantity,
            "avg_price": avg_price,
            "current_price": current_price,
        }

    async def _refresh_overseas_holdings(
        self, context: TradingContext
    ) -> dict[str, Any] | None:
        """Refresh overseas holdings from KIS API."""
        latest_holdings = await context.kis.fetch_my_overseas_stocks()

        # Find the stock in the refreshed holdings (normalize symbol for comparison)
        from app.core.symbol import to_db_symbol

        latest = next(
            (
                s
                for s in latest_holdings
                if to_db_symbol(s.get("ovrs_pdno", "")) == context.symbol
            ),
            None,
        )

        if not latest:
            return None

        # Use ord_psbl_qty (orderable quantity) for sell, fallback to ovrs_cblc_qty
        quantity = int(
            float(
                latest.get("ord_psbl_qty")
                or latest.get("ovrs_cblc_qty")
                or context.quantity
            )
        )
        avg_price = float(latest.get("pchs_avg_pric") or context.avg_price)
        # now_pric2 is the current price field for overseas stocks
        current_price = float(latest.get("now_pric2") or context.current_price)

        return {
            "quantity": quantity,
            "avg_price": avg_price,
            "current_price": current_price,
        }
