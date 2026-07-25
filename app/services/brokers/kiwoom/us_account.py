"""Kiwoom US mock account reads and deposit parsing."""

from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Protocol

from app.services.brokers.kiwoom import constants


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


def _optional_body(**values: str | None) -> dict[str, str]:
    return {key: value for key, value in values.items() if value not in (None, "")}


def extract_usd_deposit(payload: dict[str, Any]) -> str | None:
    """Return exact USD deposit text, or None when broker evidence is invalid."""

    raw = payload.get("d0_usd_fx_entr")
    if raw in (None, ""):
        return None
    text = str(raw).replace(",", "").strip()
    try:
        value = Decimal(text)
    except (InvalidOperation, ValueError):
        return None
    if not value.is_finite():
        return None
    return format(value, "f")


class KiwoomUsAccountClient:
    def __init__(self, client: _SupportsPostApi) -> None:
        self._client = client

    async def get_open_orders(
        self,
        *,
        order_date: str | None = None,
        side_code: str | None = None,
        stex_tp: str | None = None,
        symbol: str | None = None,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ACCOUNT_OPEN_ORDERS_API_ID,
            path=constants.US_ACCOUNT_PATH,
            body=_optional_body(
                ord_dt=order_date,
                slby_tp=side_code,
                stex_tp=stex_tp,
                stk_cd=symbol,
            ),
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_positions(
        self,
        *,
        stex_tp: str | None = None,
        symbol: str | None = None,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ACCOUNT_POSITIONS_API_ID,
            path=constants.US_ACCOUNT_PATH,
            body=_optional_body(stex_tp=stex_tp, stk_cd=symbol),
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_today_orders(
        self,
        *,
        side_code: str | None = None,
        stex_tp: str | None = None,
        symbol: str | None = None,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ACCOUNT_TODAY_ORDERS_API_ID,
            path=constants.US_ACCOUNT_PATH,
            body=_optional_body(
                slby_tp=side_code,
                stex_tp=stex_tp,
                stk_cd=symbol,
            ),
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_foreign_deposit(
        self,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ACCOUNT_FOREIGN_DEPOSIT_API_ID,
            path=constants.US_ACCOUNT_PATH,
            body={},
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_us_deposit_detail(
        self,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.US_ACCOUNT_DEPOSIT_DETAIL_API_ID,
            path=constants.US_ACCOUNT_PATH,
            body={},
            cont_yn=cont_yn,
            next_key=next_key,
        )
