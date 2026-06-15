from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.schemas.open_orders import (
    OpenOrderRow,
    OpenOrdersResponse,
    OpenOrderSourceState,
)


def test_open_orders_schema_serializes_decimal_rows() -> None:
    ordered_at = dt.datetime(2026, 6, 15, 9, 1, tzinfo=dt.UTC)
    response = OpenOrdersResponse(
        market="all",
        count=1,
        data_state="ok",
        as_of=ordered_at,
        items=[
            OpenOrderRow(
                broker="kis",
                market="kr",
                symbol="005930",
                symbol_name="삼성전자",
                side="buy",
                order_type="limit",
                time_in_force=None,
                price=Decimal("70000"),
                quantity=Decimal("10"),
                remaining_qty=Decimal("8"),
                filled_qty=Decimal("2"),
                status="pending",
                raw_status="접수",
                ordered_at=ordered_at,
                order_no="K1",
                exchange="KRX",
                currency="KRW",
            )
        ],
        sources=[
            OpenOrderSourceState(
                broker="kis",
                market="kr",
                status="ok",
                fetched_at=ordered_at,
                count=1,
                message=None,
            )
        ],
        warnings=[],
        empty_reason=None,
    )

    dumped = response.model_dump(mode="json")
    assert dumped["data_state"] == "ok"
    assert dumped["items"][0]["price"] == "70000"
    assert dumped["items"][0]["remaining_qty"] == "8"
    assert dumped["sources"][0]["broker"] == "kis"


def test_normalize_kis_kr_order_maps_domestic_pending_shape() -> None:
    from app.services.current_orders_service import normalize_kis_order
    row = normalize_kis_order(
        {
            "ord_no": "K1",
            "pdno": "005930",
            "prdt_name": "삼성전자",
            "sll_buy_dvsn_cd": "02",
            "ord_qty": "10",
            "ord_unpr": "70000",
            "rmn_qty": "8",
            "ord_dt": "20260615",
            "ord_tmd": "090100",
            "ord_dvsn_name": "지정가",
        },
        market="kr",
        exchange="KRX",
    )

    assert row.broker == "kis"
    assert row.market == "kr"
    assert row.symbol == "005930"
    assert row.symbol_name == "삼성전자"
    assert row.side == "buy"
    assert row.price == Decimal("70000")
    assert row.quantity == Decimal("10")
    assert row.remaining_qty == Decimal("8")
    assert row.order_no == "K1"
    assert row.exchange == "KRX"
    assert row.currency == "KRW"
    assert row.ordered_at is not None
    assert row.ordered_at.tzinfo is not None


def test_normalize_kis_us_order_maps_overseas_pending_shape() -> None:
    from app.services.current_orders_service import normalize_kis_order
    row = normalize_kis_order(
        {
            "odno": "U1",
            "pdno": "AAPL",
            "prdt_name": "Apple",
            "sll_buy_dvsn_cd": "01",
            "ft_ord_qty": "5",
            "ft_ord_unpr3": "180.5",
            "ft_ccld_qty": "2",
            "nccs_qty": "3",
            "prcs_stat_name": "접수",
            "ord_dt": "20260615",
            "ord_tmd": "223000",
        },
        market="us",
        exchange="NASD",
    )

    assert row.market == "us"
    assert row.symbol == "AAPL"
    assert row.side == "sell"
    assert row.price == Decimal("180.5")
    assert row.quantity == Decimal("5")
    assert row.filled_qty == Decimal("2")
    assert row.remaining_qty == Decimal("3")
    assert row.exchange == "NASD"
    assert row.currency == "USD"


def test_normalize_upbit_order_maps_wait_order_shape() -> None:
    from app.services.current_orders_service import normalize_upbit_order
    row = normalize_upbit_order(
        {
            "uuid": "UP1",
            "market": "KRW-BTC",
            "side": "bid",
            "ord_type": "limit",
            "price": "96000000",
            "volume": "0.01",
            "remaining_volume": "0.006",
            "executed_volume": "0.004",
            "state": "wait",
            "created_at": "2026-06-15T00:01:00+00:00",
        }
    )

    assert row.broker == "upbit"
    assert row.market == "crypto"
    assert row.symbol == "KRW-BTC"
    assert row.side == "buy"
    assert row.order_type == "limit"
    assert row.price == Decimal("96000000")
    assert row.quantity == Decimal("0.01")
    assert row.remaining_qty == Decimal("0.006")
    assert row.filled_qty == Decimal("0.004")
    assert row.status == "pending"
    assert row.raw_status == "wait"
    assert row.exchange == "UPBIT"
    assert row.currency == "KRW"

