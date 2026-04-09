"""KIS Trading Service - Order execution with analysis.

This module provides functions to process buy/sell orders for domestic
and overseas stocks based on analysis results.

Stage 1 Refactoring (2026-03):
- Added exception handling to all process_* functions
- Internal logic moved to _impl functions
- Errors are absorbed and returned as structured payloads
- Public contracts (return type dict[str, Any]) unchanged
"""

import asyncio
import logging
from dataclasses import dataclass
from typing import Any, Protocol, cast

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.services.brokers.kis.client import KISClient
from app.services.kis_trading_contracts import (
    OrderStepResult,
    _map_exception_to_result,
)

logger = logging.getLogger(__name__)

# KIS 매수 설정 (주식은 설정이 없으면 매수하지 않음)
KIS_MIN_BALANCE = 100000  # 최소 예수금

# 메시지 상수
MSG_NO_SETTINGS = "종목 설정 없음 - 매수 건너뜀"


def _coerce_optional_float(value: object) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def _coerce_positive_int(value: object, *, default: int = 0) -> int:
    if isinstance(value, bool):
        return default
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return default


def _open_async_session() -> AsyncSession:
    from app.core.db import AsyncSessionLocal

    session = cast(object, AsyncSessionLocal())
    return cast(AsyncSession, session)


def _present_prices(*values: object) -> list[float]:
    prices: list[float] = []
    for value in values:
        coerced = _coerce_optional_float(value)
        if coerced is not None:
            prices.append(coerced)
    return prices


class SupportsOrderExecution(Protocol):
    """Market-specific order execution abstraction."""

    market: str

    async def place_order(
        self,
        kis: KISClient,
        symbol: str,
        order_type: str,
        quantity: int,
        price: float,
        *,
        exchange_code: str | None = None,
    ) -> dict[str, Any]: ...

    async def adjust_sell_qty(
        self, kis: KISClient, symbol: str, balance_qty: int
    ) -> int: ...

    def resolve_exchange_code(
        self, settings: Any, fallback: str | None
    ) -> str | None: ...


@dataclass(frozen=True, slots=True)
class DomesticOrderOps:
    market: str = "domestic"

    async def place_order(
        self, kis, symbol, order_type, quantity, price, *, exchange_code=None
    ):
        return await kis.order_korea_stock(
            stock_code=symbol,
            order_type=order_type,
            quantity=quantity,
            price=int(price),
        )

    async def adjust_sell_qty(self, kis, symbol, balance_qty):
        return balance_qty

    def resolve_exchange_code(self, settings, fallback):
        return None


@dataclass(frozen=True, slots=True)
class OverseasOrderOps:
    market: str = "overseas"

    async def place_order(
        self, kis, symbol, order_type, quantity, price, *, exchange_code=None
    ):
        return await kis.order_overseas_stock(
            symbol=symbol,
            exchange_code=exchange_code,
            order_type=order_type,
            quantity=quantity,
            price=price,
        )

    async def adjust_sell_qty(self, kis, symbol, balance_qty):
        my_stocks = await kis.fetch_my_overseas_stocks()
        normalized = to_db_symbol(symbol)
        target = next(
            (
                s
                for s in my_stocks
                if to_db_symbol(s.get("ovrs_pdno", "")) == normalized
            ),
            None,
        )
        if target:
            actual = _coerce_positive_int(
                target.get("ord_psbl_qty", target.get("ovrs_cblc_qty", 0))
            )
            if actual < balance_qty:
                logger.info(
                    "[%s] 주문가능수량 조정: %s -> %s (KIS 계좌 기준)",
                    symbol,
                    balance_qty,
                    actual,
                )
                balance_qty = actual
        return balance_qty

    def resolve_exchange_code(self, settings, fallback):
        if settings and settings.exchange_code:
            return settings.exchange_code
        return fallback


_DOMESTIC_OPS = DomesticOrderOps()
_OVERSEAS_OPS = OverseasOrderOps()


# =============================================================================
# DOMESTIC BUY
# =============================================================================


async def _process_buy_orders_impl(
    ops: SupportsOrderExecution,
    kis_client: KISClient,
    symbol: str,
    current_price: float,
    avg_buy_price: float,
    exchange_code: str | None = None,
) -> OrderStepResult:
    """Unified buy order implementation. Market-specific behavior via ops."""
    from app.services.stock_info_service import StockAnalysisService
    from app.services.symbol_trade_settings_service import SymbolTradeSettingsService

    async with _open_async_session() as db:
        service = StockAnalysisService(db)
        analysis = await service.get_latest_analysis_by_symbol(symbol)

        # 1. 기본 조건: 현재가가 평균 매수가보다 1% 낮아야 함
        if avg_buy_price > 0:
            target_price = avg_buy_price * 0.99
            if current_price >= target_price:
                return OrderStepResult(
                    success=False,
                    message=f"1% 매수 조건 미충족: 현재가 {current_price} >= 목표가 {target_price}"
                    if ops.market == "domestic"
                    else "1% 매수 조건 미충족",
                )

        # 2. 분석 결과 확인
        if not analysis:
            return OrderStepResult(success=False, message="분석 결과 없음")

        # 2.5 종목 설정 확인
        settings_service = SymbolTradeSettingsService(db)
        settings = await settings_service.get_by_symbol(symbol)
        if not settings or not settings.is_active:
            logger.info("[%s] %s", symbol, MSG_NO_SETTINGS)
            return OrderStepResult(success=False, message=MSG_NO_SETTINGS)

        buy_price_levels = settings.buy_price_levels
        resolved_exchange = ops.resolve_exchange_code(settings, exchange_code)

        appropriate_buy_min = _coerce_optional_float(
            getattr(analysis, "appropriate_buy_min", None)
        )
        appropriate_buy_max = _coerce_optional_float(
            getattr(analysis, "appropriate_buy_max", None)
        )
        buy_hope_min = _coerce_optional_float(getattr(analysis, "buy_hope_min", None))
        buy_hope_max = _coerce_optional_float(getattr(analysis, "buy_hope_max", None))

        # 3. 가격 정보 확인 (스마트 선택 로직)
        if buy_price_levels == 1:
            use_lower_price = (
                appropriate_buy_max is not None
                and avg_buy_price > 0
                and appropriate_buy_max < avg_buy_price
            )
            if use_lower_price:
                if appropriate_buy_min is not None:
                    buy_prices = [appropriate_buy_min]
                elif buy_hope_min is not None:
                    buy_prices = [buy_hope_min]
                else:
                    buy_prices = []
            else:
                if appropriate_buy_max is not None:
                    buy_prices = [appropriate_buy_max]
                elif appropriate_buy_min is not None:
                    buy_prices = [appropriate_buy_min]
                else:
                    buy_prices = []
        else:
            buy_prices = _present_prices(
                appropriate_buy_min,
                appropriate_buy_max,
                buy_hope_min,
                buy_hope_max,
            )
            buy_prices = buy_prices[:buy_price_levels]

        if not buy_prices:
            return OrderStepResult(
                success=False,
                message="분석 결과에 매수 가격 정보 없음",
            )

        # 4. 조건에 맞는 가격 필터링
        threshold_price = avg_buy_price * 0.99 if avg_buy_price > 0 else float("inf")
        valid_prices = [
            p for p in buy_prices if p < threshold_price and p < current_price
        ]

        if not valid_prices:
            return OrderStepResult(
                success=False,
                message=f"조건에 맞는 매수 가격 없음 ({buy_price_levels}개 가격대 중 유효 없음)",
            )

        # 5. 수량 확인
        quantity = _coerce_positive_int(settings.buy_quantity_per_order)
        if quantity < 1:
            return OrderStepResult(success=False, message="설정된 수량이 1 미만")

        # 6. 주문 실행
        success_count = 0
        ordered_prices: list[float] = []
        ordered_quantities: list[int] = []
        total_amount = 0.0

        for price in valid_prices:
            res = await ops.place_order(
                kis_client,
                symbol,
                "buy",
                quantity,
                price,
                exchange_code=resolved_exchange,
            )
            if res and res.get("odno"):
                success_count += 1
                ordered_prices.append(price)
                ordered_quantities.append(quantity)
                total_amount += price * quantity

            await asyncio.sleep(0.2)

        return OrderStepResult(
            success=success_count > 0,
            message=f"{success_count}개 주문 성공 (설정: {buy_price_levels}개 가격대)",
            orders_placed=success_count,
            prices=ordered_prices,
            quantities=ordered_quantities,
            total_amount=total_amount,
        )


async def process_kis_domestic_buy_orders_with_analysis(
    kis_client: KISClient, symbol: str, current_price: float, avg_buy_price: float
) -> dict[str, Any]:
    """분석 결과를 기반으로 KIS 국내 주식 매수 주문 처리."""
    try:
        result = await _process_buy_orders_impl(
            _DOMESTIC_OPS, kis_client, symbol, current_price, avg_buy_price
        )
        return result.to_payload()
    except Exception as exc:
        logger.exception(f"[{symbol}] Domestic buy order failed: {exc}")
        return _map_exception_to_result(exc, f"domestic buy for {symbol}").to_payload()


# =============================================================================
# OVERSEAS BUY
# =============================================================================


async def process_kis_overseas_buy_orders_with_analysis(
    kis_client: KISClient,
    symbol: str,
    current_price: float,
    avg_buy_price: float,
    exchange_code: str = "NASD",
) -> dict[str, Any]:
    """분석 결과를 기반으로 KIS 해외 주식 매수 주문 처리."""
    try:
        result = await _process_buy_orders_impl(
            _OVERSEAS_OPS,
            kis_client,
            symbol,
            current_price,
            avg_buy_price,
            exchange_code,
        )
        return result.to_payload()
    except Exception as exc:
        logger.exception(f"[{symbol}] Overseas buy order failed: {exc}")
        return _map_exception_to_result(exc, f"overseas buy for {symbol}").to_payload()


# =============================================================================
# DOMESTIC SELL
# =============================================================================


async def _process_sell_orders_impl(
    ops: SupportsOrderExecution,
    kis_client: KISClient,
    symbol: str,
    current_price: float,
    avg_buy_price: float,
    balance_qty: int,
    exchange_code: str | None = None,
) -> OrderStepResult:
    """Unified sell order implementation. Market-specific behavior via ops."""
    from app.services.stock_info_service import StockAnalysisService
    from app.services.symbol_trade_settings_service import SymbolTradeSettingsService

    async with _open_async_session() as db:
        service = StockAnalysisService(db)
        analysis = await service.get_latest_analysis_by_symbol(symbol)

        if not analysis:
            return OrderStepResult(success=False, message="분석 결과 없음")

        # exchange_code resolution (overseas only — domestic returns None)
        resolved_exchange = exchange_code
        if exchange_code is not None:
            settings_service = SymbolTradeSettingsService(db)
            settings = await settings_service.get_by_symbol(symbol)
            resolved_exchange = ops.resolve_exchange_code(settings, exchange_code)

        # Overseas: verify actual orderable qty from KIS account
        balance_qty = await ops.adjust_sell_qty(kis_client, symbol, balance_qty)
        if balance_qty <= 0:
            return OrderStepResult(success=False, message="주문가능수량 없음")

        sell_prices = _present_prices(
            getattr(analysis, "appropriate_sell_min", None),
            getattr(analysis, "appropriate_sell_max", None),
            getattr(analysis, "sell_target_min", None),
            getattr(analysis, "sell_target_max", None),
        )

        if not sell_prices:
            return OrderStepResult(success=False, message="매도 가격 정보 없음")

        min_sell_price = avg_buy_price * 1.01
        valid_prices = [
            p for p in sell_prices if p >= min_sell_price and p >= current_price
        ]
        valid_prices.sort()

        if not valid_prices:
            if current_price >= min_sell_price:
                res = await ops.place_order(
                    kis_client,
                    symbol,
                    "sell",
                    balance_qty,
                    current_price,
                    exchange_code=resolved_exchange,
                )
                if res and res.get("odno"):
                    return OrderStepResult(
                        success=True,
                        message="목표가 도달로 전량 매도",
                        orders_placed=1,
                        prices=[current_price],
                        quantities=[balance_qty],
                        total_volume=balance_qty,
                        expected_amount=current_price * balance_qty,
                    )
                else:
                    return OrderStepResult(success=False, message="매도 주문 실패")
            return OrderStepResult(success=False, message="매도 조건 미충족")

        split_count = len(valid_prices)
        qty_per_order = balance_qty // split_count

        if qty_per_order < 1:
            target_price = valid_prices[0]
            res = await ops.place_order(
                kis_client,
                symbol,
                "sell",
                balance_qty,
                target_price,
                exchange_code=resolved_exchange,
            )
            if res and res.get("odno"):
                return OrderStepResult(
                    success=True,
                    message="전량 매도 주문 (분할 불가)"
                    if ops.market == "domestic"
                    else "전량 매도 주문",
                    orders_placed=1,
                    prices=[target_price],
                    quantities=[balance_qty],
                    total_volume=balance_qty,
                    expected_amount=target_price * balance_qty,
                )
            return OrderStepResult(success=False, message="매도 주문 실패")

        success_count = 0
        remaining_qty = balance_qty
        ordered_prices: list[float] = []
        ordered_quantities: list[int] = []
        total_volume = 0
        expected_amount = 0.0

        for i, price in enumerate(valid_prices):
            is_last = i == len(valid_prices) - 1
            qty = remaining_qty if is_last else qty_per_order

            if qty < 1:
                continue

            res = await ops.place_order(
                kis_client,
                symbol,
                "sell",
                qty,
                price,
                exchange_code=resolved_exchange,
            )
            if res and res.get("odno"):
                success_count += 1
                remaining_qty -= qty
                ordered_prices.append(price)
                ordered_quantities.append(qty)
                total_volume += qty
                expected_amount += price * qty

            await asyncio.sleep(0.2)

        return OrderStepResult(
            success=success_count > 0,
            message=f"{success_count}건 분할 매도 주문 완료",
            orders_placed=success_count,
            prices=ordered_prices,
            quantities=ordered_quantities,
            total_volume=total_volume,
            expected_amount=expected_amount,
        )


async def process_kis_domestic_sell_orders_with_analysis(
    kis_client: KISClient,
    symbol: str,
    current_price: float,
    avg_buy_price: float,
    balance_qty: int,
) -> dict[str, Any]:
    """분석 결과를 기반으로 KIS 국내 주식 매도 주문 처리."""
    try:
        result = await _process_sell_orders_impl(
            _DOMESTIC_OPS, kis_client, symbol, current_price, avg_buy_price, balance_qty
        )
        return result.to_payload()
    except Exception as exc:
        logger.exception(f"[{symbol}] Domestic sell order failed: {exc}")
        return _map_exception_to_result(exc, f"domestic sell for {symbol}").to_payload()


# =============================================================================
# OVERSEAS SELL
# =============================================================================


async def process_kis_overseas_sell_orders_with_analysis(
    kis_client: KISClient,
    symbol: str,
    current_price: float,
    avg_buy_price: float,
    balance_qty: int,
    exchange_code: str = "NASD",
) -> dict[str, Any]:
    """분석 결과를 기반으로 KIS 해외 주식 매도 주문 처리."""
    try:
        result = await _process_sell_orders_impl(
            _OVERSEAS_OPS,
            kis_client,
            symbol,
            current_price,
            avg_buy_price,
            balance_qty,
            exchange_code,
        )
        return result.to_payload()
    except Exception as exc:
        logger.exception(f"[{symbol}] Overseas sell order failed: {exc}")
        return _map_exception_to_result(exc, f"overseas sell for {symbol}").to_payload()
