from __future__ import annotations

from decimal import Decimal
from typing import Any

import pytest

from app.services.brokers.kiwoom import constants
from app.services.brokers.kiwoom.us_orders import (
    KiwoomUsOrderClient,
    KiwoomUsOrderRejected,
    build_us_place_order_body,
    validate_us_order_id,
)


class FakeClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []
        self.account_no = "US-MOCK"

    async def post_api(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
        return {"return_code": 0, "ord_no": "000000282"}


def test_limit_and_market_body_format() -> None:
    assert build_us_place_order_body(
        side="buy",
        symbol="NVDA",
        stex_tp="ND",
        quantity=10,
        trde_tp="00",
        price=Decimal("213.0400"),
    ) == {
        "stex_tp": "ND",
        "stk_cd": "NVDA",
        "ord_qty": "10",
        "ord_uv": "213.0400",
        "trde_tp": "00",
    }
    assert (
        build_us_place_order_body(
            side="buy",
            symbol="NVDA",
            stex_tp="ND",
            quantity=1,
            trde_tp="03",
            price=None,
        )["ord_uv"]
        == ""
    )


def test_market_rejects_price_and_limit_requires_price() -> None:
    with pytest.raises(KiwoomUsOrderRejected, match="requires price"):
        build_us_place_order_body(
            side="buy",
            symbol="NVDA",
            stex_tp="ND",
            quantity=1,
            trde_tp="00",
            price=None,
        )
    with pytest.raises(KiwoomUsOrderRejected, match="must omit price"):
        build_us_place_order_body(
            side="buy",
            symbol="NVDA",
            stex_tp="ND",
            quantity=1,
            trde_tp="03",
            price=Decimal("1"),
        )


@pytest.mark.parametrize("trde_tp", ["26", "27", "30"])
def test_documented_advanced_buy_types_preserve_limit_price(trde_tp: str) -> None:
    body = build_us_place_order_body(
        side="buy",
        symbol="BRK.B",
        stex_tp="NY",
        quantity=1,
        trde_tp=trde_tp,
        price="123.4500",
    )
    assert body["ord_uv"] == "123.4500"


def test_documented_sell_stop_types_require_stop_price() -> None:
    stop_limit = build_us_place_order_body(
        side="sell",
        symbol="NVDA",
        stex_tp="ND",
        quantity=1,
        trde_tp="34",
        price="210.00",
        stop_price="205.00",
    )
    assert stop_limit["ord_uv"] == "210.00"
    assert stop_limit["stop_pric"] == "205.00"

    stop_market = build_us_place_order_body(
        side="sell",
        symbol="NVDA",
        stex_tp="ND",
        quantity=1,
        trde_tp="35",
        price=None,
        stop_price="205.00",
    )
    assert stop_market["ord_uv"] == ""
    assert stop_market["stop_pric"] == "205.00"

    with pytest.raises(KiwoomUsOrderRejected, match="requires stop_price"):
        build_us_place_order_body(
            side="sell",
            symbol="NVDA",
            stex_tp="ND",
            quantity=1,
            trde_tp="35",
            price=None,
        )


@pytest.mark.asyncio
async def test_buy_sell_modify_cancel_use_exact_us_payloads() -> None:
    fake = FakeClient()
    client = KiwoomUsOrderClient(fake)

    await client.place_buy_order(
        symbol="NVDA", stex_tp="ND", quantity=1, trde_tp="00", price="213.04"
    )
    await client.place_sell_order(
        symbol="TSM", stex_tp="NY", quantity=2, trde_tp="03", price=None
    )
    await client.modify_order(
        original_order_no="000000282",
        symbol="NVDA",
        stex_tp="ND",
        new_price="210.00",
        stop_price="205.00",
    )
    await client.cancel_order(
        original_order_no="000000283", symbol="NVDA", stex_tp="ND"
    )

    assert [call["api_id"] for call in fake.calls] == [
        constants.US_ORDER_BUY_API_ID,
        constants.US_ORDER_SELL_API_ID,
        constants.US_ORDER_MODIFY_API_ID,
        constants.US_ORDER_CANCEL_API_ID,
    ]
    assert all(call["path"] == constants.US_ORDER_PATH for call in fake.calls)
    assert fake.calls[2]["body"] == {
        "orig_ord_no": "000000282",
        "stex_tp": "ND",
        "stk_cd": "NVDA",
        "mdfy_uv": "210.00",
        "stop_pric": "205.00",
    }
    assert fake.calls[3]["body"] == {
        "orig_ord_no": "000000283",
        "stex_tp": "ND",
        "stk_cd": "NVDA",
    }


@pytest.mark.parametrize("exchange", ["KRX", "NXT", "NASD", ""])
def test_rejects_non_kiwoom_us_exchange(exchange: str) -> None:
    with pytest.raises(KiwoomUsOrderRejected, match="stex_tp"):
        build_us_place_order_body(
            side="buy",
            symbol="NVDA",
            stex_tp=exchange,
            quantity=1,
            trde_tp="00",
            price="1",
        )


@pytest.mark.parametrize("order_id", ["", "00000028A", "../000000282", "1" * 19])
def test_rejects_non_digit_or_unbounded_order_id(order_id: str) -> None:
    with pytest.raises(KiwoomUsOrderRejected, match="digits"):
        validate_us_order_id(order_id)


@pytest.mark.parametrize("order_id", ["000000282", "282", "1" * 18])
def test_accepts_bounded_all_digit_order_id(order_id: str) -> None:
    """ROB-867 review P2: documented nine digits is unverified against the
    live mock, so cancel/modify must accept any bounded all-digit id rather
    than strand an accepted order over a width mismatch."""
    assert validate_us_order_id(order_id) == order_id
