# tests/services/brokers/kis/test_live_order_expiry.py
from app.services.brokers.kis.live_order_expiry import classify_day_order_expiry


def _row(order_no="0011001100", prcs="", rvse=""):
    return {"odno": order_no, "prcs_stat_name": prcs, "rvse_cncl_dvsn_cd": rvse}


def test_live_status_token_stays_pending_even_when_market_closed():
    rows = [_row(prcs="접수완료")]
    assert classify_day_order_expiry(
        rows=rows, order_no="0011001100", market_closed=True
    ) == "pending"


def test_terminal_status_token_is_expired():
    rows = [_row(prcs="취소")]
    assert classify_day_order_expiry(
        rows=rows, order_no="0011001100", market_closed=False
    ) == "expired"


def test_time_guard_expires_when_market_closed_and_no_status():
    rows = [_row(prcs="")]
    assert classify_day_order_expiry(
        rows=rows, order_no="0011001100", market_closed=True
    ) == "expired"


def test_stays_pending_when_market_open_and_no_status():
    rows = [_row(prcs="")]
    assert classify_day_order_expiry(
        rows=rows, order_no="0011001100", market_closed=False
    ) == "pending"


def test_no_matching_row_stays_pending():
    # No row for this order_no → not our branch's job; stay pending (caller
    # already routes NONE-verdict elsewhere).
    rows = [_row(order_no="9999999999", prcs="")]
    assert classify_day_order_expiry(
        rows=rows, order_no="0011001100", market_closed=True
    ) == "pending"
