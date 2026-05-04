# tests/test_kiwoom_domestic_account.py
"""Verify Kiwoom domestic account/order-history queries."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.domestic_account import (
    KiwoomDomesticAccountClient,
)


class FakeClient:
    def __init__(self, payload: dict[str, Any] | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.account_no = "12345678-01"
        self._payload = payload or {
            "return_code": 0,
            "return_msg": "정상",
            "rows": [],
            "continuation": {"cont_yn": "N", "next_key": ""},
        }

    async def post_api(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return self._payload


@pytest.mark.asyncio
async def test_get_orderable_amount_uses_kt00010():
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_orderable_amount(symbol="005930")
    assert fake.calls[-1]["api_id"] == constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID
    assert fake.calls[-1]["body"]["stk_cd"] == "005930"


@pytest.mark.asyncio
async def test_get_balance_uses_kt00018():
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_balance()
    assert fake.calls[-1]["api_id"] == constants.ACCOUNT_BALANCE_API_ID


@pytest.mark.asyncio
async def test_get_order_status_uses_kt00009_and_passes_continuation():
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_order_status(cont_yn="Y", next_key="page-2")
    call = fake.calls[-1]
    assert call["api_id"] == constants.ACCOUNT_ORDER_STATUS_API_ID
    assert call["cont_yn"] == "Y"
    assert call["next_key"] == "page-2"


@pytest.mark.asyncio
async def test_get_order_detail_uses_kt00007():
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_order_detail(order_no="0000111222")
    call = fake.calls[-1]
    assert call["api_id"] == constants.ACCOUNT_ORDER_DETAIL_API_ID
    assert call["body"]["ord_no"] == "0000111222"


@pytest.mark.asyncio
async def test_account_methods_never_log_account_no(caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="app.services.brokers.kiwoom")
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_balance()
    rendered = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "12345678-01" not in rendered
