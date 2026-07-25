"""Transactional activation of immutable ROB-849 paper cohorts."""

from __future__ import annotations

from sqlalchemy import select, text
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
from app.services.crypto_execution_mapping import (
    map_alpaca_paper_to_binance_public_spot,
    map_binance_public_spot_to_alpaca_paper,
)
from app.services.paper_cohort.contracts import (
    CohortActivation,
    CohortAssignmentInput,
    PaperCohortError,
)
from app.services.paper_validation.locking import lock_validation_streams

_ACTIVATABLE_STATES = frozenset(
    {"offline_eligible", "shadow_soak", "paper_active", "promotion_eligible"}
)


class PaperCohortService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def _lock(self, cohort_id: str) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"paper-cohort:{cohort_id}"},
        )

    async def _existing(self, cohort_id: str) -> PaperValidationCohort | None:
        return await self._session.scalar(
            select(PaperValidationCohort).where(
                PaperValidationCohort.cohort_id == cohort_id
            )
        )

    async def _latest_validation(
        self, validation_id: str
    ) -> PaperValidationStateTransition | None:
        return await self._session.scalar(
            select(PaperValidationStateTransition)
            .where(PaperValidationStateTransition.validation_id == validation_id)
            .order_by(PaperValidationStateTransition.sequence.desc())
            .limit(1)
        )

    async def _validate_registry(self, item: CohortAssignmentInput) -> None:
        experiment = await self._session.scalar(
            select(ResearchStrategyExperiment).where(
                ResearchStrategyExperiment.experiment_id == item.experiment_id
            )
        )
        run = await self._session.get(ResearchBacktestRun, item.source_backtest_run_id)
        if experiment is None or run is None:
            raise PaperCohortError("registry_identity_mismatch")
        if not all(
            (
                experiment.experiment_id == item.experiment_hash,
                experiment.strategy_version == item.strategy_version_id,
                experiment.strategy_hash == item.strategy_hash,
                experiment.frozen_config_hash == item.config_hash,
                experiment.policy_hash == item.policy_hash,
                run.strategy_experiment_id == experiment.id,
                run.strategy_version == experiment.strategy_version,
                run.market == "spot",
                run.timeframe == "1m",
                run.trial_status == "completed",
            )
        ):
            raise PaperCohortError("registry_identity_mismatch")

    async def _validate_authoritative_state(
        self, request: CohortActivation, item: CohortAssignmentInput
    ) -> None:
        latest = await self._latest_validation(item.validation_id)
        if latest is None:
            raise PaperCohortError("validation_identity_mismatch")
        if not all(
            (
                latest.validation_version == item.validation_version,
                latest.experiment_id == item.experiment_id,
                latest.strategy_version_id == item.strategy_version_id,
                latest.cohort_id == request.cohort_id,
                latest.experiment_hash == item.experiment_hash,
                latest.cohort_hash == request.expected_cohort_hash,
                latest.strategy_hash == item.strategy_hash,
                latest.config_hash == item.config_hash,
                latest.policy_hash == item.policy_hash,
                latest.input_hash == item.input_hash,
            )
        ):
            raise PaperCohortError("validation_identity_mismatch")
        if latest.new_state not in _ACTIVATABLE_STATES:
            raise PaperCohortError("validation_state_not_eligible")

    @staticmethod
    def _validate_symbol_mapping(request: CohortActivation) -> None:
        for symbol in request.symbols:
            mapping = map_binance_public_spot_to_alpaca_paper(symbol)
            if (
                map_alpaca_paper_to_binance_public_spot(mapping.execution_symbol)
                != symbol
            ):
                raise PaperCohortError("symbol_mapping_mismatch")

    async def activate(self, request: CohortActivation) -> PaperValidationCohort:
        computed_hash = request.computed_cohort_hash()
        if computed_hash != request.expected_cohort_hash:
            raise PaperCohortError("cohort_hash_mismatch")
        self._validate_symbol_mapping(request)
        # Lifecycle lock suffix: sorted validation streams, then this cohort.
        await lock_validation_streams(
            self._session, (item.validation_id for item in request.assignments)
        )
        await self._lock(request.cohort_id)

        existing = await self._existing(request.cohort_id)
        if existing is not None:
            if existing.cohort_hash == request.expected_cohort_hash:
                return existing
            raise PaperCohortError("activation_conflict")

        for item in request.assignments:
            await self._validate_registry(item)
            await self._validate_authoritative_state(request, item)

        row = PaperValidationCohort(
            cohort_id=request.cohort_id,
            cohort_hash=request.expected_cohort_hash,
            venues=list(request.venues),
            symbols=list(request.symbols),
            market=request.market,
            leverage=request.leverage,
            interval=request.interval,
            required_lookback=request.required_lookback,
            max_capture_skew_ms=request.max_capture_skew_ms,
            max_ticker_age_ms=request.max_ticker_age_ms,
            capital_notional_usd=request.capital_notional_usd,
            assignment_count=len(request.assignments),
            activated_at=request.activated_at,
            stop_at=request.stop_at,
        )
        self._session.add(row)
        self._session.add_all(
            [
                PaperValidationCohortAssignment(
                    assignment_id=item.assignment_id,
                    cohort_id=request.cohort_id,
                    ordinal=item.ordinal,
                    role=item.role,
                    validation_id=item.validation_id,
                    validation_version=item.validation_version,
                    experiment_id=item.experiment_id,
                    source_backtest_run_id=item.source_backtest_run_id,
                    strategy_version_id=item.strategy_version_id,
                    target_weights=item.weights_json(),
                    experiment_hash=item.experiment_hash,
                    strategy_hash=item.strategy_hash,
                    config_hash=item.config_hash,
                    policy_hash=item.policy_hash,
                    input_hash=item.input_hash,
                )
                for item in request.assignments
            ]
        )
        await self._session.flush()
        return row


__all__ = ["PaperCohortService"]
