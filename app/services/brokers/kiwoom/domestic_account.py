# app/services/brokers/kiwoom/domestic_account.py
"""Kiwoom domestic account/order-history queries.

All methods delegate to the parent client's ``post_api`` and never log the
account number or token. The exact body field names mirror Kiwoom REST docs;
they are passed through untransformed for the parent project to consume.
"""

from __future__ import annotations

from typing import Any, Protocol

from app.services.brokers.kiwoom import constants

ACCOUNT_PATH = "/api/dostk/acnt"


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


class KiwoomDomesticAccountClient:
    def __init__(self, client: _SupportsPostApi) -> None:
        self._client = client

    async def get_orderable_amount(
        self,
        *,
        symbol: str,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID,
            path=ACCOUNT_PATH,
            body={"stk_cd": str(symbol).strip()},
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_balance(
        self,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.ACCOUNT_BALANCE_API_ID,
            path=ACCOUNT_PATH,
            # ROB-418 — kt00018 requires qry_tp; omitting it returns return_code 2
            # (필수입력 파라미터=qry_tp). Value is convention-default, smoke-confirmed.
            body={"qry_tp": constants.ACCOUNT_BALANCE_QRY_TP_DEFAULT},
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_order_status(
        self,
        *,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.ACCOUNT_ORDER_STATUS_API_ID,
            path=ACCOUNT_PATH,
            # ROB-418 — kt00009 requires stk_bond_tp; omitting it returns
            # return_code 2 (필수입력 파라미터=stk_bond_tp). Convention-default,
            # smoke-confirmed.
            body={"stk_bond_tp": constants.ACCOUNT_ORDER_STK_BOND_TP_DEFAULT},
            cont_yn=cont_yn,
            next_key=next_key,
        )

    async def get_order_detail(
        self,
        *,
        order_no: str,
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        return await self._client.post_api(
            api_id=constants.ACCOUNT_ORDER_DETAIL_API_ID,
            path=ACCOUNT_PATH,
            body={"ord_no": str(order_no).strip()},
            cont_yn=cont_yn,
            next_key=next_key,
        )
