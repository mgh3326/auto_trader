from __future__ import annotations

import pytest

from app.mcp_server.tooling.orders_modify_cancel import (
    _map_kis_status,
    _normalize_kis_domestic_order,
    _normalize_kis_overseas_order,
)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("ordered", "filled", "remaining", "status_name", "expected"),
    [
        # Live / filled / partial — unchanged behavior.
        (10, 10, 0, "체결", "filled"),
        (10, 0, 10, "접수", "pending"),
        (10, 5, 5, "체결", "partial"),
        (10, 10, 0, None, "filled"),
        (10, 10, 0, "", "filled"),
        (10, 5, 5, None, "partial"),
        (10, 0, 10, None, "pending"),
        # Explicit cancel evidence wins even with 0/0.
        (8, 0, 0, "주문취소", "cancelled"),
        # ROB-657: dead order (nothing filled, nothing left) → expired,
        # regardless of a stale/absent status name.
        (8, 0, 0, None, "expired"),
        (8, 0, 0, "", "expired"),
        (8, 0, 0, "접수", "expired"),
        (8, 0, 0, "미체결", "expired"),
        # Degenerate empty row → no order to expire.
        (0, 0, 0, None, "pending"),
    ],
)
def test_map_kis_status_handles_named_and_unnamed_statuses(
    ordered: int,
    filled: int,
    remaining: int,
    status_name: str | None,
    expected: str,
) -> None:
    assert _map_kis_status(ordered, filled, remaining, status_name) == expected


def _build_domestic_order(**overrides: str) -> dict[str, str]:
    base = {
        "ord_dt": "20260401",
        "ord_tmd": "095032",
        "odno": "0012345678",
        "sll_buy_dvsn_cd": "02",
        "pdno": "035720",
        "prdt_name": "카카오",
        "ord_qty": "10",
        "ord_unpr": "47500",
    }
    base.update(overrides)
    return base


@pytest.mark.unit
@pytest.mark.parametrize(
    (
        "order",
        "expected_status",
        "expected_filled_qty",
        "expected_remaining_qty",
        "expected_avg_price",
    ),
    [
        (
            _build_domestic_order(
                tot_ccld_qty="10",
                avg_prvs="47250",
                rmn_qty="0",
                ccld_cndt_name="없음",
                excg_id_dvsn_cd="SOR",
                ordr_empno="OpnAPI",
            ),
            "filled",
            10,
            0,
            47250,
        ),
        (
            _build_domestic_order(
                tot_ccld_qty="0",
                avg_prvs="0",
                rmn_qty="10",
                ccld_cndt_name="없음",
            ),
            "pending",
            0,
            10,
            0,
        ),
        (
            _build_domestic_order(
                tot_ccld_qty="5",
                avg_prvs="47250",
                rmn_qty="5",
                ccld_cndt_name="없음",
            ),
            "partial",
            5,
            5,
            47250,
        ),
    ],
)
def test_normalize_kis_domestic_order_supports_output1_field_names(
    order: dict[str, str],
    expected_status: str,
    expected_filled_qty: int,
    expected_remaining_qty: int,
    expected_avg_price: int,
) -> None:
    normalized = _normalize_kis_domestic_order(order)

    assert normalized["status"] == expected_status
    assert normalized["filled_qty"] == expected_filled_qty
    assert normalized["remaining_qty"] == expected_remaining_qty
    assert normalized["filled_avg_price"] == expected_avg_price


@pytest.mark.unit
def test_normalize_kis_domestic_order_keeps_output_compatibility_for_filled() -> None:
    normalized = _normalize_kis_domestic_order(
        _build_domestic_order(
            ccld_qty="10",
            ccld_unpr="47250",
            prcs_stat_name="체결",
        )
    )

    assert normalized["status"] == "filled"
    assert normalized["filled_qty"] == 10
    assert normalized["remaining_qty"] == 0
    assert normalized["filled_avg_price"] == 47250


@pytest.mark.unit
def test_normalize_kis_domestic_order_keeps_output_pending_status() -> None:
    normalized = _normalize_kis_domestic_order(
        _build_domestic_order(
            ccld_qty="0",
            ccld_unpr="0",
            prcs_stat_name="미체결",
        )
    )

    assert normalized["status"] == "pending"
    assert normalized["filled_qty"] == 0
    assert normalized["remaining_qty"] == 10


@pytest.mark.unit
def test_normalize_kis_domestic_order_dead_order_is_expired_not_live() -> None:
    # ROB-657 repro: 기아 000270, 8 ordered, 0 filled, 0 remaining.
    normalized = _normalize_kis_domestic_order(
        _build_domestic_order(
            pdno="000270",
            ord_qty="8",
            tot_ccld_qty="0",
            rmn_qty="0",
        )
    )

    assert normalized["status"] == "expired"
    assert normalized["is_live"] is False
    assert normalized["ordered_qty"] == 8
    assert normalized["filled_qty"] == 0
    assert normalized["remaining_qty"] == 0


@pytest.mark.unit
def test_normalize_kis_domestic_order_live_pending_is_live() -> None:
    normalized = _normalize_kis_domestic_order(
        _build_domestic_order(
            ord_qty="10",
            tot_ccld_qty="0",
            rmn_qty="10",
        )
    )

    assert normalized["status"] == "pending"
    assert normalized["is_live"] is True


@pytest.mark.unit
def test_normalize_kis_overseas_order_reports_is_live() -> None:
    live = _normalize_kis_overseas_order(
        {
            "odno": "0007654321",
            "sll_buy_dvsn_cd": "02",
            "pdno": "AAPL",
            "ft_ord_qty": "10",
            "ft_ccld_qty": "0",
            "ft_ord_unpr3": "200.5",
            "ord_dt": "20260401",
            "ord_tmd": "223000",
        }
    )
    assert live["status"] == "pending"
    assert live["is_live"] is True
    assert live["remaining_qty"] == 10

    done = _normalize_kis_overseas_order(
        {
            "odno": "0007654322",
            "sll_buy_dvsn_cd": "02",
            "pdno": "AAPL",
            "ft_ord_qty": "10",
            "ft_ccld_qty": "10",
            "ft_ccld_unpr3": "201.0",
            "ord_dt": "20260401",
            "ord_tmd": "223500",
        }
    )
    assert done["status"] == "filled"
    assert done["is_live"] is False
