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
"""

import logging
from abc import ABC, abstractmethod
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from app.analysis.analyzer import Analyzer
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
