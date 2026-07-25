# tests/services/brokers/kis/test_live_order_expiry_session.py
import datetime

import pytest

from app.services.brokers.kis.live_order_expiry import (
    REASON_NXT_CARRY,
    REASON_REGULAR_BUY_CONSERVATIVE,
    REASON_REGULAR_BUY_UNSETTLED_1530,
    REASON_UNKNOWN_SESSION,
    SESSION_NXT_AFTER,
    SESSION_OFF,
    SESSION_PREMARKET,
    SESSION_REGULAR,
    classify_kr_accept_session,
    kr_day_order_expiry,
    parse_kis_ordered_at,
)

KST = datetime.timezone(datetime.timedelta(hours=9))


def _at(h, m):
    return datetime.datetime(2026, 7, 3, h, m, 0, tzinfo=KST)


@pytest.mark.unit
@pytest.mark.parametrize(
    "hh,mm,expected",
    [
        (8, 0, SESSION_PREMARKET),
        (8, 49, SESSION_PREMARKET),
        (8, 50, SESSION_OFF),  # premarket close is exclusive
        (9, 0, SESSION_REGULAR),
        (15, 29, SESSION_REGULAR),
        (15, 30, SESSION_OFF),  # regular close is exclusive
        (15, 45, SESSION_OFF),  # KRX-close↔NXT-open gap
        (16, 0, SESSION_NXT_AFTER),
        (19, 59, SESSION_NXT_AFTER),
        (20, 0, SESSION_OFF),  # NXT close is exclusive
        (7, 0, SESSION_OFF),
    ],
)
def test_classify_kr_accept_session_windows(hh, mm, expected):
    assert classify_kr_accept_session(_at(hh, mm)) == expected


@pytest.mark.unit
def test_classify_treats_naive_as_kst():
    naive = datetime.datetime(2026, 7, 3, 9, 30, 0)
    assert classify_kr_accept_session(naive) == SESSION_REGULAR


@pytest.mark.unit
def test_regular_sell_carries_to_nxt_2000():
    iso, reason = kr_day_order_expiry(accepted_at=_at(9, 30), side="sell")
    assert iso == "2026-07-03T20:00:00+09:00"
    assert reason == REASON_NXT_CARRY


@pytest.mark.unit
def test_regular_buy_conservative_default_is_2000():
    iso, reason = kr_day_order_expiry(accepted_at=_at(9, 30), side="buy")
    assert iso == "2026-07-03T20:00:00+09:00"
    assert reason == REASON_REGULAR_BUY_CONSERVATIVE


@pytest.mark.unit
def test_regular_buy_downgrade_gated_to_1530():
    iso, reason = kr_day_order_expiry(
        accepted_at=_at(9, 30), side="buy", unsettled_regular_buy_downgrade=True
    )
    assert iso == "2026-07-03T15:30:00+09:00"
    assert reason == REASON_REGULAR_BUY_UNSETTLED_1530


@pytest.mark.unit
def test_downgrade_does_not_touch_sell():
    iso, reason = kr_day_order_expiry(
        accepted_at=_at(9, 30), side="sell", unsettled_regular_buy_downgrade=True
    )
    assert iso == "2026-07-03T20:00:00+09:00"
    assert reason == REASON_NXT_CARRY


@pytest.mark.unit
@pytest.mark.parametrize("hh,mm", [(8, 10), (16, 30)])
def test_premarket_and_nxt_after_buy_carry_to_2000(hh, mm):
    iso, reason = kr_day_order_expiry(accepted_at=_at(hh, mm), side="buy")
    assert iso == "2026-07-03T20:00:00+09:00"
    assert reason == REASON_NXT_CARRY


@pytest.mark.unit
def test_off_session_is_unknown_reason_but_2000():
    iso, reason = kr_day_order_expiry(accepted_at=_at(15, 45), side="buy")
    assert iso == "2026-07-03T20:00:00+09:00"
    assert reason == REASON_UNKNOWN_SESSION


@pytest.mark.unit
def test_accept_session_override_skips_reclassify():
    # Pass a mismatching pre-classified session to prove it is honored verbatim.
    iso, reason = kr_day_order_expiry(
        accepted_at=_at(15, 45), side="buy", accept_session=SESSION_REGULAR
    )
    assert reason == REASON_REGULAR_BUY_CONSERVATIVE
    assert iso == "2026-07-03T20:00:00+09:00"


@pytest.mark.unit
def test_parse_kis_ordered_at_hhmmss():
    dt = parse_kis_ordered_at("20260703 093015")
    assert dt == datetime.datetime(2026, 7, 3, 9, 30, 15, tzinfo=KST)


@pytest.mark.unit
def test_parse_kis_ordered_at_short_time_padded():
    dt = parse_kis_ordered_at("20260703 0925")
    assert dt == datetime.datetime(2026, 7, 3, 9, 25, 0, tzinfo=KST)


@pytest.mark.unit
@pytest.mark.parametrize(
    "bad", ["", "  ", None, "20260703", "notadate 093015", "2026 09"]
)
def test_parse_kis_ordered_at_bad_returns_none(bad):
    assert parse_kis_ordered_at(bad) is None
