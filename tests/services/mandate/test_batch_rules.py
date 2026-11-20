"""hk 3795 T3a: the pure rules of the morning batch (§5.1), without a database.

I22 (batch unicity) lives in the database trigger and is tested in
``test_batches_db.py``; here the session table of §5.1 runs line by line against a
fake calendar, and the callback data / nonce primitives are pinned.
"""

from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from app.services.mandate import batch_rules as rules

pytestmark = pytest.mark.unit

KST = ZoneInfo("Asia/Seoul")
MARKET = "equity_kr"


class FakeCalendar:
    """KR-like regular sessions 09:00-15:30 KST on the listed dates."""

    def __init__(self, sessions: list[date]) -> None:
        self.sessions = set(sessions)

    def bounds(self, market: str, day: date):
        if day not in self.sessions:
            return None
        return (
            datetime(day.year, day.month, day.day, 9, 0, tzinfo=KST),
            datetime(day.year, day.month, day.day, 15, 30, tzinfo=KST),
        )


# 2026-11-23 is a Monday. The sessions of the §5.1 examples.
MON, TUE, WED, THU, FRI = (date(2026, 11, d) for d in (23, 24, 25, 26, 27))
NEXT_MON = date(2026, 11, 30)
CAL = FakeCalendar([MON, TUE, THU, FRI, NEXT_MON])  # Wednesday is a holiday


def at(day: date, hour: int, minute: int = 0) -> datetime:
    return datetime(day.year, day.month, day.day, hour, minute, tzinfo=KST)


def effective(created: datetime, approved: datetime):
    window = rules.batch_window(MARKET, created, CAL)
    return window.target_session, rules.effective_session(MARKET, approved, window, CAL)


# The session table of §5.1, row by row: (created, approved, expected T, expected effect).
SESSION_TABLE = [
    pytest.param(at(MON, 8, 20), at(MON, 8, 40), MON, MON, id="mon-0820-approved-0840"),
    pytest.param(at(MON, 8, 20), at(MON, 9, 10), MON, TUE, id="mon-0820-approved-0910"),
    pytest.param(at(MON, 8, 20), at(MON, 15, 40), MON, None, id="mon-0820-expired"),
    pytest.param(
        at(MON, 10, 0), at(MON, 14, 0), TUE, TUE, id="mon-1000-next-day-batch"
    ),
    pytest.param(
        at(FRI, 16, 0),
        at(FRI, 16, 0) + timedelta(days=1, hours=-6),
        NEXT_MON,
        NEXT_MON,
        id="fri-1600-approved-sat-1000",
    ),
    pytest.param(
        at(TUE, 16, 0), at(WED, 12, 0), THU, THU, id="tue-1600-holiday-approved-wed"
    ),
    pytest.param(
        at(TUE, 16, 0), at(THU, 9, 5), THU, FRI, id="tue-1600-holiday-approved-thu-0905"
    ),
]


@pytest.mark.parametrize(("created", "approved", "target", "expected"), SESSION_TABLE)
def test_i22_session_table_of_the_design(created, approved, target, expected) -> None:
    got_target, got_effect = effective(created, approved)
    assert got_target == target
    assert got_effect == expected


def test_the_saturday_row_really_is_saturday_ten_oclock() -> None:
    approved = at(FRI, 16, 0) + timedelta(days=1, hours=-6)
    assert approved == at(date(2026, 11, 28), 10, 0)


def test_deadlines_are_the_open_and_the_close_of_the_target_session() -> None:
    window = rules.batch_window(MARKET, at(MON, 8, 20), CAL)
    assert window.approval_deadline_at == at(MON, 9, 0)
    assert window.expires_at == at(MON, 15, 30)


def test_a_batch_made_exactly_at_the_open_targets_the_next_session() -> None:
    """T opens strictly after ``now``: at 09:00 sharp Monday's own open is not after it."""
    assert rules.batch_window(MARKET, at(MON, 9, 0), CAL).target_session == TUE


def test_approval_exactly_at_the_deadline_takes_effect_after_the_target() -> None:
    window = rules.batch_window(MARKET, at(MON, 8, 20), CAL)
    assert rules.effective_session(MARKET, at(MON, 9, 0), window, CAL) == TUE
    assert (
        rules.effective_session(
            MARKET, at(MON, 9, 0) - timedelta(microseconds=1), window, CAL
        )
        == MON
    )


def test_approval_exactly_at_the_expiry_has_no_effect() -> None:
    window = rules.batch_window(MARKET, at(MON, 8, 20), CAL)
    assert rules.effective_session(MARKET, window.expires_at, window, CAL) is None


def test_a_calendar_without_any_session_fails_closed() -> None:
    with pytest.raises(rules.NoSession):
        rules.batch_window(MARKET, at(MON, 8, 0), FakeCalendar([]))
    window = rules.BatchWindow(MON, at(MON, 9, 0), at(MON, 15, 30))
    with pytest.raises(rules.NoSession):
        rules.effective_session(MARKET, at(MON, 10, 0), window, FakeCalendar([MON]))


def test_naive_time_is_refused() -> None:
    with pytest.raises(ValueError):
        rules.batch_window(MARKET, datetime(2026, 11, 23, 8, 0), CAL)


def test_us_market_reads_the_new_york_calendar_day() -> None:
    """2026-11-23 23:30 UTC is already Tuesday in Seoul but still Monday in New York."""

    class UsCal:
        def bounds(self, market: str, day: date):
            if day.weekday() >= 5:
                return None
            ny = ZoneInfo("America/New_York")
            return (
                datetime(day.year, day.month, day.day, 9, 30, tzinfo=ny),
                datetime(day.year, day.month, day.day, 16, 0, tzinfo=ny),
            )

    now = datetime(2026, 11, 23, 23, 30, tzinfo=UTC)  # Monday 18:30 New York
    assert rules.batch_window("equity_us", now, UsCal()).target_session == TUE


# ------------------------------------------------------------ callback data / nonce
def test_callback_data_round_trips_and_fits_64_bytes_for_the_largest_card_id() -> None:
    nonce = rules.new_nonce()
    assert len(nonce) == 22
    for member_id in (1, 35, 36, 10**9, 2**63 - 1):
        for action in ("approve", "reject"):
            data = rules.build_callback_data(action, member_id, nonce)
            assert len(data.encode()) <= 64
            parsed = rules.parse_callback_data(data)
            assert parsed == rules.ParsedCallback(action, member_id, nonce)


@pytest.mark.parametrize(
    "data",
    [
        None,
        5,
        "",
        "mpb",
        "mpb:a:1",
        "mpb:x:1:" + "A" * 22,
        "mpb:a:1:" + "A" * 21,
        "mpb:a:1:" + "A" * 23,
        "mpb:a:0:" + "A" * 22,
        "mpb:a:!:" + "A" * 22,
        "op:a:1:" + "A" * 22,
        "mpb:a:1:" + "A" * 21 + "+",
        "mpb:a:1:" + "A" * 22 + "\n",
        "mpb:a:zzzzzzzzzzzzzz:" + "A" * 22,
        "mpb:a:" + rules._base36(2**63) + ":" + "A" * 22,  # first id above a bigint
        "mpb:a:1:" + "\ud800" * 22,  # a lone surrogate must not raise
        "mpb:a:1:" + "é" * 22,
    ],
)
def test_callback_data_that_is_not_exactly_the_format_is_none(data) -> None:
    assert rules.parse_callback_data(data) is None


def test_the_mpb_prefix_is_this_path_even_when_the_rest_is_malformed() -> None:
    assert rules.is_mandate_callback_data("mpb:garbage")
    assert not rules.is_mandate_callback_data("op:12345678:x")
    assert not rules.is_mandate_callback_data(None)
    assert not rules.is_mandate_callback_data("mpbx:a:1:" + "A" * 22)


def test_the_builder_refuses_bad_inputs() -> None:
    with pytest.raises(ValueError):
        rules.build_callback_data("approve", 2**63, "A" * 22)
    with pytest.raises(ValueError):
        rules.build_callback_data("approve", 0, "A" * 22)
    with pytest.raises(ValueError):
        rules.build_callback_data("maybe", 1, "A" * 22)
    with pytest.raises(ValueError):
        rules.build_callback_data("approve", 1, "short")


def test_nonce_digest_is_domain_separated_and_matches_only_its_nonce() -> None:
    nonce = rules.new_nonce()
    stored = rules.nonce_digest(nonce)
    assert len(stored) == 64 and stored != nonce
    assert stored != __import__("hashlib").sha256(nonce.encode()).hexdigest()
    assert rules.nonce_matches(nonce, stored)
    assert not rules.nonce_matches(rules.new_nonce(), stored)
    assert not rules.nonce_matches("not-a-nonce", stored)
    assert rules.new_nonce() != rules.new_nonce()


def test_the_keyboard_carries_one_approve_and_one_reject_button() -> None:
    nonce = rules.new_nonce()
    board = rules.inline_keyboard(77, nonce)
    buttons = board["inline_keyboard"][0]
    assert [b["text"] for b in buttons] == ["승인", "기각"]
    actions = [rules.parse_callback_data(b["callback_data"]).action for b in buttons]
    assert actions == ["approve", "reject"]
