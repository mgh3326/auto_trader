from __future__ import annotations

import hashlib
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import DBAPIError, IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_validation import (
    PaperValidationPostmortemReview,
    PaperValidationStateTransition,
    StrategyHypothesisDraft,
)
from app.models.research_backtest import ResearchStrategyExperiment

HASH_FIELDS = (
    "strategy_hash",
    "code_hash",
    "params_hash",
    "dataset_manifest_hash",
    "universe_hash",
    "pit_hash",
    "frozen_config_hash",
    "policy_hash",
    "benchmark_hash",
    "cost_hash",
    "mdd_hash",
)


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


async def _experiment(session: AsyncSession) -> ResearchStrategyExperiment:
    nonce = uuid4().hex
    hashes = {name: _hash(f"{nonce}:{name}") for name in HASH_FIELDS}
    experiment_id = _hash(f"{nonce}:experiment")
    row = ResearchStrategyExperiment(
        experiment_id=experiment_id,
        strategy_key=f"strategy-{nonce}",
        strategy_version="strategy-v1",
        manifest={},
        **hashes,
    )
    session.add(row)
    await session.flush()
    return row


def _transition(
    experiment: ResearchStrategyExperiment,
    *,
    validation_id: str | None = None,
    sequence: int = 1,
    idempotency_key: str | None = None,
    strategy_hash: str | None = None,
) -> PaperValidationStateTransition:
    return PaperValidationStateTransition(
        validation_id=validation_id or f"validation-{uuid4().hex}",
        validation_version=1,
        experiment_id=experiment.experiment_id,
        strategy_version_id=experiment.strategy_version,
        cohort_id="cohort-opaque-1",
        sequence=sequence,
        idempotency_key=idempotency_key or f"transition-{uuid4().hex}",
        request_hash=_hash(f"request-{uuid4().hex}"),
        prior_state=None,
        new_state="draft",
        actor_id="operator-1",
        actor_role="operator",
        reason_code="validation_registered",
        reason_text="registered immutable validation identity",
        experiment_hash=experiment.experiment_id,
        cohort_hash=_hash("cohort"),
        strategy_hash=strategy_hash or experiment.strategy_hash,
        config_hash=experiment.frozen_config_hash,
        policy_hash=experiment.policy_hash,
        input_hash=_hash("input"),
        input_bundle_id="bundle-1",
        policy_version="policy-v1",
        evidence_ids=["experiment-registry"],
    )


def test_migration_descends_from_starting_head_and_defines_db_triggers() -> None:
    source = (
        Path(__file__).resolve().parents[3]
        / "alembic"
        / "versions"
        / "20260713_rob848_paper_validation.py"
    ).read_text()

    assert 'revision = "20260713_rob848_paper_validation"' in source
    assert 'down_revision = "20260713_rob866_manual_alerts"' in source
    assert "reject_paper_validation_audit_mutation" in source
    assert "validate_paper_validation_experiment_identity" in source


@pytest.mark.asyncio
async def test_transition_update_and_delete_are_rejected_by_db_trigger(
    db_session: AsyncSession,
) -> None:
    experiment = await _experiment(db_session)
    event = _transition(experiment)
    db_session.add(event)
    await db_session.flush()
    event_id = event.id
    await db_session.commit()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            update(PaperValidationStateTransition)
            .where(PaperValidationStateTransition.id == event_id)
            .values(reason_text="mutated")
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            delete(PaperValidationStateTransition).where(
                PaperValidationStateTransition.id == event_id
            )
        )
        await db_session.commit()
    await db_session.rollback()


@pytest.mark.asyncio
async def test_sequence_and_idempotency_uniqueness_are_db_enforced(
    db_session: AsyncSession,
) -> None:
    experiment = await _experiment(db_session)
    validation_id = f"validation-{uuid4().hex}"
    event = _transition(experiment, validation_id=validation_id)
    db_session.add(event)
    await db_session.flush()

    duplicate_sequence = _transition(experiment, validation_id=validation_id)
    with pytest.raises(IntegrityError, match="transition_sequence"):
        async with db_session.begin_nested():
            db_session.add(duplicate_sequence)
            await db_session.flush()

    duplicate_key = _transition(
        experiment,
        validation_id=validation_id,
        sequence=2,
        idempotency_key=event.idempotency_key,
    )
    duplicate_key.prior_state = "draft"
    duplicate_key.new_state = "offline_eligible"
    with pytest.raises(IntegrityError, match="transition_idempotency"):
        async with db_session.begin_nested():
            db_session.add(duplicate_key)
            await db_session.flush()


@pytest.mark.asyncio
async def test_experiment_fk_and_hash_mismatch_fail_closed(
    db_session: AsyncSession,
) -> None:
    experiment = await _experiment(db_session)

    mismatched = _transition(experiment, strategy_hash=_hash("wrong-strategy"))
    with pytest.raises(DBAPIError, match="experiment identity mismatch"):
        async with db_session.begin_nested():
            db_session.add(mismatched)
            await db_session.flush()

    missing_fk = _transition(experiment)
    missing_fk.experiment_id = _hash("missing-experiment")
    missing_fk.experiment_hash = missing_fk.experiment_id
    with pytest.raises(IntegrityError):
        async with db_session.begin_nested():
            db_session.add(missing_fk)
            await db_session.flush()


@pytest.mark.asyncio
async def test_direct_invalid_graph_insert_is_rejected(
    db_session: AsyncSession,
) -> None:
    experiment = await _experiment(db_session)
    invalid = _transition(experiment)
    invalid.new_state = "promoted"

    with pytest.raises(IntegrityError, match="history continuity mismatch"):
        async with db_session.begin_nested():
            db_session.add(invalid)
            await db_session.flush()


@pytest.mark.asyncio
async def test_direct_researcher_transition_is_rejected(
    db_session: AsyncSession,
) -> None:
    experiment = await _experiment(db_session)
    invalid = _transition(experiment)
    invalid.actor_role = "researcher"

    with pytest.raises(IntegrityError, match="transition_actor_role"):
        async with db_session.begin_nested():
            db_session.add(invalid)
            await db_session.flush()


@pytest.mark.asyncio
async def test_direct_sequence_gap_and_prior_mismatch_are_rejected(
    db_session: AsyncSession,
) -> None:
    experiment = await _experiment(db_session)
    validation_id = f"validation-{uuid4().hex}"

    missing_first = _transition(
        experiment,
        validation_id=validation_id,
        sequence=2,
    )
    missing_first.prior_state = "draft"
    missing_first.new_state = "offline_eligible"
    with pytest.raises(DBAPIError, match="history continuity mismatch"):
        async with db_session.begin_nested():
            db_session.add(missing_first)
            await db_session.flush()

    first = _transition(experiment, validation_id=validation_id)
    db_session.add(first)
    await db_session.flush()

    gap = _transition(experiment, validation_id=validation_id, sequence=99)
    gap.prior_state = "promotion_eligible"
    gap.new_state = "promoted"
    with pytest.raises(DBAPIError, match="history continuity mismatch"):
        async with db_session.begin_nested():
            db_session.add(gap)
            await db_session.flush()

    wrong_prior = _transition(experiment, validation_id=validation_id, sequence=2)
    wrong_prior.prior_state = "offline_eligible"
    wrong_prior.new_state = "shadow_soak"
    with pytest.raises(DBAPIError, match="history continuity mismatch"):
        async with db_session.begin_nested():
            db_session.add(wrong_prior)
            await db_session.flush()


@pytest.mark.asyncio
async def test_direct_narrative_must_exact_match_existing_validation_stream(
    db_session: AsyncSession,
) -> None:
    experiment = await _experiment(db_session)
    validation_id = f"validation-{uuid4().hex}"
    transition = _transition(experiment, validation_id=validation_id)
    db_session.add(transition)
    await db_session.flush()
    mismatched = StrategyHypothesisDraft(
        validation_id=validation_id,
        validation_version=1,
        experiment_id=experiment.experiment_id,
        strategy_version_id=experiment.strategy_version,
        cohort_id="different-cohort",
        idempotency_key=f"hypothesis-{uuid4().hex}",
        request_hash=_hash(f"request-{uuid4().hex}"),
        author_id="researcher-1",
        author_role="researcher",
        mechanism="mechanism",
        universe=["KRX:005930"],
        horizon="5d",
        entry_criteria=["entry"],
        exit_criteria=["exit"],
        invalidation_criteria=["invalidate"],
        data_requirements=["PIT bars"],
        expected_cost_hurdle="0.003",
        turnover_bound="0.25",
        risk_bound="0.02",
        cited_evidence=["evidence-1"],
        experiment_hash=experiment.experiment_id,
        cohort_hash=transition.cohort_hash,
        strategy_hash=experiment.strategy_hash,
        config_hash=experiment.frozen_config_hash,
        policy_hash=experiment.policy_hash,
        input_hash=transition.input_hash,
    )

    with pytest.raises(DBAPIError, match="audit stream identity mismatch"):
        async with db_session.begin_nested():
            db_session.add(mismatched)
            await db_session.flush()


@pytest.mark.asyncio
async def test_all_three_audit_tables_have_immutable_triggers(
    db_session: AsyncSession,
) -> None:
    rows = (
        await db_session.execute(
            text(
                "SELECT event_object_table FROM information_schema.triggers "
                "WHERE trigger_schema='research' "
                "AND trigger_name LIKE 'trg_paper_validation_%_immutable'"
            )
        )
    ).scalars()

    assert set(rows) == {
        "paper_validation_state_transitions",
        "strategy_hypothesis_drafts",
        "paper_validation_postmortem_reviews",
    }


@pytest.mark.parametrize(
    ("model", "mutable_field", "changed_value"),
    [
        (StrategyHypothesisDraft, "mechanism", "mutated mechanism"),
        (PaperValidationPostmortemReview, "review_text", "mutated review"),
    ],
)
@pytest.mark.asyncio
async def test_hypothesis_and_review_update_delete_are_rejected(
    db_session: AsyncSession,
    model: type[StrategyHypothesisDraft | PaperValidationPostmortemReview],
    mutable_field: str,
    changed_value: str,
) -> None:
    experiment = await _experiment(db_session)
    transition = _transition(experiment)
    db_session.add(transition)
    await db_session.flush()
    common = {
        "validation_id": transition.validation_id,
        "validation_version": transition.validation_version,
        "experiment_id": transition.experiment_id,
        "strategy_version_id": transition.strategy_version_id,
        "cohort_id": transition.cohort_id,
        "idempotency_key": f"audit-{uuid4().hex}",
        "request_hash": _hash(f"request-{uuid4().hex}"),
        "experiment_hash": transition.experiment_hash,
        "cohort_hash": transition.cohort_hash,
        "strategy_hash": transition.strategy_hash,
        "config_hash": transition.config_hash,
        "policy_hash": transition.policy_hash,
        "input_hash": transition.input_hash,
    }
    if model is StrategyHypothesisDraft:
        row = StrategyHypothesisDraft(
            **common,
            author_id="researcher-1",
            author_role="researcher",
            mechanism="mechanism",
            universe=["KRX:005930"],
            horizon="5d",
            entry_criteria=["entry"],
            exit_criteria=["exit"],
            invalidation_criteria=["invalidate"],
            data_requirements=["PIT bars"],
            expected_cost_hurdle="0.003",
            turnover_bound="0.25",
            risk_bound="0.02",
            cited_evidence=["evidence-1"],
        )
    else:
        row = PaperValidationPostmortemReview(
            **common,
            evaluator_id="reviewer-1",
            evaluator_role="reviewer",
            review_text="review",
            cited_evidence=["evidence-1"],
        )
    db_session.add(row)
    await db_session.flush()
    row_id = row.id
    await db_session.commit()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(
            update(model)
            .where(model.id == row_id)
            .values({mutable_field: changed_value})
        )
        await db_session.commit()
    await db_session.rollback()

    with pytest.raises(DBAPIError, match="append-only"):
        await db_session.execute(delete(model).where(model.id == row_id))
        await db_session.commit()
    await db_session.rollback()
