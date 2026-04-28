# pyright: reportAttributeAccessIssue=false, reportMissingTypeArgument=false
from __future__ import annotations

import datetime
import logging
from dataclasses import dataclass
from typing import Any, cast

import pandas as pd
from pandas import DataFrame

from app.core.symbol import to_kis_symbol

from . import constants
from ._base_market_data import MarketDataBase, _empty_minute_frame


@dataclass(slots=True)
class OverseasMinuteChartPage:
    frame: pd.DataFrame
    has_more: bool
    next_keyb: str | None = None


def _empty_overseas_minute_chart_page() -> OverseasMinuteChartPage:
    return OverseasMinuteChartPage(frame=_empty_minute_frame(), has_more=False)


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


class OverseasMarketDataMixin(MarketDataBase):
    """Overseas (해외) market data methods.

    Daily chart and minute chart operations for overseas stocks.
    """

    # ── Daily Chart ──

    async def _paginate_overseas_daily(
        self,
        symbol: str,
        excd: str,
        n: int,
        period: str,
    ) -> list[dict]:
        """해외주식 일봉 데이터를 페이지네이션으로 수집.

        최대 5회 반복하여 n개 이상의 원시 행을 확보한다.
        """
        rows: list[dict] = []
        max_iterations = 5
        iteration = 0

        while len(rows) < n and iteration < max_iterations:
            if rows:
                oldest_date = min(r.get("xymd", "99999999") for r in rows)
                try:
                    oldest_dt = datetime.datetime.strptime(oldest_date, "%Y%m%d")
                    bymd = (oldest_dt - datetime.timedelta(days=1)).strftime("%Y%m%d")
                except Exception:
                    bymd = ""
            else:
                bymd = ""

            params = {
                "AUTH": "",
                "EXCD": excd,
                "SYMB": to_kis_symbol(symbol),
                "GUBN": {"D": "0", "W": "1", "M": "2"}.get(period.upper(), "0"),
                "BYMD": bymd,
                "MODP": "1",
            }

            logging.info(
                f"해외주식 일봉 조회 요청 (반복 {iteration + 1}/{max_iterations}) - "
                f"symbol: {symbol}, exchange: {excd}, bymd: {bymd}"
            )

            js = await self._request_with_token_retry(
                tr_id=constants.OVERSEAS_DAILY_CHART_TR,
                url=self._kis_url(constants.OVERSEAS_DAILY_CHART_URL),
                params=params,
                timeout=10,
                api_name="inquire_overseas_daily_price",
            )

            chunk = js.get("output2") or js.get("output") or []
            if not chunk:
                logging.info(
                    f"더 이상 과거 데이터가 없음. 현재까지 수집: {len(rows)}개"
                )
                break

            rows.extend(chunk)
            iteration += 1
            logging.info(f"누적 데이터: {len(rows)}개 / 목표: {n}개")

        return rows

    @staticmethod
    def _build_overseas_daily_frame(rows: list[dict], n: int) -> pd.DataFrame:
        """해외주식 일봉 원시 rows를 OHLCV DataFrame으로 변환."""
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

    async def inquire_overseas_daily_price(
        self,
        symbol: str,
        exchange_code: str = "NASD",
        n: int = 200,
        period: str = "D",
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
        excd_map = {"NASD": "NAS", "NYSE": "NYS", "AMEX": "AMS"}
        excd = excd_map.get(exchange_code, exchange_code[:3])
        rows = await self._paginate_overseas_daily(symbol, excd, n, period)
        return self._build_overseas_daily_frame(rows, n)

    # ── Minute Chart ──

    async def inquire_overseas_minute_chart(
        self,
        symbol: str,
        exchange_code: str = "NASD",
        n: int = 120,
        keyb: str = "",
    ) -> OverseasMinuteChartPage:
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

        js = await self._request_with_token_retry(
            tr_id=constants.OVERSEAS_MINUTE_CHART_TR,
            url=self._kis_url(constants.OVERSEAS_MINUTE_CHART_URL),
            params=params,
            timeout=10,
            api_name="inquire_overseas_minute_chart",
        )

        chunk = js.get("output2")
        if chunk is None:
            chunk = js.get("output")

        if chunk is None or chunk == []:
            return _empty_overseas_minute_chart_page()

        validated_rows = _validate_overseas_minute_chart_chunk(chunk)

        for row in validated_rows:
            row["xhms"] = str(row["xhms"]).zfill(6)

        frame = self._build_ohlcv_dataframe(
            rows=validated_rows,
            column_mapping={
                "xymd": "date",
                "xhms": "time",
                "open": "open",
                "high": "high",
                "low": "low",
                "close": "close",
                "volume": "volume",
                "value": "value",
            },
            datetime_format="%Y%m%d%H%M%S",
            limit=len(validated_rows),
        )

        if frame.empty and validated_rows:
            raise RuntimeError(
                "Malformed KIS overseas minute chart payload: invalid xymd/xhms format"
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
