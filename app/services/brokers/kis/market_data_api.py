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
    ERROR_TOKEN_EXPIRED,
    ERROR_TOKEN_INVALID,
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
        token = await self._transport.ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {token}",
            "tr_id": DOMESTIC_VOLUME_TR,
        }

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_COND_SCR_DIV_CODE": "20171",
            "FID_INPUT_ISCD": "0000",
            "FID_DIV_CLS_CODE": "0",
            "FID_BLNG_CLS_CODE": "1",
            "FID_TRGT_CLS_CODE": "11111111",
            "FID_TRGT_EXLS_CLS_CODE": "0000001100",
            "FID_INPUT_PRICE_1": "0",
            "FID_INPUT_PRICE_2": "1000000",
            "FID_VOL_CNT": "100000",
            "FID_INPUT_DATE_1": "",
        }

        js = await self._transport.request(
            "GET",
            f"{BASE_URL}{DOMESTIC_VOLUME_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="volume_rank",
            tr_id=DOMESTIC_VOLUME_TR,
        )
        if js["rt_cd"] == "0":
            results = js["output"][:limit]
            # Safe debug sample without float conversion that could fail
            sample_data = [
                (r.get("hts_kor_isnm", ""), r.get("acml_vol", "0")) for r in results[:3]
            ]
            logging.debug(
                f"volume_rank: Received {len(js['output'])} results, "
                f"returning {len(results)}. Sample: {sample_data}"
            )
            return results
        raise RuntimeError(
            js.get("msg1") or f"KIS API error (msg_cd={js.get('msg_cd', 'unknown')})"
        )

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
        token = await self._transport.ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {token}",
            "tr_id": MARKET_CAP_RANK_TR,
        }
        js = await self._transport.request(
            "GET",
            f"{BASE_URL}{MARKET_CAP_RANK_URL}",
            headers=hdr,
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_COND_SCR_DIV_CODE": "20174",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_TRGT_CLS_CODE": "0",
                "FID_TRGT_EXLS_CLS_CODE": "0",
                "FID_INPUT_PRICE_1": "",
                "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": "",
            },
            timeout=5,
            api_name="market_cap_rank",
            tr_id=MARKET_CAP_RANK_TR,
        )
        if js["rt_cd"] == "0":
            return js["output"][:limit]
        raise RuntimeError(
            js.get("msg1") or f"KIS API error (msg_cd={js.get('msg_cd', 'unknown')})"
        )

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
        token = await self._transport.ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {token}",
            "tr_id": FLUCTUATION_RANK_TR,
        }

        # FID_PRC_CLS_CODE: "0"=전체 (공식 API 문서 기준)
        prc_cls_code = "0"
        # FID_RANK_SORT_CLS_CODE: "0"=상승률, "3"=하락율 (공식 API 문서 기준)
        rank_sort_cls_code = "3" if direction == "down" else "0"

        logging.debug(
            f"fluctuation_rank: direction={direction}, "
            f"FID_PRC_CLS_CODE={prc_cls_code}, "
            f"FID_RANK_SORT_CLS_CODE={rank_sort_cls_code}"
        )

        js = await self._transport.request(
            "GET",
            f"{BASE_URL}{FLUCTUATION_RANK_URL}",
            headers=hdr,
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_COND_SCR_DIV_CODE": "20170",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_RANK_SORT_CLS_CODE": rank_sort_cls_code,
                "FID_INPUT_CNT_1": "0",
                "FID_PRC_CLS_CODE": prc_cls_code,
                "FID_INPUT_PRICE_1": "",
                "FID_INPUT_PRICE_2": "",
                "FID_VOL_CNT": "",
                "FID_TRGT_CLS_CODE": "0",
                "FID_TRGT_EXLS_CLS_CODE": "0",
                "FID_RSFL_RATE1": "",
                "FID_RSFL_RATE2": "",
            },
            timeout=5,
            api_name="fluctuation_rank",
            tr_id=FLUCTUATION_RANK_TR,
        )

        if js["rt_cd"] == "0":
            results = js["output"]
            # Sort: up → descending (highest first), down → ascending (lowest first).
            if direction == "up":
                results.sort(key=lambda x: float(x.get("prdy_ctrt", 0)), reverse=True)
                return results[:limit]

            negatives = [
                item for item in results if float(item.get("prdy_ctrt", 0)) < 0
            ]
            negatives.sort(key=lambda x: float(x.get("prdy_ctrt", 0)))
            return negatives[:limit]

        raise RuntimeError(
            js.get("msg1") or f"KIS API error (msg_cd={js.get('msg_cd', 'unknown')})"
        )

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
        token = await self._transport.ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {token}",
            "tr_id": FOREIGN_BUYING_RANK_TR,
        }
        js = await self._transport.request(
            "GET",
            f"{BASE_URL}{FOREIGN_BUYING_RANK_URL}",
            headers=hdr,
            params={
                "FID_COND_MRKT_DIV_CODE": "V",
                "FID_COND_SCR_DIV_CODE": "16449",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_RANK_SORT_CLS_CODE": "0",
                "FID_ETC_CLS_CODE": "1",
            },
            timeout=5,
            api_name="foreign_buying_rank",
            tr_id=FOREIGN_BUYING_RANK_TR,
        )
        if js["rt_cd"] == "0":
            return js["output"][:limit]
        raise RuntimeError(
            js.get("msg1") or f"KIS API error (msg_cd={js.get('msg_cd', 'unknown')})"
        )

    # =========================================================================
    # Price & Orderbook Methods
    # =========================================================================

    async def inquire_price(self, code: str, market: str = "UN") -> DataFrame:
        """단일 종목 현재가·기본정보 조회

        Args:
            code: 6자리 종목코드 (예: "005930")
            market: K(코스피)/Q(코스닥)/UN(통합)

        Returns:
            현재가 정보 DataFrame (columns: code, date, time, open, high, low, close, volume, value)
        """
        token = await self._transport.ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {token}",
            "tr_id": DOMESTIC_PRICE_TR,
        }

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),  # 000000 형태도 OK
        }

        js = await self._transport.request(
            "GET",
            f"{BASE_URL}{DOMESTIC_PRICE_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="inquire_price",
            tr_id=DOMESTIC_PRICE_TR,
        )

        if js["rt_cd"] != "0":
            if js.get("msg_cd") in [
                ERROR_TOKEN_EXPIRED,
                ERROR_TOKEN_INVALID,
            ]:
                # Token expired - transport layer handles refresh, retry once
                token = await self._transport.ensure_token()
                hdr = self._hdr_base | {
                    "authorization": f"Bearer {token}",
                    "tr_id": DOMESTIC_PRICE_TR,
                }
                js = await self._transport.request(
                    "GET",
                    f"{BASE_URL}{DOMESTIC_PRICE_URL}",
                    headers=hdr,
                    params=params,
                    timeout=5,
                    api_name="inquire_price",
                    tr_id=DOMESTIC_PRICE_TR,
                )
                if js["rt_cd"] != "0":
                    raise RuntimeError(f"{js['msg_cd']} {js['msg1']}")
            else:
                raise RuntimeError(f"{js['msg_cd']} {js['msg1']}")

        out = js["output"]  # 단일 dict
        trade_date_str = out.get("stck_bsop_date")  # 예: '20250805'
        if trade_date_str:
            trade_date = pd.to_datetime(trade_date_str, format="%Y%m%d")
        else:
            # 필드가 없으면 오늘 날짜
            trade_date = pd.Timestamp(datetime.date.today())

        # 체결 시각
        time_str = out.get("stck_cntg_hour") or out.get("stck_cntg_time")  # 'HHMMSS'
        if time_str:
            trade_time = pd.to_datetime(time_str, format="%H%M%S").time()
        else:
            trade_time = datetime.datetime.now().time()  # 필드가 없으면 현재 시각

        row = {
            "code": out["stck_shrn_iscd"],
            "date": trade_date,
            "time": trade_time,
            "open": float(out["stck_oprc"]),
            "high": float(out["stck_hgpr"]),
            "low": float(out["stck_lwpr"]),
            "close": float(out["stck_prpr"]),
            "volume": int(out["acml_vol"]),
            "value": int(out["acml_tr_pbmn"]),
        }
        return pd.DataFrame([row]).set_index("code")  # index = 종목코드

    async def inquire_orderbook(self, code: str, market: str = "UN") -> dict:
        """주식 호가(orderbook) 조회 - 10단계 매수/매도 호가

        Args:
            code: 6자리 종목코드 (예: "005930")
            market: K(코스피)/Q(코스닥)/UN(통합)

        Returns:
            호가 정보 딕셔너리 (매수/매도 10단계 호가 포함)
        """
        token = await self._transport.ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {token}",
            "tr_id": ORDERBOOK_TR,
        }

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),
        }

        js = await self._transport.request(
            "GET",
            f"{BASE_URL}{ORDERBOOK_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="inquire_orderbook",
            tr_id=ORDERBOOK_TR,
        )

        if js["rt_cd"] != "0":
            if js.get("msg_cd") in [
                ERROR_TOKEN_EXPIRED,
                ERROR_TOKEN_INVALID,
            ]:
                # Token expired - transport layer handles refresh, retry once
                token = await self._transport.ensure_token()
                hdr = self._hdr_base | {
                    "authorization": f"Bearer {token}",
                    "tr_id": ORDERBOOK_TR,
                }
                js = await self._transport.request(
                    "GET",
                    f"{BASE_URL}{ORDERBOOK_URL}",
                    headers=hdr,
                    params=params,
                    timeout=5,
                    api_name="inquire_orderbook",
                    tr_id=ORDERBOOK_TR,
                )
                if js["rt_cd"] != "0":
                    raise RuntimeError(f"{js['msg_cd']} {js['msg1']}")
            else:
                raise RuntimeError(f"{js['msg_cd']} {js['msg1']}")

        output = js.get("output1")
        if output is None:
            output = js.get("output")
        if not isinstance(output, dict):
            raise RuntimeError("inquire_orderbook: missing valid output1/output dict")
        return output

    async def fetch_fundamental_info(self, code: str, market: str = "UN") -> dict:
        """종목의 기본 정보를 가져와 딕셔너리로 반환

        Args:
            code: 6자리 종목코드 (예: "005930")
            market: K(코스피)/Q(코스닥)/UN(통합)

        Returns:
            기본 정보 딕셔너리 (종목코드, 종목명, 현재가, 등락률, 거래량, 시가총액 등)
        """
        token = await self._transport.ensure_token()
        hdr = self._hdr_base | {
            "authorization": f"Bearer {token}",
            "tr_id": DOMESTIC_PRICE_TR,
        }

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),  # 000000 형태도 OK
        }

        js = await self._transport.request(
            "GET",
            f"{BASE_URL}{DOMESTIC_PRICE_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="fetch_fundamental_info",
            tr_id=DOMESTIC_PRICE_TR,
        )

        if js["rt_cd"] != "0":
            if js.get("msg_cd") in [
                ERROR_TOKEN_EXPIRED,
                ERROR_TOKEN_INVALID,
            ]:
                # Token expired - transport layer handles refresh, retry once
                token = await self._transport.ensure_token()
                hdr = self._hdr_base | {
                    "authorization": f"Bearer {token}",
                    "tr_id": DOMESTIC_PRICE_TR,
                }
                js = await self._transport.request(
                    "GET",
                    f"{BASE_URL}{DOMESTIC_PRICE_URL}",
                    headers=hdr,
                    params=params,
                    timeout=5,
                    api_name="fetch_fundamental_info",
                    tr_id=DOMESTIC_PRICE_TR,
                )
                if js["rt_cd"] != "0":
                    raise RuntimeError(f"{js['msg_cd']} {js['msg1']}")
            else:
                raise RuntimeError(f"{js['msg_cd']} {js['msg1']}")

        out = js["output"]  # 단일 dict

        # 기본 정보 구성
        fundamental_data = {
            "종목코드": out.get("stck_shrn_iscd"),
            "종목명": out.get("hts_kor_isnm"),
            "현재가": out.get("stck_prpr"),
            "전일대비": out.get("prdy_vrss"),
            "등락률": out.get("prdy_ctrt"),
            "거래량": out.get("acml_vol"),
            "거래대금": out.get("acml_tr_pbmn"),
            "시가총액": out.get("hts_avls"),
            "상장주수": out.get("lstn_stcn"),
            "외국인비율": out.get("frgn_hlg"),
            "52주최고": out.get("w52_hgpr"),
            "52주최저": out.get("w52_lwpr"),
        }

        # None이 아닌 값만 반환
        return {k: v for k, v in fundamental_data.items() if v is not None}

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
