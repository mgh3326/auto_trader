# tests/mcp_server/tooling/test_orders_history_expiry_reason.py
import pytest

from app.mcp_server.tooling.orders_modify_cancel import (
    _normalize_kis_domestic_order,
    _normalize_kis_overseas_order,
)


@pytest.mark.unit
def test_kr_domestic_regular_buy_reason_is_conservative():
    row = {
        "odno": "0011001100",
        "pdno": "005930",
        "sll_buy_dvsn_cd": "02",  # buy
        "ord_qty": "3",
        "rmn_qty": "3",
        "ord_unpr": "70000",
        "ord_dt": "20260703",
        "ord_tmd": "093015",
    }
    out = _normalize_kis_domestic_order(row)
    assert out["expiry_reason"] == "regular_buy_conservative_20_00"


@pytest.mark.unit
def test_kr_domestic_regular_sell_reason_is_nxt_carry():
    row = {
        "odno": "0011001101",
        "pdno": "005930",
        "sll_buy_dvsn_cd": "01",  # sell
        "ord_qty": "3",
        "rmn_qty": "3",
        "ord_unpr": "70000",
        "ord_dt": "20260703",
        "ord_tmd": "093015",
    }
    out = _normalize_kis_domestic_order(row)
    assert out["expiry_reason"] == "nxt_carry"


@pytest.mark.unit
def test_kr_domestic_unparseable_ordered_at_reason_none():
    row = {
        "odno": "0011001102",
        "pdno": "005930",
        "sll_buy_dvsn_cd": "02",
        "ord_qty": "3",
        "rmn_qty": "3",
        "ord_unpr": "70000",
        # no ord_dt / ord_tmd → ordered_at is " " → unparseable
    }
    out = _normalize_kis_domestic_order(row)
    assert out["expiry_reason"] is None


@pytest.mark.unit
def test_us_overseas_reason_is_us_day_order():
    row = {
        "odno": "US-9",
        "pdno": "AAPL",
        "sll_buy_dvsn_cd": "02",
        "ft_ord_qty": "1",
        "nccs_qty": "1",
        "ft_ord_unpr3": "200",
        "ord_dt": "20260703",
        "ord_tmd": "230000",
    }
    out = _normalize_kis_overseas_order(row)
    assert out["expiry_reason"] == "us_day_order"
