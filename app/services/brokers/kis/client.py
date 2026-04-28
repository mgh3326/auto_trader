# pyright: reportAttributeAccessIssue=false, reportImplicitOverride=false, reportPrivateUsage=false
from __future__ import annotations

import datetime
from typing import Any, cast

import pandas as pd
from pandas import DataFrame

from app.core.async_rate_limiter import get_limiter
from app.core.config import settings
from app.services.redis_token_manager import RedisTokenManager

from .account import AccountClient, extract_domestic_cash_summary_from_integrated_margin
from .base import BaseKISClient
from .constants import DOMESTIC_BALANCE_TR as BALANCE_TR
from .constants import DOMESTIC_BALANCE_URL as BALANCE_URL
from .constants import DOMESTIC_ORDER_URL as KOREA_ORDER_URL
from .constants import INTEGRATED_MARGIN_TR, INTEGRATED_MARGIN_URL
from .domestic_orders import DomesticOrderClient
from .market_data import (
    MarketDataClient,
    OverseasMinuteChartPage,
    _aggregate_minute_candles_frame,
)
from .overseas_orders import OverseasOrderClient
from .protocols import KISClientProtocol

__all__ = [
    "BALANCE_TR",
    "BALANCE_URL",
    "INTEGRATED_MARGIN_TR",
    "INTEGRATED_MARGIN_URL",
    "KISClient",
    "KOREA_ORDER_URL",
    "extract_domestic_cash_summary_from_integrated_margin",
    "get_limiter",
    "kis",
    "settings",
]


class _KISSettingsView:
    """Expose live or KIS mock credentials without cross-account fallback."""

    def __init__(self, *, is_mock: bool) -> None:
        self._is_mock = is_mock

    def __getattr__(self, name: str) -> Any:
        return getattr(settings, name)

    @property
    def kis_app_key(self) -> str:
        if self._is_mock:
            return str(settings.kis_mock_app_key or "")
        return settings.kis_app_key

    @property
    def kis_app_secret(self) -> str:
        if self._is_mock:
            return str(settings.kis_mock_app_secret or "")
        return settings.kis_app_secret

    @property
    def kis_account_no(self) -> str | None:
        if self._is_mock:
            return settings.kis_mock_account_no
        return settings.kis_account_no

    @property
    def kis_base_url(self) -> str:
        if self._is_mock:
            return settings.kis_mock_base_url
        return settings.kis_base_url

    @property
    def kis_access_token(self) -> str | None:
        if self._is_mock:
            return settings.kis_mock_access_token
        return settings.kis_access_token

    @kis_access_token.setter
    def kis_access_token(self, value: str | None) -> None:
        if self._is_mock:
            settings.kis_mock_access_token = value
        else:
            settings.kis_access_token = value


class KISClient(BaseKISClient):
    """KIS API facade client that delegates to specialized sub-clients.

    This class serves as the public interface for KIS API operations,
    assembling and delegating to sub-clients for specific domains:
    - MarketDataClient: Market data and price information
    - AccountClient: Account balances and holdings
    - DomesticOrderClient: Domestic (Korean) stock orders
    - OverseasOrderClient: Overseas stock orders

    The actual HTTP transport, token management, and rate limiting
    are inherited from BaseKISClient.
    """

    def __init__(self, *, is_mock: bool = False) -> None:
        self._is_mock_client = is_mock
        self._settings_view = _KISSettingsView(is_mock=is_mock)
        super().__init__()
        if is_mock:
            self._token_manager = RedisTokenManager("kis_mock")
        parent: KISClientProtocol = cast(KISClientProtocol, cast(object, self))
        self._market_data: MarketDataClient = MarketDataClient(parent)
        self._account: AccountClient = AccountClient(parent)
        self._domestic_orders: DomesticOrderClient = DomesticOrderClient(parent)
        self._overseas_orders: OverseasOrderClient = OverseasOrderClient(parent)

    @property
    def _settings(self) -> Any:
        return self._settings_view

    async def _get_limiter(self, api_key: str, *, rate: int, period: float) -> Any:
        return await get_limiter("kis", api_key, rate=rate, period=period)

    @staticmethod
    def _aggregate_intraday_to_hour(df: pd.DataFrame) -> pd.DataFrame:
        return _aggregate_minute_candles_frame(df, 60, include_partial=True)

    async def volume_rank(
        self, market: str = "J", limit: int = 30
    ) -> list[dict[str, Any]]:
        return await self._market_data.volume_rank(market, limit)

    async def market_cap_rank(
        self, market: str = "J", limit: int = 30
    ) -> list[dict[str, Any]]:
        return await self._market_data.market_cap_rank(market, limit)

    async def fluctuation_rank(
        self, market: str = "J", direction: str = "up", limit: int = 30
    ) -> list[dict[str, Any]]:
        return await self._market_data.fluctuation_rank(market, direction, limit)

    async def foreign_buying_rank(
        self, market: str = "J", limit: int = 30
    ) -> list[dict[str, Any]]:
        return await self._market_data.foreign_buying_rank(market, limit)

    async def inquire_price(self, code: str, market: str = "J") -> DataFrame:
        return await self._market_data.inquire_price(code, market)

    async def inquire_orderbook(self, code: str, market: str = "J") -> dict[str, Any]:
        return await self._market_data.inquire_orderbook(code, market)

    async def inquire_orderbook_snapshot(
        self,
        code: str,
        market: str = "J",
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        return await self._market_data.inquire_orderbook_snapshot(code, market)

    async def fetch_fundamental_info(
        self, code: str, market: str = "J"
    ) -> dict[str, Any]:
        return await self._market_data.fetch_fundamental_info(code, market)

    async def inquire_daily_itemchartprice(
        self,
        code: str,
        market: str = "J",
        n: int = 200,
        adj: bool = True,
        period: str = "D",
        end_date: pd.Timestamp | None = None,
        per_call_days: int = 150,
    ) -> pd.DataFrame:
        return await self._market_data.inquire_daily_itemchartprice(
            code, market, n, adj, period, end_date, per_call_days
        )

    async def inquire_investor(
        self, code: str, market: str = "J"
    ) -> list[dict[str, Any]]:
        return await self._market_data.inquire_investor(code, market)

    async def inquire_short_selling(
        self,
        code: str,
        start_date: datetime.date,
        end_date: datetime.date,
        market: str = "J",
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        return await self._market_data.inquire_short_selling(
            code,
            start_date,
            end_date,
            market,
        )

    async def inquire_time_dailychartprice(
        self,
        code: str,
        market: str = "J",
        n: int = 200,
        end_date: pd.Timestamp | None = None,
        end_time: str | None = None,
    ) -> pd.DataFrame:
        return await self._market_data.inquire_time_dailychartprice(
            code, market, n, end_date, end_time
        )

    async def inquire_minute_chart(
        self,
        code: str,
        market: str = "J",
        time_unit: int = 1,
        n: int = 200,
        end_date: pd.Timestamp | None = None,
    ) -> pd.DataFrame:
        return await self._market_data.inquire_minute_chart(
            code, market, time_unit, n, end_date
        )

    async def fetch_minute_candles(
        self,
        code: str,
        market: str = "J",
        end_date: pd.Timestamp | None = None,
    ) -> dict[str, DataFrame]:
        return await self._market_data.fetch_minute_candles(code, market, end_date)

    async def inquire_overseas_daily_price(
        self,
        symbol: str,
        exchange_code: str = "NASD",
        n: int = 200,
        period: str = "D",
    ) -> pd.DataFrame:
        return await self._market_data.inquire_overseas_daily_price(
            symbol, exchange_code, n, period
        )

    async def inquire_overseas_minute_chart(
        self,
        symbol: str,
        exchange_code: str = "NASD",
        n: int = 120,
        keyb: str = "",
    ) -> OverseasMinuteChartPage:
        return await self._market_data.inquire_overseas_minute_chart(
            symbol, exchange_code, n, keyb
        )

    async def fetch_my_stocks(
        self,
        is_mock: bool = False,
        is_overseas: bool = False,
        exchange_code: str = "NASD",
        currency_code: str = "USD",
    ) -> list[dict[str, Any]]:
        return await self._account.fetch_my_stocks(
            is_mock, is_overseas, exchange_code, currency_code
        )

    async def inquire_domestic_cash_balance(
        self, is_mock: bool = False
    ) -> dict[str, Any]:
        return await self._account.inquire_domestic_cash_balance(is_mock)

    async def inquire_overseas_margin(
        self, is_mock: bool = False
    ) -> list[dict[str, Any]]:
        return await self._account.inquire_overseas_margin(is_mock)

    async def inquire_integrated_margin(
        self,
        is_mock: bool = False,
        cma_evlu_amt_icld_yn: str = "N",
        wcrc_frcr_dvsn_cd: str = "01",
        fwex_ctrt_frcr_dvsn_cd: str = "01",
    ) -> dict[str, Any]:
        return await self._account.inquire_integrated_margin(
            is_mock,
            cma_evlu_amt_icld_yn,
            wcrc_frcr_dvsn_cd,
            fwex_ctrt_frcr_dvsn_cd,
        )

    async def fetch_my_overseas_stocks(
        self,
        is_mock: bool = False,
        exchange_code: str = "NASD",
        currency_code: str = "USD",
    ) -> list[dict[str, Any]]:
        return await self._account.fetch_my_overseas_stocks(
            is_mock, exchange_code, currency_code
        )

    async def fetch_my_us_stocks(
        self, is_mock: bool = False, exchange: str = "NASD"
    ) -> list[dict[str, Any]]:
        return await self._account.fetch_my_us_stocks(is_mock, exchange)

    async def inquire_korea_orders(
        self,
        is_mock: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._domestic_orders.inquire_korea_orders(is_mock)

    async def order_korea_stock(
        self,
        stock_code: str,
        order_type: str,
        quantity: int,
        price: int = 0,
        is_mock: bool = False,
    ) -> dict[str, Any]:
        return await self._domestic_orders.order_korea_stock(
            stock_code, order_type, quantity, price, is_mock
        )

    async def sell_korea_stock(
        self,
        stock_code: str,
        quantity: int,
        price: int = 0,
        is_mock: bool = False,
    ) -> dict[str, Any]:
        return await self._domestic_orders.sell_korea_stock(
            stock_code, quantity, price, is_mock
        )

    async def cancel_korea_order(
        self,
        order_number: str,
        stock_code: str,
        quantity: int,
        price: int,
        order_type: str,
        is_mock: bool = False,
        krx_fwdg_ord_orgno: str | None = None,
    ) -> dict[str, Any]:
        return await self._domestic_orders.cancel_korea_order(
            order_number,
            stock_code,
            quantity,
            price,
            order_type,
            is_mock,
            krx_fwdg_ord_orgno,
        )

    async def inquire_daily_order_domestic(
        self,
        start_date: str,
        end_date: str,
        stock_code: str = "",
        side: str = "00",
        order_number: str = "",
        is_mock: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._domestic_orders.inquire_daily_order_domestic(
            start_date, end_date, stock_code, side, order_number, is_mock
        )

    async def modify_korea_order(
        self,
        order_number: str,
        stock_code: str,
        quantity: int,
        new_price: int,
        is_mock: bool = False,
        krx_fwdg_ord_orgno: str | None = None,
    ) -> dict[str, Any]:
        return await self._domestic_orders.modify_korea_order(
            order_number,
            stock_code,
            quantity,
            new_price,
            is_mock,
            krx_fwdg_ord_orgno,
        )

    async def order_overseas_stock(
        self,
        symbol: str,
        exchange_code: str,
        order_type: str,
        quantity: int,
        price: float = 0.0,
        is_mock: bool = False,
    ) -> dict[str, Any]:
        return await self._overseas_orders.order_overseas_stock(
            symbol, exchange_code, order_type, quantity, price, is_mock
        )

    async def buy_overseas_stock(
        self,
        symbol: str,
        exchange_code: str,
        quantity: int,
        price: float = 0.0,
        is_mock: bool = False,
    ) -> dict[str, Any]:
        return await self._overseas_orders.buy_overseas_stock(
            symbol, exchange_code, quantity, price, is_mock
        )

    async def sell_overseas_stock(
        self,
        symbol: str,
        exchange_code: str,
        quantity: int,
        price: float = 0.0,
        is_mock: bool = False,
    ) -> dict[str, Any]:
        return await self._overseas_orders.sell_overseas_stock(
            symbol, exchange_code, quantity, price, is_mock
        )

    async def inquire_overseas_orders(
        self,
        exchange_code: str = "NASD",
        is_mock: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._overseas_orders.inquire_overseas_orders(
            exchange_code, is_mock
        )

    async def cancel_overseas_order(
        self,
        order_number: str,
        symbol: str,
        exchange_code: str,
        quantity: int,
        is_mock: bool = False,
    ) -> dict[str, Any]:
        return await self._overseas_orders.cancel_overseas_order(
            order_number, symbol, exchange_code, quantity, is_mock
        )

    async def inquire_daily_order_overseas(
        self,
        start_date: str,
        end_date: str,
        symbol: str = "%",
        exchange_code: str = "NASD",
        side: str = "00",
        order_number: str = "",
        is_mock: bool = False,
    ) -> list[dict[str, Any]]:
        return await self._overseas_orders.inquire_daily_order_overseas(
            start_date, end_date, symbol, exchange_code, side, order_number, is_mock
        )

    async def modify_overseas_order(
        self,
        order_number: str,
        symbol: str,
        exchange_code: str,
        quantity: int,
        new_price: float,
        is_mock: bool = False,
    ) -> dict[str, Any]:
        return await self._overseas_orders.modify_overseas_order(
            order_number, symbol, exchange_code, quantity, new_price, is_mock
        )


kis = KISClient()
