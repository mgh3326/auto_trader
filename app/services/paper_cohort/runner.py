"""Deterministic canonical-snapshot cohort orchestration."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from decimal import Decimal
from typing import Protocol

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_cohort import (
    CanonicalMarketSnapshot,
    PaperCohortDecision,
    PaperCohortVenueIntent,
    PaperRunOrderLink,
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.composition import build_paper_execution_application
from app.services.brokers.paper.contracts import (
    ExperimentProvenanceVerifier,
    PaperOperationResult,
    PaperOperationStatus,
    PaperOrderRequest,
)
from app.services.paper_cohort.contracts import PaperCohortError, RunMode
from app.services.paper_cohort.market_snapshot import (
    CanonicalSnapshotPayload,
    SnapshotCaptureRequest,
)
from app.services.paper_cohort.native_links import (
    NativeOrderIdentity,
    NativeOrderResolver,
)
from app.services.paper_cohort.signals import (
    CanonicalTargetSignal,
    SignalComputationInput,
    VenueQuote,
    build_would_order_evidence,
    compute_target_signal,
)
from app.services.research_canonical_hash import canonical_sha256


class SnapshotCapture(Protocol):
    async def capture(
        self, request: SnapshotCaptureRequest
    ) -> CanonicalSnapshotPayload: ...


class VenueQuoteProvider(Protocol):
    async def get_quote(self, venue: str, symbol: str) -> VenueQuote: ...


class PaperApplication(Protocol):
    async def submit(self, request: PaperOrderRequest) -> PaperOperationResult: ...


class NativeResolver(Protocol):
    async def resolve(
        self, venue: str, client_order_id: str, broker_order_id: str
    ) -> NativeOrderIdentity: ...


class CohortRunInvocation(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cohort_id: str
    run_id: str
    round_decision_id: str
    mode: RunMode


class CohortRunResult(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")

    cohort_id: str
    run_id: str
    round_decision_id: str
    snapshot_id: str
    snapshot_hash: str
    decision_count: int
    intent_count: int


def _identity(prefix: str, payload: dict[str, object]) -> str:
    return f"{prefix}-{canonical_sha256(payload)[:40]}"


class PaperCohortRunner:
    def __init__(
        self,
        session: AsyncSession,
        *,
        capture: SnapshotCapture,
        quote_provider: VenueQuoteProvider,
        verifier: ExperimentProvenanceVerifier | None = None,
        application_factory: (
            Callable[[ExperimentProvenanceVerifier], PaperApplication] | None
        ) = None,
        native_resolver: NativeResolver | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self._session = session
        self._capture = capture
        self._quote_provider = quote_provider
        self._verifier = verifier
        self._application_factory = application_factory
        self._native_resolver = native_resolver or NativeOrderResolver(session)
        self._clock = clock or (lambda: datetime.now(UTC))

    async def _cohort(
        self, cohort_id: str
    ) -> tuple[PaperValidationCohort, list[PaperValidationCohortAssignment]]:
        cohort = await self._session.scalar(
            select(PaperValidationCohort).where(
                PaperValidationCohort.cohort_id == cohort_id
            )
        )
        if cohort is None:
            raise PaperCohortError("cohort_not_found")
        assignments = list(
            (
                await self._session.scalars(
                    select(PaperValidationCohortAssignment)
                    .where(PaperValidationCohortAssignment.cohort_id == cohort_id)
                    .order_by(PaperValidationCohortAssignment.ordinal)
                )
            ).all()
        )
        if not assignments:
            raise PaperCohortError("cohort_not_found")
        return cohort, assignments

    @staticmethod
    def _signal_input(
        cohort: PaperValidationCohort,
        assignment: PaperValidationCohortAssignment,
        symbol: str,
    ) -> SignalComputationInput:
        return SignalComputationInput(
            cohort_id=cohort.cohort_id,
            assignment_id=assignment.assignment_id,
            experiment_id=assignment.experiment_id,
            strategy_version_id=assignment.strategy_version_id,
            strategy_hash=assignment.strategy_hash,
            config_hash=assignment.config_hash,
            policy_hash=assignment.policy_hash,
            symbol=symbol,
            target_weight=assignment.target_weights[symbol],
            capital_notional_usd=cohort.capital_notional_usd,
        )

    async def run(self, invocation: CohortRunInvocation) -> CohortRunResult:
        if invocation.mode is RunMode.PAPER_ACTIVE and self._verifier is None:
            raise PaperCohortError("provenance_verifier_unavailable")
        cohort, assignments = await self._cohort(invocation.cohort_id)
        if cohort.stop_at is not None and self._clock() >= cohort.stop_at:
            raise PaperCohortError("cohort_stopped")
        snapshot_id = _identity(
            "snapshot",
            {
                "cohort_id": invocation.cohort_id,
                "run_id": invocation.run_id,
                "round_decision_id": invocation.round_decision_id,
            },
        )
        snapshot = await self._capture.capture(
            SnapshotCaptureRequest(
                snapshot_id=snapshot_id,
                cohort_id=invocation.cohort_id,
                run_id=invocation.run_id,
                round_decision_id=invocation.round_decision_id,
                required_lookback=cohort.required_lookback,
                max_capture_skew_ms=cohort.max_capture_skew_ms,
                max_ticker_age_ms=cohort.max_ticker_age_ms,
            )
        )
        self._session.add(
            CanonicalMarketSnapshot(
                snapshot_id=snapshot.snapshot_id,
                cohort_id=snapshot.cohort_id,
                run_id=snapshot.run_id,
                round_decision_id=snapshot.round_decision_id,
                schema_id=snapshot.schema_id,
                source=snapshot.source,
                host=snapshot.host,
                interval=snapshot.interval,
                required_lookback=snapshot.required_lookback,
                max_capture_skew_ms=snapshot.max_capture_skew_ms,
                max_ticker_age_ms=snapshot.max_ticker_age_ms,
                capture_started_at=snapshot.capture_started_at,
                capture_completed_at=snapshot.capture_completed_at,
                payload=snapshot.model_dump(mode="json"),
                content_hash=snapshot.content_hash,
            )
        )
        await self._session.flush()

        signals: list[tuple[str, CanonicalTargetSignal]] = []
        for assignment in assignments:
            for symbol in cohort.symbols:
                signal = compute_target_signal(
                    snapshot,
                    self._signal_input(cohort, assignment, symbol),
                )
                decision_id = _identity(
                    "decision",
                    {
                        "cohort_id": invocation.cohort_id,
                        "run_id": invocation.run_id,
                        "round_decision_id": invocation.round_decision_id,
                        "assignment_id": assignment.assignment_id,
                        "symbol": symbol,
                        "snapshot_hash": snapshot.content_hash,
                    },
                )
                self._session.add(
                    PaperCohortDecision(
                        decision_id=decision_id,
                        cohort_id=invocation.cohort_id,
                        run_id=invocation.run_id,
                        round_decision_id=invocation.round_decision_id,
                        assignment_id=assignment.assignment_id,
                        symbol=symbol,
                        snapshot_id=snapshot.snapshot_id,
                        snapshot_hash=snapshot.content_hash,
                        mode=invocation.mode.value,
                        signal_payload=signal.model_dump(mode="json"),
                        signal_hash=signal.signal_hash,
                    )
                )
                signals.append((decision_id, signal))
        await self._session.flush()

        intent_count = 0
        active_intents: list[
            tuple[PaperCohortVenueIntent, CanonicalTargetSignal, dict[str, object]]
        ] = []
        for decision_id, signal in signals:
            for venue in cohort.venues:
                quote = await self._quote_provider.get_quote(venue, signal.symbol)
                would_order = build_would_order_evidence(signal, quote)
                idempotency_key = canonical_sha256(
                    {
                        "decision_id": decision_id,
                        "venue": venue,
                        "signal_hash": signal.signal_hash,
                    }
                )
                evidence = would_order.model_dump(mode="json")
                evidence["idempotency_key"] = idempotency_key
                request_payload: dict[str, object] = {
                    "signal_hash": signal.signal_hash,
                    "order": evidence["order"],
                    "reason_code": evidence["reason_code"],
                }
                intent = PaperCohortVenueIntent(
                    intent_id=_identity(
                        "intent",
                        {"decision_id": decision_id, "venue": venue},
                    ),
                    cohort_id=invocation.cohort_id,
                    run_id=invocation.run_id,
                    decision_id=decision_id,
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_hash=snapshot.content_hash,
                    venue=venue,
                    request_payload=request_payload,
                    request_hash=canonical_sha256(request_payload),
                    venue_quote_evidence=would_order.quote_evidence,
                    would_order_evidence=evidence,
                )
                self._session.add(intent)
                active_intents.append((intent, signal, evidence))
                intent_count += 1
        await self._session.flush()
        if invocation.mode is RunMode.PAPER_ACTIVE:
            assert self._verifier is not None
            application = (
                build_paper_execution_application(verifier=self._verifier)
                if self._application_factory is None
                else self._application_factory(self._verifier)
            )
            for intent, signal, evidence in active_intents:
                order = evidence.get("order")
                if not isinstance(order, dict):
                    raise PaperCohortError("unsupported_capability")
                venue = Broker(intent.venue)
                request = PaperOrderRequest(
                    intent_id=intent.intent_id,
                    experiment_id=signal.experiment_id,
                    run_id=intent.run_id,
                    cohort_id=intent.cohort_id,
                    strategy_version_id=signal.strategy_version_id,
                    strategy_hash=signal.strategy_hash,
                    config_hash=signal.config_hash,
                    policy_hash=signal.policy_hash,
                    venue=venue,
                    account_mode="demo" if venue is Broker.BINANCE else "paper",
                    product="spot" if venue is Broker.BINANCE else "crypto",
                    symbol=str(order["symbol"]),
                    side=str(order["side"]),  # type: ignore[arg-type]
                    order_type=str(order["order_type"]),  # type: ignore[arg-type]
                    time_in_force=(
                        None
                        if order.get("time_in_force") is None
                        else str(order["time_in_force"])
                    ),
                    qty=(
                        None if order.get("qty") is None else Decimal(str(order["qty"]))
                    ),
                    notional=(
                        None
                        if order.get("notional") is None
                        else Decimal(str(order["notional"]))
                    ),
                    price=(
                        None
                        if order.get("price") is None
                        else Decimal(str(order["price"]))
                    ),
                    market_snapshot_id=snapshot.snapshot_id,
                    market_snapshot_hash=snapshot.content_hash,
                    market_snapshot_as_of=snapshot.capture_completed_at,
                    market_snapshot_source=snapshot.source,
                    source_buy_reference=None,
                )
                result = await application.submit(request)
                if (
                    result.status is not PaperOperationStatus.SUCCEEDED
                    or result.native_client_order_id is None
                    or result.native_order_id is None
                ):
                    raise PaperCohortError(str(result.reason_code))
                native = await self._native_resolver.resolve(
                    intent.venue,
                    result.native_client_order_id,
                    result.native_order_id,
                )
                self._session.add(
                    PaperRunOrderLink(
                        cohort_id=intent.cohort_id,
                        run_id=intent.run_id,
                        decision_id=intent.decision_id,
                        snapshot_id=intent.snapshot_id,
                        snapshot_hash=intent.snapshot_hash,
                        venue=intent.venue,
                        native_ledger_kind=native.ledger_kind,
                        native_ledger_row_id=native.ledger_row_id,
                        client_order_id=native.client_order_id,
                        broker_order_id=native.broker_order_id,
                    )
                )
            await self._session.flush()
        return CohortRunResult(
            cohort_id=invocation.cohort_id,
            run_id=invocation.run_id,
            round_decision_id=invocation.round_decision_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.content_hash,
            decision_count=len(signals),
            intent_count=intent_count,
        )


__all__ = ["CohortRunInvocation", "CohortRunResult", "PaperCohortRunner"]
