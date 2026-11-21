"""KIS domestic futures day-session reads, implemented on the shared transport."""

from __future__ import annotations

import io
import os
import zipfile
from typing import Any

import httpx

# KIS examples: https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/domestic_futureoption/domestic_futureoption_functions.py
# Mock deposit TR follows the official sample's V-prefix conversion:
# https://github.com/koreainvestment/open-trading-api/blob/main/examples_user/kis_auth.py
# Index master layout: https://github.com/koreainvestment/open-trading-api/blob/main/stocks_info/domestic_index_future_code.py
_MASTER_URL = (
    "https://new.real.download.dws.co.kr/common/master/fo_idx_code_mts.mst.zip"
)
_PREFIX = "/uapi/domestic-futureoption/v1/trading/"


def _enabled() -> None:
    if os.environ.get("KIS_FUTURES_READ_ENABLED") != "true":
        raise RuntimeError("KIS_FUTURES_READ_ENABLED이 true가 아닙니다")


def _account(account: str) -> dict[str, str]:
    if len(account) != 8 or not account.isascii() or not account.isdigit():
        raise ValueError("선물 계좌번호는 숫자 8자리여야 합니다")
    return {"CANO": account, "ACNT_PRDT_CD": "03"}


class FuturesRead:
    def __init__(self, parent: Any) -> None:
        self._parent = parent

    async def _get(
        self, endpoint: str, tr: str, params: dict[str, str], cont: str = ""
    ) -> tuple[dict[str, Any], dict[str, str]]:
        _enabled()
        if (
            self._parent._is_mock_client
            and self._parent._settings.kis_base_url.rstrip("/")
            != "https://openapivts.koreainvestment.com:29443"
        ):
            raise RuntimeError("모의 KIS 호스트가 올바르지 않습니다")
        await self._parent._ensure_token()
        _enabled()
        headers = self._parent._hdr_base | {
            "authorization": f"Bearer {self._parent._settings.kis_access_token}",
            "tr_id": tr,
            "tr_cont": cont,
        }
        (
            data,
            response_headers,
        ) = await self._parent._request_with_rate_limit_with_headers(
            "GET",
            self._parent._kis_url(_PREFIX + endpoint),
            headers=headers,
            params=params,
            api_name="futures_read",
            tr_id=tr,
        )
        if data.get("rt_cd") != "0":
            raise RuntimeError("KIS 선물 조회가 거부되었습니다")
        return data, response_headers

    async def balance(self, account: str) -> dict[str, Any]:
        params = _account(account) | {
            "MGNA_DVSN": "01",
            "EXCC_STAT_CD": "1",
            "CTX_AREA_FK200": "",
            "CTX_AREA_NK200": "",
        }
        tr = "VTFO6118R" if self._parent._is_mock_client else "CTFO6118R"
        positions: list[dict[str, Any]] = []
        summary: dict[str, Any] | None = None
        seen: set[tuple[str, str]] = set()
        cont = ""
        for _ in range(100):
            data, headers = await self._get("inquire-balance", tr, params, cont)
            rows = data.get("output1")
            totals = data.get("output2")
            if (
                not isinstance(rows, list)
                or not all(isinstance(row, dict) for row in rows)
                or not isinstance(totals, dict)
            ):
                raise RuntimeError("KIS 잔고 응답 형식을 읽을 수 없습니다")
            positions.extend(rows)
            if summary is None:
                summary = totals
            if headers.get("tr_cont", "").strip().upper() not in {"M", "F"}:
                return {"positions": positions, "summary": summary}
            cursor = (data.get("ctx_area_fk200", ""), data.get("ctx_area_nk200", ""))
            if (
                not all(isinstance(v, str) for v in cursor)
                or not cursor[1]
                or cursor in seen
            ):
                raise RuntimeError("KIS 잔고 연속조회 키를 읽을 수 없습니다")
            seen.add(cursor)
            params["CTX_AREA_FK200"], params["CTX_AREA_NK200"] = cursor
            cont = "N"
        raise RuntimeError("KIS 잔고 연속조회 한도를 초과했습니다")

    async def deposit(self, account: str) -> dict[str, Any]:
        tr = "VTRP6550R" if self._parent._is_mock_client else "CTRP6550R"
        data, _ = await self._get("inquire-deposit", tr, _account(account))
        if not isinstance(data.get("output"), dict):
            raise RuntimeError("KIS 예수금 응답 형식을 읽을 수 없습니다")
        return data["output"]

    async def orderable(
        self, account: str, symbol: str, side: str, price: str, order_type: str
    ) -> dict[str, Any]:
        if not all(
            isinstance(v, str) and v for v in (symbol, side, price, order_type)
        ) or side not in {"01", "02"}:
            raise ValueError("주문가능 조회 입력이 올바르지 않습니다")
        params = _account(account) | {
            "PDNO": symbol,
            "SLL_BUY_DVSN_CD": side,
            "UNIT_PRICE": price,
            "ORD_DVSN_CD": order_type,
        }
        tr = "VTTO5105R" if self._parent._is_mock_client else "TTTO5105R"
        data, _ = await self._get("inquire-psbl-order", tr, params)
        if not isinstance(data.get("output"), dict):
            raise RuntimeError("KIS 주문가능 응답 형식을 읽을 수 없습니다")
        return data["output"]

    async def master(self) -> list[dict[str, str]]:
        _enabled()
        async with httpx.AsyncClient(follow_redirects=False, timeout=15) as client:
            response = await client.get(_MASTER_URL)
            response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            info = archive.getinfo("fo_idx_code_mts.mst")
            if info.file_size > 20_000_000:
                raise ValueError("종목마스터 크기가 너무 큽니다")
            rows = archive.read(info).decode("cp949").splitlines()
        names = (
            "product_type",
            "short_code",
            "standard_code",
            "name",
            "atm",
            "strike",
            "month_type",
            "underlying_code",
            "underlying_name",
        )
        result = []
        for row in rows:
            values = row.split("|")
            if len(values) != len(names):
                raise ValueError("종목마스터 행 형식이 올바르지 않습니다")
            result.append(
                dict(zip(names, (value.strip() for value in values), strict=True))
            )
        return result
