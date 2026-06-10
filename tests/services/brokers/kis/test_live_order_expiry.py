import datetime

from app.services.brokers.kis.live_order_expiry import (
    classify_day_order_expiry,
    nxt_session_closed,
)

KST = datetime.timezone(datetime.timedelta(hours=9))


def _row(order_no="0011001100", prcs="", rvse=""):
    return {"odno": order_no, "prcs_stat_name": prcs, "rvse_cncl_dvsn_cd": rvse}


def test_live_status_token_stays_pending_even_when_market_closed():
    rows = [_row(prcs="접수완료")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0011001100", market_closed=True)
        == "pending"
    )


def test_terminal_status_token_is_expired():
    rows = [_row(prcs="취소")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0011001100", market_closed=False)
        == "expired"
    )


def test_time_guard_expires_when_market_closed_and_no_status():
    rows = [_row(prcs="")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0011001100", market_closed=True)
        == "expired"
    )


def test_stays_pending_when_market_open_and_no_status():
    rows = [_row(prcs="")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0011001100", market_closed=False)
        == "pending"
    )


def test_no_matching_row_stays_pending():
    # No row for this order_no → not our branch's job; stay pending (caller
    # already routes NONE-verdict elsewhere).
    rows = [_row(order_no="9999999999", prcs="")]
    assert (
        classify_day_order_expiry(rows=rows, order_no="0011001100", market_closed=True)
        == "pending"
    )


# --- nxt_session_closed (ROB-487) ----------------------------------------------


def test_nxt_open_before_2000_kst_same_day():
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 19, 59, tzinfo=KST),
        )
        is False
    )


def test_nxt_closed_at_exactly_2000_kst():
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 20, 0, tzinfo=KST),
        )
        is True
    )


def test_nxt_closed_next_day_morning():
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 10, 9, 3, tzinfo=KST),
        )
        is True
    )


def test_nxt_naive_now_assumed_kst():
    # naive now 는 KST 관례 (app/core/timezone.to_kst_naive 와 동일 가정)
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 19, 2),
        )
        is False
    )


def test_nxt_utc_aware_now_converted():
    # 6/9 11:30 UTC == 6/9 20:30 KST → closed
    assert (
        nxt_session_closed(
            order_date=datetime.date(2026, 6, 9),
            now=datetime.datetime(2026, 6, 9, 11, 30, tzinfo=datetime.UTC),
        )
        is True
    )
