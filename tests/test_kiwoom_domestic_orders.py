# tests/test_kiwoom_domestic_orders.py
"""Verify Kiwoom domestic order payloads for buy/sell/modify/cancel."""

from __future__ import annotations

from typing import Any

import pytest

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.domestic_orders import (
    KiwoomDomesticOrderClient,
    KiwoomOrderRejected,
)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.account_no = "12345678-01"

    async def post_api(
        self,
        *,
        api_id: str,
        path: str,
        body: dict[str, Any],
        cont_yn: str | None = None,
        next_key: str | None = None,
    ) -> dict[str, Any]:
        self.calls.append(
            {
                "api_id": api_id,
                "path": path,
                "body": body,
                "cont_yn": cont_yn,
                "next_key": next_key,
            }
        )
        return {
            "return_code": 0,
            "return_msg": "정상",
            "ord_no": "0000111222",
            "continuation": {"cont_yn": "N", "next_key": ""},
        }


@pytest.mark.asyncio
async def test_buy_order_uses_kt10000_and_krx_only():
    fake = FakeClient()
    orders = KiwoomDomesticOrderClient(fake)

    result = await orders.place_buy_order(
        symbol="005930",
        quantity=1,
        price=70000,
    )
    call = fake.calls[-1]

    assert call["api_id"] == constants.ORDER_BUY_API_ID
    assert call["path"] == constants.ORDER_PATH
    assert call["body"]["dmst_stex_tp"] == constants.MOCK_EXCHANGE_KRX
    assert call["body"]["stk_cd"] == "005930"
    assert call["body"]["ord_qty"] == "1"
    assert call["body"]["ord_uv"] == "70000"
    assert result["ord_no"] == "0000111222"


@pytest.mark.asyncio
async def test_sell_order_uses_kt10001():
    fake = FakeClient()
    orders = KiwoomDomesticOrderClient(fake)

    await orders.place_sell_order(symbol="005930", quantity=2, price=71000)

    assert fake.calls[-1]["api_id"] == constants.ORDER_SELL_API_ID
    assert fake.calls[-1]["body"]["ord_qty"] == "2"
    assert fake.calls[-1]["body"]["ord_uv"] == "71000"


@pytest.mark.asyncio
async def test_modify_order_uses_kt10002_and_carries_orig_no():
    fake = FakeClient()
    orders = KiwoomDomesticOrderClient(fake)

    await orders.modify_order(
        original_order_no="0000111222",
        symbol="005930",
        new_quantity=3,
        new_price=72000,
    )

    body = fake.calls[-1]["body"]
    assert fake.calls[-1]["api_id"] == constants.ORDER_MODIFY_API_ID
    assert body["orig_ord_no"] == "0000111222"
    assert body["mdfy_qty"] == "3"
    assert body["mdfy_uv"] == "72000"
    assert body["dmst_stex_tp"] == constants.MOCK_EXCHANGE_KRX


@pytest.mark.asyncio
async def test_cancel_order_uses_kt10003():
    fake = FakeClient()
    orders = KiwoomDomesticOrderClient(fake)

    await orders.cancel_order(
        original_order_no="0000111222",
        symbol="005930",
        cancel_quantity=1,
    )

    body = fake.calls[-1]["body"]
    assert fake.calls[-1]["api_id"] == constants.ORDER_CANCEL_API_ID
    assert body["orig_ord_no"] == "0000111222"
    assert body["cncl_qty"] == "1"
    assert body["dmst_stex_tp"] == constants.MOCK_EXCHANGE_KRX


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_exchange", ["NXT", "SOR", "nxt", "sor"])
async def test_buy_rejects_nxt_and_sor(bad_exchange):
    fake = FakeClient()
    orders = KiwoomDomesticOrderClient(fake)

    with pytest.raises(KiwoomOrderRejected, match="KRX"):
        await orders.place_buy_order(
            symbol="005930",
            quantity=1,
            price=70000,
            exchange=bad_exchange,
        )
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "bad_id",
    ["", "   ", "../etc", "a/b", "a?b=c", "a,b", "a b", "a\nb"],
)
async def test_cancel_rejects_unsafe_order_ids(bad_id):
    fake = FakeClient()
    orders = KiwoomDomesticOrderClient(fake)

    with pytest.raises(KiwoomOrderRejected):
        await orders.cancel_order(
            original_order_no=bad_id,
            symbol="005930",
            cancel_quantity=1,
        )
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_quantity", [0, -1])
async def test_buy_rejects_non_positive_quantity_before_post_api(bad_quantity):
    fake = FakeClient()
    orders = KiwoomDomesticOrderClient(fake)

    with pytest.raises(KiwoomOrderRejected, match="quantity"):
        await orders.place_buy_order(
            symbol="005930",
            quantity=bad_quantity,
            price=70000,
        )
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_price", [0, -1])
async def test_buy_rejects_non_positive_price_before_post_api(bad_price):
    fake = FakeClient()
    orders = KiwoomDomesticOrderClient(fake)

    with pytest.raises(KiwoomOrderRejected, match="price"):
        await orders.place_buy_order(
            symbol="005930",
            quantity=1,
            price=bad_price,
        )
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_quantity", [0, -1])
async def test_sell_rejects_non_positive_quantity_before_post_api(bad_quantity):
    fake = FakeClient()
    orders = KiwoomDomesticOrderClient(fake)

    with pytest.raises(KiwoomOrderRejected, match="quantity"):
        await orders.place_sell_order(
            symbol="005930",
            quantity=bad_quantity,
            price=71000,
        )
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_price", [0, -1])
async def test_sell_rejects_non_positive_price_before_post_api(bad_price):
    fake = FakeClient()
    orders = KiwoomDomesticOrderClient(fake)

    with pytest.raises(KiwoomOrderRejected, match="price"):
        await orders.place_sell_order(
            symbol="005930",
            quantity=1,
            price=bad_price,
        )
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_quantity", [0, -1])
async def test_modify_rejects_non_positive_quantity_before_post_api(bad_quantity):
    fake = FakeClient()
    orders = KiwoomDomesticOrderClient(fake)

    with pytest.raises(KiwoomOrderRejected, match="new_quantity"):
        await orders.modify_order(
            original_order_no="0000111222",
            symbol="005930",
            new_quantity=bad_quantity,
            new_price=72000,
        )
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_price", [0, -1])
async def test_modify_rejects_non_positive_price_before_post_api(bad_price):
    fake = FakeClient()
    orders = KiwoomDomesticOrderClient(fake)

    with pytest.raises(KiwoomOrderRejected, match="new_price"):
        await orders.modify_order(
            original_order_no="0000111222",
            symbol="005930",
            new_quantity=1,
            new_price=bad_price,
        )
    assert fake.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("bad_quantity", [0, -1])
async def test_cancel_rejects_non_positive_quantity_before_post_api(bad_quantity):
    fake = FakeClient()
    orders = KiwoomDomesticOrderClient(fake)

    with pytest.raises(KiwoomOrderRejected, match="cancel_quantity"):
        await orders.cancel_order(
            original_order_no="0000111222",
            symbol="005930",
            cancel_quantity=bad_quantity,
        )
    assert fake.calls == []
