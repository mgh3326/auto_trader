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
from app.services.brokers.paper.contracts import (
    PaperOrderRequest,
    VerifiedExperimentProvenance,
)
from app.services.paper_cohort.contracts import PaperCohortError
from app.services.paper_validation.contracts import (
    FrozenInputStamp,
    PaperOrderAuthorization,
    PolicyStamp,
    ValidationIdentity,
    ValidationState,
)


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

    async def verify(self, request: PaperOrderRequest) -> VerifiedExperimentProvenance:
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
        if cohort.stop_at is not None and self._clock() >= cohort.stop_at:
            raise PaperCohortError("cohort_stopped")
        if decision.mode != "paper_active":
            self._fail()

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
            intent.snapshot_hash == snapshot.content_hash,
            decision.snapshot_hash == snapshot.content_hash,
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
        authorization = await self._validation.authorize_order_submission(
            self._caller_id, identity
        )
        if authorization.state is not ValidationState.PAPER_ACTIVE:
            raise PaperCohortError("paper_active_state_required")
        reference_price = Decimal(str(decision.signal_payload.get("reference_price")))
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
