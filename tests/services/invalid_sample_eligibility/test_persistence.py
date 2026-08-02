"""ROB-1036 §4.3-5/6 — append-only persistence and aggregate non-re-entry.

Runs against the isolated pytest database only (``tests/conftest.py`` pins
``test_db``). No broker, account, network, or production database is touched.
"""

from __future__ import annotations

import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.invalid_sample_eligibility import (
    InvalidSampleCleanupBinding,
    InvalidSampleCleanupLifecycleEvent,
    SampleEligibilityDecision,
)
from app.models.review import TradeForecast
from app.services.invalid_sample_eligibility.binding import (
    CleanupBindingError,
    build_cleanup_binding,
)
from app.services.invalid_sample_eligibility.contract import (
    CONTRACT_VERSION,
    CalibrationEligibility,
    EligibilityContractError,
    EligibilitySubject,
    EligibilitySubjectKind,
    ForecastOutcomeObservability,
    OperationalReliabilityEligibility,
    TradePerformanceEligibility,
)
from app.services.invalid_sample_eligibility.post_fill import (
    FillEvidenceCompleteness,
    PositionEffectEvidence,
    PostFillCompletionStatus,
    PostFillManualReviewReason,
)
from app.services.invalid_sample_eligibility.read_model import (
    EligibilityDomain,
    EligibilityPredicate,
    build_eligible_forecast_calibration_aggregate,
)
from app.services.invalid_sample_eligibility.service import (
    InvalidSampleEligibilityService,
)
from app.services.trade_journal import forecast_service as svc

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]

NOW = datetime(2026, 8, 2, 6, 0, tzinfo=UTC)
CALIBRATION_PREDICATE = EligibilityPredicate(
    contract_version=CONTRACT_VERSION,
    domain=EligibilityDomain.CALIBRATION,
    admitted=frozenset({CalibrationEligibility.INCLUDE}),
)


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    # Append-only triggers block DELETE, so the fixture disables them for the
    # duration of the cleanup instead of pretending the rows are mutable.
    for table in (
        "invalid_sample_cleanup_lifecycle_events",
        "invalid_sample_cleanup_bindings",
        "sample_eligibility_decisions",
    ):
        await db_session.execute(
            text(f"ALTER TABLE review.{table} DISABLE TRIGGER USER")
        )
        await db_session.execute(text(f"DELETE FROM review.{table}"))
        await db_session.execute(
            text(f"ALTER TABLE review.{table} ENABLE TRIGGER USER")
        )
    await db_session.execute(delete(TradeForecast))
    await db_session.commit()
    yield


def _subject(ref: str) -> EligibilitySubject:
    return EligibilitySubject(kind=EligibilitySubjectKind.FORECAST, ref=ref)


async def _record(
    service: InvalidSampleEligibilityService,
    subject: EligibilitySubject,
    *,
    calibration: CalibrationEligibility,
    trade: TradePerformanceEligibility = TradePerformanceEligibility.INCLUDE,
    observability: ForecastOutcomeObservability = (
        ForecastOutcomeObservability.OBSERVABLE
    ),
    reason: str = "operator decision",
    evidence: dict | None = None,
):
    return await service.record_decision(
        subject=subject,
        forecast_outcome_observability=observability,
        calibration_eligibility=calibration,
        trade_performance_eligibility=trade,
        operational_reliability_eligibility=OperationalReliabilityEligibility.INCLUDE,
        decision_reason=reason,
        decided_by="test-operator",
        evidence=evidence or {"source": "uber-d2-execution-result-2026-07-31.md"},
    )


async def _make_scored_forecast(
    db_session: AsyncSession, *, created_by: str = "claude", probability: str = "0.8"
) -> TradeForecast:
    row = TradeForecast(
        created_by=created_by,
        symbol="UBER",
        instrument_type="equity_us",
        forecast_target={
            "kind": "terminal_close",
            "direction": "up",
            "target_price": 100.0,
            "outcome_rule_version": "terminal-close-v1-up-gte-down-lt",
        },
        probability=Decimal(probability),
        review_date=date(2026, 7, 30),
        status="closed",
        outcome=True,
        brier_score=Decimal("0.04000"),
        resolved_at=NOW,
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)
    return row


def _binding(client_order_id: str = "cleanup-uber-001", **overrides):
    kwargs = {
        "forecast_id": uuid.uuid4(),
        "sample_ref": "uber-d2-cleanup",
        "approval_id": "approval-uber-001",
        "approval_hash": "a" * 64,
        "approval_expires_at": NOW + timedelta(minutes=5),
        "approval_session_id": "session-A",
        "mission_id": "invalid-sample-cleanup-mission-1",
        "account_mode": "alpaca_paper_lab",
        "client_order_id": client_order_id,
        "lifecycle_correlation_id": f"corr-{client_order_id}",
        "now": NOW,
        "session_id": "session-A",
    }
    kwargs.update(overrides)
    return build_cleanup_binding(**kwargs)


# --- §4.3-5: append-only, no mutable update/delete -------------------------


async def test_decision_row_cannot_be_updated(db_session: AsyncSession) -> None:
    service = InvalidSampleEligibilityService(db_session)
    subject = _subject(str(uuid.uuid4()))
    await _record(service, subject, calibration=CalibrationEligibility.INCLUDE)
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            update(SampleEligibilityDecision)
            .where(SampleEligibilityDecision.subject_ref == subject.ref)
            .values(calibration_eligibility=CalibrationEligibility.EXCLUDE.value)
        )
    await db_session.rollback()


async def test_decision_row_cannot_be_deleted(db_session: AsyncSession) -> None:
    service = InvalidSampleEligibilityService(db_session)
    subject = _subject(str(uuid.uuid4()))
    await _record(service, subject, calibration=CalibrationEligibility.INCLUDE)
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            delete(SampleEligibilityDecision).where(
                SampleEligibilityDecision.subject_ref == subject.ref
            )
        )
    await db_session.rollback()


async def test_binding_and_lifecycle_event_rows_are_append_only(
    db_session: AsyncSession,
) -> None:
    service = InvalidSampleEligibilityService(db_session)
    binding = _binding()
    await service.record_cleanup_binding(binding)
    await service.record_post_fill_evidence(
        binding=binding,
        fill_evidence=FillEvidenceCompleteness.COMPLETE,
        position_effect=PositionEffectEvidence.CONSISTENT,
    )
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            update(InvalidSampleCleanupBinding).values(mission_id="tampered")
        )
    await db_session.rollback()

    with pytest.raises(DBAPIError):
        await db_session.execute(delete(InvalidSampleCleanupLifecycleEvent))
    await db_session.rollback()


async def test_correction_is_a_superseding_revision(db_session: AsyncSession) -> None:
    service = InvalidSampleEligibilityService(db_session)
    subject = _subject(str(uuid.uuid4()))

    first = await _record(service, subject, calibration=CalibrationEligibility.INCLUDE)
    second = await _record(
        service,
        subject,
        calibration=CalibrationEligibility.EXCLUDE,
        observability=ForecastOutcomeObservability.BLOCKED_PENDING_AUDIT_EVIDENCE,
        trade=TradePerformanceEligibility.EXCLUDE,
        reason="invalid sample: cleanup lifecycle blocked",
    )
    await db_session.commit()

    assert first.revision_no == 1
    assert first.supersedes_revision_no is None
    assert second.revision_no == 2
    assert second.supersedes_revision_no == 1

    rows = (
        (
            await db_session.execute(
                select(SampleEligibilityDecision)
                .where(SampleEligibilityDecision.subject_ref == subject.ref)
                .order_by(SampleEligibilityDecision.revision_no)
            )
        )
        .scalars()
        .all()
    )
    assert [row.revision_no for row in rows] == [1, 2]
    assert rows[0].calibration_eligibility == CalibrationEligibility.INCLUDE.value

    latest = await service.get_decision(subject)
    assert latest.revision_no == 2
    assert latest.calibration_eligibility is CalibrationEligibility.EXCLUDE


async def test_duplicate_revision_number_is_rejected_by_the_database(
    db_session: AsyncSession,
) -> None:
    service = InvalidSampleEligibilityService(db_session)
    subject = _subject(str(uuid.uuid4()))
    await _record(service, subject, calibration=CalibrationEligibility.INCLUDE)
    await db_session.commit()

    db_session.add(
        SampleEligibilityDecision(
            subject_kind=subject.kind.value,
            subject_ref=subject.ref,
            contract_version=CONTRACT_VERSION,
            revision_no=1,
            supersedes_revision_no=None,
            forecast_outcome_observability=(
                ForecastOutcomeObservability.OBSERVABLE.value
            ),
            calibration_eligibility=CalibrationEligibility.EXCLUDE.value,
            trade_performance_eligibility=TradePerformanceEligibility.INCLUDE.value,
            operational_reliability_eligibility=(
                OperationalReliabilityEligibility.INCLUDE.value
            ),
            decision_reason="branch attempt",
            decided_by="test",
            evidence={},
            evidence_hash="0" * 64,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_revision_gap_is_rejected_by_the_database(
    db_session: AsyncSession,
) -> None:
    subject = _subject(str(uuid.uuid4()))
    db_session.add(
        SampleEligibilityDecision(
            subject_kind=subject.kind.value,
            subject_ref=subject.ref,
            contract_version=CONTRACT_VERSION,
            revision_no=3,
            supersedes_revision_no=1,  # gap: must be 2
            forecast_outcome_observability=(
                ForecastOutcomeObservability.OBSERVABLE.value
            ),
            calibration_eligibility=CalibrationEligibility.INCLUDE.value,
            trade_performance_eligibility=TradePerformanceEligibility.INCLUDE.value,
            operational_reliability_eligibility=(
                OperationalReliabilityEligibility.INCLUDE.value
            ),
            decision_reason="gap attempt",
            decided_by="test",
            evidence={},
            evidence_hash="0" * 64,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_tampered_evidence_hash_is_refused_on_read(
    db_session: AsyncSession,
) -> None:
    service = InvalidSampleEligibilityService(db_session)
    subject = _subject(str(uuid.uuid4()))
    await _record(service, subject, calibration=CalibrationEligibility.EXCLUDE)
    await db_session.commit()

    # Only reachable by bypassing the append-only trigger — exactly the tampering
    # the digest exists to catch.
    await db_session.execute(
        text("ALTER TABLE review.sample_eligibility_decisions DISABLE TRIGGER USER")
    )
    await db_session.execute(
        update(SampleEligibilityDecision)
        .where(SampleEligibilityDecision.subject_ref == subject.ref)
        .values(evidence={"source": "swapped"})
    )
    await db_session.execute(
        text("ALTER TABLE review.sample_eligibility_decisions ENABLE TRIGGER USER")
    )
    db_session.expire_all()

    with pytest.raises(EligibilityContractError) as excinfo:
        await service.get_decision(subject)
    assert excinfo.value.code == "evidence_hash_mismatch"
    await db_session.rollback()


# --- §4.3-6: idempotent replay --------------------------------------------


async def test_duplicate_binding_authoring_is_idempotent(
    db_session: AsyncSession,
) -> None:
    service = InvalidSampleEligibilityService(db_session)
    binding = _binding()
    first = await service.record_cleanup_binding(binding)
    second = await service.record_cleanup_binding(binding)
    await db_session.commit()

    assert first.id == second.id
    count = await db_session.scalar(
        select(text("count(*)")).select_from(InvalidSampleCleanupBinding)
    )
    assert count == 1


async def test_conflicting_binding_for_same_client_order_id_is_refused(
    db_session: AsyncSession,
) -> None:
    service = InvalidSampleEligibilityService(db_session)
    await service.record_cleanup_binding(_binding())
    await db_session.commit()

    with pytest.raises(CleanupBindingError) as excinfo:
        await service.record_cleanup_binding(_binding(mission_id="other-mission"))
    assert excinfo.value.code == "conflicting_binding_for_client_order_id"
    await db_session.rollback()


async def test_duplicate_post_fill_delivery_appends_once(
    db_session: AsyncSession,
) -> None:
    """A replayed callback/reconcile/outbox delivery is a no-op."""

    service = InvalidSampleEligibilityService(db_session)
    binding = _binding()
    await service.record_cleanup_binding(binding)

    for _ in range(3):
        completion = await service.record_post_fill_evidence(
            binding=binding,
            fill_evidence=FillEvidenceCompleteness.COMPLETE,
            position_effect=PositionEffectEvidence.CONSISTENT,
            evidence={"broker_order_id": "abc"},
        )
        assert completion.status is PostFillCompletionStatus.COMPLETE
    await db_session.commit()

    count = await db_session.scalar(
        select(text("count(*)")).select_from(InvalidSampleCleanupLifecycleEvent)
    )
    assert count == 1


async def test_manual_review_event_is_recorded_not_completed(
    db_session: AsyncSession,
) -> None:
    service = InvalidSampleEligibilityService(db_session)
    binding = _binding()
    await service.record_cleanup_binding(binding)

    completion = await service.record_post_fill_evidence(
        binding=binding,
        fill_evidence=FillEvidenceCompleteness.COMPLETE,
        position_effect=PositionEffectEvidence.ABSENT,
    )
    await db_session.commit()

    assert completion.status is PostFillCompletionStatus.MANUAL_REVIEW
    assert completion.reason is (
        PostFillManualReviewReason.ABSENT_POSITION_EFFECT_EVIDENCE
    )
    row = (
        (await db_session.execute(select(InvalidSampleCleanupLifecycleEvent)))
        .scalars()
        .one()
    )
    assert row.event_kind == "post_fill_manual_review"
    assert row.completion_status == "manual_review"
    assert row.manual_review_reason == (
        PostFillManualReviewReason.ABSENT_POSITION_EFFECT_EVIDENCE.value
    )


async def test_post_fill_evidence_requires_a_persisted_binding(
    db_session: AsyncSession,
) -> None:
    service = InvalidSampleEligibilityService(db_session)
    with pytest.raises(CleanupBindingError) as excinfo:
        await service.record_post_fill_evidence(
            binding=_binding(),
            fill_evidence=FillEvidenceCompleteness.COMPLETE,
            position_effect=PositionEffectEvidence.CONSISTENT,
        )
    assert excinfo.value.code == "unbound_lifecycle"
    await db_session.rollback()


# --- §4.3-3/10: aggregates ------------------------------------------------


async def test_excluded_forecast_leaves_the_legacy_calibration_aggregate(
    db_session: AsyncSession,
) -> None:
    included = await _make_scored_forecast(db_session, created_by="claude")
    excluded = await _make_scored_forecast(db_session, created_by="claude")

    before = await svc.build_forecast_calibration_aggregate(db_session)
    assert before["groups"][0]["sample_size"] == 2

    service = InvalidSampleEligibilityService(db_session)
    await _record(
        service,
        _subject(str(excluded.forecast_id)),
        calibration=CalibrationEligibility.EXCLUDE,
        trade=TradePerformanceEligibility.EXCLUDE,
        observability=ForecastOutcomeObservability.BLOCKED_PENDING_AUDIT_EVIDENCE,
    )
    await db_session.commit()

    after = await svc.build_forecast_calibration_aggregate(db_session)
    assert after["groups"][0]["sample_size"] == 1

    remaining = await svc.list_scored_forecasts_for_calibration(db_session)
    assert [row.id for row in remaining] == [included.id]


async def test_removing_the_sql_exclusion_readmits_the_excluded_forecast(
    db_session: AsyncSession,
) -> None:
    """Mutation evidence for the legacy entry point.

    ``apply_eligibility_exclusion=False`` is exactly the mutant "drop the
    eligibility filter". If the excluded row did not reappear, the filter above
    would be proving nothing.
    """

    await _make_scored_forecast(db_session)
    excluded = await _make_scored_forecast(db_session)
    service = InvalidSampleEligibilityService(db_session)
    await _record(
        service,
        _subject(str(excluded.forecast_id)),
        calibration=CalibrationEligibility.EXCLUDE,
    )
    await db_session.commit()

    filtered = await svc.list_scored_forecasts_for_calibration(db_session)
    mutated = await svc.list_scored_forecasts_for_calibration(
        db_session, apply_eligibility_exclusion=False
    )

    assert len(filtered) == 1
    assert len(mutated) == 2
    assert excluded.id in {row.id for row in mutated}
    assert excluded.id not in {row.id for row in filtered}


async def test_superseding_revision_can_readmit_a_previously_excluded_forecast(
    db_session: AsyncSession,
) -> None:
    """Only the *latest* revision decides — the chain is not a tombstone."""

    forecast = await _make_scored_forecast(db_session)
    service = InvalidSampleEligibilityService(db_session)
    subject = _subject(str(forecast.forecast_id))
    await _record(service, subject, calibration=CalibrationEligibility.EXCLUDE)
    await db_session.commit()
    assert await svc.list_scored_forecasts_for_calibration(db_session) == []

    await _record(
        service,
        subject,
        calibration=CalibrationEligibility.INCLUDE,
        reason="audit-grade provider evidence landed",
    )
    await db_session.commit()

    rows = await svc.list_scored_forecasts_for_calibration(db_session)
    assert [row.id for row in rows] == [forecast.id]


async def test_eligible_cohort_reports_counts_and_reasons(
    db_session: AsyncSession,
) -> None:
    included = await _make_scored_forecast(db_session)
    excluded = await _make_scored_forecast(db_session)
    await _make_scored_forecast(db_session)  # no decision → unidentifiable

    service = InvalidSampleEligibilityService(db_session)
    await _record(
        service,
        _subject(str(included.forecast_id)),
        calibration=CalibrationEligibility.INCLUDE,
    )
    await _record(
        service,
        _subject(str(excluded.forecast_id)),
        calibration=CalibrationEligibility.EXCLUDE,
    )
    await db_session.commit()

    result = await build_eligible_forecast_calibration_aggregate(
        db_session,
        contract_version=CONTRACT_VERSION,
        predicate=CALIBRATION_PREDICATE,
    )

    assert result["contract_version"] == CONTRACT_VERSION
    assert result["eligibility_counts"] == {
        "included": 1,
        "excluded": 1,
        "unidentifiable": 1,
    }
    assert result["eligibility_reasons"][CalibrationEligibility.EXCLUDE.value] == 1
    assert (
        result["eligibility_reasons"][CalibrationEligibility.UNIDENTIFIABLE.value] == 1
    )
    assert sum(group["sample_size"] for group in result["groups"]) == 1


async def test_eligible_cohort_rejects_a_mismatched_predicate(
    db_session: AsyncSession,
) -> None:
    with pytest.raises(EligibilityContractError) as excinfo:
        await build_eligible_forecast_calibration_aggregate(
            db_session,
            contract_version="some-other-contract.v9",
            predicate=CALIBRATION_PREDICATE,
        )
    assert excinfo.value.code == "predicate_contract_version_mismatch"


async def test_eligible_cohort_rejects_a_non_calibration_predicate(
    db_session: AsyncSession,
) -> None:
    trade_predicate = EligibilityPredicate(
        contract_version=CONTRACT_VERSION,
        domain=EligibilityDomain.TRADE_PERFORMANCE,
        admitted=frozenset({TradePerformanceEligibility.INCLUDE}),
    )
    with pytest.raises(EligibilityContractError) as excinfo:
        await build_eligible_forecast_calibration_aggregate(
            db_session,
            contract_version=CONTRACT_VERSION,
            predicate=trade_predicate,
        )
    assert excinfo.value.code == "wrong_predicate_domain"


async def test_recording_a_decision_never_resolves_the_forecast(
    db_session: AsyncSession,
) -> None:
    """§4.3-2: the outcome record survives an invalid-sample decision untouched."""

    forecast = await _make_scored_forecast(db_session)
    before = (forecast.status, forecast.outcome, forecast.brier_score)

    service = InvalidSampleEligibilityService(db_session)
    await _record(
        service,
        _subject(str(forecast.forecast_id)),
        calibration=CalibrationEligibility.EXCLUDE,
        trade=TradePerformanceEligibility.EXCLUDE,
        observability=ForecastOutcomeObservability.BLOCKED_PENDING_AUDIT_EVIDENCE,
    )
    await db_session.commit()
    await db_session.refresh(forecast)

    assert (forecast.status, forecast.outcome, forecast.brier_score) == before
    assert forecast.resolved_at is not None
