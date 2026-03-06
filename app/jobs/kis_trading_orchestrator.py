"""
Trading orchestrator and market strategy implementations for KIS automation.

This module defines the strategy pattern for market-specific behavior (domestic
vs overseas) and the orchestrator that executes trading automation steps.

The MarketStrategy ABC defines the interface for market-specific operations:
- fetch_holdings: Get current stock holdings from the broker
- fetch_open_orders: Get pending orders
- get_exchange_code: Get exchange code for a stock
- get_analyzer: Get the appropriate analyzer for the market

The DomesticStrategy and OverseasStrategy implement these for Korean and
US/overseas markets respectively.

The TradingOrchestrator class executes trading automation steps sequentially
with failure policy handling and result aggregation.
"""

import asyncio
import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

from app.jobs.kis_trading_types import (
    FailurePolicy,
    StepOutcome,
    StepResult,
    TradingContext,
)

if TYPE_CHECKING:
    from app.analysis.analyzer import Analyzer
    from app.jobs.kis_trading_steps import TradingStep
    from app.services.brokers.kis.client import KISClient

logger = logging.getLogger(__name__)


class MarketStrategy(ABC):
    """
    Abstract base class for market-specific trading behavior.

    Defines the interface for operations that differ between domestic (Korean)
    and overseas (US, etc.) markets. Each market implementation handles:
    - How to fetch holdings from the broker
    - How to fetch open/pending orders
    - How to resolve exchange codes
    - Which analyzer to use for stock analysis

    This allows the TradingOrchestrator to work with any market by using
    the strategy pattern, keeping market-specific logic separate from
    the orchestration logic.
    """

    @property
    @abstractmethod
    def market_name(self) -> str:
        """
        Return the human-readable market name.

        Used for logging and notifications.
        Example: "국내주식", "해외주식"
        """
        ...

    @property
    @abstractmethod
    def market_type(self) -> str:
        """
        Return the market type identifier.

        Used for notifications and result categorization.
        Example: "국내주식", "해외주식"
        """
        ...

    @abstractmethod
    async def fetch_holdings(self, kis: "KISClient") -> list[dict[str, Any]]:
        """
        Fetch current stock holdings from the broker.

        Args:
            kis: KIS client for API calls

        Returns:
            List of stock holding dictionaries with keys:
            - symbol: Stock code (pdno for domestic, ovrs_pdno for overseas)
            - name: Stock name
            - quantity: Number of shares
            - avg_price: Average purchase price
            - current_price: Current market price
            - exchange_code: Exchange code (empty for domestic)
            - is_manual: Whether this is a manual holding (토스 etc.)
        """
        ...

    @abstractmethod
    async def fetch_open_orders(self, kis: "KISClient") -> list[dict[str, Any]]:
        """
        Fetch pending/open orders from the broker.

        Args:
            kis: KIS client for API calls

        Returns:
            List of open order dictionaries with keys:
            - order_id: Order number
            - symbol: Stock code
            - order_type: "buy" or "sell"
            - quantity: Order quantity
            - price: Order price
            - exchange_code: Exchange code (for overseas)
        """
        ...

    @abstractmethod
    def get_exchange_code(self, stock: dict[str, Any]) -> str:
        """
        Get the exchange code for a stock.

        Args:
            stock: Stock holding dictionary

        Returns:
            Exchange code string (empty for domestic stocks)
        """
        ...

    @abstractmethod
    def get_analyzer(self) -> "Analyzer":
        """
        Get the appropriate analyzer for this market.

        Returns:
            Analyzer instance (KISAnalyzer for domestic, YahooAnalyzer for overseas)
        """
        ...

    @abstractmethod
    async def resolve_exchange_code(
        self,
        symbol: str,
        stock: dict[str, Any],
    ) -> str:
        """
        Resolve the exchange code for an overseas stock.

        For domestic stocks, returns empty string.
        For overseas stocks, resolves from stock data or DB lookup.

        Args:
            symbol: Stock symbol
            stock: Stock holding dictionary (may contain preferred exchange)

        Returns:
            Exchange code string
        """
        ...


class DomesticStrategy(MarketStrategy):
    """
    Market strategy for domestic (Korean) stocks.

    Uses KIS API for Korean market operations:
    - fetch_my_stocks() for holdings
    - inquire_korea_orders() for open orders
    - KISAnalyzer for analysis
    """

    @property
    def market_name(self) -> str:
        return "국내주식"

    @property
    def market_type(self) -> str:
        return "국내주식"

    async def fetch_holdings(self, kis: "KISClient") -> list[dict[str, Any]]:
        """
        Fetch domestic stock holdings from KIS.

        Also merges manual holdings (토스 etc.) from the database.

        Returns:
            List of stock holdings with keys:
            - pdno: Stock code
            - prdt_name: Stock name
            - hldg_qty: Holding quantity
            - ord_psbl_qty: Orderable quantity (minus pending orders)
            - pchs_avg_pric: Average purchase price
            - prpr: Current price
            - _is_manual: True for manual holdings
        """
        from app.core.db import AsyncSessionLocal
        from app.models.manual_holdings import MarketType
        from app.services.manual_holdings_service import ManualHoldingsService

        # Fetch KIS holdings
        my_stocks = await kis.fetch_my_stocks()

        # Fetch manual holdings (토스 등) and merge
        async with AsyncSessionLocal() as db:
            manual_service = ManualHoldingsService(db)
            user_id = 1  # Fixed for now (multi-user support later)
            manual_holdings = await manual_service.get_holdings_by_user(
                user_id=user_id, market_type=MarketType.KR
            )

        # Add manual holdings to the list
        for holding in manual_holdings:
            ticker = holding.ticker
            # Skip if already in KIS holdings
            if any(s.get("pdno") == ticker for s in my_stocks):
                continue

            # Convert to KIS format
            qty_str = str(holding.quantity)
            my_stocks.append(
                {
                    "pdno": ticker,
                    "prdt_name": holding.display_name or ticker,
                    "hldg_qty": qty_str,
                    "ord_psbl_qty": qty_str,  # No pending orders for manual
                    "pchs_avg_pric": str(holding.avg_price),
                    "prpr": str(holding.avg_price),  # Will be updated via API later
                    "_is_manual": True,
                }
            )

        logger.info(
            "[DomesticStrategy] 보유 종목 조회 완료: KIS %d건 + 수동 %d건",
            len(my_stocks) - len(manual_holdings),
            len([s for s in my_stocks if s.get("_is_manual")]),
        )

        return my_stocks

    async def fetch_open_orders(self, kis: "KISClient") -> list[dict[str, Any]]:
        """
        Fetch pending orders for domestic stocks.

        Returns:
            List of open orders with Korean market format
        """
        orders = await kis.inquire_korea_orders(is_mock=False)
        logger.info("[DomesticStrategy] 미체결 주문 조회 완료: %d건", len(orders))
        return orders

    def get_exchange_code(self, stock: dict[str, Any]) -> str:
        """
        Get exchange code for domestic stock (always empty).

        Domestic stocks don't have exchange codes.
        """
        return ""

    def get_analyzer(self) -> "Analyzer":
        """
        Get KISAnalyzer for domestic stock analysis.
        """
        from app.analysis.service_analyzers import KISAnalyzer

        return KISAnalyzer()

    async def resolve_exchange_code(
        self,
        symbol: str,
        stock: dict[str, Any],
    ) -> str:
        """
        Resolve exchange code for domestic stock (always empty).

        Domestic stocks don't need exchange codes.
        """
        return ""

    async def fetch_current_price_for_manual(
        self,
        kis: "KISClient",
        symbol: str,
        default_price: float,
    ) -> float:
        """
        Fetch current price for a manual holding via KIS API.

        Manual holdings don't have real-time prices from KIS holdings,
        so we need to fetch them separately.

        Args:
            kis: KIS client
            symbol: Stock code
            default_price: Fallback price if API fails

        Returns:
            Current price from API or default
        """
        try:
            price_info = await kis.fetch_fundamental_info(symbol)
            current_price = float(price_info.get("현재가", default_price))
            logger.debug(
                "[DomesticStrategy] 수동잔고 현재가 조회: %s = %s원",
                symbol,
                current_price,
            )
            return current_price
        except Exception as e:
            logger.warning(
                "[DomesticStrategy] 수동잔고 현재가 조회 실패 (%s): %s, 기본값 사용",
                symbol,
                e,
            )
            return default_price


def _extract_overseas_order_id(order: dict) -> str:
    """
    Extract order ID from an overseas order dict.

    Handles various field names used in API responses.

    Args:
        order: Order dictionary

    Returns:
        Order ID string or empty string if not found
    """
    for key in ("odno", "ODNO", "ord_no", "ORD_NO"):
        value = order.get(key)
        if value:
            return str(value).strip()
    return ""


class OverseasStrategy(MarketStrategy):
    """
    Market strategy for overseas (US, etc.) stocks.

    Uses KIS API for overseas market operations:
    - fetch_my_overseas_stocks() for holdings
    - inquire_overseas_orders() for open orders (across all exchanges)
    - YahooAnalyzer for analysis
    """

    @property
    def market_name(self) -> str:
        return "해외주식"

    @property
    def market_type(self) -> str:
        return "해외주식"

    async def fetch_holdings(self, kis: "KISClient") -> list[dict[str, Any]]:
        """
        Fetch overseas stock holdings from KIS.

        Also merges manual holdings (토스 etc.) from the database.

        Returns:
            List of stock holdings with keys:
            - ovrs_pdno: Stock symbol
            - ovrs_item_name: Stock name
            - ovrs_cblc_qty: Holding quantity
            - ord_psbl_qty: Orderable quantity (minus pending orders)
            - pchs_avg_pric: Average purchase price
            - now_pric2: Current price
            - ovrs_excg_cd: Exchange code
            - _is_manual: True for manual holdings
        """
        from app.core.db import AsyncSessionLocal
        from app.core.symbol import to_db_symbol
        from app.models.manual_holdings import MarketType
        from app.services.manual_holdings_service import ManualHoldingsService

        # Fetch KIS holdings
        my_stocks = await kis.fetch_my_overseas_stocks()

        # Fetch manual holdings (토스 등) and merge
        async with AsyncSessionLocal() as db:
            manual_service = ManualHoldingsService(db)
            user_id = 1  # Fixed for now (multi-user support later)
            manual_holdings = await manual_service.get_holdings_by_user(
                user_id=user_id, market_type=MarketType.US
            )

        # Add manual holdings to the list
        for holding in manual_holdings:
            ticker = holding.ticker
            # Skip if already in KIS holdings (normalize symbol for comparison)
            if any(
                to_db_symbol(s.get("ovrs_pdno", "")) == ticker for s in my_stocks
            ):
                continue

            # Convert to KIS format
            qty_str = str(holding.quantity)
            my_stocks.append(
                {
                    "ovrs_pdno": ticker,
                    "ovrs_item_name": holding.display_name or ticker,
                    "ovrs_cblc_qty": qty_str,
                    "ord_psbl_qty": qty_str,  # No pending orders for manual
                    "pchs_avg_pric": str(holding.avg_price),
                    "now_pric2": "0",  # Will be updated via API later
                    "_is_manual": True,
                }
            )

        logger.info(
            "[OverseasStrategy] 보유 종목 조회 완료: KIS %d건 + 수동 %d건",
            len(my_stocks) - len(manual_holdings),
            len([s for s in my_stocks if s.get("_is_manual")]),
        )

        return my_stocks

    async def fetch_open_orders(self, kis: "KISClient") -> list[dict[str, Any]]:
        """
        Fetch pending orders for overseas stocks across all exchanges.

        Queries NASD, NYSE, and AMEX exchanges and deduplicates by order ID.

        Returns:
            List of open orders with overseas market format
        """
        orders_by_id: dict[str, dict] = {}
        anonymous_orders: list[dict] = []

        # Query all three exchanges
        for exchange_code in ("NASD", "NYSE", "AMEX"):
            try:
                open_orders = await kis.inquire_overseas_orders(
                    exchange_code=exchange_code,
                    is_mock=False,
                )
            except Exception as exc:
                logger.warning(
                    "[OverseasStrategy] 미체결 주문 조회 실패 (exchange=%s): %s",
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

        orders = list(orders_by_id.values()) + anonymous_orders
        logger.info("[OverseasStrategy] 미체결 주문 조회 완료: %d건", len(orders))
        return orders

    def get_exchange_code(self, stock: dict[str, Any]) -> str:
        """
        Get exchange code for overseas stock.

        Args:
            stock: Stock holding dictionary

        Returns:
            Exchange code (e.g., "NASD", "NYSE", "AMEX")
        """
        return str(stock.get("ovrs_excg_cd", "")).strip().upper()

    def get_analyzer(self) -> "Analyzer":
        """
        Get YahooAnalyzer for overseas stock analysis.
        """
        from app.analysis.service_analyzers import YahooAnalyzer

        return YahooAnalyzer()

    async def resolve_exchange_code(
        self,
        symbol: str,
        stock: dict[str, Any],
    ) -> str:
        """
        Resolve exchange code for overseas stock.

        First checks if stock has a preferred exchange code,
        otherwise looks up from the database.

        Args:
            symbol: Stock symbol
            stock: Stock holding dictionary (may contain ovrs_excg_cd)

        Returns:
            Exchange code string
        """
        from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

        # Check if stock has a preferred exchange code
        preferred_exchange = stock.get("ovrs_excg_cd")
        normalized_preferred = str(preferred_exchange or "").strip().upper()

        if normalized_preferred:
            return normalized_preferred

        # Fall back to database lookup
        return await get_us_exchange_by_symbol(symbol)

    async def fetch_current_price_for_manual(
        self,
        kis: "KISClient",
        symbol: str,
        default_price: float,
    ) -> float:
        """
        Fetch current price for a manual overseas holding via KIS API.

        Manual holdings don't have real-time prices from KIS holdings,
        so we need to fetch them separately.

        Args:
            kis: KIS client
            symbol: Stock symbol
            default_price: Fallback price if API fails

        Returns:
            Current price from API or default
        """
        try:
            price_df = await kis.inquire_overseas_price(symbol)
            if not price_df.empty:
                current_price = float(price_df.iloc[0]["close"])
                logger.debug(
                    "[OverseasStrategy] 수동잔고 현재가 조회: %s = $%.2f",
                    symbol,
                    current_price,
                )
                return current_price
            return default_price
        except Exception as e:
            logger.warning(
                "[OverseasStrategy] 수동잔고 현재가 조회 실패 (%s): %s, 기본값 사용",
                symbol,
                e,
            )
            return default_price


class TradingOrchestrator:
    """
    Executes trading automation steps sequentially.

    The orchestrator manages the execution of steps for each stock holding,
    handling failure policies, skip conditions, and aggregating results.

    Usage:
        steps = [
            AnalyzeStep(),
            CancelBuyOrdersStep(),
            BuyStep(),
            RefreshStep(),
            CancelSellOrdersStep(),
            SellStep(),
        ]

        orchestrator = TradingOrchestrator(
            strategy=DomesticStrategy(),
            steps=steps,
        )

        result = await orchestrator.run(kis_client)
    """

    def __init__(
        self,
        strategy: MarketStrategy,
        steps: list["TradingStep"],
    ) -> None:
        self.strategy = strategy
        self.steps = steps

    async def run(self, kis: "KISClient") -> dict:
        """
        Execute all steps for all holdings.

        Args:
            kis: KIS client for API calls

        Returns:
            Dictionary with status and results:
            - status: "completed", "stopped", or "partial"
            - message: Human-readable summary
            - results: List of per-stock step results
        """
        # Fetch holdings and open orders using strategy
        holdings = await self.strategy.fetch_holdings(kis)
        open_orders = await self.strategy.fetch_open_orders(kis)

        # Check for empty holdings
        if not holdings:
            logger.info(
                "[%s] 보유 종목 없음",
                self.strategy.market_name,
            )
            return {
                "status": "completed",
                "message": f"보유 중인 {self.strategy.market_name}이 없습니다.",
                "results": [],
            }

        logger.info(
            "[%s] %d개 종목 처리 시작",
            self.strategy.market_name,
            len(holdings),
        )

        results = []
        global_stop = False

        # Process each stock
        for stock in holdings:
            if global_stop:
                logger.warning(
                    "[%s] 전체 자동화 중지됨 (STOP_ALL)",
                    self.strategy.market_name,
                )
                break

            # Create context for this stock
            context = TradingContext(
                stock=stock,
                open_orders=open_orders,
                kis=kis,
                strategy=self.strategy,
            )

            # Execute steps for this stock
            stock_steps, should_stop_all = await self._run_stock_automation(context)

            results.append({
                "symbol": context.symbol,
                "name": context.name,
                "steps": stock_steps,
            })

            # Check for STOP_ALL
            if should_stop_all:
                global_stop = True

            # Rate limiting between stocks
            await asyncio.sleep(0.2)

        logger.info(
            "[%s] %d개 종목 처리 완료",
            self.strategy.market_name,
            len(results),
        )

        if global_stop:
            logger.warning(
                "[%s] 전체 자동화 중지 (STOP_ALL)",
                self.strategy.market_name,
            )
            return {
                "status": "stopped",
                "message": "STOP_ALL failure policy triggered",
                "results": results,
            }

        return {
            "status": "completed",
            "message": f"{len(results)}개 종목 처리 완료",
            "results": results,
        }

    async def _run_stock_automation(
        self,
        context: TradingContext,
    ) -> tuple[list[dict[str, Any]], bool]:
        """
        Execute all steps for a single stock.

        Args:
            context: Trading context for this stock

        Returns:
            Tuple of (stock_steps, should_stop_all)
            - stock_steps: List of step result dictionaries
            - should_stop_all: True if STOP_ALL was triggered
        """
        stock_steps: list[dict[str, Any]] = []
        should_stop_all = False

        logger.info(
            "[%s] 종목 처리 시작: %s (%s)",
            self.strategy.market_name,
            context.name,
            context.symbol,
        )

        # Execute each step sequentially
        for step in self.steps:
            # Check skip conditions
            if step.should_skip(context):
                skip_reason = "Skip condition met"
                step._log_skip(context, skip_reason)
                stock_steps.append({
                    "step": step.name,
                    "result": {
                        "skipped": True,
                        "reason": skip_reason,
                    },
                })
                continue

            # Execute step
            try:
                outcome = await step.execute(context)

                # Record step result
                stock_steps.append({
                    "step": step.name,
                    "result": {
                        "success": outcome.result == StepResult.SUCCESS,
                        "skipped": outcome.result == StepResult.SKIP,
                        "message": outcome.message,
                        "data": outcome.data,
                    },
                })

                # Handle failure policy
                if outcome.result == StepResult.FAILURE:
                    if step.failure_policy == FailurePolicy.STOP_STOCK:
                        logger.warning(
                            "[%s] %s 단계 실패 - 종목 처리 중단 (STOP_STOCK)",
                            self.strategy.market_name,
                            step.name,
                        )
                        break  # Stop processing this stock
                    elif step.failure_policy == FailurePolicy.STOP_ALL:
                        logger.error(
                            "[%s] %s 단계 실패 - 전체 자동화 중단 (STOP_ALL)",
                            self.strategy.market_name,
                            step.name,
                        )
                        should_stop_all = True
                        break  # Stop processing this stock
                    # else: CONTINUE - just log and continue
                    logger.warning(
                        "[%s] %s 단계 실패 - 계속 진행 (CONTINUE): %s",
                        self.strategy.market_name,
                        step.name,
                        outcome.message,
                    )

            except Exception as e:
                # Unexpected error during step execution
                logger.error(
                    "[%s] %s 단계 예외 발생: %s",
                    self.strategy.market_name,
                    step.name,
                    e,
                    exc_info=e,
                )
                stock_steps.append({
                    "step": step.name,
                    "result": {
                        "success": False,
                        "error": str(e),
                    },
                })

                # Treat unexpected errors according to step's failure policy
                if step.failure_policy == FailurePolicy.STOP_STOCK:
                    break
                elif step.failure_policy == FailurePolicy.STOP_ALL:
                    should_stop_all = True
                    break

        return stock_steps, should_stop_all
