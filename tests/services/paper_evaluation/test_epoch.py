"""Tests for ROB-850 evaluation epoch management."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from app.services.paper_evaluation.contracts import (
    EpochIdentity,
    EpochResetReason,
    EvaluationConfigError,
    ViewName,
)
from app.services.paper_evaluation.epoch import (
    compute_calendar_days,
    create_epoch_identity,
    filter_epochs_by_cohort,
    is_epoch_active,
    should_create_new_epoch,
)

pytestmark = pytest.mark.unit

_HASH = "a" * 64
_EQUITY: dict[ViewName, Decimal] = {
    ViewName.BINANCE_BROKER: Decimal("10000"),
    ViewName.ALPACA_BROKER: Decimal("10000"),
    ViewName.CANONICAL_SHADOW: Decimal("10000"),
}


def make_epoch(
    *,
    epoch_id: str = "epoch-1",
    cohort_id: str = "cohort-1",
    started_at: datetime | None = None,
    initial_equity: dict[ViewName, Decimal] | None = None,
    reset_reason: EpochResetReason | None = None,
    prior_epoch_id: str | None = None,
    config_hash: str = _HASH,
) -> EpochIdentity:
    return create_epoch_identity(
        epoch_id=epoch_id,
        cohort_id=cohort_id,
        config_hash=config_hash,
        initial_equity=initial_equity if initial_equity is not None else dict(_EQUITY),
        started_at=started_at or datetime(2026, 1, 1, 12, 0, tzinfo=UTC),
        reset_reason=reset_reason,
        prior_epoch_id=prior_epoch_id,
    )


# ---------------------------------------------------------------------------
# should_create_new_epoch
# ---------------------------------------------------------------------------


def test_account_reset_always_creates_new_epoch() -> None:
    epoch = make_epoch()
    assert should_create_new_epoch(
        epoch, reset_reason=EpochResetReason.ACCOUNT_RESET
    )


def test_api_key_recreation_always_creates_new_epoch() -> None:
    epoch = make_epoch()
    assert should_create_new_epoch(
        epoch, reset_reason=EpochResetReason.API_KEY_RECREATION
    )


def test_initial_equity_change_detected() -> None:
    epoch = make_epoch()
    new_equity = dict(_EQUITY)
    new_equity[ViewName.BINANCE_BROKER] = Decimal("20000")
    assert should_create_new_epoch(
        epoch,
        reset_reason=EpochResetReason.INITIAL_EQUITY_CHANGE,
        new_initial_equity=new_equity,
    )


def test_initial_equity_unchanged_does_not_create_epoch() -> None:
    epoch = make_epoch()
    assert not should_create_new_epoch(
        epoch,
        reset_reason=EpochResetReason.INITIAL_EQUITY_CHANGE,
        new_initial_equity=dict(_EQUITY),
    )


def test_initial_equity_change_in_any_view_detected() -> None:
    epoch = make_epoch()
    for view in _EQUITY:
        new_equity = dict(_EQUITY)
        new_equity[view] = Decimal("99999")
        assert should_create_new_epoch(
            epoch,
            reset_reason=EpochResetReason.INITIAL_EQUITY_CHANGE,
            new_initial_equity=new_equity,
        ), f"equity change for {view} should trigger a new epoch"


def test_initial_equity_change_without_new_equity_is_conservative_true() -> None:
    epoch = make_epoch()
    assert should_create_new_epoch(
        epoch, reset_reason=EpochResetReason.INITIAL_EQUITY_CHANGE
    )


# ---------------------------------------------------------------------------
# create_epoch_identity
# ---------------------------------------------------------------------------


def test_create_epoch_identity_rejects_naive_datetime() -> None:
    with pytest.raises(EvaluationConfigError):
        create_epoch_identity(
            epoch_id="epoch-1",
            cohort_id="cohort-1",
            config_hash=_HASH,
            initial_equity=dict(_EQUITY),
            started_at=datetime(2026, 1, 1),  # naive
        )


def test_create_epoch_identity_rejects_wrong_view_count() -> None:
    bad_equity = {
        ViewName.BINANCE_BROKER: Decimal("10000"),
        ViewName.ALPACA_BROKER: Decimal("10000"),
    }
    with pytest.raises(EvaluationConfigError):
        create_epoch_identity(
            epoch_id="epoch-1",
            cohort_id="cohort-1",
            config_hash=_HASH,
            initial_equity=bad_equity,
            started_at=datetime(2026, 1, 1, tzinfo=UTC),
        )


def test_create_epoch_identity_returns_valid_epoch() -> None:
    epoch = make_epoch()
    assert epoch.epoch_id == "epoch-1"
    assert epoch.started_at.tzinfo is not None
    assert set(epoch.initial_equity) == set(_EQUITY)


# ---------------------------------------------------------------------------
# compute_calendar_days
# ---------------------------------------------------------------------------


def test_compute_calendar_days_same_day_is_zero() -> None:
    start = datetime(2026, 1, 1, 0, 0, tzinfo=UTC)
    end = datetime(2026, 1, 1, 23, 59, tzinfo=UTC)
    assert compute_calendar_days(start, end) == 0


def test_compute_calendar_days_cross_midnight_is_one() -> None:
    start = datetime(2026, 1, 1, 23, 59, tzinfo=UTC)
    end = datetime(2026, 1, 2, 0, 1, tzinfo=UTC)
    assert compute_calendar_days(start, end) == 1


def test_compute_calendar_days_six_days() -> None:
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    end = start + timedelta(days=6)
    assert compute_calendar_days(start, end) == 6


def test_compute_calendar_days_seven_days() -> None:
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    end = start + timedelta(days=7)
    assert compute_calendar_days(start, end) == 7


def test_compute_calendar_days_59_days() -> None:
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    end = start + timedelta(days=59)
    assert compute_calendar_days(start, end) == 59


def test_compute_calendar_days_60_days() -> None:
    start = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
    end = start + timedelta(days=60)
    assert compute_calendar_days(start, end) == 60


def test_compute_calendar_days_rejects_naive_start() -> None:
    with pytest.raises(EvaluationConfigError):
        compute_calendar_days(
            datetime(2026, 1, 1),
            datetime(2026, 1, 8, tzinfo=UTC),
        )


def test_compute_calendar_days_rejects_naive_end() -> None:
    with pytest.raises(EvaluationConfigError):
        compute_calendar_days(
            datetime(2026, 1, 1, tzinfo=UTC),
            datetime(2026, 1, 8),
        )


# ---------------------------------------------------------------------------
# filter_epochs_by_cohort + prior epochs remain queryable
# ---------------------------------------------------------------------------


def test_filter_epochs_by_cohort_returns_sorted_and_filtered() -> None:
    e1 = make_epoch(epoch_id="e1", started_at=datetime(2026, 1, 1, tzinfo=UTC))
    e2 = make_epoch(
        epoch_id="e2",
        started_at=datetime(2026, 1, 5, tzinfo=UTC),
        prior_epoch_id="e1",
        reset_reason=EpochResetReason.ACCOUNT_RESET,
    )
    e3 = make_epoch(
        cohort_id="cohort-2",
        epoch_id="e3",
        started_at=datetime(2026, 1, 3, tzinfo=UTC),
    )
    result = filter_epochs_by_cohort([e3, e2, e1], "cohort-1")
    assert [epoch.epoch_id for epoch in result] == ["e1", "e2"]
    assert all(epoch.cohort_id == "cohort-1" for epoch in result)


def test_prior_epoch_remains_separately_queryable() -> None:
    e1 = make_epoch(epoch_id="e1", started_at=datetime(2026, 1, 1, tzinfo=UTC))
    e2 = make_epoch(
        epoch_id="e2",
        started_at=datetime(2026, 1, 5, tzinfo=UTC),
        prior_epoch_id="e1",
        reset_reason=EpochResetReason.API_KEY_RECREATION,
    )
    result = filter_epochs_by_cohort([e1, e2], "cohort-1")
    assert len(result) == 2
    assert e1 in result
    assert e2 in result


# ---------------------------------------------------------------------------
# Epochs are never spliced — distinct epoch_id per reset
# ---------------------------------------------------------------------------


def test_epochs_never_spliced_distinct_ids_for_different_resets() -> None:
    e1 = make_epoch(epoch_id="e1", started_at=datetime(2026, 1, 1, tzinfo=UTC))
    e2 = make_epoch(
        epoch_id="e2",
        started_at=datetime(2026, 1, 5, tzinfo=UTC),
        prior_epoch_id="e1",
        reset_reason=EpochResetReason.ACCOUNT_RESET,
    )
    e3 = make_epoch(
        epoch_id="e3",
        started_at=datetime(2026, 1, 10, tzinfo=UTC),
        prior_epoch_id="e2",
        reset_reason=EpochResetReason.INITIAL_EQUITY_CHANGE,
        initial_equity={
            ViewName.BINANCE_BROKER: Decimal("20000"),
            ViewName.ALPACA_BROKER: Decimal("10000"),
            ViewName.CANONICAL_SHADOW: Decimal("10000"),
        },
    )
    assert len({e1.epoch_id, e2.epoch_id, e3.epoch_id}) == 3
    assert e2.prior_epoch_id == "e1"
    assert e3.prior_epoch_id == "e2"
    # prior epochs remain queryable
    all_epochs = filter_epochs_by_cohort([e1, e2, e3], "cohort-1")
    assert [epoch.epoch_id for epoch in all_epochs] == ["e1", "e2", "e3"]


# ---------------------------------------------------------------------------
# is_epoch_active
# ---------------------------------------------------------------------------


def test_is_epoch_active_when_started_at_at_or_before_now() -> None:
    epoch = make_epoch(started_at=datetime(2026, 1, 1, tzinfo=UTC))
    assert is_epoch_active(epoch, now=datetime(2026, 1, 2, tzinfo=UTC))
    assert is_epoch_active(epoch, now=datetime(2026, 1, 1, tzinfo=UTC))


def test_is_epoch_inactive_before_start() -> None:
    epoch = make_epoch(started_at=datetime(2026, 1, 10, tzinfo=UTC))
    assert not is_epoch_active(epoch, now=datetime(2026, 1, 1, tzinfo=UTC))
