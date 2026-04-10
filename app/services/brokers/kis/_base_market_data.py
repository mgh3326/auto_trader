# pyright: reportAttributeAccessIssue=false, reportMissingTypeArgument=false
from __future__ import annotations

from typing import TYPE_CHECKING, Any, cast

import pandas as pd
from pandas import DataFrame

from . import constants

if TYPE_CHECKING:
    from .protocols import KISClientProtocol

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


class MarketDataBase:
    """Shared infrastructure for KIS market data sub-clients.

    Provides token-retrying HTTP requests and OHLCV DataFrame building.
    """

    def __init__(self, parent: KISClientProtocol) -> None:
        self._parent = parent

    @property
    def _settings(self):
        return self._parent._settings

    async def _request_with_token_retry(
        self,
        tr_id: str,
        url: str,
        params: dict[str, Any] | None = None,
        *,
        method: str = "GET",
        json_body: dict[str, Any] | None = None,
        timeout: float = 5.0,
        api_name: str = "unknown",
    ) -> dict[str, Any]:
        """KIS API 요청 + 토큰 만료 시 1회 재시도.

        토큰 만료 코드(EGW00123, EGW00121) 수신 시 토큰을 갱신하고
        동일 요청을 최대 1회 재시도한다.
        """
        for attempt in range(2):
            await self._parent._ensure_token()
            hdr = self._parent._hdr_base | {
                "authorization": f"Bearer {self._settings.kis_access_token}",
                "tr_id": tr_id,
            }
            js = await self._parent._request_with_rate_limit(
                method,
                url,
                headers=hdr,
                params=params,
                json_body=json_body,
                timeout=timeout,
                api_name=api_name,
                tr_id=tr_id,
            )

            if js.get("rt_cd") == "0":
                return js

            if attempt == 0 and js.get("msg_cd") in constants.TOKEN_EXPIRED_CODES:
                await self._parent._token_manager.clear_token()
                continue

            msg1 = js.get("msg1")
            msg_cd = js.get("msg_cd", "unknown")
            raise RuntimeError(msg1 or f"KIS API error (msg_cd={msg_cd})")

        raise RuntimeError("KIS API token retry exhausted")

    @staticmethod
    def _build_ohlcv_dataframe(
        rows: list[dict[str, Any]],
        column_mapping: dict[str, str],
        datetime_format: str,
        limit: int,
    ) -> pd.DataFrame:
        """원시 API rows를 표준 OHLCV DataFrame으로 변환.

        Parameters
        ----------
        rows : list[dict]
            KIS API 응답의 원시 행 목록
        column_mapping : dict
            KIS 컬럼명 → 표준 컬럼명 매핑.
            date + time 결합: ``{"date_col": "date", "time_col": "time", ...}``
        datetime_format : str
            date + time 문자열 결합 후 파싱할 strftime 포맷 (예: "%Y%m%d%H%M%S")
        limit : int
            반환할 최대 행 수 (tail 적용)
        """
        frame = (
            pd.DataFrame(rows)
            .rename(columns=column_mapping)
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
                    format=datetime_format,
                    errors="coerce",
                )
            )
            .dropna(subset=["datetime"])
            .assign(
                date=lambda d: d["datetime"].dt.date,
                time=lambda d: d["datetime"].dt.time,
            )
            .loc[:, _MINUTE_FRAME_COLUMNS]
            .drop_duplicates(subset=["datetime"], keep="first")
            .sort_values("datetime")
            .tail(max(int(limit), 1))
            .reset_index(drop=True)
        )
        return frame
