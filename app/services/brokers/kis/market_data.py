# pyright: reportAttributeAccessIssue=false, reportImplicitStringConcatenation=false, reportMissingTypeArgument=false, reportUnnecessaryIsInstance=false, reportUnreachable=false
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any, cast

import pandas as pd
from pandas import DataFrame

from app.core.symbol import to_kis_symbol

from . import constants

_MINUTE_FRAME_COLUMNS = [
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


def _empty_minute_frame() -> pd.DataFrame:
    return pd.DataFrame(columns=_MINUTE_FRAME_COLUMNS)


@dataclass(slots=True)
class OverseasMinuteChartPage:
    frame: pd.DataFrame
    has_more: bool
    next_keyb: str | None = None


def _empty_overseas_minute_chart_page() -> OverseasMinuteChartPage:
    return OverseasMinuteChartPage(frame=_empty_minute_frame(), has_more=False)


def _aggregate_minute_candles_frame(
    df_1min: pd.DataFrame,
    time_unit: int,
    *,
    include_partial: bool = False,
) -> pd.DataFrame:
    if df_1min.empty:
        return _empty_minute_frame()

    grouped_minutes = df_1min.copy().sort_values("datetime")
    grouped_minutes["time_group"] = grouped_minutes["datetime"].dt.floor(
        f"{time_unit}min"
    )

    aggregated = (
        grouped_minutes.groupby("time_group")
        .agg(
            {
                "open": "first",
                "high": "max",
                "low": "min",
                "close": "last",
                "volume": "sum",
                "value": "sum",
            }
        )
        .reset_index()
    )

    if not include_partial:
        group_sizes = grouped_minutes.groupby("time_group").size()
        aggregated = aggregated[
            aggregated["time_group"].map(group_sizes).ge(time_unit).fillna(False)
        ]

    if aggregated.empty:
        return _empty_minute_frame()

    aggregated = aggregated.rename(columns={"time_group": "datetime"})
    aggregated["date"] = aggregated["datetime"].dt.date
    aggregated["time"] = aggregated["datetime"].dt.time
    return cast(DataFrame, aggregated.loc[:, _MINUTE_FRAME_COLUMNS].copy())


if TYPE_CHECKING:
    from .protocols import KISClientProtocol


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


def _validate_overseas_minute_chart_chunk(chunk: Any) -> list[dict[str, Any]]:
    if not isinstance(chunk, list):
        raise RuntimeError(
            "Malformed KIS overseas minute chart payload: expected list in output2/output"
        )

    validated: list[dict[str, Any]] = []
    for index, row in enumerate(chunk):
        if not isinstance(row, dict):
            raise RuntimeError(
                f"Malformed KIS overseas minute chart payload at row {index}: expected object"
            )

        close_value = row.get("last")
        if close_value in (None, ""):
            close_value = row.get("clos")

        volume_value = row.get("evol")
        if volume_value in (None, ""):
            volume_value = row.get("tvol")

        value_value = row.get("eamt")
        if value_value in (None, ""):
            value_value = row.get("tamt")

        missing = []
        for field in ("xymd", "xhms", "open", "high", "low"):
            if row.get(field) in (None, ""):
                missing.append(field)
        if close_value in (None, ""):
            missing.append("last/clos")
        if volume_value in (None, ""):
            missing.append("evol")
        if value_value in (None, ""):
            missing.append("eamt")

        if missing:
            missing_fields = ", ".join(missing)
            raise RuntimeError(
                f"Malformed KIS overseas minute chart payload at row {index}: missing {missing_fields}"
            )

        normalized_row = dict(row)
        for numeric_field, numeric_value, parser in (
            ("open", row["open"], float),
            ("high", row["high"], float),
            ("low", row["low"], float),
            ("close", close_value, float),
            ("volume", volume_value, int),
            ("value", value_value, int),
        ):
            try:
                normalized_row[numeric_field] = parser(str(numeric_value).strip())
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    "Malformed KIS overseas minute chart payload at row "
                    f"{index}: invalid numeric field {numeric_field}"
                ) from exc

        validated.append(normalized_row)

    return validated


def _has_overseas_minute_pagination(output1: Any) -> bool:
    if not isinstance(output1, dict):
        return False

    truthy_values = {"Y", "1", "TRUE"}
    next_flag = str(output1.get("next", "")).strip().upper()
    more_flag = str(output1.get("more", "")).strip().upper()
    return next_flag in truthy_values or more_flag in truthy_values


class MarketDataClient:
    """Client for KIS market data operations.

    Handles price data, charts, orderbook, and ranking information.
    """

    def __init__(self, parent: KISClientProtocol) -> None:
        self._parent = parent

    @property
    def _settings(self):
        return self._parent._settings

    async def volume_rank(self, market: str = "J", limit: int = 30) -> list[dict]:
        await self._parent._ensure_token()
        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": constants.DOMESTIC_VOLUME_TR,
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

        js = await self._parent._request_with_rate_limit(
            "GET",
            f"{constants.BASE}{constants.DOMESTIC_VOLUME_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="volume_rank",
            tr_id=constants.DOMESTIC_VOLUME_TR,
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
        if js["msg_cd"] == "EGW00123":
            await self._parent._token_manager.clear_token()
            await self._parent._ensure_token()
            return await self.volume_rank(market, limit)
        elif js["msg_cd"] == "EGW00121":
            await self._parent._token_manager.clear_token()
            await self._parent._ensure_token()
            return await self.volume_rank(market, limit)
        raise RuntimeError(
            js.get("msg1") or f"KIS API error (msg_cd={js.get('msg_cd', 'unknown')})"
        )

    async def market_cap_rank(self, market: str = "J", limit: int = 30) -> list[dict]:
        await self._parent._ensure_token()
        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": constants.MARKET_CAP_RANK_TR,
        }
        js = await self._parent._request_with_rate_limit(
            "GET",
            f"{constants.BASE}{constants.MARKET_CAP_RANK_URL}",
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
            tr_id=constants.MARKET_CAP_RANK_TR,
        )
        if js["rt_cd"] == "0":
            return js["output"][:limit]
        if js["msg_cd"] == "EGW00123":
            await self._parent._token_manager.clear_token()
            await self._parent._ensure_token()
            return await self.market_cap_rank(market, limit)
        elif js["msg_cd"] == "EGW00121":
            await self._parent._token_manager.clear_token()
            await self._parent._ensure_token()
            return await self.market_cap_rank(market, limit)
        raise RuntimeError(
            js.get("msg1") or f"KIS API error (msg_cd={js.get('msg_cd', 'unknown')})"
        )

    async def fluctuation_rank(
        self, market: str = "J", direction: str = "up", limit: int = 30
    ) -> list[dict]:
        await self._parent._ensure_token()
        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": constants.FLUCTUATION_RANK_TR,
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

        js = await self._parent._request_with_rate_limit(
            "GET",
            f"{constants.BASE}{constants.FLUCTUATION_RANK_URL}",
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
            tr_id=constants.FLUCTUATION_RANK_TR,
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

        if js["msg_cd"] in ("EGW00123", "EGW00121"):
            await self._parent._token_manager.clear_token()
            await self._parent._ensure_token()
            return await self.fluctuation_rank(market, direction, limit)

        raise RuntimeError(
            js.get("msg1") or f"KIS API error (msg_cd={js.get('msg_cd', 'unknown')})"
        )

    async def foreign_buying_rank(
        self, market: str = "J", limit: int = 30
    ) -> list[dict]:
        await self._parent._ensure_token()
        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": constants.FOREIGN_BUYING_RANK_TR,
        }
        js = await self._parent._request_with_rate_limit(
            "GET",
            f"{constants.BASE}{constants.FOREIGN_BUYING_RANK_URL}",
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
            tr_id=constants.FOREIGN_BUYING_RANK_TR,
        )
        if js["rt_cd"] == "0":
            return js["output"][:limit]
        if js["msg_cd"] == "EGW00123":
            await self._parent._token_manager.clear_token()
            await self._parent._ensure_token()
            return await self.foreign_buying_rank(market, limit)
        elif js["msg_cd"] == "EGW00121":
            await self._parent._token_manager.clear_token()
            await self._parent._ensure_token()
            return await self.foreign_buying_rank(market, limit)
        raise RuntimeError(
            js.get("msg1") or f"KIS API error (msg_cd={js.get('msg_cd', 'unknown')})"
        )

    async def inquire_price(self, code: str, market: str = "UN") -> DataFrame:
        """
        단일 종목 현재가·기본정보 조회
        :param code: 6자리 종목코드(005930)
        :param market: K(코스피)/Q(코스닥)/UN(통합)
        :return: API output 딕셔너리
        """
        await self._parent._ensure_token()

        # 요청 헤더
        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": constants.DOMESTIC_PRICE_TR,
        }

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),  # 000000 형태도 OK
        }

        js = await self._parent._request_with_rate_limit(
            "GET",
            f"{constants.BASE}{constants.DOMESTIC_PRICE_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="inquire_price",
            tr_id=constants.DOMESTIC_PRICE_TR,
        )
        if js["rt_cd"] != "0":
            if js.get("msg_cd") in [
                "EGW00123",
                "EGW00121",
            ]:  # 토큰 만료 또는 유효하지 않은 토큰
                # Redis에서 토큰 삭제 후 새로 발급
                await self._parent._token_manager.clear_token()
                await self._parent._ensure_token()
                # 재시도 1회
                return await self.inquire_price(code, market)
            raise RuntimeError(f"{js['msg_cd']} {js['msg1']}")
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

    async def _request_orderbook_snapshot(self, code: str, market: str = "UN") -> dict:
        await self._parent._ensure_token()

        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": constants.DOMESTIC_ORDERBOOK_TR,
        }

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),
        }

        js = await self._parent._request_with_rate_limit(
            "GET",
            f"{constants.BASE}{constants.DOMESTIC_ORDERBOOK_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="inquire_orderbook",
            tr_id=constants.DOMESTIC_ORDERBOOK_TR,
        )
        if js["rt_cd"] != "0":
            if js.get("msg_cd") in [
                "EGW00123",
                "EGW00121",
            ]:
                await self._parent._token_manager.clear_token()
                await self._parent._ensure_token()
                return await self._request_orderbook_snapshot(code, market)
            raise RuntimeError(f"{js['msg_cd']} {js['msg1']}")
        return js

    async def inquire_orderbook(self, code: str, market: str = "UN") -> dict:
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
        market: str = "UN",
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

        for attempt in range(2):
            await self._parent._ensure_token()
            hdr = self._parent._hdr_base | {
                "authorization": f"Bearer {self._settings.kis_access_token}",
                "tr_id": constants.DOMESTIC_SHORT_SELLING_TR,
            }
            js = await self._parent._request_with_rate_limit(
                "GET",
                f"{constants.BASE}{constants.DOMESTIC_SHORT_SELLING_URL}",
                headers=hdr,
                params=params,
                timeout=5,
                api_name="inquire_short_selling",
                tr_id=constants.DOMESTIC_SHORT_SELLING_TR,
            )

            if js.get("rt_cd") == "0":
                break

            if attempt == 0 and js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                await self._parent._token_manager.clear_token()
                continue

            raise RuntimeError(f"{js.get('msg_cd')} {js.get('msg1')}")
        else:
            raise RuntimeError("Failed to fetch KIS daily short selling data")

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

    async def fetch_fundamental_info(self, code: str, market: str = "UN") -> dict:
        """
        종목의 기본 정보를 가져와 딕셔너리로 반환합니다.
        :param code: 6자리 종목코드(005930)
        :param market: K(코스피)/Q(코스닥)/UN(통합)
        :return: 기본 정보 딕셔너리
        """
        await self._parent._ensure_token()

        # 요청 헤더
        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": constants.DOMESTIC_PRICE_TR,
        }

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),  # 000000 형태도 OK
        }

        js = await self._parent._request_with_rate_limit(
            "GET",
            f"{constants.BASE}{constants.DOMESTIC_PRICE_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="fetch_fundamental_info",
            tr_id=constants.DOMESTIC_PRICE_TR,
        )
        if js["rt_cd"] != "0":
            if js.get("msg_cd") in [
                "EGW00123",
                "EGW00121",
            ]:  # 토큰 만료 또는 유효하지 않은 토큰
                # Redis에서 토큰 삭제 후 새로 발급
                await self._parent._token_manager.clear_token()
                await self._parent._ensure_token()
                # 재시도 1회
                return await self.fetch_fundamental_info(code, market)
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

    async def inquire_daily_itemchartprice(
        self,
        code: str,
        market: str = "UN",
        n: int = 200,  # 최종 확보하고 싶은 캔들 수
        adj: bool = True,
        period: str = "D",  # D/W/M (일/주/월봉)
        end_date: datetime.date | None = None,  # None이면 오늘까지
        per_call_days: int = 150,  # 한 번 호출 시 조회 날짜 폭
    ) -> pd.DataFrame:
        """
        ✅ KIS 일봉/주봉/월봉을 여러 번 호출해 최근 n개 OHLCV만 반환
           (이평 같은 지표 계산은 외부에서!)
        컬럼: date • open • high • low • close • volume • value
        """
        n = normalize_daily_chart_lookback(n)
        await self._parent._ensure_token()
        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": constants.DOMESTIC_DAILY_CHART_TR,
        }

        end = end_date or datetime.date.today()
        rows: list[dict] = []

        while len(rows) < n:
            start = end - datetime.timedelta(days=per_call_days)
            params = {
                "FID_COND_MRKT_DIV_CODE": market,
                "FID_INPUT_ISCD": code.zfill(6),
                "FID_PERIOD_DIV_CODE": period,  # 'D'|'W'|'M'
                "FID_ORG_ADJ_PRC": "0" if adj else "1",  # 0=수정, 1=원본
                "FID_INPUT_DATE_1": start.strftime("%Y%m%d"),
                "FID_INPUT_DATE_2": end.strftime("%Y%m%d"),
            }

            js = await self._parent._request_with_rate_limit(
                "GET",
                f"{constants.BASE}{constants.DOMESTIC_DAILY_CHART_URL}",
                headers=hdr,
                params=params,
                timeout=5,
                api_name="inquire_daily_itemchartprice",
                tr_id=constants.DOMESTIC_DAILY_CHART_TR,
            )

            if js.get("rt_cd") != "0":
                if js.get("msg_cd") in [
                    "EGW00123",
                    "EGW00121",
                ]:  # 토큰 만료 또는 유효하지 않은 토큰
                    # Redis에서 토큰 삭제 후 새로 발급
                    await self._parent._token_manager.clear_token()
                    await self._parent._ensure_token()
                    continue
                raise RuntimeError(f"{js.get('msg_cd')} {js.get('msg1')}")

            chunk = js.get("output2") or js.get("output") or []
            if not chunk:
                break  # 더 과거 없음

            _validate_daily_itemchartprice_chunk(chunk)

            rows.extend(chunk)

            # 다음 루프에서 더 과거로
            oldest_str = min(str(c["stck_bsop_date"]) for c in chunk)
            try:
                oldest = datetime.datetime.strptime(oldest_str, "%Y%m%d").date()
            except ValueError as exc:
                raise RuntimeError(
                    "Malformed KIS daily chart payload: invalid stck_bsop_date format"
                ) from exc
            end = oldest - datetime.timedelta(days=1)

        # ---- DataFrame 변환 (지표 계산 없음) ----
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

    async def inquire_time_dailychartprice(
        self,
        code: str,
        market: str = "UN",
        n: int = 200,
        end_date: datetime.date | None = None,
        end_time: str | None = None,
    ) -> pd.DataFrame:
        await self._parent._ensure_token()

        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": constants.DOMESTIC_TIME_DAILY_CHART_TR,
        }

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

        js = await self._parent._request_with_rate_limit(
            "GET",
            f"{constants.BASE}{constants.DOMESTIC_TIME_DAILY_CHART_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="inquire_time_dailychartprice",
            tr_id=constants.DOMESTIC_TIME_DAILY_CHART_TR,
        )

        rows = js.get("output2") or js.get("output") or []
        if not rows:
            if js.get("rt_cd") != "0":
                raise RuntimeError(f"{js.get('msg_cd')} {js.get('msg1')}")
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

        frame = (
            pd.DataFrame(rows)
            .rename(
                columns={
                    "stck_bsop_date": "date",
                    "stck_cntg_hour": "time",
                    "stck_oprc": "open",
                    "stck_hgpr": "high",
                    "stck_lwpr": "low",
                    "stck_prpr": "close",
                    "cntg_vol": "volume",
                    "acml_tr_pbmn": "value",
                }
            )
            .astype(
                {
                    "date": "str",
                    "time": "str",
                    "open": "float",
                    "high": "float",
                    "low": "float",
                    "close": "float",
                    "volume": "int",
                    "value": "int",
                },
                errors="ignore",
            )
            .assign(
                datetime=lambda d: pd.to_datetime(
                    d["date"] + d["time"],
                    format="%Y%m%d%H%M%S",
                    errors="coerce",
                )
            )
            .dropna(subset=["datetime"])
            .assign(
                date=lambda d: pd.to_datetime(d["datetime"]).dt.date,
                time=lambda d: pd.to_datetime(d["datetime"]).dt.time,
            )
            .loc[
                :,
                [
                    "datetime",
                    "date",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ],
            ]
            .drop_duplicates(subset=["datetime"], keep="first")
            .sort_values("datetime")
            .tail(max(int(n), 1))
            .reset_index(drop=True)
        )
        return frame

    async def inquire_minute_chart(
        self,
        code: str,
        market: str = "UN",
        time_unit: int = 1,  # 1: 1분, 3: 3분, 5: 5분, 10: 10분, 15: 15분, 30: 30분, 45: 45분, 60: 60분
        n: int = 200,  # 최종 확보하고 싶은 캔들 수
        end_date: datetime.date | None = None,  # None이면 오늘까지
    ) -> pd.DataFrame:
        """
        KIS 분봉 데이터 조회
        컬럼: datetime, date, time, open, high, low, close, volume, value

        Parameters
        ----------
        code : str
            6자리 종목코드 (예: "005930")
        market : str, default "UN"
            시장 구분 (UN: 통합, K: 코스피, Q: 코스닥)
        time_unit : int, default 1
            분봉 단위 (1, 3, 5, 10, 15, 30, 45, 60)
        n : int, default 200
            가져올 캔들 수 (최대 200)
        end_date : datetime.date, optional
            종료 날짜 (None이면 오늘까지)
        """
        await self._parent._ensure_token()

        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": constants.DOMESTIC_MINUTE_CHART_TR,
        }

        # KIS 분봉 API는 time_unit 파라미터를 제대로 인식하지 못하는 문제가 있음
        # 현재로서는 모든 시간대에서 동일한 데이터가 반환됨
        # 향후 API 문서 업데이트나 기술지원을 통해 해결 필요

        # 현재 시간을 시분초로 설정 (장 시간 내에만 작동)
        current_time = datetime.datetime.now().strftime("%H%M%S")

        params = {
            "FID_COND_MRKT_DIV_CODE": market,
            "FID_INPUT_ISCD": code.zfill(6),
            "FID_INPUT_HOUR_1": current_time,  # 현재 시분초
            "FID_INPUT_DATE_1": (end_date or datetime.date.today()).strftime("%Y%m%d"),
            "FID_INPUT_DATE_2": (end_date or datetime.date.today()).strftime("%Y%m%d"),
            "FID_INPUT_TIME_1": "01",  # 1분봉으로 고정 (API가 time_unit을 지원하지 않음)
            "FID_INPUT_TIME_2": "01",  # 1분봉으로 고정
            "FID_PW_DATA_INCU_YN": "N",
            "FID_ETC_CLS_CODE": "",
        }

        js = await self._parent._request_with_rate_limit(
            "GET",
            f"{constants.BASE}{constants.DOMESTIC_MINUTE_CHART_URL}",
            headers=hdr,
            params=params,
            timeout=5,
            api_name="inquire_minute_chart",
            tr_id=constants.DOMESTIC_MINUTE_CHART_TR,
        )

        # 디버깅을 위한 로깅 추가
        logging.info(f"KIS 분봉 API 응답: {js}")

        # rt_cd가 비어있어도 output2에 데이터가 있을 수 있음
        rows = js.get("output2") or js.get("output") or []

        if not rows:
            # 데이터가 없는 경우 빈 DataFrame 반환
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

        # 데이터가 있으면 성공으로 처리 (rt_cd가 비어있어도)
        logging.info(f"KIS 분봉 API에서 {len(rows)}개 데이터 수집 성공")

        # DataFrame 변환
        df = (
            pd.DataFrame(rows)
            .rename(
                columns={
                    "stck_bsop_date": "date",
                    "stck_cntg_hour": "time",
                    "stck_oprc": "open",
                    "stck_hgpr": "high",
                    "stck_lwpr": "low",
                    "stck_prpr": "close",
                    "cntg_vol": "volume",
                    "acml_tr_pbmn": "value",
                }
            )
            .astype(
                {
                    "date": "str",
                    "time": "str",
                    "open": "float",
                    "high": "float",
                    "low": "float",
                    "close": "float",
                    "volume": "int",
                    "value": "int",
                },
                errors="ignore",
            )
            .assign(
                # 날짜와 시간을 결합하여 datetime 생성
                datetime=lambda d: pd.to_datetime(
                    d["date"] + d["time"], format="%Y%m%d%H%M%S"
                ),
                date=lambda d: pd.to_datetime(d["datetime"]).dt.date,
                time=lambda d: pd.to_datetime(d["datetime"]).dt.time,
            )
            .loc[
                :,
                [
                    "datetime",
                    "date",
                    "time",
                    "open",
                    "high",
                    "low",
                    "close",
                    "volume",
                    "value",
                ],
            ]
            .drop_duplicates(subset=["datetime"], keep="first")
            .sort_values("datetime")
            .tail(n)  # 요청한 개수만
            .reset_index(drop=True)
        )

        return df

    async def fetch_minute_candles(
        self,
        code: str,
        market: str = "UN",
        end_date: datetime.date | None = None,
    ) -> dict:
        """
        분봉 데이터를 가져와서 60분, 5분, 1분 캔들로 반환

        Args:
            code: 종목코드
            market: 시장 구분 (UN: 통합)
            end_date: 종료 날짜 (None이면 오늘)

        Returns:
            분봉 캔들 데이터 딕셔너리
        """
        minute_candles = {}

        try:
            logging.info(f"분봉 데이터 수집 시작: {code}")

            # 단일 요청으로 200개 1분봉 수집
            df_1min = await self.inquire_minute_chart(
                code, market, time_unit=1, n=200, end_date=end_date
            )

            if not df_1min.empty:
                logging.info(f"1분봉 {len(df_1min)}개 수집 완료")

                minute_candles["1min"] = df_1min

                # 1분봉 데이터를 5분봉으로 가공
                df_5min = self._aggregate_minute_candles(df_1min, 5)
                minute_candles["5min"] = df_5min

                # 1분봉 데이터를 60분봉으로 가공
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
                # 데이터가 없는 경우 빈 DataFrame으로 설정
                empty_df = _empty_minute_frame()
                minute_candles = {"60min": empty_df, "5min": empty_df, "1min": empty_df}
                logging.warning("수집된 데이터가 없습니다")

        except Exception as e:
            logging.warning(f"분봉 데이터 수집 실패 ({code}): {e}")
            # 실패한 경우 빈 DataFrame으로 설정
            empty_df = _empty_minute_frame()
            minute_candles = {"60min": empty_df, "5min": empty_df, "1min": empty_df}

        return minute_candles

    async def inquire_overseas_daily_price(
        self,
        symbol: str,
        exchange_code: str = "NASD",
        n: int = 200,
        period: str = "D",  # D/W/M
    ) -> pd.DataFrame:
        """
        해외주식 일봉/주봉/월봉 조회 (국내주식처럼 충분한 데이터 확보)

        Args:
            symbol: 종목 심볼 (예: "AAPL")
            exchange_code: 거래소 코드 (NASD/NYSE/AMEX 등)
            n: 조회할 캔들 수 (최소 200개 권장, 이동평균선 계산용)
            period: D(일봉)/W(주봉)/M(월봉)

        Returns:
            DataFrame with columns: date, open, high, low, close, volume
        """
        await self._parent._ensure_token()

        # KIS API는 거래소 코드를 3자리로 사용: NASD -> NAS, NYSE -> NYS, AMEX -> AMS
        excd_map = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
        excd = excd_map.get(exchange_code, exchange_code[:3])

        hdr = self._parent._hdr_base | {
            "authorization": f"Bearer {self._settings.kis_access_token}",
            "tr_id": constants.OVERSEAS_DAILY_CHART_TR,
        }

        rows: list[dict] = []
        max_iterations = 5  # 최대 5번 반복 (충분한 데이터 확보)
        iteration = 0

        # 국내주식처럼 충분한 데이터를 확보할 때까지 반복 조회
        while len(rows) < n and iteration < max_iterations:
            # BYMD 파라미터: 빈 값이면 최근, 날짜를 지정하면 해당 날짜부터 과거로
            if rows:
                # 이전에 가져온 데이터의 가장 오래된 날짜 찾기
                oldest_date = min(r.get("xymd", "99999999") for r in rows)
                # 하루 전으로 설정
                try:
                    oldest_dt = datetime.datetime.strptime(oldest_date, "%Y%m%d")
                    bymd = (oldest_dt - datetime.timedelta(days=1)).strftime("%Y%m%d")
                except Exception:
                    bymd = ""
            else:
                bymd = ""  # 첫 요청은 최신 데이터부터

            params = {
                "AUTH": "",
                "EXCD": excd,  # 거래소코드 (3자리)
                "SYMB": to_kis_symbol(symbol),  # 심볼 (DB형식 . -> KIS형식 /)
                "GUBN": {"D": "0", "W": "1", "M": "2"}.get(period.upper(), "0"),
                "BYMD": bymd,  # 조회기준일자
                "MODP": "1",  # 0:수정주가 미반영, 1:수정주가 반영
            }

            logging.info(
                f"해외주식 일봉 조회 요청 (반복 {iteration + 1}/{max_iterations}) - symbol: {symbol}, exchange: {excd}, bymd: {bymd}"
            )

            js = await self._parent._request_with_rate_limit(
                "GET",
                f"{constants.BASE}{constants.OVERSEAS_DAILY_CHART_URL}",
                headers=hdr,
                params=params,
                timeout=10,
                api_name="inquire_overseas_daily_price",
                tr_id=constants.OVERSEAS_DAILY_CHART_TR,
            )

            if js.get("rt_cd") != "0":
                if js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                    await self._parent._token_manager.clear_token()
                    await self._parent._ensure_token()
                    continue
                raise RuntimeError(f"{js.get('msg_cd')} {js.get('msg1')}")

            chunk = js.get("output2") or js.get("output") or []
            if not chunk:
                logging.info(
                    f"더 이상 과거 데이터가 없음. 현재까지 수집: {len(rows)}개"
                )
                break

            rows.extend(chunk)
            iteration += 1
            logging.info(f"누적 데이터: {len(rows)}개 / 목표: {n}개")

        if not rows:
            return pd.DataFrame(
                columns=["date", "open", "high", "low", "close", "volume"]
            )

        df = (
            pd.DataFrame(rows)
            .rename(
                columns={
                    "xymd": "date",
                    "open": "open",
                    "high": "high",
                    "low": "low",
                    "clos": "close",
                    "tvol": "volume",
                }
            )
            .astype(
                {
                    "date": "str",
                    "open": "float",
                    "high": "float",
                    "low": "float",
                    "close": "float",
                    "volume": "int",
                },
                errors="ignore",
            )
            .assign(date=lambda d: pd.to_datetime(d["date"], format="%Y%m%d"))
            .drop_duplicates(subset=["date"], keep="first")
            .sort_values("date")
            .tail(n)
            .reset_index(drop=True)
        )
        logging.info(f"해외주식 일봉 조회 완료: {len(df)}개 데이터 반환")
        return df

    async def inquire_overseas_minute_chart(
        self,
        symbol: str,
        exchange_code: str = "NASD",
        n: int = 120,
        keyb: str = "",
    ) -> OverseasMinuteChartPage:
        await self._parent._ensure_token()

        excd = constants.OVERSEAS_EXCHANGE_MAP.get(exchange_code, exchange_code[:3])
        requested_rows = min(max(int(n), 1), 120)
        next_flag = "1" if keyb else ""

        params = {
            "AUTH": "",
            "EXCD": excd,
            "SYMB": to_kis_symbol(symbol),
            "NMIN": "1",
            "PINC": "1",
            "NEXT": next_flag,
            "NREC": str(requested_rows),
            "FILL": "",
            "KEYB": keyb,
        }

        for attempt in range(2):
            hdr = self._parent._hdr_base | {
                "authorization": f"Bearer {self._settings.kis_access_token}",
                "tr_id": constants.OVERSEAS_MINUTE_CHART_TR,
            }
            js = await self._parent._request_with_rate_limit(
                "GET",
                f"{constants.BASE}{constants.OVERSEAS_MINUTE_CHART_URL}",
                headers=hdr,
                params=params,
                timeout=10,
                api_name="inquire_overseas_minute_chart",
                tr_id=constants.OVERSEAS_MINUTE_CHART_TR,
            )

            if js.get("rt_cd") == "0":
                break

            if attempt == 0 and js.get("msg_cd") in ["EGW00123", "EGW00121"]:
                await self._parent._token_manager.clear_token()
                await self._parent._ensure_token()
                continue

            raise RuntimeError(f"{js.get('msg_cd')} {js.get('msg1')}")
        else:
            raise RuntimeError("Failed to fetch overseas minute chart")

        chunk = js.get("output2")
        if chunk is None:
            chunk = js.get("output")
        if chunk is None or chunk == []:
            return _empty_overseas_minute_chart_page()

        validated_rows = _validate_overseas_minute_chart_chunk(chunk)
        frame = pd.DataFrame(validated_rows).rename(
            columns={
                "xymd": "date",
                "xhms": "time",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
                "value": "value",
            }
        )
        frame["date"] = frame["date"].astype("string")
        frame["time"] = frame["time"].astype("string").str.zfill(6)
        frame["datetime"] = pd.to_datetime(
            frame["date"] + frame["time"],
            format="%Y%m%d%H%M%S",
            errors="coerce",
        )
        if frame["datetime"].isna().any():
            raise RuntimeError(
                "Malformed KIS overseas minute chart payload: invalid xymd/xhms format"
            )

        frame = (
            frame.assign(
                date=lambda d: pd.to_datetime(d["datetime"]).dt.date,
                time=lambda d: pd.to_datetime(d["datetime"]).dt.time,
            )
            .loc[:, _MINUTE_FRAME_COLUMNS]
            .drop_duplicates(subset=["datetime"], keep="first")
            .sort_values("datetime")
            .reset_index(drop=True)
        )

        has_more = _has_overseas_minute_pagination(js.get("output1"))
        next_keyb = None
        if has_more:
            oldest = frame["datetime"].min() - datetime.timedelta(minutes=1)
            next_keyb = oldest.strftime("%Y%m%d%H%M%S")

        return OverseasMinuteChartPage(
            frame=cast(DataFrame, frame.copy()),
            has_more=has_more,
            next_keyb=next_keyb,
        )

    def _aggregate_minute_candles(
        self,
        df_1min: pd.DataFrame,
        time_unit: int,
        *,
        include_partial: bool = False,
    ) -> pd.DataFrame:
        """
        1분봉 데이터를 지정된 시간 단위로 집계

        Args:
            df_1min: 1분봉 DataFrame
            time_unit: 집계할 시간 단위 (분)

        Returns:
            집계된 DataFrame (완전한 시간대만)
        """
        return _aggregate_minute_candles_frame(
            df_1min,
            time_unit,
            include_partial=include_partial,
        )
