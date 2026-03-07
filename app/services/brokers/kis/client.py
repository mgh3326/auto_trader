"""KIS (Korea Investment & Securities) API client facade.

This module provides a simplified facade interface to the KIS API.
All implementation details have been moved to specialized modules:
- transport.py: HTTP communication and rate limiting
- market_data_api.py: Market data operations (prices, charts, rankings)
- holdings_api.py: Balance and holdings operations
- orders_api.py: Order placement, cancellation, and inquiry
- constants.py: API constants (URLs, TR IDs)
"""

import datetime
from typing import Any

import pandas as pd
from pandas import DataFrame

from app.core.config import settings
from app.services.brokers.kis.constants import (
    BASE_URL,
    CHART_PERIOD_DAY,
    CHART_PERIOD_MONTH,
    CHART_PERIOD_WEEK,
    CHART_REQUEST_TIMEOUT,
    CHART_TIME_UNIT_10MIN,
    CHART_TIME_UNIT_15MIN,
    CHART_TIME_UNIT_1MIN,
    CHART_TIME_UNIT_30MIN,
    CHART_TIME_UNIT_3MIN,
    CHART_TIME_UNIT_45MIN,
    CHART_TIME_UNIT_5MIN,
    CHART_TIME_UNIT_60MIN,
    DEFAULT_CANDLES,
    DEFAULT_CHART_DAYS,
    DEFAULT_TIMEOUT,
    DOMESTIC_BALANCE_TR,
    DOMESTIC_BALANCE_TR_MOCK,
    DOMESTIC_BALANCE_URL,
    DOMESTIC_DAILY_CHART_TR,
    DOMESTIC_DAILY_CHART_URL,
    DOMESTIC_DAILY_ORDER_TR,
    DOMESTIC_DAILY_ORDER_TR_MOCK,
    DOMESTIC_DAILY_ORDER_URL,
    DOMESTIC_MARKET_CODES,
    DOMESTIC_MINUTE_CHART_TR,
    DOMESTIC_MINUTE_CHART_URL,
    DOMESTIC_ORDER_BUY_TR,
    DOMESTIC_ORDER_BUY_TR_MOCK,
    DOMESTIC_ORDER_CANCEL_TR,
    DOMESTIC_ORDER_CANCEL_TR_MOCK,
    DOMESTIC_ORDER_CANCEL_URL,
    DOMESTIC_ORDER_INQUIRY_TR,
    DOMESTIC_ORDER_INQUIRY_URL,
    DOMESTIC_ORDER_SELL_TR,
    DOMESTIC_ORDER_SELL_TR_MOCK,
    DOMESTIC_ORDER_URL,
    DOMESTIC_PRICE_TR,
    DOMESTIC_PRICE_URL,
    DOMESTIC_VOLUME_TR,
    DOMESTIC_VOLUME_URL,
    ERROR_TOKEN_EXPIRED,
    ERROR_TOKEN_INVALID,
    FLUCTUATION_RANK_TR,
    FLUCTUATION_RANK_URL,
    FOREIGN_BUYING_RANK_TR,
    FOREIGN_BUYING_RANK_URL,
    INTEGRATED_MARGIN_TR,
    INTEGRATED_MARGIN_TR_MOCK,
    INTEGRATED_MARGIN_URL,
    MARKET_CAP_RANK_TR,
    MARKET_CAP_RANK_URL,
    MAX_CHART_ITERATIONS,
    MAX_PAGES,
    MAX_TOKEN_RETRIES,
    ORDERBOOK_TR,
    ORDERBOOK_URL,
    OVERSEAS_BALANCE_TR,
    OVERSEAS_BALANCE_TR_MOCK,
    OVERSEAS_BALANCE_URL,
    OVERSEAS_BUYABLE_AMOUNT_TR,
    OVERSEAS_BUYABLE_AMOUNT_TR_MOCK,
    OVERSEAS_BUYABLE_AMOUNT_URL,
    OVERSEAS_CURRENCIES,
    OVERSEAS_DAILY_CHART_TR,
    OVERSEAS_DAILY_CHART_URL,
    OVERSEAS_DAILY_ORDER_TR,
    OVERSEAS_DAILY_ORDER_TR_MOCK,
    OVERSEAS_DAILY_ORDER_URL,
    OVERSEAS_EXCHANGE_MAP,
    OVERSEAS_EXCHANGE_NAMES,
    OVERSEAS_MARGIN_TR,
    OVERSEAS_MARGIN_TR_MOCK,
    OVERSEAS_MARGIN_URL,
    OVERSEAS_MINUTE_CHART_TR,
    OVERSEAS_MINUTE_CHART_URL,
    OVERSEAS_ORDER_BUY_TR,
    OVERSEAS_ORDER_BUY_TR_MOCK,
    OVERSEAS_ORDER_CANCEL_TR,
    OVERSEAS_ORDER_CANCEL_TR_MOCK,
    OVERSEAS_ORDER_CANCEL_URL,
    OVERSEAS_ORDER_INQUIRY_TR,
    OVERSEAS_ORDER_INQUIRY_URL,
    OVERSEAS_ORDER_SELL_TR,
    OVERSEAS_ORDER_SELL_TR_MOCK,
    OVERSEAS_ORDER_URL,
    OVERSEAS_PERIOD_CHART_TR,
    OVERSEAS_PERIOD_CHART_URL,
    OVERSEAS_PRICE_TR,
    OVERSEAS_PRICE_URL,
    PAGE_DELAY,
    PRICE_ADJUSTED,
    PRICE_ORIGINAL,
    SUCCESS_CODE,
    TIME_DAILY_CHART_TR,
    TIME_DAILY_CHART_URL,
    TOKEN_RETRY_DELAY,
    get_exchange_code_3digit,
    get_mock_tr_id,
)
from app.services.brokers.kis.holdings_api import HoldingsAPI
from app.services.brokers.kis.market_data_api import MarketDataAPI
from app.services.brokers.kis.orders_api import OrdersAPI
from app.services.brokers.kis.transport import KISTransport


class KISClient:
    """KIS (Korea Investment & Securities) API client.

    This is the main facade class that provides access to all KIS API operations
    through composition of specialized internal modules:
    - _transport: HTTP communication, rate limiting, token management
    - _holdings: Balance and holdings operations
    - _orders: Order placement, cancellation, and inquiry
    - _market_data: Price, chart, and ranking data

    The internal modules are instantiated in __init__ and delegate actual
    operations to the appropriate specialized classes.
    """

    def __init__(self) -> None:
        """Initialize the KIS client with internal modules."""
        # Internal modules - composed from specialized classes
        self._transport = KISTransport()
        self._holdings = HoldingsAPI(self._transport)
        self._orders = OrdersAPI(self._transport)
        self._market_data = MarketDataAPI(self._transport)

        # Legacy attributes kept for backward compatibility
        self._hdr_base = {
            "appkey": settings.kis_app_key,
            "appsecret": settings.kis_app_secret,
            "tr_id": "FHPST01710000",
            "custtype": "P",
        }

    # =================================================================
    # Market Data API - Delegates to MarketDataAPI
    # =================================================================

    async def volume_rank(self, market: str = "J", limit: int = 30) -> list[dict]:
        """거래량 순위 조회 (Delegates to MarketDataAPI)."""
        return await self._market_data.volume_rank(market=market, limit=limit)

    async def market_cap_rank(self, market: str = "J", limit: int = 30) -> list[dict]:
        """시가총액 순위 조회 (Delegates to MarketDataAPI)."""
        return await self._market_data.market_cap_rank(market=market, limit=limit)

    async def fluctuation_rank(
        self, market: str = "J", direction: str = "up", limit: int = 30
    ) -> list[dict]:
        """등락률 순위 조회 (Delegates to MarketDataAPI)."""
        return await self._market_data.fluctuation_rank(
            market=market, direction=direction, limit=limit
        )

    async def foreign_buying_rank(
        self, market: str = "J", limit: int = 30
    ) -> list[dict]:
        """외국인 순매수 순위 조회 (Delegates to MarketDataAPI)."""
        return await self._market_data.foreign_buying_rank(market=market, limit=limit)

    async def inquire_price(self, code: str, market: str = "UN") -> DataFrame:
        """단일 종목 현재가·기본정보 조회 (Delegates to MarketDataAPI)."""
        return await self._market_data.inquire_price(code=code, market=market)

    async def inquire_orderbook(self, code: str, market: str = "UN") -> dict:
        """주식 호가(orderbook) 조회 - 10단계 매수/매도 호가 (Delegates to MarketDataAPI)."""
        return await self._market_data.inquire_orderbook(code=code, market=market)

    async def fetch_fundamental_info(self, code: str, market: str = "UN") -> dict:
        """종목의 기본 정보를 가져와 딕셔너리로 반환 (Delegates to MarketDataAPI)."""
        return await self._market_data.fetch_fundamental_info(code=code, market=market)

    async def inquire_daily_itemchartprice(
        self,
        code: str,
        market: str = "UN",
        n: int = 200,
        adj: bool = True,
        period: str = "D",
        end_date: datetime.date | None = None,
        per_call_days: int = 150,
    ) -> pd.DataFrame:
        """KIS 일봉/주봉/월봉 조회 (Delegates to MarketDataAPI)."""
        return await self._market_data.inquire_daily_itemchartprice(
            code=code,
            market=market,
            n=n,
            adj=adj,
            period=period,
            end_date=end_date,
            per_call_days=per_call_days,
        )

    async def inquire_time_dailychartprice(
        self,
        code: str,
        market: str = "UN",
        n: int = 200,
        end_date: datetime.date | None = None,
        end_time: str | None = None,
    ) -> pd.DataFrame:
        """당일 분봉 데이터 조회 (Delegates to MarketDataAPI)."""
        return await self._market_data.inquire_time_dailychartprice(
            code=code,
            market=market,
            n=n,
            end_date=end_date,
            end_time=end_time,
        )

    async def inquire_minute_chart(
        self,
        code: str,
        market: str = "UN",
        time_unit: int = 1,
        n: int = 200,
        end_date: datetime.date | None = None,
    ) -> pd.DataFrame:
        """KIS 분봉 데이터 조회 (Delegates to MarketDataAPI)."""
        return await self._market_data.inquire_minute_chart(
            code=code,
            market=market,
            time_unit=time_unit,
            n=n,
            end_date=end_date,
        )

    async def fetch_minute_candles(
        self,
        code: str,
        market: str = "UN",
        end_date: datetime.date | None = None,
    ) -> dict:
        """분봉 데이터를 가져와서 60분, 5분, 1분 캔들로 반환 (Delegates to MarketDataAPI)."""
        return await self._market_data.fetch_minute_candles(
            code=code,
            market=market,
            end_date=end_date,
        )

    async def inquire_overseas_daily_price(
        self,
        symbol: str,
        exchange_code: str = "NASD",
        n: int = 200,
        period: str = "D",
    ) -> pd.DataFrame:
        """해외주식 일봉/주봉/월봉 조회 (Delegates to MarketDataAPI)."""
        return await self._market_data.inquire_overseas_daily_price(
            symbol=symbol,
            exchange_code=exchange_code,
            n=n,
            period=period,
        )

    # =================================================================
    # Holdings/Balance API - Delegates to HoldingsAPI
    # =================================================================

    async def fetch_my_stocks(
        self,
        is_mock: bool = False,
        is_overseas: bool = False,
        exchange_code: str = "NASD",
        currency_code: str = "USD",
    ) -> list[dict]:
        """보유 주식 목록 조회 (Delegates to HoldingsAPI)."""
        return await self._holdings.fetch_my_stocks(
            is_mock=is_mock,
            is_overseas=is_overseas,
            exchange_code=exchange_code,
            currency_code=currency_code,
        )

    async def inquire_domestic_cash_balance(self, is_mock: bool = False) -> dict:
        """국내주식 현금 잔고 조회 (Delegates to HoldingsAPI)."""
        return await self._holdings.inquire_domestic_cash_balance(is_mock=is_mock)

    async def inquire_overseas_margin(self, is_mock: bool = False) -> list[dict]:
        """해외증거금 통화별 조회 (Delegates to HoldingsAPI)."""
        return await self._holdings.inquire_overseas_margin(is_mock=is_mock)

    async def inquire_integrated_margin(
        self,
        is_mock: bool = False,
        cma_evlu_amt_icld_yn: str = "N",
        wcrc_frcr_dvsn_cd: str = "01",
        fwex_ctrt_frcr_dvsn_cd: str = "01",
    ) -> dict:
        """통합증거금 조회 (Delegates to HoldingsAPI)."""
        return await self._holdings.inquire_integrated_margin(
            is_mock=is_mock,
            cma_evlu_amt_icld_yn=cma_evlu_amt_icld_yn,
            wcrc_frcr_dvsn_cd=wcrc_frcr_dvsn_cd,
            fwex_ctrt_frcr_dvsn_cd=fwex_ctrt_frcr_dvsn_cd,
        )

    async def fetch_my_overseas_stocks(
        self,
        is_mock: bool = False,
        exchange_code: str = "NASD",
        currency_code: str = "USD",
    ) -> list[dict]:
        """해외 보유 주식 목록 조회 (Delegates to HoldingsAPI)."""
        return await self._holdings.fetch_my_overseas_stocks(
            is_mock=is_mock,
            exchange_code=exchange_code,
            currency_code=currency_code,
        )

    async def fetch_my_us_stocks(
        self, is_mock: bool = False, exchange: str = "NASD"
    ) -> list[dict]:
        """미국 보유 주식 목록 조회 (Delegates to HoldingsAPI)."""
        return await self._holdings.fetch_my_us_stocks(
            is_mock=is_mock, exchange=exchange
        )

    # =================================================================
    # Orders API - Delegates to OrdersAPI
    # =================================================================

    async def order_overseas_stock(
        self,
        symbol: str,
        exchange_code: str,
        order_type: str,
        quantity: int,
        price: float = 0.0,
        is_mock: bool = False,
    ) -> dict:
        """해외주식 주문 (매수/매도) - Delegates to OrdersAPI."""
        return await self._orders.order_overseas_stock(
            symbol, exchange_code, order_type, quantity, price, is_mock
        )

    async def buy_overseas_stock(
        self,
        symbol: str,
        exchange_code: str,
        quantity: int,
        price: float = 0.0,
        is_mock: bool = False,
    ) -> dict:
        """해외주식 매수 주문 편의 메서드 - Delegates to OrdersAPI."""
        return await self._orders.buy_overseas_stock(
            symbol, exchange_code, quantity, price, is_mock
        )

    async def sell_overseas_stock(
        self,
        symbol: str,
        exchange_code: str,
        quantity: int,
        price: float = 0.0,
        is_mock: bool = False,
    ) -> dict:
        """해외주식 매도 주문 편의 메서드 - Delegates to OrdersAPI."""
        return await self._orders.sell_overseas_stock(
            symbol, exchange_code, quantity, price, is_mock
        )

    async def inquire_overseas_orders(
        self,
        exchange_code: str = "NASD",
        is_mock: bool = False,
    ) -> list[dict]:
        """해외주식 미체결 주문 조회 - Delegates to OrdersAPI."""
        return await self._orders.inquire_overseas_orders(
            exchange_code=exchange_code,
            is_mock=is_mock,
        )

    async def cancel_overseas_order(
        self,
        order_number: str,
        symbol: str,
        exchange_code: str,
        quantity: int,
        is_mock: bool = False,
    ) -> dict:
        """해외주식 주문 취소 - Delegates to OrdersAPI."""
        return await self._orders.cancel_overseas_order(
            order_number, symbol, exchange_code, quantity, is_mock
        )

    async def inquire_korea_orders(
        self,
        is_mock: bool = False,
    ) -> list[dict]:
        """국내주식 정정취소가능주문 조회 - Delegates to OrdersAPI."""
        return await self._orders.inquire_korea_orders(is_mock=is_mock)

    async def order_korea_stock(
        self,
        stock_code: str,
        order_type: str,
        quantity: int,
        price: int = 0,
        is_mock: bool = False,
    ) -> dict:
        """국내주식 주문 (매수/매도) - Delegates to OrdersAPI."""
        return await self._orders.order_korea_stock(
            stock_code, order_type, quantity, price, is_mock
        )

    async def sell_korea_stock(
        self,
        stock_code: str,
        quantity: int,
        price: int = 0,
        is_mock: bool = False,
    ) -> dict:
        """국내주식 매도 주문 편의 메서드 - Delegates to OrdersAPI."""
        return await self._orders.sell_korea_stock(stock_code, quantity, price, is_mock)

    async def cancel_korea_order(
        self,
        order_number: str,
        stock_code: str,
        quantity: int,
        price: int,
        order_type: str,
        is_mock: bool = False,
        krx_fwdg_ord_orgno: str | None = None,
    ) -> dict:
        """국내주식 주문 취소 - Delegates to OrdersAPI."""
        return await self._orders.cancel_korea_order(
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
    ) -> list[dict]:
        """국내주식 일별 체결조회 (주문 히스토리) - Delegates to OrdersAPI."""
        return await self._orders.inquire_daily_order_domestic(
            start_date, end_date, stock_code, side, order_number, is_mock
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
    ) -> list[dict]:
        """해외주식 일별 체결조회 (주문 히스토리) - Delegates to OrdersAPI."""
        return await self._orders.inquire_daily_order_overseas(
            start_date, end_date, symbol, exchange_code, side, order_number, is_mock
        )

    async def modify_korea_order(
        self,
        order_number: str,
        stock_code: str,
        quantity: int,
        new_price: int,
        is_mock: bool = False,
        krx_fwdg_ord_orgno: str | None = None,
    ) -> dict:
        """국내주식 주문 정정 (가격/수량 변경) - Delegates to OrdersAPI."""
        return await self._orders.modify_korea_order(
            order_number, stock_code, quantity, new_price, is_mock, krx_fwdg_ord_orgno
        )

    async def modify_overseas_order(
        self,
        order_number: str,
        symbol: str,
        exchange_code: str,
        quantity: int,
        new_price: float,
        is_mock: bool = False,
    ) -> dict:
        """해외주식 주문 정정 (가격/수량 변경) - Delegates to OrdersAPI."""
        return await self._orders.modify_overseas_order(
            order_number, symbol, exchange_code, quantity, new_price, is_mock
        )


kis = KISClient()  # 싱글턴


# ============================================================================
# BACKWARD COMPATIBILITY ALIASES
# ============================================================================
# These aliases ensure existing code importing from client.py continues to work

# Balance aliases (tests use these shorter names)
BALANCE_TR = DOMESTIC_BALANCE_TR
BALANCE_TR_MOCK = DOMESTIC_BALANCE_TR_MOCK
BALANCE_URL = DOMESTIC_BALANCE_URL

# Order aliases
KOREA_ORDER_URL = DOMESTIC_ORDER_URL

# ============================================================================
# HELPER FUNCTIONS (Backward Compatibility)
# ============================================================================

def extract_domestic_cash_summary_from_integrated_margin(
    margin_data: dict[str, Any],
) -> dict[str, Any]:
    """Extract domestic cash summary from integrated margin data.

    Args:
        margin_data: Raw margin data from inquire_integrated_margin()

    Returns:
        Dict with 'balance', 'orderable', and 'raw' keys
    """
    raw = margin_data.get("raw", margin_data)

    # Extract cash amounts from various possible key locations
    balance_str = margin_data.get(
        "stck_cash_objt_amt"
    ) or raw.get("stck_cash_objt_amt", "0")
    orderable_str = margin_data.get(
        "stck_itgr_cash100_ord_psbl_amt"
    ) or raw.get("stck_itgr_cash100_ord_psbl_amt", "0")

    return {
        "balance": float(balance_str) if balance_str else 0.0,
        "orderable": float(orderable_str) if orderable_str else 0.0,
        "raw": raw,
    }
