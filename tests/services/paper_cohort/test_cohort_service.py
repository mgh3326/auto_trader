from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_cohort import (
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)
from app.models.paper_validation import PaperValidationStateTransition
from app.models.research_backtest import (
    ResearchBacktestRun,
    ResearchStrategyExperiment,
)
from app.services.paper_cohort.cohort_service import (
    PaperCohortError,
    PaperCohortService,
)
from app.services.paper_cohort.contracts import (
    CohortActivation,
    CohortAssignmentInput,
    SymbolTargetWeight,
)
from tests.services.paper_validation.conftest import stable_hash

pytestmark = pytest.mark.integration


def _assignment(
    experiment: ResearchStrategyExperiment,
    run: ResearchBacktestRun,
    *,
    nonce: str,
    role: str = "champion",
    ordinal: int = 0,
) -> CohortAssignmentInput:
    return CohortAssignmentInput(
        assignment_id=f"assignment-{nonce}-{ordinal}",
        ordinal=ordinal,
        role=role,
        validation_id=f"validation-{nonce}-{ordinal}",
        validation_version=1,
        experiment_id=experiment.experiment_id,
        source_backtest_run_id=run.id,
        strategy_version_id=experiment.strategy_version,
        target_weights=(
            SymbolTargetWeight(symbol="BTCUSDT", weight=Decimal("0.6")),
            SymbolTargetWeight(symbol="ETHUSDT", weight=Decimal("0.4")),
        ),
        experiment_hash=experiment.experiment_id,
        strategy_hash=experiment.strategy_hash,
        config_hash=experiment.frozen_config_hash,
        policy_hash=experiment.policy_hash,
        input_hash=stable_hash(f"input-{nonce}-{ordinal}"),
    )


def _activation(
    assignments: tuple[CohortAssignmentInput, ...],
    *,
    nonce: str,
    capital: Decimal = Decimal("10000"),
) -> CohortActivation:
    provisional = CohortActivation(
        cohort_id=f"cohort-{nonce}",
        expected_cohort_hash="0" * 64,
        venues=("binance", "alpaca"),
        symbols=("BTCUSDT", "ETHUSDT"),
        market="spot",
        leverage=Decimal("1"),
        interval="1m",
        required_lookback=30,
        max_capture_skew_ms=2000,
        max_ticker_age_ms=5000,
        capital_notional_usd=capital,
        activated_at=datetime(2026, 7, 14, 1, 0, tzinfo=UTC),
        stop_at=datetime(2026, 7, 21, 1, 0, tzinfo=UTC),
        assignments=assignments,
    )
    return provisional.model_copy(
        update={"expected_cohort_hash": provisional.computed_cohort_hash()}
    )


async def _registry_rows(
    db_session: AsyncSession, nonce: str
) -> tuple[ResearchStrategyExperiment, ResearchBacktestRun]:
    experiment = ResearchStrategyExperiment(
        experiment_id=stable_hash(f"experiment-{nonce}"),
        strategy_key=f"strategy-{nonce}",
        strategy_version="strategy-v1",
        strategy_hash=stable_hash(f"strategy-{nonce}"),
        code_hash=stable_hash(f"code-{nonce}"),
        params_hash=stable_hash(f"params-{nonce}"),
        dataset_manifest_hash=stable_hash(f"dataset-{nonce}"),
        universe_hash=stable_hash(f"universe-{nonce}"),
        pit_hash=stable_hash(f"pit-{nonce}"),
        frozen_config_hash=stable_hash(f"config-{nonce}"),
        policy_hash=stable_hash(f"policy-{nonce}"),
        benchmark_hash=stable_hash(f"benchmark-{nonce}"),
        cost_hash=stable_hash(f"cost-{nonce}"),
        mdd_hash=stable_hash(f"mdd-{nonce}"),
        manifest={},
    )
    db_session.add(experiment)
    await db_session.flush()
    run = ResearchBacktestRun(
        run_id=f"backtest-{nonce}",
        strategy_name=experiment.strategy_key,
        strategy_version=experiment.strategy_version,
        exchange="binance",
        market="spot",
        timeframe="1m",
        runner="pytest",
        total_trades=10,
        profit_factor=Decimal("1.2"),
        max_drawdown=Decimal("0.1"),
        strategy_experiment_id=experiment.id,
        trial_index=1,
        trial_status="completed",
        trial_idempotency_key=f"trial-{nonce}",
    )
    db_session.add(run)
    await db_session.flush()
    return experiment, run


async def _authoritative_history(
    db_session: AsyncSession,
    request: CohortActivation,
    *,
    state: str = "shadow_soak",
) -> None:
    valid_path = [
        "draft",
        "offline_eligible",
        "shadow_soak",
        "paper_active",
        "promotion_eligible",
    ]
    if state in valid_path:
        path = valid_path[: valid_path.index(state) + 1]
    elif state in {"promoted", "rejected", "aborted"}:
        path = [*valid_path, state]
    else:
        raise ValueError(f"unsupported test state: {state}")
    for assignment in request.assignments:
        for sequence, new_state in enumerate(path, start=1):
            db_session.add(
                PaperValidationStateTransition(
                    validation_id=assignment.validation_id,
                    validation_version=assignment.validation_version,
                    experiment_id=assignment.experiment_id,
                    strategy_version_id=assignment.strategy_version_id,
                    cohort_id=request.cohort_id,
                    sequence=sequence,
                    idempotency_key=(f"activate-{assignment.validation_id}-{sequence}"),
                    request_hash=stable_hash(
                        f"request-{assignment.validation_id}-{sequence}"
                    ),
                    prior_state=None if sequence == 1 else path[sequence - 2],
                    new_state=new_state,
                    actor_id="operator-1",
                    actor_role="operator",
                    reason_code="test_evidence",
                    reason_text="authoritative ROB-848 test history",
                    experiment_hash=assignment.experiment_hash,
                    cohort_hash=request.expected_cohort_hash,
                    strategy_hash=assignment.strategy_hash,
                    config_hash=assignment.config_hash,
                    policy_hash=assignment.policy_hash,
                    input_hash=assignment.input_hash,
                    input_bundle_id=f"bundle-{assignment.assignment_id}",
                    policy_version="policy-v1",
                    evidence_ids=["evidence-1"],
                )
            )
    await db_session.flush()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("venues", ("alpaca", "binance")),
        ("symbols", ("ETHUSDT", "BTCUSDT")),
        ("market", "margin"),
        ("leverage", Decimal("2")),
        ("interval", "5m"),
    ],
)
@pytest.mark.asyncio
async def test_contract_rejects_non_v1_cohort_shape(
    db_session: AsyncSession, field: str, value: object
) -> None:
    nonce = uuid4().hex
    experiment, run = await _registry_rows(db_session, nonce)
    assignment = _assignment(experiment, run, nonce=nonce)
    request = _activation((assignment,), nonce=nonce)

    with pytest.raises(ValidationError):
        CohortActivation.model_validate(
            {**request.model_dump(mode="python"), field: value}
        )


@pytest.mark.asyncio
async def test_contract_requires_one_champion_and_at_most_two_challengers(
    db_session: AsyncSession,
) -> None:
    nonce = uuid4().hex
    experiment, run = await _registry_rows(db_session, nonce)
    champion = _assignment(experiment, run, nonce=nonce)

    with pytest.raises(ValidationError):
        _activation(
            (champion.model_copy(update={"role": "challenger", "ordinal": 1}),),
            nonce=nonce,
        )

    too_many = (champion,) + tuple(
        champion.model_copy(
            update={
                "assignment_id": f"assignment-{nonce}-{ordinal}",
                "validation_id": f"validation-{nonce}-{ordinal}",
                "experiment_id": stable_hash(f"other-experiment-{ordinal}"),
                "experiment_hash": stable_hash(f"other-experiment-{ordinal}"),
                "role": "challenger",
                "ordinal": ordinal,
            }
        )
        for ordinal in (1, 2, 3)
    )
    with pytest.raises(ValidationError):
        _activation(too_many, nonce=nonce)


@pytest.mark.asyncio
async def test_activate_persists_frozen_registry_and_authoritative_identity(
    db_session: AsyncSession,
) -> None:
    nonce = uuid4().hex
    experiment, run = await _registry_rows(db_session, nonce)
    request = _activation((_assignment(experiment, run, nonce=nonce),), nonce=nonce)
    await _authoritative_history(db_session, request)

    row = await PaperCohortService(db_session).activate(request)
    await db_session.commit()

    assert row.cohort_id == request.cohort_id
    assert row.cohort_hash == request.expected_cohort_hash
    assignment = await db_session.scalar(
        select(PaperValidationCohortAssignment).where(
            PaperValidationCohortAssignment.cohort_id == request.cohort_id
        )
    )
    assert assignment is not None
    assert assignment.target_weights == {"BTCUSDT": "0.6", "ETHUSDT": "0.4"}
    assert assignment.source_backtest_run_id == run.id


@pytest.mark.asyncio
async def test_activate_identical_replay_returns_original_row_and_conflict_fails(
    db_session: AsyncSession,
) -> None:
    nonce = uuid4().hex
    experiment, run = await _registry_rows(db_session, nonce)
    request = _activation((_assignment(experiment, run, nonce=nonce),), nonce=nonce)
    await _authoritative_history(db_session, request, state="paper_active")
    service = PaperCohortService(db_session)

    first = await service.activate(request)
    await db_session.commit()
    replay = await service.activate(request)
    assert replay.id == first.id

    conflict = _activation(
        request.assignments,
        nonce=nonce,
        capital=request.capital_notional_usd + Decimal("1"),
    )
    with pytest.raises(PaperCohortError) as exc_info:
        await service.activate(conflict)
    assert exc_info.value.reason_code == "activation_conflict"
    count = await db_session.scalar(
        select(func.count())
        .select_from(PaperValidationCohort)
        .where(PaperValidationCohort.cohort_id == request.cohort_id)
    )
    assert count == 1


@pytest.mark.parametrize(
    ("mutation", "reason_code"),
    [
        ("strategy_hash", "registry_identity_mismatch"),
        ("input_hash", "validation_identity_mismatch"),
    ],
)
@pytest.mark.asyncio
async def test_activate_fails_closed_on_registry_or_rob848_mismatch(
    db_session: AsyncSession, mutation: str, reason_code: str
) -> None:
    nonce = uuid4().hex
    experiment, run = await _registry_rows(db_session, nonce)
    good = _assignment(experiment, run, nonce=nonce)
    request = _activation((good,), nonce=nonce)
    await _authoritative_history(db_session, request)
    bad_assignment = good.model_copy(update={mutation: stable_hash(f"bad-{mutation}")})
    bad = _activation((bad_assignment,), nonce=nonce)

    with pytest.raises(PaperCohortError) as exc_info:
        await PaperCohortService(db_session).activate(bad)
    assert exc_info.value.reason_code == reason_code


@pytest.mark.parametrize("state", ["draft", "aborted"])
@pytest.mark.asyncio
async def test_activate_rejects_draft_or_terminal_validation_state(
    db_session: AsyncSession,
    state: str,
) -> None:
    nonce = uuid4().hex
    experiment, run = await _registry_rows(db_session, nonce)
    request = _activation((_assignment(experiment, run, nonce=nonce),), nonce=nonce)
    await _authoritative_history(db_session, request, state=state)

    with pytest.raises(PaperCohortError) as exc_info:
        await PaperCohortService(db_session).activate(request)
    assert exc_info.value.reason_code == "validation_state_not_eligible"


def test_contract_requires_aware_ordered_activation_window() -> None:
    with pytest.raises(ValidationError):
        CohortActivation(
            cohort_id="cohort-x",
            expected_cohort_hash="0" * 64,
            venues=("binance", "alpaca"),
            symbols=("BTCUSDT", "ETHUSDT"),
            market="spot",
            leverage=Decimal("1"),
            interval="1m",
            required_lookback=30,
            max_capture_skew_ms=2000,
            max_ticker_age_ms=5000,
            capital_notional_usd=Decimal("1"),
            activated_at=datetime.now(),
            stop_at=datetime.now(UTC) - timedelta(days=1),
            assignments=(),
        )
