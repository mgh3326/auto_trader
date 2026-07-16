"""RED-first tests for the ROB-929 defensive-proposal approval-window TTL floor."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from app.core.timezone import KST
from app.services.order_proposals.defensive_ttl import (
    DEFENSIVE_EXIT_INTENTS,
    resolve_defensive_valid_until,
)


def test_defensive_exit_intents_cover_loss_cut_and_defensive_trim():
    assert DEFENSIVE_EXIT_INTENTS == frozenset({"loss_cut", "defensive_trim"})


@pytest.mark.parametrize(
    ("now_kst_naive", "expected_kst_naive"),
    [
        # Before today's US window (22:30-23:30 KST) -> floors to today's end.
        ((2026, 7, 15, 9, 0), (2026, 7, 15, 23, 30)),
        # Inside today's US window -> floors to today's end.
        ((2026, 7, 15, 22, 45), (2026, 7, 15, 23, 30)),
        # After today's US window closed -> rolls to tomorrow's end.
        ((2026, 7, 15, 23, 45), (2026, 7, 16, 23, 30)),
    ],
)
def test_us_market_floors_to_next_approval_window_end(
    now_kst_naive, expected_kst_naive
):
    now = datetime(*now_kst_naive, tzinfo=KST)
    expected = datetime(*expected_kst_naive, tzinfo=KST)

    floor = resolve_defensive_valid_until("equity_us", now)

    assert floor == expected


@pytest.mark.parametrize(
    ("now_kst_naive", "expected_kst_naive"),
    [
        # Before the morning window (08:10-09:30 KST) -> floors to its end.
        ((2026, 7, 15, 7, 0), (2026, 7, 15, 9, 30)),
        # Between the morning and noon windows -> floors to the nearer (noon) end.
        ((2026, 7, 15, 10, 0), (2026, 7, 15, 12, 15)),
        # After both KR windows closed today -> rolls to tomorrow's first window.
        ((2026, 7, 15, 13, 0), (2026, 7, 16, 9, 30)),
    ],
)
def test_kr_market_floors_to_nearest_approval_window_end(
    now_kst_naive, expected_kst_naive
):
    now = datetime(*now_kst_naive, tzinfo=KST)
    expected = datetime(*expected_kst_naive, tzinfo=KST)

    floor = resolve_defensive_valid_until("equity_kr", now)

    assert floor == expected


def test_unknown_market_has_no_defined_window():
    now = datetime(2026, 7, 15, 9, 0, tzinfo=KST)

    assert resolve_defensive_valid_until("crypto", now) is None


def test_returns_timezone_aware_result_comparable_to_utc_now():
    now = datetime(2026, 7, 15, 9, 0, tzinfo=UTC)

    floor = resolve_defensive_valid_until("equity_us", now)

    assert floor is not None
    assert floor.tzinfo is not None
    assert floor > now


def test_naive_now_is_rejected():
    with pytest.raises(ValueError, match="timezone-aware"):
        resolve_defensive_valid_until("equity_us", datetime(2026, 7, 15, 9, 0))
