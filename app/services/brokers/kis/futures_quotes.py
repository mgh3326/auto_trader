"""KIS domestic index futures quotes (price, 1-minute bars) — read only (hk 6148).

Official samples this follows (TR ids, paths, market division code, field names):

* price   — ``/uapi/domestic-futureoption/v1/quotations/inquire-price``, TR
  ``FHMIF10000000``, ``FID_COND_MRKT_DIV_CODE`` ``F`` (index futures), output1/2/3.
  https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_futureoption/inquire_price/inquire_price.py
* minute  — ``.../quotations/inquire-time-fuopchartprice``, TR ``FHKIF03020200``;
  ``FID_HOUR_CLS_CODE`` 60 = 1 minute, ``FID_PW_DATA_INCU_YN`` N = today,
  ``FID_FAKE_TICK_INCU_YN`` N = no fake ticks, ``FID_INPUT_DATE_1``/``_HOUR_1`` =
  paging cursor; at most 102 rows per call; output2 rows carry ``stck_bsop_date``,
  ``stck_cntg_hour``, ``futs_oprc``/``futs_hgpr``/``futs_lwpr``/``futs_prpr``,
  ``cntg_vol``, ``acml_tr_pbmn``.
  https://github.com/koreainvestment/open-trading-api/blob/main/examples_llm/domestic_futureoption/inquire_time_fuopchartprice/inquire_time_fuopchartprice.py
* the same paths and TR ids appear in
  https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/domestic_futureoption/domestic_futureoption_functions.py

Night session: the official samples document **no** night-session quote or
minute-chart TR (the ``(야간)`` samples are balance, order history, orderable
amount and margin; night quotes exist only as realtime websocket feeds). So
``session="night"`` raises :class:`FuturesQuotesUnsupported` — nothing is guessed.

``[unverified]`` until the operator's first smoke: that mini KOSPI200 futures use
market code ``F`` like the regular contract, and whether ``stck_cntg_hour`` labels
the bar start (the collector assumes start, the convention of the other KIS minute
charts, and drops bars that could still be forming).

Hard boundary: only ``/quotations/`` paths and the two TR ids above are reachable
from here. No ``/trading/`` path, no order TR, no account number. Gate:
``KIS_FUTURES_QUOTES_ENABLED`` exactly ``"true"`` (default off), separate from
``KIS_FUTURES_READ_ENABLED``, checked before the token lookup and again after it.
Live host only (the account-less quote endpoints use the real app key namespace).
"""

from __future__ import annotations

import os
from datetime import date
from typing import Any

from app.services.brokers.kis.futures_read import fetch_master_rows

GATE_ENV = "KIS_FUTURES_QUOTES_ENABLED"
LIVE_HOST = "https://openapi.koreainvestment.com:9443"

QUOTES_PATH_PREFIX = "/uapi/domestic-futureoption/v1/quotations/"
PRICE_ENDPOINT = "inquire-price"
MINUTE_ENDPOINT = "inquire-time-fuopchartprice"
TR_PRICE = "FHMIF10000000"
TR_MINUTE = "FHKIF03020200"
MARKET_DIV_INDEX_FUTURES = "F"

MAX_MINUTE_PAGES = 10
#: Day session open (KST); paging stops once the oldest row reaches it.
DAY_OPEN_HOUR = "084500"
#: Starting cursor: after the day close, so the first page is the newest rows.
DAY_START_CURSOR = "160000"


class FuturesQuotesUnsupported(RuntimeError):
    """The requested quote surface has no documented KIS TR."""


class FuturesQuotesGateOff(RuntimeError):
    """``KIS_FUTURES_QUOTES_ENABLED`` is not exactly ``"true"``."""


def gate_check() -> None:
    if os.environ.get(GATE_ENV) != "true":
        raise FuturesQuotesGateOff(f"{GATE_ENV}이 true가 아닙니다")



def _symbol(symbol: str) -> str:
    if not isinstance(symbol, str) or not (
        symbol.isascii() and symbol.isalnum() and 4 <= len(symbol) <= 12
    ):
        raise ValueError("선물 단축코드 형식이 올바르지 않습니다")
    return symbol


class FuturesQuotes:
    def __init__(self, parent: Any) -> None:
        self._parent = parent

    async def _get(
        self, endpoint: str, tr: str, params: dict[str, str]
    ) -> dict[str, Any]:
        gate_check()
        if (
            self._parent._is_mock_client
            or self._parent._settings.kis_base_url.rstrip("/") != LIVE_HOST
        ):
            raise RuntimeError("선물 시세는 실전 KIS 호스트에서만 읽습니다")
        await self._parent._ensure_token()
        gate_check()
        headers = self._parent._hdr_base | {
            "authorization": f"Bearer {self._parent._settings.kis_access_token}",
            "tr_id": tr,
            "tr_cont": "",
        }
        data, _ = await self._parent._request_with_rate_limit_with_headers(
            "GET",
            self._parent._kis_url(QUOTES_PATH_PREFIX + endpoint),
            headers=headers,
            params=params,
            api_name="futures_quotes",
            tr_id=tr,
        )
        if data.get("rt_cd") != "0":
            raise RuntimeError("KIS 선물 시세 조회가 거부되었습니다")
        return data

    async def master(self) -> list[dict[str, str]]:
        gate_check()
        return await fetch_master_rows()

    async def price(self, symbol: str) -> dict[str, Any]:
        gate_check()
        params = {
            "FID_COND_MRKT_DIV_CODE": MARKET_DIV_INDEX_FUTURES,
            "FID_INPUT_ISCD": _symbol(symbol),
        }
        data = await self._get(PRICE_ENDPOINT, TR_PRICE, params)
        outputs = {key: data.get(key) for key in ("output1", "output2", "output3")}
        if not isinstance(outputs["output1"], dict):
            raise RuntimeError("KIS 선물 시세 응답 형식을 읽을 수 없습니다")
        return outputs

    async def minute_bars(
        self,
        symbol: str,
        *,
        session: str = "day",
        day: date,
        until_hour: str = DAY_START_CURSOR,
    ) -> list[dict[str, Any]]:
        """Raw output2 rows of one KST day, ascending, de-duplicated.

        Pages backwards with the (date, hour) cursor until the day open is reached
        or a page adds nothing new. Hitting ``MAX_MINUTE_PAGES`` raises (a partial
        day is never presented as complete).
        """
        gate_check()
        if session != "day":
            raise FuturesQuotesUnsupported(
                "야간 선물 시세·분봉 TR이 KIS 공식 샘플에 없습니다"
            )
        iscd = _symbol(symbol)
        if not (len(until_hour) == 6 and until_hour.isdigit()):
            raise ValueError("조회 시각 형식이 올바르지 않습니다 (HHMMSS)")
        date_text = day.strftime("%Y%m%d")
        seen: dict[tuple[str, str], dict[str, Any]] = {}
        cursor = until_hour
        for _ in range(MAX_MINUTE_PAGES):
            params = {
                "FID_COND_MRKT_DIV_CODE": MARKET_DIV_INDEX_FUTURES,
                "FID_INPUT_ISCD": iscd,
                "FID_HOUR_CLS_CODE": "60",
                "FID_PW_DATA_INCU_YN": "N",
                "FID_FAKE_TICK_INCU_YN": "N",
                "FID_INPUT_DATE_1": date_text,
                "FID_INPUT_HOUR_1": cursor,
            }
            data = await self._get(MINUTE_ENDPOINT, TR_MINUTE, params)
            rows = data.get("output2")
            if not isinstance(rows, list) or not all(
                isinstance(row, dict) for row in rows
            ):
                raise RuntimeError("KIS 선물 분봉 응답 형식을 읽을 수 없습니다")
            fresh = 0
            hours: list[str] = []
            for row in rows:
                key = (str(row.get("stck_bsop_date", "")), str(row.get("stck_cntg_hour", "")))
                if not (key[0] and key[1]):
                    raise RuntimeError("KIS 선물 분봉 행의 시각을 읽을 수 없습니다")
                if key[0] == date_text:
                    hours.append(key[1])
                if key not in seen:
                    seen[key] = row
                    fresh += 1
            if fresh == 0 or not hours:
                break
            oldest = min(hours)
            if oldest <= DAY_OPEN_HOUR or oldest >= cursor:
                break
            cursor = oldest
        else:
            raise RuntimeError("KIS 선물 분봉 조회 한도를 초과했습니다")
        return [seen[key] for key in sorted(seen)]

    async def close(self) -> None:
        await self._parent.close()
