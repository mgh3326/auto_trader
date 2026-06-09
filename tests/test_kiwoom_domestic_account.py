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
async def test_get_orderable_amount_includes_dmst_stex_tp():
    # ROB-460 — get_orderable_cash(symbol=...) routes here (kt00010). Its sibling
    # account-cash read kt00018 was PROVEN (2026-06-09 live) to require
    # dmst_stex_tp (국내거래소구분); leaving kt00010 — the SAME tool's symbol path —
    # without it would reproduce the partial-fix that produced ROB-460. The value
    # "KRX" is proven correct by every order endpoint (kt10000-kt10003).
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_orderable_amount(symbol="005930")
    call = fake.calls[-1]
    assert call["api_id"] == constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID
    assert call["body"]["stk_cd"] == "005930"
    assert call["body"]["dmst_stex_tp"] == constants.ACCOUNT_DMST_STEX_TP_DEFAULT


@pytest.mark.asyncio
async def test_get_balance_uses_kt00018_with_qry_tp_and_dmst_stex_tp():
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_balance()
    call = fake.calls[-1]
    assert call["api_id"] == constants.ACCOUNT_BALANCE_API_ID
    # ROB-418 — kt00018 requires qry_tp (operator return_code 2 without it).
    assert call["body"]["qry_tp"] == constants.ACCOUNT_BALANCE_QRY_TP_DEFAULT
    # ROB-460 — kt00018 ALSO requires dmst_stex_tp; omitting it returned
    # return_code 2 (필수입력 파라미터=dmst_stex_tp) on 2026-06-09 live via
    # get_positions/get_orderable_cash.
    assert call["body"]["dmst_stex_tp"] == constants.ACCOUNT_DMST_STEX_TP_DEFAULT


@pytest.mark.asyncio
async def test_get_order_status_uses_kt00009_with_stk_bond_tp_and_continuation():
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_order_status(cont_yn="Y", next_key="page-2")
    call = fake.calls[-1]
    assert call["api_id"] == constants.ACCOUNT_ORDER_STATUS_API_ID
    # ROB-418 — kt00009 requires stk_bond_tp (operator return_code 2 without it).
    assert call["body"]["stk_bond_tp"] == constants.ACCOUNT_ORDER_STK_BOND_TP_DEFAULT
    # ROB-460 boundary — kt00009 is an order-history read (different tool,
    # get_order_history), already recovered by ROB-418, and NOT proven to need
    # dmst_stex_tp. Do not speculatively add it to a working endpoint; scope is
    # operator-smoke-validated (see kiwoom-mock-smoke runbook).
    assert "dmst_stex_tp" not in call["body"]
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
    # ROB-460 boundary — kt00007 order-detail read left untouched (not proven to
    # need dmst_stex_tp; not exercised by the bug). Smoke-validated follow-up.
    assert "dmst_stex_tp" not in call["body"]


@pytest.mark.asyncio
async def test_account_methods_never_log_account_no(caplog):
    import logging

    caplog.set_level(logging.DEBUG, logger="app.services.brokers.kiwoom")
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_balance()
    rendered = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "12345678-01" not in rendered
