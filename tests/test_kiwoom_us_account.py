from __future__ import annotations

from typing import Any

import pytest

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.us_account import (
    KiwoomUsAccountClient,
    extract_usd_deposit,
)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.account_no = "US-MOCK"

    async def post_api(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"return_code": 0, "result_list": []}


@pytest.mark.asyncio
async def test_account_methods_use_proven_tr_ids_and_optional_filters() -> None:
    fake = FakeClient()
    account = KiwoomUsAccountClient(fake)

    await account.get_open_orders(
        side_code="2", stex_tp="ND", symbol="NVDA", cont_yn="Y", next_key="p2"
    )
    await account.get_positions(stex_tp="NY", symbol="TSM")
    await account.get_today_orders(side_code="0")
    await account.get_us_deposit_detail()

    assert fake.calls[0] == {
        "api_id": constants.US_ACCOUNT_OPEN_ORDERS_API_ID,
        "path": constants.US_ACCOUNT_PATH,
        "body": {"slby_tp": "2", "stex_tp": "ND", "stk_cd": "NVDA"},
        "cont_yn": "Y",
        "next_key": "p2",
    }
    assert fake.calls[1]["body"] == {"stex_tp": "NY", "stk_cd": "TSM"}
    assert fake.calls[2]["api_id"] == constants.US_ACCOUNT_TODAY_ORDERS_API_ID
    assert fake.calls[3]["api_id"] == constants.US_ACCOUNT_DEPOSIT_DETAIL_API_ID
    assert fake.calls[3]["body"] == {}


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        ({"d0_usd_fx_entr": "18042538.7700"}, "18042538.7700"),
        ({"d0_usd_fx_entr": "1,234.50"}, "1234.50"),
        ({"d0_usd_fx_entr": ""}, None),
        ({"d0_usd_fx_entr": "not-a-number"}, None),
        ({}, None),
    ],
)
def test_extract_usd_deposit_is_precise_and_fail_closed(
    payload: dict[str, Any], expected: str | None
) -> None:
    assert extract_usd_deposit(payload) == expected


# ROB-1088 — US order-history/open-orders reads use their own TR ids
# (ust21050/ust21070/ust21510/ust21160), never kt00009. This is a scope
# boundary regression guard, not new behavior: KiwoomUsAccountClient never
# references ACCOUNT_ORDER_STATUS_API_ID ("kt00009") or mrkt_tp, so the
# kt00009 mrkt_tp gap (ROB-1111/ROB-1088) has no US counterpart to fix.
@pytest.mark.asyncio
async def test_us_account_methods_never_use_kt00009_or_mrkt_tp() -> None:
    fake = FakeClient()
    account = KiwoomUsAccountClient(fake)

    await account.get_open_orders()
    await account.get_positions()
    await account.get_today_orders()
    await account.get_foreign_deposit()
    await account.get_us_deposit_detail()

    for call in fake.calls:
        assert call["api_id"] != constants.ACCOUNT_ORDER_STATUS_API_ID
        assert "mrkt_tp" not in call["body"]


def test_us_account_api_ids_are_ust_prefixed_not_kt00009() -> None:
    # ROB-1088 — explicit constant-level boundary: the US TR id family is
    # entirely separate ("ust2xxxx"), confirming KR's kt00009 mrkt_tp gap
    # (ROB-1111) is KR-only by construction, not by incidental non-use.
    us_api_ids = {
        constants.US_ACCOUNT_OPEN_ORDERS_API_ID,
        constants.US_ACCOUNT_POSITIONS_API_ID,
        constants.US_ACCOUNT_TODAY_ORDERS_API_ID,
        constants.US_ACCOUNT_DEPOSIT_DETAIL_API_ID,
        constants.US_ACCOUNT_FOREIGN_DEPOSIT_API_ID,
    }
    assert constants.ACCOUNT_ORDER_STATUS_API_ID not in us_api_ids
    for api_id in us_api_ids:
        assert api_id.startswith("ust")
