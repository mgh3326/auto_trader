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
async def test_get_order_status_uses_kt00009_with_required_params_and_continuation():
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_order_status(cont_yn="Y", next_key="page-2")
    call = fake.calls[-1]
    assert call["api_id"] == constants.ACCOUNT_ORDER_STATUS_API_ID
    # ROB-418 — kt00009 requires stk_bond_tp (operator return_code 2 without it).
    assert call["body"]["stk_bond_tp"] == constants.ACCOUNT_ORDER_STK_BOND_TP_DEFAULT
    # ROB-1111 — kt00009 ALSO requires mrkt_tp (operator return_code 2 without it).
    assert call["body"]["mrkt_tp"] == constants.ACCOUNT_ORDER_MRKT_TP_DEFAULT
    # ROB-1088 (2026-07-28 official-doc fix) — kt00009 requires all five fields
    # per the official Kiwoom REST docs (Required=Y on every one of
    # stk_bond_tp/mrkt_tp/sell_tp/qry_tp/dmst_stex_tp). sell_tp/qry_tp/
    # dmst_stex_tp were previously omitted as "unproven speculation"; that was
    # a contract mismatch, not caution — the official doc lists them as
    # required just like stk_bond_tp/mrkt_tp.
    assert call["body"]["sell_tp"] == constants.ACCOUNT_ORDER_SELL_TP_DEFAULT
    assert call["body"]["qry_tp"] == constants.ACCOUNT_ORDER_QRY_TP_DEFAULT
    # dmst_stex_tp: official docs allow %/KRX/NXT/SOR, but kiwoom_mock is
    # KRX-only (MOCK_REJECTED_EXCHANGES={"NXT","SOR"}) — "KRX" is the only
    # selection consistent with that fail-closed boundary.
    assert call["body"]["dmst_stex_tp"] == constants.ACCOUNT_DMST_STEX_TP_DEFAULT
    assert call["body"]["dmst_stex_tp"] == "KRX"
    assert call["cont_yn"] == "Y"
    assert call["next_key"] == "page-2"


@pytest.mark.asyncio
async def test_get_order_status_body_is_exactly_the_official_five_fields():
    # ROB-1088 — pins the exact wire body to the official Kiwoom kt00009
    # contract (all 5 Required=Y fields, no more, no less). This intentionally
    # replaces an earlier two-field-only pin that was found to mismatch the
    # official contract during independent verification of PR #1708.
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_order_status()
    assert fake.calls[-1]["body"] == {
        "stk_bond_tp": constants.ACCOUNT_ORDER_STK_BOND_TP_DEFAULT,
        "mrkt_tp": constants.ACCOUNT_ORDER_MRKT_TP_DEFAULT,
        "sell_tp": constants.ACCOUNT_ORDER_SELL_TP_DEFAULT,
        "qry_tp": constants.ACCOUNT_ORDER_QRY_TP_DEFAULT,
        "dmst_stex_tp": constants.ACCOUNT_DMST_STEX_TP_DEFAULT,
    }


@pytest.mark.asyncio
async def test_get_order_status_dmst_stex_tp_never_nxt_or_sor():
    # ROB-1088 — explicit regression guard for the KRX-only boundary: even
    # though the official docs permit "%"(전체)/NXT/SOR as dmst_stex_tp values,
    # kt00009 must never select a value inside MOCK_REJECTED_EXCHANGES, and
    # must not use "%" either (that would blend NXT/SOR rows into a
    # KRX-only-intended surface).
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_order_status()
    dmst_stex_tp = fake.calls[-1]["body"]["dmst_stex_tp"]
    assert dmst_stex_tp not in constants.MOCK_REJECTED_EXCHANGES
    assert dmst_stex_tp != "%"
    assert dmst_stex_tp == constants.MOCK_EXCHANGE_KRX


@pytest.mark.asyncio
async def test_get_order_detail_body_is_exactly_the_official_seven_fields():
    # ROB-1155 — pins the exact kt00007 wire body to the official contract.
    # Official request table (verified 2026-07-29 against
    # https://openapi.kiwoom.com/m/guide/apiguide?apiId=kt00007&jobTp=FS_JOB_TP&jobTpCode=08
    # and the local extraction of the same doc) is 7 fields:
    #   ord_dt(N) qry_tp(Y) stk_bond_tp(Y) sell_tp(Y) stk_cd(N) fr_ord_no(N)
    #   dmst_stex_tp(Y)
    # The previous implementation sent {"ord_no": ...} — a field that does not
    # exist in this TR's request table — and omitted all four Required=Y fields.
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_order_detail()
    call = fake.calls[-1]
    assert call["api_id"] == constants.ACCOUNT_ORDER_DETAIL_API_ID
    assert call["body"] == {
        "ord_dt": "",
        "qry_tp": constants.ACCOUNT_ORDER_DETAIL_QRY_TP_DEFAULT,
        "stk_bond_tp": constants.ACCOUNT_ORDER_DETAIL_STK_BOND_TP_DEFAULT,
        "sell_tp": constants.ACCOUNT_ORDER_DETAIL_SELL_TP_DEFAULT,
        "stk_cd": "",
        "fr_ord_no": "",
        "dmst_stex_tp": constants.ACCOUNT_DMST_STEX_TP_DEFAULT,
    }
    # ord_no is NOT an official kt00007 request field.
    assert "ord_no" not in call["body"]


@pytest.mark.asyncio
async def test_get_order_detail_sends_optional_fields_as_empty_strings():
    # ROB-1155 — the official Request Example spells the optional fields out as
    # "" rather than omitting the keys; key-omission is not documented as
    # equivalent to an empty value, so we mirror the example.
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_order_detail()
    body = fake.calls[-1]["body"]
    for key in ("ord_dt", "stk_cd", "fr_ord_no"):
        assert key in body
        assert body[key] == ""


@pytest.mark.asyncio
async def test_get_order_detail_maps_optional_inputs_onto_official_fields():
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_order_detail(
        order_date="20260729",
        qry_tp=constants.ACCOUNT_ORDER_DETAIL_QRY_TP_FILLED,
        symbol="005930",
        from_order_no="0000123",
        cont_yn="Y",
        next_key="page-2",
    )
    call = fake.calls[-1]
    assert call["body"] == {
        "ord_dt": "20260729",
        "qry_tp": "4",
        "stk_bond_tp": constants.ACCOUNT_ORDER_DETAIL_STK_BOND_TP_DEFAULT,
        "sell_tp": constants.ACCOUNT_ORDER_DETAIL_SELL_TP_DEFAULT,
        "stk_cd": "005930",
        "fr_ord_no": "0000123",
        "dmst_stex_tp": constants.ACCOUNT_DMST_STEX_TP_DEFAULT,
    }
    assert call["cont_yn"] == "Y"
    assert call["next_key"] == "page-2"


@pytest.mark.asyncio
async def test_get_order_detail_defaults_to_krx():
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_order_detail()
    assert fake.calls[-1]["body"]["dmst_stex_tp"] == constants.MOCK_EXCHANGE_KRX


@pytest.mark.asyncio
async def test_get_order_detail_allows_nxt_for_read_only_observation():
    # ROB-1155 — kt00007 is a read-only observation surface, so NXT is a legal
    # query scope (CP6 needs to see which venue the broker recorded). This does
    # NOT relax the order path: see
    # test_order_krx_pin_is_unaffected_by_read_venue_allowlist below.
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    await acct.get_order_detail(dmst_stex_tp="NXT")
    assert fake.calls[-1]["body"]["dmst_stex_tp"] == "NXT"


@pytest.mark.asyncio
@pytest.mark.parametrize("venue", ["%", "SOR", "", "krx-nxt", "ALL", None])
async def test_get_order_detail_rejects_venues_outside_the_read_allowlist(venue):
    # ROB-1155 — "%"(전체) and SOR are official kt00007 values but deliberately
    # NOT allowlisted: "%" would blend every venue into a surface whose safety
    # boundary is per-venue, and SOR has no observation need.
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    with pytest.raises(ValueError):
        await acct.get_order_detail(dmst_stex_tp=venue)
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("qry_tp", ["0", "5", "", "unfilled", None])
async def test_get_order_detail_rejects_qry_tp_outside_official_values(qry_tp):
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    with pytest.raises(ValueError):
        await acct.get_order_detail(qry_tp=qry_tp)
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("order_date", ["2026-07-29", "2026072", "abcdefgh"])
async def test_get_order_detail_rejects_malformed_order_date(order_date):
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    with pytest.raises(ValueError):
        await acct.get_order_detail(order_date=order_date)
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("from_order_no", ["12a", "0000123/../x", "12 34"])
async def test_get_order_detail_rejects_non_numeric_from_order_no(from_order_no):
    fake = FakeClient()
    acct = KiwoomDomesticAccountClient(fake)
    with pytest.raises(ValueError):
        await acct.get_order_detail(from_order_no=from_order_no)
    assert fake.calls == []


def test_order_krx_pin_is_unaffected_by_read_venue_allowlist():
    # ROB-1155 hard invariant — the read-only venue allowlist must never leak
    # into the order path. NXT/SOR stay rejected exchanges for orders, and the
    # order-side default stays KRX.
    assert constants.ACCOUNT_READ_VENUE_ALLOWLIST == frozenset({"KRX", "NXT"})
    assert constants.MOCK_REJECTED_EXCHANGES == frozenset({"NXT", "SOR"})
    assert constants.MOCK_EXCHANGE_KRX == "KRX"
    assert "NXT" in constants.MOCK_REJECTED_EXCHANGES
    assert "SOR" not in constants.ACCOUNT_READ_VENUE_ALLOWLIST
    assert "%" not in constants.ACCOUNT_READ_VENUE_ALLOWLIST


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
