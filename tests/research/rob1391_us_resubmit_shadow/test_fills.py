"""Conservative / optimistic fill rules (prereg §3) -- boundary by boundary."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from research.rob1391_us_resubmit_shadow.fills import (
    conservative_candidate,
    eligible_bars,
    simulate_conservative,
    simulate_optimistic,
    volume_cap,
)
from research.rob1391_us_resubmit_shadow.market_data import (
    OK,
    UNKNOWN,
    AskEvidence,
    parse_bar,
)
from research.rob1391_us_resubmit_shadow.parents import tick_size
from tests.research.rob1391_us_resubmit_shadow.helpers import bar_json

pytestmark = pytest.mark.unit

OPEN = datetime(2025, 11, 18, 14, 30, tzinfo=UTC)
CLOSE = datetime(2025, 11, 18, 21, 0, tzinfo=UTC)
P = Decimal("99.50")


def bar(minute: int, *, low: float, v: int | None = 50_000):
    return parse_bar(
        bar_json(OPEN + timedelta(minutes=minute), o=99.7, h=99.8, low=low, c=99.6, v=v)
    )


def ask_found(_a, _b):
    return AskEvidence(OK, found=True, ask=P)


def ask_absent(_a, _b):
    return AskEvidence(OK, found=False)


def ask_unknown(_a, _b):
    return AskEvidence(UNKNOWN, reason="pagination_cap")


def unlimited(_filled):
    return 10_000


def run(
    bars,
    *,
    remaining=5,
    ask=ask_found,
    affordable=unlimited,
    submit=OPEN + timedelta(seconds=1),
):
    return simulate_conservative(
        bars=tuple(bars),
        submit_at=submit,
        close_utc=CLOSE,
        limit=P,
        remaining=remaining,
        affordable=affordable,
        ask_lookup=ask,
    )


def test_tick_size_one_dollar_boundary():
    assert tick_size(Decimal("1.00")) == Decimal("0.01")
    assert tick_size(Decimal("0.9999")) == Decimal("0.0001")


def test_one_tick_trade_through_is_required():
    assert conservative_candidate(bar(1, low=99.49), P) is True  # P - 1 tick
    assert conservative_candidate(bar(1, low=99.50), P) is False  # touch only
    assert conservative_candidate(bar(1, low=99.495), P) is False


def test_sub_dollar_tick():
    p = Decimal("0.5000")
    assert conservative_candidate(bar(1, low=0.4999), p) is True
    assert conservative_candidate(bar(1, low=0.5000), p) is False


def test_touch_without_trade_through_never_fills():
    out = run([bar(1, low=99.50), bar(2, low=99.50)])
    assert out.status == OK and out.qty == 0


def test_valid_ask_at_or_below_limit_is_required():
    assert run([bar(1, low=99.40)], ask=ask_absent).qty == 0
    assert run([bar(1, low=99.40)], ask=ask_found).qty == 5


def test_volume_cap_is_floor_of_one_percent():
    assert volume_cap(Decimal(99)) == 0
    assert volume_cap(Decimal(100)) == 1
    assert volume_cap(Decimal(199)) == 1
    assert volume_cap(Decimal(200)) == 2
    out = run(
        [bar(1, low=99.40, v=199), bar(2, low=99.40, v=99), bar(3, low=99.40, v=300)]
    )
    assert [f.qty for f in out.fills] == [1, 3]  # 1 + 0 (cap 0) + min(3, 4 left)
    assert out.qty == 4


def test_quantity_never_exceeds_remaining_or_budget():
    out = run([bar(1, low=99.40, v=1_000_000)], remaining=3)
    assert out.qty == 3
    out = run(
        [bar(1, low=99.40, v=1_000_000)],
        remaining=3,
        affordable=lambda filled: 2 - filled,
    )
    assert out.qty == 2


def test_partial_fills_accumulate_and_only_remainder_is_reused():
    out = run(
        [bar(1, low=99.40, v=200), bar(2, low=99.40, v=200), bar(3, low=99.40, v=200)],
        remaining=5,
    )
    assert [f.qty for f in out.fills] == [2, 2, 1]


def test_fill_price_is_always_the_limit_no_gap_gain():
    out = run([bar(1, low=95.00, v=10_000)])
    assert {f.price for f in out.fills} == {P}


def test_bars_starting_before_submission_are_excluded():
    submit = OPEN + timedelta(minutes=1, seconds=30)
    bars = [bar(1, low=99.0), bar(2, low=99.0)]
    assert [b.start_utc for b in eligible_bars(tuple(bars), submit, CLOSE)] == [
        OPEN + timedelta(minutes=2)
    ]


def test_bar_ending_after_close_is_excluded():
    last = parse_bar(bar_json(CLOSE - timedelta(seconds=30), o=1, h=1, low=1, c=1, v=1))
    assert eligible_bars((last,), OPEN, CLOSE) == []


def test_unknown_ask_is_unknown_not_zero():
    out = run([bar(1, low=99.40)], ask=ask_unknown)
    assert out.status == UNKNOWN and out.unknown_reason.startswith("ask_unobservable")
    assert out.fills == []


def test_missing_volume_is_unknown():
    out = run([bar(1, low=99.40, v=None)])
    assert out.status == UNKNOWN and out.unknown_reason == "bar_missing_low_or_volume"


def test_known_fills_before_unknown_minute_are_kept_but_session_is_unknown():
    calls = iter([AskEvidence(OK, found=True), AskEvidence(UNKNOWN, reason="http_500")])
    out = run(
        [bar(1, low=99.40, v=200), bar(2, low=99.40, v=200)],
        ask=lambda a, b: next(calls),
    )
    assert out.status == UNKNOWN and out.qty == 2


def test_optimistic_touch_fills_whole_remainder_and_is_separate():
    bars = (bar(0, low=99.50, v=10),)
    out = simulate_optimistic(
        bars=bars,
        submit_at=OPEN + timedelta(seconds=5),
        close_utc=CLOSE,
        limit=P,
        remaining=4,
        affordable=unlimited,
    )
    assert out.qty == 4  # touch is enough; no volume cap; submission minute included
    assert run(list(bars), remaining=4).qty == 0  # conservative: no fill
