from __future__ import annotations

import pytest

from app.mcp_server.tooling.orders_modify_cancel import (
    _map_kis_status,
    _normalize_kis_domestic_order,
)

@pytest.mark.unit
@pytest.mark.parametrize(
    ("filled", "remaining", "status_name", "expected"),
    [
        (10, 0, "체결", "filled"),
        (0, 10, "접수", "pending"),
        (5, 5, "체결", "partial"),
        (0, 0, "주문취소", "cancelled"),
        (10, 0, None, "filled"),
        (10, 0, "", "filled"),
        (5, 5, None, "partial"),
        (0, 10, None, "pending"),
        (0, 0, None, "pending"),
    ],
)
def test_map_kis_status_handles_named_and_unnamed_statuses(
    filled: int,
    remaining: int,
    status_name: str | None,
    expected: str,
) -> None:
    assert _map_kis_status(filled, remaining, status_name) == expected

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
    ("order", "expected_status", "expected_filled_qty", "expected_remaining_qty", "expected_avg_price"),
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
