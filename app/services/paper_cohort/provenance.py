"""ROB-845 provenance verifier backed by ROB-849 rows and ROB-848 state."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_cohort import (
    CanonicalMarketSnapshot,
    PaperCohortDecision,
    PaperCohortVenueIntent,
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)
from app.models.research_backtest import ResearchBacktestRun, ResearchStrategyExperiment
from app.services.brokers.paper.contracts import (
    PaperOrderRequest,
    VerifiedExperimentProvenance,
)
from app.services.paper_cohort.contracts import (
    CohortActivation,
    CohortAssignmentInput,
    PaperCohortError,
    SymbolTargetWeight,
)
from app.services.paper_cohort.market_snapshot import CanonicalSnapshotPayload
from app.services.paper_cohort.signals import (
    CanonicalTargetSignal,
    SignalComputationInput,
    VenueQuote,
    build_would_order_evidence,
    compute_target_signal,
)
from app.services.paper_validation.contracts import (
    FrozenInputStamp,
    PaperOrderAuthorization,
    PolicyStamp,
    ValidationIdentity,
    ValidationState,
)
from app.services.research_canonical_hash import canonical_sha256


class ValidationAuthorizer(Protocol):
    async def authorize_order_submission(
        self, caller_id: str, identity: ValidationIdentity
    ) -> PaperOrderAuthorization: ...


class PaperCohortProvenanceVerifier:
    def __init__(
        self,
        session: AsyncSession,
        *,
        validation_service: ValidationAuthorizer,
        caller_id: str,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._validation = validation_service
        self._caller_id = caller_id
        self._clock = clock or (lambda: datetime.now(UTC))

    @staticmethod
    def _fail() -> None:
        raise PaperCohortError("provenance_mismatch")

    @staticmethod
    def _cohort_contract(
        cohort: PaperValidationCohort,
        assignments: list[PaperValidationCohortAssignment],
    ) -> CohortActivation:
        return CohortActivation(
            cohort_id=cohort.cohort_id,
            expected_cohort_hash=cohort.cohort_hash,
            venues=tuple(cohort.venues),  # type: ignore[arg-type]
            symbols=tuple(cohort.symbols),  # type: ignore[arg-type]
            market=cohort.market,  # type: ignore[arg-type]
            leverage=cohort.leverage,
            interval=cohort.interval,  # type: ignore[arg-type]
            required_lookback=cohort.required_lookback,
            max_capture_skew_ms=cohort.max_capture_skew_ms,
            max_ticker_age_ms=cohort.max_ticker_age_ms,
            capital_notional_usd=cohort.capital_notional_usd,
            activated_at=cohort.activated_at,
            stop_at=cohort.stop_at,
            assignments=tuple(
                CohortAssignmentInput(
                    assignment_id=item.assignment_id,
                    ordinal=item.ordinal,
                    role=item.role,  # type: ignore[arg-type]
                    validation_id=item.validation_id,
                    validation_version=item.validation_version,
                    experiment_id=item.experiment_id,
                    source_backtest_run_id=item.source_backtest_run_id,
                    strategy_version_id=item.strategy_version_id,
                    target_weights=tuple(
                        SymbolTargetWeight(
                            symbol=symbol,  # type: ignore[arg-type]
                            weight=Decimal(item.target_weights[symbol]),
                        )
                        for symbol in ("BTCUSDT", "ETHUSDT")
                    ),  # type: ignore[arg-type]
                    experiment_hash=item.experiment_hash,
                    strategy_hash=item.strategy_hash,
                    config_hash=item.config_hash,
                    policy_hash=item.policy_hash,
                    input_hash=item.input_hash,
                )
                for item in assignments
            ),
        )

    async def verify(self, request: PaperOrderRequest) -> VerifiedExperimentProvenance:
        return await self._verify(request, authorize_submission=True)

    async def verify_persisted(
        self, request: PaperOrderRequest
    ) -> VerifiedExperimentProvenance:
        """Verify immutable ROB-849/native evidence without authorizing a new POST.

        This narrower path exists for replay reconciliation and owned order
        control after a validation has been stopped or aborted.  It must never
        be wired into the fresh submit composition root.
        """

        return await self._verify(request, authorize_submission=False)

    async def _verify(
        self,
        request: PaperOrderRequest,
        *,
        authorize_submission: bool,
    ) -> VerifiedExperimentProvenance:
        intent = await self._session.scalar(
            select(PaperCohortVenueIntent).where(
                PaperCohortVenueIntent.intent_id == request.intent_id
            )
        )
        if intent is None:
            self._fail()
        assert intent is not None
        decision = await self._session.scalar(
            select(PaperCohortDecision).where(
                PaperCohortDecision.decision_id == intent.decision_id
            )
        )
        snapshot = await self._session.scalar(
            select(CanonicalMarketSnapshot).where(
                CanonicalMarketSnapshot.snapshot_id == intent.snapshot_id
            )
        )
        cohort = await self._session.scalar(
            select(PaperValidationCohort).where(
                PaperValidationCohort.cohort_id == intent.cohort_id
            )
        )
        if decision is None or snapshot is None or cohort is None:
            self._fail()
        assert decision is not None and snapshot is not None and cohort is not None
        assignment = await self._session.scalar(
            select(PaperValidationCohortAssignment).where(
                PaperValidationCohortAssignment.assignment_id == decision.assignment_id
            )
        )
        if assignment is None:
            self._fail()
        assert assignment is not None
        assignments = list(
            (
                await self._session.scalars(
                    select(PaperValidationCohortAssignment)
                    .where(
                        PaperValidationCohortAssignment.cohort_id == cohort.cohort_id
                    )
                    .order_by(PaperValidationCohortAssignment.ordinal)
                )
            ).all()
        )
        experiment = await self._session.scalar(
            select(ResearchStrategyExperiment).where(
                ResearchStrategyExperiment.experiment_id == assignment.experiment_id
            )
        )
        backtest = await self._session.get(
            ResearchBacktestRun, assignment.source_backtest_run_id
        )
        if experiment is None or backtest is None:
            self._fail()
        assert experiment is not None and backtest is not None
        if (
            authorize_submission
            and cohort.stop_at is not None
            and self._clock() >= cohort.stop_at
        ):
            raise PaperCohortError("cohort_stopped")
        if decision.mode != "paper_active":
            self._fail()

        try:
            snapshot_payload = CanonicalSnapshotPayload.model_validate(snapshot.payload)
            signal = CanonicalTargetSignal.model_validate(decision.signal_payload)
            recomputed_signal = compute_target_signal(
                snapshot_payload,
                SignalComputationInput(
                    cohort_id=cohort.cohort_id,
                    assignment_id=assignment.assignment_id,
                    experiment_id=assignment.experiment_id,
                    strategy_version_id=assignment.strategy_version_id,
                    strategy_hash=assignment.strategy_hash,
                    config_hash=assignment.config_hash,
                    policy_hash=assignment.policy_hash,
                    symbol=decision.symbol,
                    target_weight=Decimal(assignment.target_weights[decision.symbol]),
                    capital_notional_usd=cohort.capital_notional_usd,
                ),
            )
            quote_payload = intent.venue_quote_evidence

            def optional_decimal(name: str) -> Decimal | None:
                value = quote_payload.get(name)
                return None if value == "not_applicable" else Decimal(str(value))

            persisted_quote = VenueQuote(
                venue=str(quote_payload["venue"]),  # type: ignore[arg-type]
                symbol=str(quote_payload["symbol"]),
                bid_price=Decimal(str(quote_payload["bid_price"])),
                ask_price=Decimal(str(quote_payload["ask_price"])),
                bid_qty=Decimal(str(quote_payload["bid_qty"])),
                ask_qty=Decimal(str(quote_payload["ask_qty"])),
                fetched_at=datetime.fromisoformat(str(quote_payload["fetched_at"])),
                qty_increment=optional_decimal("qty_increment"),
                min_qty=optional_decimal("min_qty"),
                min_notional=optional_decimal("min_notional"),
            )
            recomputed_would_order = build_would_order_evidence(
                recomputed_signal, persisted_quote
            )
            recomputed_cohort_hash = self._cohort_contract(
                cohort, assignments
            ).computed_cohort_hash()
        except (ArithmeticError, KeyError, TypeError, ValueError):
            self._fail()
            raise AssertionError("unreachable")

        order = intent.request_payload.get("order")
        if not isinstance(order, dict):
            self._fail()
        venue = request.venue.value
        expected_symbol = order.get("symbol")
        expected_qty = order.get("qty")
        expected_notional = order.get("notional")
        expected_price = order.get("price")
        expected_tif = order.get("time_in_force")
        exact = (
            intent.intent_id == request.intent_id,
            intent.request_hash == canonical_sha256(intent.request_payload),
            intent.cohort_id == decision.cohort_id == snapshot.cohort_id,
            intent.run_id == decision.run_id == snapshot.run_id,
            intent.snapshot_id == decision.snapshot_id == snapshot.snapshot_id,
            intent.snapshot_hash == decision.snapshot_hash == snapshot.content_hash,
            decision.round_decision_id == snapshot.round_decision_id,
            assignment.cohort_id == cohort.cohort_id,
            decision.assignment_id == assignment.assignment_id,
            decision.symbol == signal.symbol,
            signal.cohort_id == cohort.cohort_id,
            signal.assignment_id == assignment.assignment_id,
            signal.snapshot_id == snapshot.snapshot_id,
            signal.snapshot_hash == snapshot.content_hash,
            signal.signal_hash == decision.signal_hash,
            signal.recomputed_signal_hash() == decision.signal_hash,
            signal == recomputed_signal,
            intent.venue_quote_evidence == recomputed_would_order.quote_evidence,
            intent.would_order_evidence.get("reason_code")
            == recomputed_would_order.reason_code,
            intent.would_order_evidence.get("order") == recomputed_would_order.order,
            intent.request_payload.get("order") == recomputed_would_order.order,
            intent.request_payload.get("reason_code")
            == recomputed_would_order.reason_code,
            snapshot_payload.snapshot_id == snapshot.snapshot_id,
            snapshot_payload.cohort_id == snapshot.cohort_id,
            snapshot_payload.run_id == snapshot.run_id,
            snapshot_payload.round_decision_id == snapshot.round_decision_id,
            snapshot_payload.content_hash == snapshot.content_hash,
            snapshot_payload.recomputed_content_hash() == snapshot.content_hash,
            snapshot_payload.schema_id == snapshot.schema_id,
            snapshot_payload.source == snapshot.source,
            snapshot_payload.host == snapshot.host,
            snapshot_payload.interval == snapshot.interval,
            snapshot_payload.required_lookback == snapshot.required_lookback,
            snapshot_payload.max_capture_skew_ms == snapshot.max_capture_skew_ms,
            snapshot_payload.max_ticker_age_ms == snapshot.max_ticker_age_ms,
            snapshot_payload.capture_started_at == snapshot.capture_started_at,
            snapshot_payload.capture_completed_at == snapshot.capture_completed_at,
            recomputed_cohort_hash == cohort.cohort_hash,
            experiment.experiment_id == assignment.experiment_hash,
            experiment.strategy_version == assignment.strategy_version_id,
            experiment.strategy_hash == assignment.strategy_hash,
            experiment.frozen_config_hash == assignment.config_hash,
            experiment.policy_hash == assignment.policy_hash,
            backtest.strategy_experiment_id == experiment.id,
            backtest.strategy_version == experiment.strategy_version,
            backtest.market == "spot",
            backtest.timeframe == "1m",
            backtest.trial_status == "completed",
            request.experiment_id == assignment.experiment_id,
            request.run_id == intent.run_id,
            request.cohort_id == cohort.cohort_id,
            request.strategy_version_id == assignment.strategy_version_id,
            request.strategy_hash == assignment.strategy_hash,
            request.config_hash == assignment.config_hash,
            request.policy_hash == assignment.policy_hash,
            venue == intent.venue,
            request.product == ("spot" if venue == "binance" else "crypto"),
            request.account_mode == ("demo" if venue == "binance" else "paper"),
            request.symbol == expected_symbol,
            request.side == order.get("side"),
            request.order_type == order.get("order_type"),
            request.time_in_force == expected_tif,
            request.qty
            == (None if expected_qty is None else Decimal(str(expected_qty))),
            request.notional
            == (None if expected_notional is None else Decimal(str(expected_notional))),
            request.price
            == (None if expected_price is None else Decimal(str(expected_price))),
            request.market_snapshot_id == snapshot.snapshot_id,
            request.market_snapshot_hash == snapshot.content_hash,
            request.market_snapshot_as_of == snapshot.capture_completed_at,
            request.market_snapshot_source == snapshot.source,
            decision.signal_hash == intent.request_payload.get("signal_hash"),
        )
        if not all(exact):
            self._fail()

        identity = ValidationIdentity(
            validation_id=assignment.validation_id,
            validation_version=assignment.validation_version,
            experiment_id=assignment.experiment_id,
            strategy_version_id=assignment.strategy_version_id,
            cohort_id=cohort.cohort_id,
            experiment_hash=assignment.experiment_hash,
            cohort_hash=cohort.cohort_hash,
            strategy_hash=assignment.strategy_hash,
            config_hash=assignment.config_hash,
            policy_hash=assignment.policy_hash,
            input_hash=assignment.input_hash,
        )
        if authorize_submission:
            authorization = await self._validation.authorize_order_submission(
                self._caller_id, identity
            )
            if authorization.state is not ValidationState.PAPER_ACTIVE:
                raise PaperCohortError("paper_active_state_required")
        reference_price = Decimal(signal.reference_price)
        if not reference_price.is_finite() or reference_price <= 0:
            self._fail()
        return VerifiedExperimentProvenance(
            **request.model_dump(),
            decision_id=decision.decision_id,
            reference_price=reference_price,
            source_buy_client_order_id=None,
        )


class CohortFrozenInputHashProvider:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_stamp(self, identity: ValidationIdentity) -> FrozenInputStamp:
        assignment = await self._session.scalar(
            select(PaperValidationCohortAssignment).where(
                PaperValidationCohortAssignment.validation_id == identity.validation_id,
                PaperValidationCohortAssignment.cohort_id == identity.cohort_id,
            )
        )
        if assignment is None or assignment.input_hash != identity.input_hash:
            raise PaperCohortError("validation_identity_mismatch")
        return FrozenInputStamp(
            bundle_id=f"cohort:{identity.cohort_id}:{assignment.assignment_id}",
            content_hash=assignment.input_hash,
            verified=True,
        )


class CohortPolicyHashProvider:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_stamp(self, identity: ValidationIdentity) -> PolicyStamp:
        assignment = await self._session.scalar(
            select(PaperValidationCohortAssignment).where(
                PaperValidationCohortAssignment.validation_id == identity.validation_id,
                PaperValidationCohortAssignment.cohort_id == identity.cohort_id,
            )
        )
        if assignment is None or assignment.policy_hash != identity.policy_hash:
            raise PaperCohortError("validation_identity_mismatch")
        return PolicyStamp(
            version=assignment.strategy_version_id,
            content_hash=assignment.policy_hash,
            verified=True,
        )


__all__ = [
    "CohortFrozenInputHashProvider",
    "CohortPolicyHashProvider",
    "PaperCohortProvenanceVerifier",
]
