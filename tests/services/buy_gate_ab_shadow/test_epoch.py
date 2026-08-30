from __future__ import annotations

from copy import deepcopy
from dataclasses import FrozenInstanceError
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.buy_gate_ab_collection_epoch import BuyGateABCollectionEpoch
from app.services.buy_gate_ab_shadow.epoch import (
    COLLECTION_EPOCH,
    assess_collection_readiness,
)
from app.services.buy_gate_ab_shadow.spec import POLICY_PROJECTION


def test_collection_epoch_marker_rejects_armed_at_mutation() -> None:
    original = COLLECTION_EPOCH.collection_armed_at
    mutation_rejected = False
    try:
        COLLECTION_EPOCH.collection_armed_at = original + timedelta(seconds=1)  # type: ignore[misc]
    except FrozenInstanceError:
        mutation_rejected = True

    # Keep this as a plain assertion: the required negative mutant must fail as
    # AssertionError, never as a missing key or collection error.
    assert mutation_rejected is True
    assert COLLECTION_EPOCH.collection_armed_at == original


def test_first_valid_record_is_nullable_and_never_moves_the_window() -> None:
    as_of = datetime(2026, 9, 10, 12, 0, tzinfo=UTC)
    without_record = assess_collection_readiness(
        as_of=as_of,
        event_count=0,
        all_events_matured=False,
    )
    with_observation = assess_collection_readiness(
        as_of=as_of,
        event_count=1,
        all_events_matured=False,
        first_valid_record_at=datetime(2026, 9, 9, 3, 0, tzinfo=UTC),
    )

    assert without_record.first_valid_record_at is None
    assert with_observation.first_valid_record_at is not None
    assert COLLECTION_EPOCH.collection_start.isoformat() == "2026-08-31"
    assert COLLECTION_EPOCH.collection_end_exclusive.isoformat() == "2026-09-28"
    assert (
        without_record.as_dict()["collection_start"]
        == with_observation.as_dict()["collection_start"]
    )
    assert (
        without_record.as_dict()["collection_end_exclusive"]
        == with_observation.as_dict()["collection_end_exclusive"]
    )


def test_zero_events_still_close_as_insufficient_sample_no_firing() -> None:
    readiness = assess_collection_readiness(
        as_of=datetime(2026, 9, 28, 0, 0, tzinfo=UTC),
        event_count=0,
        all_events_matured=False,
    )

    assert readiness.collection_window_closed is True
    assert readiness.all_events_matured is True
    assert readiness.scoring_ready is True
    assert readiness.status == "INSUFFICIENT_SAMPLE"
    assert readiness.outcome == "NO_FIRING"


def test_scoring_ready_is_window_closed_and_all_events_matured() -> None:
    before_close = assess_collection_readiness(
        as_of=datetime(2026, 9, 27, 0, 0, tzinfo=UTC),
        event_count=1,
        all_events_matured=True,
    )
    immature = assess_collection_readiness(
        as_of=datetime(2026, 9, 28, 0, 0, tzinfo=UTC),
        event_count=1,
        all_events_matured=False,
    )
    ready = assess_collection_readiness(
        as_of=datetime(2026, 9, 28, 0, 0, tzinfo=UTC),
        event_count=1,
        all_events_matured=True,
    )

    assert before_close.collection_window_closed is False
    assert before_close.all_events_matured is True
    assert before_close.scoring_ready is False
    assert immature.collection_window_closed is True
    assert immature.all_events_matured is False
    assert immature.scoring_ready is False
    assert ready.collection_window_closed is True
    assert ready.all_events_matured is True
    assert ready.scoring_ready is True


def test_market_local_session_date_is_bound_to_the_fixed_epoch() -> None:
    kr_event = datetime(2026, 8, 31, 0, 30, tzinfo=UTC)
    us_event = datetime(2026, 9, 1, 0, 30, tzinfo=UTC)

    assert COLLECTION_EPOCH.session_date("kr", kr_event).isoformat() == "2026-08-31"
    assert COLLECTION_EPOCH.session_date("us", us_event).isoformat() == "2026-08-31"
    assert COLLECTION_EPOCH.contains_event(market="kr", observed_at=kr_event)
    assert COLLECTION_EPOCH.contains_event(market="us", observed_at=us_event)


@pytest.mark.asyncio
@pytest.mark.integration
async def test_durable_epoch_row_rejects_collection_armed_at_update(
    db_session: AsyncSession,
) -> None:
    marker = BuyGateABCollectionEpoch(
        id=1,
        experiment_id=COLLECTION_EPOCH.experiment_id,
        epoch_id=COLLECTION_EPOCH.epoch_id,
        addendum_version=COLLECTION_EPOCH.addendum_version,
        collection_armed_at=COLLECTION_EPOCH.collection_armed_at,
        collection_start=COLLECTION_EPOCH.collection_start,
        collection_end_exclusive=COLLECTION_EPOCH.collection_end_exclusive,
        collection_calendar_days=COLLECTION_EPOCH.collection_calendar_days,
        collection_clock_timezone=COLLECTION_EPOCH.collection_clock_timezone,
        policy_projection_sha256=COLLECTION_EPOCH.policy_projection_sha256,
        preregistration_spec_sha256=(COLLECTION_EPOCH.preregistration_spec_sha256),
        policy_projection=deepcopy(POLICY_PROJECTION),
    )
    db_session.add(marker)
    await db_session.flush()

    with pytest.raises(DBAPIError) as rejected:
        await db_session.execute(
            update(BuyGateABCollectionEpoch)
            .where(BuyGateABCollectionEpoch.id == 1)
            .values(
                collection_armed_at=(
                    COLLECTION_EPOCH.collection_armed_at + timedelta(seconds=1)
                )
            )
        )
    assert "immutable" in str(rejected.value)
    await db_session.rollback()


def test_durable_marker_has_no_first_valid_record_boundary_column() -> None:
    assert "first_valid_record_at" not in BuyGateABCollectionEpoch.__table__.columns
