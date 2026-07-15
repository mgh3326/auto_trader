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
async def test_get_orderable_amount_exact_body_no_dmst_stex_tp():
    # ROB-891 — Official kt00010 body: stk_cd, trde_tp, uv.
    # dmst_stex_tp is NOT in the official docs for kt00010.
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_orderable_amount(symbol="005930", side="buy", price=70000)
    call = fake.calls[-1]
    assert call["api_id"] == constants.ACCOUNT_ORDERABLE_AMOUNT_API_ID
    assert call["body"] == {
        "stk_cd": "005930",
        "trde_tp": constants.TRADE_TYPE_BUY,
        "uv": "70000",
    }
    assert "dmst_stex_tp" not in call["body"]


@pytest.mark.asyncio
async def test_get_orderable_amount_buy_trde_tp_is_two():
    # ROB-891 — Official: 매수(buy) = "2"
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_orderable_amount(symbol="005930", side="buy", price=70000)
    assert fake.calls[-1]["body"]["trde_tp"] == "2"


@pytest.mark.asyncio
async def test_get_orderable_amount_sell_trde_tp_is_one():
    # ROB-891 — Official: 매도(sell) = "1"
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_orderable_amount(symbol="005930", side="sell", price=70000)
    assert fake.calls[-1]["body"]["trde_tp"] == "1"


@pytest.mark.asyncio
async def test_get_orderable_amount_serializes_price_as_string_uv():
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_orderable_amount(symbol="005930", side="buy", price=70000)
    uv = fake.calls[-1]["body"]["uv"]
    assert isinstance(uv, str)
    assert uv == "70000"


@pytest.mark.asyncio
@pytest.mark.parametrize("side", [None, "hold", "", "unknown"])
async def test_get_orderable_amount_rejects_missing_or_invalid_side_before_dispatch(
    side,
):
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    with pytest.raises(ValueError, match="side"):
        await acct.get_orderable_amount(symbol="005930", side=side, price=70000)
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "price",
    [
        None,
        0,
        -100,
        # ROB-891 — bool is an int subclass; isinstance(price, int) wrongly
        # accepted True and dispatched uv="True". type(price) is int rejects
        # both bools before any HTTP dispatch.
        True,
        False,
        1.5,
        70000.0,
        "70000",
    ],
)
async def test_get_orderable_amount_rejects_missing_or_invalid_price_before_dispatch(
    price,
):
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    with pytest.raises(ValueError, match="price"):
        await acct.get_orderable_amount(symbol="005930", side="buy", price=price)
    assert fake.calls == []


@pytest.mark.asyncio
async def test_get_orderable_amount_bool_price_never_dispatched_as_uv_string():
    # ROB-891 regression — price=True previously dispatched uv="True" because
    # isinstance(True, int) is True. Fail-closed at the service boundary.
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    with pytest.raises(ValueError, match="price"):
        await acct.get_orderable_amount(symbol="005930", side="buy", price=True)
    assert fake.calls == []
    assert all("uv" not in c.get("body", {}) for c in fake.calls)


@pytest.mark.asyncio
@pytest.mark.parametrize("price", [1, 70000, 1000000])
async def test_get_orderable_amount_positive_int_path_preserved(price):
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_orderable_amount(symbol="005930", side="buy", price=price)
    call = fake.calls[-1]
    assert call["body"]["uv"] == str(price)
    assert len(fake.calls) == 1


@pytest.mark.asyncio
async def test_get_deposit_exact_body_qry_tp_two():
    # ROB-891 — Official kt00001 body is exactly {"qry_tp": "2"}.
    # dmst_stex_tp is NOT in the official docs for kt00001.
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_deposit()
    call = fake.calls[-1]
    assert call["api_id"] == constants.ACCOUNT_DEPOSIT_API_ID
    assert call["body"] == {"qry_tp": "2"}
    assert "dmst_stex_tp" not in call["body"]


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


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "symbol",
    [
        "A005930",
        "AAPL",
        "",
        "   ",
        "5930",
        "../005930",
        "005930?x",
        "0123G0",
        "００５９３０",
        "٠٠٥٩٣٠",
        "00\n5930",
    ],
)
async def test_get_orderable_amount_rejects_noncanonical_symbol_before_post_api(
    symbol,
):
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)

    with pytest.raises(ValueError, match="symbol"):
        await acct.get_orderable_amount(symbol=symbol)

    assert fake.calls == []


@pytest.mark.asyncio
async def test_get_orderable_amount_forwards_trimmed_canonical_symbol():
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)

    await acct.get_orderable_amount(symbol=" 005930 ", side="buy", price=70000)

    assert fake.calls[-1]["body"]["stk_cd"] == "005930"
