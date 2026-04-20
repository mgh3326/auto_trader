# pyright: reportAttributeAccessIssue=false, reportImplicitStringConcatenation=false, reportMissingTypeArgument=false
from __future__ import annotations

import datetime
import logging
from typing import Any

import pandas as pd
from pandas import DataFrame

from . import constants
from ._base_market_data import (
    MarketDataBase,
    _aggregate_minute_candles_frame,
    _empty_minute_frame,
)


def _empty_day_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=constants.DAY_FRAME_COLUMNS)


def _validate_daily_itemchartprice_chunk(chunk: list[dict[str, Any]]) -> None:
    if not isinstance(chunk, list):
        raise RuntimeError(
            "Malformed KIS daily chart payload: expected list in output2/output"
        )

    for index, row in enumerate(chunk):
        if not isinstance(row, dict):
            raise RuntimeError(
                f"Malformed KIS daily chart payload at row {index}: expected object"
            )

        missing = sorted(
            field
            for field in constants.DAILY_ITEMCHARTPRICE_REQUIRED_FIELDS
            if row.get(field) is None or row.get(field) == ""
        )
        if missing:
            missing_fields = ", ".join(missing)
            raise RuntimeError(
                f"Malformed KIS daily chart payload at row {index}: missing {missing_fields}"
            )


def normalize_daily_chart_lookback(n: int) -> int:
    if n < 1:
        raise ValueError("n must be greater than or equal to 1")
    return min(n, constants.DEFAULT_CANDLES)


class DomesticMarketDataMixin(MarketDataBase):
    """Domestic (국내) market data methods.

    Ranking, price, orderbook, short selling, fundamentals,
    daily/minute chart operations for Korean domestic stocks.
    """

    # ── Ranking ──

    async def volume_rank(self, market: str = "J", limit: int = 30) -> list[dict]:
        js = await self._request_with_token_retry(
            tr_id=constants.DOMESTIC_VOLUME_TR,
            url=f"{constants.BASE}{constants.DOMESTIC_VOLUME_URL}",
            params={
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
            },
            api_name="volume_rank",
        )
        results = js["output"][:limit]
        sample_data = [
            (r.get("hts_kor_isnm", ""), r.get("acml_vol", "0")) for r in results[:3]
        ]
        logging.debug(
            f"volume_rank: Received {len(js['output'])} results, "
            f"returning {len(results)}. Sample: {sample_data}"
        )
        return results

    async def market_cap_rank(self, market: str = "J", limit: int = 30) -> list[dict]:
        js = await self._request_with_token_retry(
            tr_id=constants.MARKET_CAP_RANK_TR,
            url=f"{constants.BASE}{constants.MARKET_CAP_RANK_URL}",
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
            api_name="market_cap_rank",
        )
        return js["output"][:limit]

    async def fluctuation_rank(
        self, market: str = "J", direction: str = "up", limit: int = 30
    ) -> list[dict]:
        # FID_PRC_CLS_CODE: "0"=전체 (공식 API 문서 기준)
        prc_cls_code = "0"
        # FID_RANK_SORT_CLS_CODE: "0"=상승률, "3"=하락율 (공식 API 문서 기준)
        rank_sort_cls_code = "3" if direction == "down" else "0"

        logging.debug(
            f"fluctuation_rank: direction={direction}, "
            f"FID_PRC_CLS_CODE={prc_cls_code}, "
            f"FID_RANK_SORT_CLS_CODE={rank_sort_cls_code}"
        )

        js = await self._request_with_token_retry(
            tr_id=constants.FLUCTUATION_RANK_TR,
            url=f"{constants.BASE}{constants.FLUCTUATION_RANK_URL}",
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
            api_name="fluctuation_rank",
        )

        results = js["output"]
        if direction == "up":
            results.sort(key=lambda x: float(x.get("prdy_ctrt", 0)), reverse=True)
            return results[:limit]

        negatives = [item for item in results if float(item.get("prdy_ctrt", 0)) < 0]
        negatives.sort(key=lambda x: float(x.get("prdy_ctrt", 0)))
        return negatives[:limit]

    async def foreign_buying_rank(
        self, market: str = "J", limit: int = 30
    ) -> list[dict]:
        js = await self._request_with_token_retry(
            tr_id=constants.FOREIGN_BUYING_RANK_TR,
            url=f"{constants.BASE}{constants.FOREIGN_BUYING_RANK_URL}",
            params={
                "FID_COND_MRKT_DIV_CODE": "V",
                "FID_COND_SCR_DIV_CODE": "16449",
                "FID_INPUT_ISCD": "0000",
                "FID_DIV_CLS_CODE": "0",
                "FID_RANK_SORT_CLS_CODE": "0",
                "FID_ETC_CLS_CODE": "1",
            },
            api_name="foreign_buying_rank",
        )
        return js["output"][:limit]

    # ── Price & Orderbook ──

    async def inquire_price(self, code: str, market: str = "J") -> DataFrame:
        """
        단일 종목 현재가·기본정보 조회
        :param code: 6자리 종목코드(005930)
        :param market: K(코스피)/Q(코스닥)/UN(통합)
        :return: API output 딕셔너리
        """
        js = await self._request_with_token_retry(
            tr_id=constants.DOMESTIC_PRICE_TR,
            url=f"{constants.BASE}{constants.DOMESTIC_PRICE_URL}",
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": code.zfill(6),
            },
            api_name="inquire_price",
        )
        out = js["output"]  # 단일 dict
        trade_date_str = out.get("stck_bsop_date")  # 예: '20250805'
        if trade_date_str:
            trade_date = pd.to_datetime(trade_date_str, format="%Y%m%d")
        else:  # 필드가 없으면 오늘 날짜
            trade_date = pd.Timestamp(datetime.date.today())

        # ── ② 체결 시각 ──
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

    async def _request_orderbook_snapshot(self, code: str, market: str = "J") -> dict:
        return await self._request_with_token_retry(
            tr_id=constants.DOMESTIC_ORDERBOOK_TR,
            url=f"{constants.BASE}{constants.DOMESTIC_ORDERBOOK_URL}",
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": code.zfill(6),
            },
            api_name="inquire_orderbook",
        )

    async def inquire_orderbook(self, code: str, market: str = "J") -> dict:
        """
        주식 호가(orderbook) 조회 - 10단계 매수/매도 호가
        :param code: 6자리 종목코드(005930)
        :param market: K(코스피)/Q(코스닥)/UN(통합)
        :return: API output 딕셔너리
        """
        js = await self._request_orderbook_snapshot(code, market)
        output = js.get("output1")
        if output is None:
            output = js.get("output")
        if not isinstance(output, dict):
            raise RuntimeError("inquire_orderbook: missing valid output1/output dict")
        return output

    async def inquire_orderbook_snapshot(
        self,
        code: str,
        market: str = "J",
    ) -> tuple[dict[str, Any], dict[str, Any] | None]:
        js = await self._request_orderbook_snapshot(code, market)
        output1 = js.get("output1")
        if output1 is None:
            output1 = js.get("output")
        if not isinstance(output1, dict):
            raise RuntimeError("inquire_orderbook: missing valid output1/output dict")

        output2_raw = js.get("output2")
        output2: dict[str, Any] | None
        if isinstance(output2_raw, dict):
            output2 = output2_raw
        elif (
            isinstance(output2_raw, list)
            and len(output2_raw) == 1
            and isinstance(output2_raw[0], dict)
        ):
            output2 = output2_raw[0]
        else:
            output2 = None

        return output1, output2

    # ── Short Selling & Fundamentals ──

    async def inquire_short_selling(
        self,
        code: str,
        start_date: datetime.date,
        end_date: datetime.date,
        market: str = "J",
    ) -> tuple[dict[str, Any], list[dict[str, Any]]]:
        if start_date > end_date:
            raise ValueError("start_date must be less than or equal to end_date")

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),
            "FID_INPUT_DATE_1": start_date.strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": end_date.strftime("%Y%m%d"),
        }

        js = await self._request_with_token_retry(
            tr_id=constants.DOMESTIC_SHORT_SELLING_TR,
            url=f"{constants.BASE}{constants.DOMESTIC_SHORT_SELLING_URL}",
            params=params,
            api_name="inquire_short_selling",
        )

        output1 = js.get("output1")
        if not isinstance(output1, dict):
            raise RuntimeError(
                "Malformed KIS short selling payload: expected output1 dict"
            )

        output2_raw = js.get("output2")
        if output2_raw is None:
            output2: list[dict[str, Any]] = []
        elif isinstance(output2_raw, list):
            if not all(isinstance(row, dict) for row in output2_raw):
                raise RuntimeError(
                    "Malformed KIS short selling payload: expected output2 list of objects"
                )
            output2 = output2_raw
        else:
            raise RuntimeError(
                "Malformed KIS short selling payload: expected output2 list"
            )

        return output1, output2

    async def inquire_investor(
        self,
        code: str,
        market: str = "J",
    ) -> list[dict[str, Any]]:
        js = await self._request_with_token_retry(
            tr_id=constants.INVESTOR_TRADING_TR,
            url=f"{constants.BASE}{constants.INVESTOR_TRADING_URL}",
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": code.zfill(6),
            },
            api_name="inquire_investor",
        )
        output = js.get("output") or []
        if not isinstance(output, list):
            return []
        return output

    async def fetch_fundamental_info(self, code: str, market: str = "J") -> dict:
        """
        종목의 기본 정보를 가져와 딕셔너리로 반환합니다.
        :param code: 6자리 종목코드(005930)
        :param market: K(코스피)/Q(코스닥)/UN(통합)
        :return: 기본 정보 딕셔너리
        """
        js = await self._request_with_token_retry(
            tr_id=constants.DOMESTIC_PRICE_TR,
            url=f"{constants.BASE}{constants.DOMESTIC_PRICE_URL}",
            params={
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": code.zfill(6),
            },
            api_name="fetch_fundamental_info",
        )
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

    # ── Daily Chart ──

    @staticmethod
    def _build_domestic_daily_frame(rows: list[dict], n: int) -> pd.DataFrame:
        """국내주식 일봉 원시 rows를 OHLCV DataFrame으로 변환."""
        if not rows:
            return _empty_day_frame()

        df = (
            pd.DataFrame(rows)
            .rename(
                columns={
                    "stck_bsop_date": "date",
                    "stck_oprc": "open",
                    "stck_hgpr": "high",
                    "stck_lwpr": "low",
                    "stck_clpr": "close",
                    "acml_vol": "volume",
                    "acml_tr_pbmn": "value",
                }
            )
            .astype(
                {
                    "date": "int",
                    "open": "float",
                    "high": "float",
                    "low": "float",
                    "close": "float",
                    "volume": "int",
                    "value": "int",
                },
                errors="ignore",
            )
            .assign(date=lambda d: pd.to_datetime(d["date"], format="%Y%m%d"))
            .drop_duplicates(subset=["date"], keep="first")
            .sort_values("date")
            .tail(n)  # 요청한 개수만
            .reset_index(drop=True)
        )
        return df

    async def inquire_daily_itemchartprice(
        self,
        code: str,
        market: str = "J",
        n: int = 200,
        adj: bool = True,
        period: str = "D",
        end_date: datetime.date | None = None,
        per_call_days: int = 150,
    ) -> pd.DataFrame:
        """
        KIS 일봉/주봉/월봉을 여러 번 호출해 최근 n개 OHLCV만 반환
        컬럼: date, open, high, low, close, volume, value
        """
        n = normalize_daily_chart_lookback(n)
        end = end_date or datetime.date.today()
        rows: list[dict] = []

        while len(rows) < n:
            start = end - datetime.timedelta(days=per_call_days)
            params = {
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": code.zfill(6),
                "FID_PERIOD_DIV_CODE": period,
                "FID_ORG_ADJ_PRC": "0" if adj else "1",
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
            }

            js = await self._request_with_token_retry(
                tr_id=constants.DOMESTIC_DAILY_CHART_TR,
                url=f"{constants.BASE}{constants.DOMESTIC_DAILY_CHART_URL}",
                params=params,
                api_name="inquire_daily_itemchartprice",
            )

            chunk = js.get("output2") or js.get("output") or []
            if not chunk:
                break

            _validate_daily_itemchartprice_chunk(chunk)

            rows.extend(chunk)

            oldest_str = min(str(c["stck_bsop_date"]) for c in chunk)
            try:
                oldest = datetime.datetime.strptime(oldest_str, "%Y%m%d").date()
            except ValueError as exc:
                raise RuntimeError(
                    "Malformed KIS daily chart payload: invalid stck_bsop_date format"
                ) from exc
            end = oldest - datetime.timedelta(days=1)

        return self._build_domestic_daily_frame(rows, n)

    # ── Intraday / Minute Chart ──

    async def inquire_time_dailychartprice(
        self,
        code: str,
        market: str = "J",
        n: int = 200,
        end_date: datetime.date | None = None,
        end_time: str | None = None,
    ) -> pd.DataFrame:
        base_date = end_date or datetime.date.today()
        current_time = end_time or datetime.datetime.now().strftime("%H%M%S")
        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),
            "FID_INPUT_HOUR_1": current_time,
            "FID_INPUT_DATE_1": base_date.strftime("%Y%m%d"),
            "FID_PW_DATA_INCU_YN": "N",
            "FID_FAKE_TICK_INCU_YN": "",
            "FID_ETC_CLS_CODE": "",
        }

        js = await self._request_with_token_retry(
            tr_id=constants.DOMESTIC_TIME_DAILY_CHART_TR,
            url=f"{constants.BASE}{constants.DOMESTIC_TIME_DAILY_CHART_URL}",
            params=params,
            api_name="inquire_time_dailychartprice",
        )

        rows = js.get("output2") or js.get("output") or []
        if not rows:
            return pd.DataFrame(
                columns=[
                    "datetime",
                    "date",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ]
            )

        return self._build_ohlcv_dataframe(
            rows=rows,
            column_mapping={
                "stck_bsop_date": "date",
                "stck_cntg_hour": "time",
                "stck_oprc": "open",
                "stck_hgpr": "high",
                "stck_lwpr": "low",
                "stck_prpr": "close",
                "cntg_vol": "volume",
                "acml_tr_pbmn": "value",
            },
            datetime_format="%Y%m%d%H%M%S",
            limit=n,
        )

    async def inquire_minute_chart(
        self,
        code: str,
        market: str = "J",
        time_unit: int = 1,
        n: int = 200,
        end_date: datetime.date | None = None,
    ) -> pd.DataFrame:
        """
        KIS 분봉 데이터 조회
        컬럼: datetime, date, time, open, high, low, close, volume, value
        """
        # KIS 분봉 API는 time_unit 파라미터를 제대로 인식하지 못하는 알려진 이슈가 있음
        current_time = datetime.datetime.now().strftime("%H%M%S")

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),
            "FID_INPUT_HOUR_1": current_time,
            "FID_INPUT_DATE_1": (end_date or datetime.date.today()).strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": (end_date or datetime.date.today()).strftime("%Y%m%d"),
            "FID_INPUT_TIME_1": "01",
            "FID_INPUT_TIME_2": "01",
            "FID_PW_DATA_INCU_YN": "N",
            "FID_ETC_CLS_CODE": "",
        }

        js = await self._request_with_token_retry(
            tr_id=constants.DOMESTIC_MINUTE_CHART_TR,
            url=f"{constants.BASE}{constants.DOMESTIC_MINUTE_CHART_URL}",
            params=params,
            api_name="inquire_minute_chart",
        )

        logging.info(f"KIS 분봉 API 응답: {js}")

        rows = js.get("output2") or js.get("output") or []

        if not rows:
            logging.warning(f"KIS 분봉 API에서 데이터를 찾을 수 없음: {js}")
            return pd.DataFrame(
                columns=[
                    "datetime",
                    "date",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ]
            )

        logging.info(f"KIS 분봉 API에서 {len(rows)}개 데이터 수집 성공")

        return self._build_ohlcv_dataframe(
            rows=rows,
            column_mapping={
                "stck_bsop_date": "date",
                "stck_cntg_hour": "time",
                "stck_oprc": "open",
                "stck_hgpr": "high",
                "stck_lwpr": "low",
                "stck_prpr": "close",
                "cntg_vol": "volume",
                "acml_tr_pbmn": "value",
            },
            datetime_format="%Y%m%d%H%M%S",
            limit=n,
        )

    # ── Minute Candles Aggregation ──

    async def fetch_minute_candles(
        self,
        code: str,
        market: str = "J",
        end_date: datetime.date | None = None,
    ) -> dict:
        """분봉 데이터를 가져와서 60분, 5분, 1분 캔들로 반환."""
        minute_candles = {}

        try:
            logging.info(f"분봉 데이터 수집 시작: {code}")

            df_1min = await self.inquire_minute_chart(
                code, market, time_unit=1, n=200, end_date=end_date
            )

            if not df_1min.empty:
                logging.info(f"1분봉 {len(df_1min)}개 수집 완료")

                minute_candles["1min"] = df_1min

                df_5min = self._aggregate_minute_candles(df_1min, 5)
                minute_candles["5min"] = df_5min

                df_60min = self._aggregate_minute_candles(
                    df_1min,
                    60,
                    include_partial=True,
                )
                minute_candles["60min"] = df_60min

                logging.info(
                    f"집계 완료 - 1분봉: {len(df_1min)}개, 5분봉: {len(df_5min)}개, 60분봉: {len(df_60min)}개"
                )

            else:
                empty_df = _empty_minute_frame()
                minute_candles = {"60min": empty_df, "5min": empty_df, "1min": empty_df}
                logging.warning("수집된 데이터가 없습니다")

        except Exception as e:
            logging.warning(f"분봉 데이터 수집 실패 ({code}): {e}")
            empty_df = _empty_minute_frame()
            minute_candles = {"60min": empty_df, "5min": empty_df, "1min": empty_df}

        return minute_candles

    def _aggregate_minute_candles(
        self,
        df_1min: pd.DataFrame,
        time_unit: int,
        *,
        include_partial: bool = False,
    ) -> pd.DataFrame:
        """1분봉 데이터를 지정된 시간 단위로 집계."""
        return _aggregate_minute_candles_frame(
            df_1min,
            time_unit,
            include_partial=include_partial,
        )
