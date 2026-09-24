"""#671 — manual_cash validation, large-change guard, and the shared stale rule."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.manual_cash_settings import (
    MANUAL_CASH_MAX_ACCOUNTS,
    MANUAL_CASH_MAX_KRW,
    MANUAL_CASH_NAME_MAX_LEN,
    ManualCashValidationError,
    change_ratio,
    is_manual_cash_stale,
    parse_stored_amount,
    requires_large_change_confirmation,
    validate_accounts,
    validate_krw_amount,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("value", [0, 1, MANUAL_CASH_MAX_KRW])
def test_amount_boundaries_accepted(value: int) -> None:
    assert validate_krw_amount(value, field="amount") == value


@pytest.mark.parametrize(
    "value",
    [
        MANUAL_CASH_MAX_KRW + 1,
        -1,
        "100",
        "1,000",
        1.0,
        1.5,
        float("nan"),
        float("inf"),
        float("-inf"),
        True,
        False,
        None,
        Decimal("100"),
    ],
)
def test_amount_rejections(value: object) -> None:
    with pytest.raises(ManualCashValidationError):
        validate_krw_amount(value, field="amount")


def test_accounts_total_and_trimmed_names() -> None:
    accounts = validate_accounts(
        [{"name": "  토스 파킹 ", "amount": 1_000_000}, {"name": "CMA", "amount": 0}]
    )
    assert [(a.name, a.amount) for a in accounts] == [
        ("토스 파킹", 1_000_000),
        ("CMA", 0),
    ]


@pytest.mark.parametrize(
    "raw",
    [
        [],
        None,
        {"name": "x", "amount": 1},
        [{"name": "", "amount": 1}],
        [{"name": "   ", "amount": 1}],
        [{"amount": 1}],
        [{"name": "x" * (MANUAL_CASH_NAME_MAX_LEN + 1), "amount": 1}],
        [{"name": "x", "amount": -1}],
        [{"name": "x", "amount": "abc"}],
        [{"name": "x", "amount": float("nan")}],
        ["not-an-object"],
        [{"name": f"a{i}", "amount": 1} for i in range(MANUAL_CASH_MAX_ACCOUNTS + 1)],
        # each row within bound but the total over it
        [
            {"name": "a", "amount": MANUAL_CASH_MAX_KRW},
            {"name": "b", "amount": 1},
        ],
    ],
)
def test_accounts_rejections(raw: object) -> None:
    with pytest.raises(ManualCashValidationError):
        validate_accounts(raw)


def test_accounts_total_exactly_at_bound_is_accepted() -> None:
    accounts = validate_accounts(
        [
            {"name": "a", "amount": MANUAL_CASH_MAX_KRW - 1},
            {"name": "b", "amount": 1},
        ]
    )
    assert sum(a.amount for a in accounts) == MANUAL_CASH_MAX_KRW


@pytest.mark.parametrize(
    ("current", "new_total", "expected"),
    [
        (Decimal("100"), 150, False),  # exactly +50% — not "more than"
        (Decimal("100"), 151, True),
        (Decimal("100"), 50, False),  # exactly -50%
        (Decimal("100"), 49, True),
        (Decimal("100"), 0, True),  # -100%
        (Decimal("100"), 100, False),
        (Decimal("10000000"), 100000000, True),  # extra-zero typo
        (None, 1, True),  # nothing stored — any positive value is a jump
        (None, 0, False),
        (Decimal("0"), 1, True),
        (Decimal("0"), 0, False),
    ],
)
def test_large_change_rule(
    current: Decimal | None, new_total: int, expected: bool
) -> None:
    assert requires_large_change_confirmation(current, new_total) is expected


def test_change_ratio_has_no_baseline_for_zero() -> None:
    assert change_ratio(Decimal("0"), 5) is None
    assert change_ratio(Decimal("200"), 300) == Decimal("0.5")


NOW = datetime(2026, 9, 24, 12, 0, tzinfo=UTC)


def test_stale_rule_is_strictly_older_than_three_days() -> None:
    exactly = (NOW - timedelta(days=3)).isoformat()
    just_over = (NOW - timedelta(days=3, microseconds=1)).isoformat()
    assert is_manual_cash_stale(exactly, now=NOW) is False
    assert is_manual_cash_stale(just_over, now=NOW) is True


@pytest.mark.parametrize("value", [None, "", "not-a-date"])
def test_stale_rule_fails_closed(value: str | None) -> None:
    assert is_manual_cash_stale(value, now=NOW) is True


def test_stale_rule_reads_naive_as_utc() -> None:
    naive = (NOW - timedelta(days=1)).replace(tzinfo=None).isoformat()
    assert is_manual_cash_stale(naive, now=NOW) is False


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ({"amount": 15000000}, Decimal("15000000")),
        ({"amount": 15000000.0}, Decimal("15000000.0")),
        ({"amount": "15000000"}, Decimal("15000000")),
        ({"amount": 0}, Decimal("0")),
        ({"amount": "NaN"}, None),
        ({"amount": float("nan")}, None),
        ({"amount": "Infinity"}, None),
        ({"amount": float("inf")}, None),
        ({"amount": -1}, None),
        ({"amount": "abc"}, None),
        ({"amount": True}, None),
        ({"amount": None}, None),
        ({}, None),
        (None, None),
        ([1], None),
    ],
)
def test_parse_stored_amount(value: object, expected: Decimal | None) -> None:
    assert parse_stored_amount(value) == expected
