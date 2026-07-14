from __future__ import annotations

from datetime import timedelta
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_cohort import CanonicalMarketSnapshot
from app.services.paper_cohort.cohort_service import PaperCohortService
from app.services.paper_cohort.market_snapshot import (
    CanonicalSnapshotCapture,
    CanonicalSnapshotPayload,
)
from tests.services.paper_cohort.test_cohort_service import (
    _activation,
    _assignment,
    _authoritative_history,
    _registry_rows,
)
from tests.services.paper_cohort.test_market_snapshot import (
    CAPTURED_AT,
    FakePublicClient,
    request,
)

pytestmark = pytest.mark.integration


@pytest.mark.asyncio
async def test_canonical_snapshot_hash_survives_postgresql_jsonb_roundtrip(
    db_session: AsyncSession,
) -> None:
    nonce = uuid4().hex
    experiment, backtest = await _registry_rows(db_session, nonce)
    activation = _activation(
        (_assignment(experiment, backtest, nonce=nonce),), nonce=nonce
    )
    await _authoritative_history(db_session, activation)
    await PaperCohortService(db_session).activate(activation)

    capture_request = request().model_copy(
        update={
            "snapshot_id": f"snapshot-{nonce}",
            "cohort_id": activation.cohort_id,
            "run_id": f"run-{nonce}",
            "round_decision_id": f"round-{nonce}",
        }
    )
    clocks = iter([CAPTURED_AT, CAPTURED_AT + timedelta(milliseconds=200)])
    payload = await CanonicalSnapshotCapture(
        FakePublicClient(), clock=lambda: next(clocks)
    ).capture(capture_request)
    db_session.add(
        CanonicalMarketSnapshot(
            snapshot_id=payload.snapshot_id,
            cohort_id=payload.cohort_id,
            run_id=payload.run_id,
            round_decision_id=payload.round_decision_id,
            schema_id=payload.schema_id,
            source=payload.source,
            host=payload.host,
            interval=payload.interval,
            required_lookback=payload.required_lookback,
            max_capture_skew_ms=payload.max_capture_skew_ms,
            max_ticker_age_ms=payload.max_ticker_age_ms,
            capture_started_at=payload.capture_started_at,
            capture_completed_at=payload.capture_completed_at,
            payload=payload.model_dump(mode="json"),
            content_hash=payload.content_hash,
        )
    )
    await db_session.commit()

    stored = await db_session.scalar(
        select(CanonicalMarketSnapshot).where(
            CanonicalMarketSnapshot.snapshot_id == payload.snapshot_id
        )
    )
    assert stored is not None
    roundtripped = CanonicalSnapshotPayload.model_validate(stored.payload)
    assert roundtripped.content_hash == stored.content_hash == payload.content_hash
    assert roundtripped.recomputed_content_hash() == payload.content_hash
