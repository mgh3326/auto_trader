"""#925 — KR day-order expiry by session x venue (KRX after-market)."""

import datetime

import pytest

from app.services.brokers.kis.live_order_expiry import (
    REASON_KRX_AFTER_CLOSE,
    REASON_KRX_REGULAR_CLOSE,
    REASON_NXT_CARRY,
    REASON_REGULAR_BUY_CONSERVATIVE,
    REASON_UNKNOWN_SESSION,
    SESSION_KRX_AFTER,
    SESSION_NXT_AFTER,
    SESSION_OFF,
    SESSION_PREMARKET,
    SESSION_REGULAR,
    classify_kr_accept_session,
    kr_day_order_expiry,
)

KST = datetime.timezone(datetime.timedelta(hours=9))


def _at(h, m, s=0, us=0):
    return datetime.datetime(2026, 9, 29, h, m, s, us, tzinfo=KST)


@pytest.mark.unit
@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (_at(8, 0), SESSION_OFF),  # 08:00-08:50 is NXT-only
        (_at(8, 49, 59), SESSION_OFF),
        (_at(9, 0), SESSION_REGULAR),
        (_at(15, 29, 59), SESSION_REGULAR),
        (_at(15, 30), SESSION_OFF),
        (_at(15, 59, 59, 999999), SESSION_OFF),
        (_at(16, 0), SESSION_KRX_AFTER),
        (_at(19, 59, 59, 999999), SESSION_KRX_AFTER),
        (_at(20, 0), SESSION_OFF),
    ],
)
def test_krx_only_accept_session_windows(at, expected):
    assert classify_kr_accept_session(at, nxt_tradable=False) == expected


@pytest.mark.unit
@pytest.mark.parametrize("nxt_tradable", [None, True])
@pytest.mark.parametrize(
    ("at", "expected"),
    [
        (_at(8, 0), SESSION_PREMARKET),
        (_at(16, 0), SESSION_NXT_AFTER),
        (_at(19, 59, 59), SESSION_NXT_AFTER),
        (_at(20, 0), SESSION_OFF),
    ],
)
def test_nxt_or_unknown_venue_keeps_legacy_labels(at, expected, nxt_tradable):
    assert classify_kr_accept_session(at, nxt_tradable=nxt_tradable) == expected


@pytest.mark.unit
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_krx_after_order_dies_at_the_2000_krx_close(side):
    for at in (_at(16, 0), _at(19, 59, 59)):
        iso, reason = kr_day_order_expiry(accepted_at=at, side=side, nxt_tradable=False)
        assert iso == "2026-09-29T20:00:00+09:00"
        assert reason == REASON_KRX_AFTER_CLOSE
    iso, reason = kr_day_order_expiry(
        accepted_at=_at(17, 0), side=side, accept_session=SESSION_KRX_AFTER
    )
    assert (iso, reason) == ("2026-09-29T20:00:00+09:00", REASON_KRX_AFTER_CLOSE)


@pytest.mark.unit
@pytest.mark.parametrize("side", ["buy", "sell"])
def test_krx_only_regular_order_does_not_carry_past_1530(side):
    """#876: a regular-session order dies at the close; evening is a new order."""
    iso, reason = kr_day_order_expiry(
        accepted_at=_at(10, 0), side=side, nxt_tradable=False
    )
    assert iso == "2026-09-29T15:30:00+09:00"
    assert reason == REASON_KRX_REGULAR_CLOSE


@pytest.mark.unit
def test_krx_only_morning_accept_is_unknown_session():
    iso, reason = kr_day_order_expiry(
        accepted_at=_at(8, 10), side="sell", nxt_tradable=False
    )
    assert reason == REASON_UNKNOWN_SESSION
    assert iso == "2026-09-29T20:00:00+09:00"


@pytest.mark.unit
def test_unknown_venue_keeps_rob671_defaults():
    assert kr_day_order_expiry(accepted_at=_at(10, 0), side="sell") == (
        "2026-09-29T20:00:00+09:00",
        REASON_NXT_CARRY,
    )
    assert kr_day_order_expiry(accepted_at=_at(10, 0), side="buy") == (
        "2026-09-29T20:00:00+09:00",
        REASON_REGULAR_BUY_CONSERVATIVE,
    )
    assert kr_day_order_expiry(accepted_at=_at(17, 0), side="buy") == (
        "2026-09-29T20:00:00+09:00",
        REASON_NXT_CARRY,
    )
