# app/services/brokers/kiwoom/domestic_orders.py
"""Kiwoom domestic (KRX) order operations.

All payloads target the mock exchange (``KRX``); ``NXT``/``SOR`` are rejected
before any network call. Order-id arguments are validated against a strict
allowlist so callers cannot inject path separators, query fragments or bulk
delimiters.
"""

from __future__ import annotations

import re
from typing import Any, Protocol

from app.services.brokers.kiwoom import constants

_SAFE_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]+$")


class KiwoomOrderRejected(ValueError):
    """Raised when a Kiwoom mock order is rejected client-side before sending."""


class _SupportsPostApi(Protocol):
    account_no: str

    async def post_api(
        self,
        *,
        api_id: str,
        path: str,
        body: dict[str, Any],
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]: ...


def _ensure_krx(exchange: str | None) -> str:
    value = (exchange or constants.MOCK_EXCHANGE_KRX).strip().upper()
    if value in constants.MOCK_REJECTED_EXCHANGES:
        raise KiwoomOrderRejected(
            f"Kiwoom mock supports KRX only; rejected exchange={value!r}"
        )
    if value != constants.MOCK_EXCHANGE_KRX:
        raise KiwoomOrderRejected(
            f"Kiwoom mock supports KRX only; got {value!r}"
        )
    return value


def _ensure_order_id(value: str) -> str:
    candidate = (value or "").strip()
    if not candidate or not _SAFE_ORDER_ID_RE.fullmatch(candidate):
        raise KiwoomOrderRejected(f"Unsafe Kiwoom order id: {value!r}")
    return candidate


class KiwoomDomesticOrderClient:
    def __init__(self, client: _SupportsPostApi) -> None:
        self._client = client

    async def place_buy_order(
        self,
        *,
        symbol: str,
        quantity: int,
        price: int,
        exchange: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "dmst_stex_tp": _ensure_krx(exchange),
            "stk_cd": str(symbol).strip(),
            "ord_qty": str(int(quantity)),
            "ord_uv": str(int(price)),
            "trde_tp": "0",  # 보통가 — limit
        }
        return await self._client.post_api(
            api_id=constants.ORDER_BUY_API_ID,
            path=constants.ORDER_PATH,
            body=body,
        )

    async def place_sell_order(
        self,
        *,
        symbol: str,
        quantity: int,
        price: int,
        exchange: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "dmst_stex_tp": _ensure_krx(exchange),
            "stk_cd": str(symbol).strip(),
            "ord_qty": str(int(quantity)),
            "ord_uv": str(int(price)),
            "trde_tp": "0",
        }
        return await self._client.post_api(
            api_id=constants.ORDER_SELL_API_ID,
            path=constants.ORDER_PATH,
            body=body,
        )

    async def modify_order(
        self,
        *,
        original_order_no: str,
        symbol: str,
        new_quantity: int,
        new_price: int,
        exchange: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "dmst_stex_tp": _ensure_krx(exchange),
            "orig_ord_no": _ensure_order_id(original_order_no),
            "stk_cd": str(symbol).strip(),
            "mdfy_qty": str(int(new_quantity)),
            "mdfy_uv": str(int(new_price)),
        }
        return await self._client.post_api(
            api_id=constants.ORDER_MODIFY_API_ID,
            path=constants.ORDER_PATH,
            body=body,
        )

    async def cancel_order(
        self,
        *,
        original_order_no: str,
        symbol: str,
        cancel_quantity: int,
        exchange: str | None = None,
    ) -> dict[str, Any]:
        body = {
            "dmst_stex_tp": _ensure_krx(exchange),
            "orig_ord_no": _ensure_order_id(original_order_no),
            "stk_cd": str(symbol).strip(),
            "cncl_qty": str(int(cancel_quantity)),
        }
        return await self._client.post_api(
            api_id=constants.ORDER_CANCEL_API_ID,
            path=constants.ORDER_PATH,
            body=body,
        )
