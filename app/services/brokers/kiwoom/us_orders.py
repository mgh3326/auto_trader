"""Kiwoom US mock order payloads and transport calls."""

from __future__ import annotations

import re
from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.services.brokers.kiwoom import constants

BUY_TRADE_TYPES = frozenset({"00", "03", "26", "27", "30", "36", "37"})
SELL_TRADE_TYPES = BUY_TRADE_TYPES | frozenset({"33", "34", "35"})
PRICE_REQUIRED_TRADE_TYPES = frozenset({"00", "26", "27", "30", "34"})
STOP_REQUIRED_TRADE_TYPES = frozenset({"34", "35"})
_SYMBOL_RE = re.compile(r"^[A-Z0-9.]{1,12}$")
_ORDER_ID_RE = re.compile(r"^\d{9}$")


class KiwoomUsOrderRejected(ValueError):
    """Raised before transport when a US order request violates the contract."""


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


def validate_us_order_id(value: str) -> str:
    candidate = str(value or "").strip()
    if not _ORDER_ID_RE.fullmatch(candidate):
        raise KiwoomUsOrderRejected("Kiwoom US order id must be exactly nine digits")
    return candidate


def _symbol(value: str) -> str:
    candidate = str(value or "").strip().upper()
    if not _SYMBOL_RE.fullmatch(candidate):
        raise KiwoomUsOrderRejected("Kiwoom US symbol must use DB dot format")
    return candidate


def _stex(value: str) -> str:
    candidate = str(value or "").strip().upper()
    if candidate not in constants.US_STEX_TYPES:
        raise KiwoomUsOrderRejected(f"unsupported Kiwoom US stex_tp={value!r}")
    return candidate


def _quantity(value: int) -> int:
    try:
        candidate = int(value)
    except (TypeError, ValueError) as exc:
        raise KiwoomUsOrderRejected("quantity must be a positive integer") from exc
    if candidate <= 0:
        raise KiwoomUsOrderRejected("quantity must be a positive integer")
    return candidate


def _decimal_text(name: str, value: object) -> str:
    try:
        candidate = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise KiwoomUsOrderRejected(f"{name} must be a positive decimal") from exc
    if not candidate.is_finite() or candidate <= 0:
        raise KiwoomUsOrderRejected(f"{name} must be a positive decimal")
    return format(candidate, "f")


def build_us_place_order_body(
    *,
    side: str,
    symbol: str,
    stex_tp: str,
    quantity: int,
    trde_tp: str,
    price: object | None,
    stop_price: object | None = None,
) -> dict[str, str]:
    normalized_side = str(side).strip().lower()
    allowed = BUY_TRADE_TYPES if normalized_side == "buy" else SELL_TRADE_TYPES
    if normalized_side not in {"buy", "sell"} or trde_tp not in allowed:
        raise KiwoomUsOrderRejected(
            f"unsupported documented trde_tp={trde_tp!r} for side={side!r}"
        )
    if trde_tp in PRICE_REQUIRED_TRADE_TYPES:
        if price is None:
            raise KiwoomUsOrderRejected(f"trde_tp={trde_tp} requires price")
        order_price = _decimal_text("price", price)
    else:
        if price is not None:
            raise KiwoomUsOrderRejected(f"trde_tp={trde_tp} must omit price")
        order_price = ""

    body = {
        "stex_tp": _stex(stex_tp),
        "stk_cd": _symbol(symbol),
        "ord_qty": str(_quantity(quantity)),
        "ord_uv": order_price,
        "trde_tp": trde_tp,
    }
    if normalized_side == "sell":
        if trde_tp in STOP_REQUIRED_TRADE_TYPES:
            if stop_price is None:
                raise KiwoomUsOrderRejected(f"trde_tp={trde_tp} requires stop_price")
            body["stop_pric"] = _decimal_text("stop_price", stop_price)
        elif stop_price is not None:
            raise KiwoomUsOrderRejected(f"trde_tp={trde_tp} must omit stop_price")
    elif stop_price is not None:
        raise KiwoomUsOrderRejected("buy orders must omit stop_price")
    return body


class KiwoomUsOrderClient:
    """Low-level transport for documented Kiwoom US order request shapes."""

    def __init__(self, client: _SupportsPostApi) -> None:
        self._client = client

    async def place_buy_order(self, **kwargs: Any) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ORDER_BUY_API_ID,
            path=constants.US_ORDER_PATH,
            body=build_us_place_order_body(side="buy", **kwargs),
        )

    async def place_sell_order(self, **kwargs: Any) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ORDER_SELL_API_ID,
            path=constants.US_ORDER_PATH,
            body=build_us_place_order_body(side="sell", **kwargs),
        )

    async def modify_order(
        self,
        *,
        original_order_no: str,
        symbol: str,
        stex_tp: str,
        new_price: object,
        stop_price: object | None = None,
    ) -> dict[str, Any]:
        body = {
            "orig_ord_no": validate_us_order_id(original_order_no),
            "stex_tp": _stex(stex_tp),
            "stk_cd": _symbol(symbol),
            "mdfy_uv": _decimal_text("new_price", new_price),
        }
        if stop_price is not None:
            body["stop_pric"] = _decimal_text("stop_price", stop_price)
        return await self._client.post_api(
            api_id=constants.US_ORDER_MODIFY_API_ID,
            path=constants.US_ORDER_PATH,
            body=body,
        )

    async def cancel_order(
        self,
        *,
        original_order_no: str,
        symbol: str,
        stex_tp: str,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ORDER_CANCEL_API_ID,
            path=constants.US_ORDER_PATH,
            body={
                "orig_ord_no": validate_us_order_id(original_order_no),
                "stex_tp": _stex(stex_tp),
                "stk_cd": _symbol(symbol),
            },
        )
