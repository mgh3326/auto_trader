"""KIS API Market Data operations module.

This module provides the MarketDataAPI class that handles all market data
operations for the KIS (Korea Investment & Securities) API:
- Rankings (volume, market cap, fluctuation, foreign buying)
- Price inquiry (current price, fundamental info)
- Orderbook inquiry (호가)
- Chart data (daily, intraday, minute candles)
- Overseas daily price

The class receives KISTransport via constructor injection for HTTP communication.
"""

import datetime
import logging
from typing import TYPE_CHECKING

import pandas as pd
from pandas import DataFrame

from app.core.config import settings
from app.services.brokers.kis.constants import (
    BASE_URL,
    DOMESTIC_DAILY_CHART_TR,
    DOMESTIC_DAILY_CHART_URL,
    DOMESTIC_MINUTE_CHART_TR,
    DOMESTIC_MINUTE_CHART_URL,
    DOMESTIC_PRICE_TR,
    DOMESTIC_PRICE_URL,
    DOMESTIC_VOLUME_TR,
    DOMESTIC_VOLUME_URL,
    FLUCTUATION_RANK_TR,
    FLUCTUATION_RANK_URL,
    FOREIGN_BUYING_RANK_TR,
    FOREIGN_BUYING_RANK_URL,
    MARKET_CAP_RANK_TR,
    MARKET_CAP_RANK_URL,
    ORDERBOOK_TR,
    ORDERBOOK_URL,
    OVERSEAS_DAILY_CHART_TR,
    OVERSEAS_DAILY_CHART_URL,
    TIME_DAILY_CHART_TR,
    TIME_DAILY_CHART_URL,
)
from app.services.brokers.kis.transport import (
    _empty_day_frame,
    _log_kis_api_failure,
    _validate_daily_itemchartprice_chunk,
)

if TYPE_CHECKING:
    from app.services.brokers.kis.transport import KISTransport


class MarketDataAPI:
    """KIS API market data operations.

    This class handles all market data related operations:
    - Rankings: volume, market cap, fluctuation, foreign buying
    - Price inquiry: current price, fundamental info, orderbook
    - Chart data: daily OHLCV, intraday, minute candles
    - Overseas daily price

    The class receives KISTransport via constructor injection for HTTP
    communication, rate limiting, and token management.

    Example:
        transport = KISTransport()
        market_data = MarketDataAPI(transport)
        price_df = await market_data.inquire_price("005930", "UN")
    """

    def __init__(self, transport: "KISTransport") -> None:
        """Initialize the MarketDataAPI with a transport layer.

        Args:
            transport: KISTransport instance for HTTP communication
        """
        self._transport = transport

        # Base headers for KIS API requests
        self._hdr_base = {
            "appkey": settings.kis_app_key,
            "appsecret": settings.kis_app_secret,
            "tr_id": "FHPST01710000",  # Placeholder, overridden per request
            "custtype": "P",
        }

    # =========================================================================
    # Ranking Methods
    # =========================================================================

    async def volume_rank(
        self, market: str = "J", limit: int = 30
    ) -> list[dict]:
        """거래량 순위 조회

        Args:
            market: 시장 구분 (J: 통합, K: 코스피, Q: 코스닥)
            limit: 조회할 종목 수

        Returns:
            거래량 순위 리스트
        """
        raise NotImplementedError("volume_rank will be implemented in subtask-4-2")

    async def market_cap_rank(
        self, market: str = "J", limit: int = 30
    ) -> list[dict]:
        """시가총액 순위 조회

        Args:
            market: 시장 구분 (J: 통합, K: 코스피, Q: 코스닥)
            limit: 조회할 종목 수

        Returns:
            시가총액 순위 리스트
        """
        raise NotImplementedError("market_cap_rank will be implemented in subtask-4-2")

    async def fluctuation_rank(
        self, market: str = "J", direction: str = "up", limit: int = 30
    ) -> list[dict]:
        """등락률 순위 조회

        Args:
            market: 시장 구분 (J: 통합, K: 코스피, Q: 코스닥)
            direction: up(상승) 또는 down(하락)
            limit: 조회할 종목 수

        Returns:
            등락률 순위 리스트
        """
        raise NotImplementedError("fluctuation_rank will be implemented in subtask-4-2")

    async def foreign_buying_rank(
        self, market: str = "J", limit: int = 30
    ) -> list[dict]:
        """외국인 순매수 순위 조회

        Args:
            market: 시장 구분 (J: 통합)
            limit: 조회할 종목 수

        Returns:
            외국인 순매수 순위 리스트
        """
        raise NotImplementedError("foreign_buying_rank will be implemented in subtask-4-2")

    # =========================================================================
    # Price & Orderbook Methods
    # =========================================================================

    async def inquire_price(self, code: str, market: str = "UN") -> DataFrame:
        """단일 종목 현재가·기본정보 조회

        Args:
            code: 6자리 종목코드 (예: "005930")
            market: K(코스피)/Q(코스닥)/UN(통합)

        Returns:
            현재가 정보 DataFrame
        """
        raise NotImplementedError("inquire_price will be implemented in subtask-4-3")

    async def inquire_orderbook(self, code: str, market: str = "UN") -> dict:
        """주식 호가(orderbook) 조회 - 10단계 매수/매도 호가

        Args:
            code: 6자리 종목코드 (예: "005930")
            market: K(코스피)/Q(코스닥)/UN(통합)

        Returns:
            호가 정보 딕셔너리
        """
        raise NotImplementedError("inquire_orderbook will be implemented in subtask-4-3")

    async def fetch_fundamental_info(self, code: str, market: str = "UN") -> dict:
        """종목의 기본 정보를 가져와 딕셔너리로 반환

        Args:
            code: 6자리 종목코드 (예: "005930")
            market: K(코스피)/Q(코스닥)/UN(통합)

        Returns:
            기본 정보 딕셔너리
        """
        raise NotImplementedError("fetch_fundamental_info will be implemented in subtask-4-3")

    # =========================================================================
    # Chart Methods
    # =========================================================================

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
        """KIS 일봉/주봉/월봉을 여러 번 호출해 최근 n개 OHLCV 반환

        Args:
            code: 6자리 종목코드 (예: "005930")
            market: K(코스피)/Q(코스닥)/UN(통합)
            n: 최종 확보하고 싶은 캔들 수
            adj: True면 수정주가, False면 원본주가
            period: D(일봉)/W(주봉)/M(월봉)
            end_date: 종료 날짜 (None이면 오늘까지)
            per_call_days: 한 번 호출 시 조회 날짜 폭

        Returns:
            OHLCV DataFrame (columns: date, open, high, low, close, volume, value)
        """
        raise NotImplementedError("inquire_daily_itemchartprice will be implemented in subtask-4-4")

    async def inquire_time_dailychartprice(
        self,
        code: str,
        market: str = "UN",
        n: int = 200,
        end_date: datetime.date | None = None,
        end_time: str | None = None,
    ) -> pd.DataFrame:
        """당일 분봉 데이터 조회

        Args:
            code: 6자리 종목코드 (예: "005930")
            market: K(코스피)/Q(코스닥)/UN(통합)
            n: 가져올 캔들 수
            end_date: 종료 날짜 (None이면 오늘)
            end_time: 종료 시간 (None이면 현재 시간)

        Returns:
            분봉 DataFrame (columns: datetime, date, time, open, high, low, close, volume, value)
        """
        raise NotImplementedError("inquire_time_dailychartprice will be implemented in subtask-4-4")

    async def inquire_minute_chart(
        self,
        code: str,
        market: str = "UN",
        time_unit: int = 1,
        n: int = 200,
        end_date: datetime.date | None = None,
    ) -> pd.DataFrame:
        """KIS 분봉 데이터 조회

        Args:
            code: 6자리 종목코드 (예: "005930")
            market: 시장 구분 (UN: 통합, K: 코스피, Q: 코스닥)
            time_unit: 분봉 단위 (1, 3, 5, 10, 15, 30, 45, 60)
            n: 가져올 캔들 수 (최대 200)
            end_date: 종료 날짜 (None이면 오늘까지)

        Returns:
            분봉 DataFrame (columns: datetime, date, time, open, high, low, close, volume, value)
        """
        raise NotImplementedError("inquire_minute_chart will be implemented in subtask-4-4")

    async def fetch_minute_candles(
        self,
        code: str,
        market: str = "UN",
        time_units: list[int] | None = None,
        n: int = 200,
    ) -> dict[str, pd.DataFrame]:
        """여러 시간대 분봉 데이터를 수집

        Args:
            code: 6자리 종목코드 (예: "005930")
            market: 시장 구분 (UN: 통합, K: 코스피, Q: 코스닥)
            time_units: 분봉 단위 리스트 (기본: [60, 5, 1])
            n: 각 시간대별 가져올 캔들 수

        Returns:
            시간대별 분봉 DataFrame 딕셔너리 {"60min": df, "5min": df, "1min": df}
        """
        raise NotImplementedError("fetch_minute_candles will be implemented in subtask-4-4")

    # =========================================================================
    # Overseas Methods
    # =========================================================================

    async def inquire_overseas_daily_price(
        self,
        symbol: str,
        exchange_code: str = "NASD",
        n: int = 200,
        period: str = "D",
    ) -> pd.DataFrame:
        """해외주식 일봉/주봉/월봉 조회

        Args:
            symbol: 종목 심볼 (예: "AAPL")
            exchange_code: 거래소 코드 (NASD, NYSE, AMEX 등)
            n: 조회할 캔들 수 (최소 200개 권장)
            period: D(일봉)/W(주봉)/M(월봉)

        Returns:
            OHLCV DataFrame (columns: date, open, high, low, close, volume)
        """
        raise NotImplementedError("inquire_overseas_daily_price will be implemented in subtask-4-5")
