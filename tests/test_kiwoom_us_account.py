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
