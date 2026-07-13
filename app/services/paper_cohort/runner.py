"""Deterministic canonical-snapshot cohort orchestration."""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Protocol
from uuid import uuid4

from pydantic import BaseModel, ConfigDict
from sqlalchemy import select, text, update
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.paper_cohort import (
    CanonicalMarketSnapshot,
    PaperCohortDecision,
    PaperCohortRunClaim,
    PaperCohortVenueIntent,
    PaperRunOrderLink,
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)
from app.models.paper_validation import PaperValidationStateTransition
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
        after_submit_hook: (
            Callable[[PaperOperationResult], Awaitable[None]] | None
        ) = None,
        enablement: Callable[[RunMode], bool] | None = None,
    ) -> None:
        self._session = session
        self._capture = capture
        self._quote_provider = quote_provider
        self._verifier = verifier
        self._application_factory = application_factory
        self._native_resolver = native_resolver or NativeOrderResolver(session)
        self._clock = clock or (lambda: datetime.now(UTC))
        self._after_submit_hook = after_submit_hook
        self._enablement = enablement or self._settings_enablement

    @staticmethod
    def _settings_enablement(mode: RunMode) -> bool:
        return settings.PAPER_COHORT_ENABLED and (
            mode is RunMode.SHADOW or settings.PAPER_EXECUTION_ENABLED
        )

    async def _claim(
        self, invocation: CohortRunInvocation
    ) -> tuple[PaperCohortRunClaim | None, CohortRunResult | None]:
        request_hash = canonical_sha256(invocation.model_dump(mode="python"))
        now = self._clock()
        owner_token = uuid4().hex
        inserted_id = await self._session.scalar(
            pg_insert(PaperCohortRunClaim)
            .values(
                cohort_id=invocation.cohort_id,
                run_id=invocation.run_id,
                round_decision_id=invocation.round_decision_id,
                request_hash=request_hash,
                owner_token=owner_token,
                lease_expires_at=now + timedelta(minutes=5),
            )
            .on_conflict_do_nothing(
                index_elements=["cohort_id", "run_id", "round_decision_id"]
            )
            .returning(PaperCohortRunClaim.id)
        )
        if inserted_id is not None:
            return await self._session.get(PaperCohortRunClaim, inserted_id), None

        existing = await self._session.scalar(
            select(PaperCohortRunClaim).where(
                PaperCohortRunClaim.cohort_id == invocation.cohort_id,
                PaperCohortRunClaim.run_id == invocation.run_id,
                PaperCohortRunClaim.round_decision_id == invocation.round_decision_id,
            )
        )
        if existing is None:
            raise PaperCohortError("invocation_claim_unavailable")
        if existing.request_hash != request_hash:
            raise PaperCohortError("invocation_conflict")
        if existing.completed_at is not None and existing.result_payload is not None:
            return None, CohortRunResult.model_validate(existing.result_payload)
        if existing.lease_expires_at > now:
            # An INSERT .. ON CONFLICT contender waits for the owner transaction.
            # Once the owner has durably prepared a paper run, briefly poll for its
            # final result so concurrent scheduler deliveries return the same result.
            existing_id = existing.id
            await self._session.rollback()
            for _ in range(100):
                await asyncio.sleep(0.05)
                observed = await self._session.scalar(
                    select(PaperCohortRunClaim)
                    .where(PaperCohortRunClaim.id == existing_id)
                    .execution_options(populate_existing=True)
                )
                if (
                    observed is not None
                    and observed.completed_at is not None
                    and observed.result_payload is not None
                ):
                    return None, CohortRunResult.model_validate(observed.result_payload)
            raise PaperCohortError("invocation_in_progress")

        takeover_id = await self._session.scalar(
            update(PaperCohortRunClaim)
            .where(
                PaperCohortRunClaim.id == existing.id,
                PaperCohortRunClaim.request_hash == request_hash,
                PaperCohortRunClaim.completed_at.is_(None),
                PaperCohortRunClaim.lease_expires_at <= now,
            )
            .values(
                owner_token=owner_token,
                lease_expires_at=now + timedelta(minutes=5),
            )
            .returning(PaperCohortRunClaim.id)
        )
        if takeover_id is None:
            raise PaperCohortError("invocation_in_progress")
        return await self._session.get(PaperCohortRunClaim, takeover_id), None

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

    async def _validate_authoritative_state(
        self,
        invocation: CohortRunInvocation,
        cohort: PaperValidationCohort,
        assignments: list[PaperValidationCohortAssignment],
    ) -> None:
        expected_state = (
            "paper_active" if invocation.mode is RunMode.PAPER_ACTIVE else "shadow_soak"
        )
        for validation_id in sorted(item.validation_id for item in assignments):
            await self._session.execute(
                text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
                {"key": validation_id},
            )
        for assignment in assignments:
            latest = await self._session.scalar(
                select(PaperValidationStateTransition)
                .where(
                    PaperValidationStateTransition.validation_id
                    == assignment.validation_id
                )
                .order_by(PaperValidationStateTransition.sequence.desc())
                .limit(1)
            )
            exact = latest is not None and all(
                (
                    latest.new_state == expected_state,
                    latest.validation_version == assignment.validation_version,
                    latest.experiment_id == assignment.experiment_id,
                    latest.strategy_version_id == assignment.strategy_version_id,
                    latest.cohort_id == cohort.cohort_id,
                    latest.experiment_hash == assignment.experiment_hash,
                    latest.cohort_hash == cohort.cohort_hash,
                    latest.strategy_hash == assignment.strategy_hash,
                    latest.config_hash == assignment.config_hash,
                    latest.policy_hash == assignment.policy_hash,
                    latest.input_hash == assignment.input_hash,
                )
            )
            if not exact:
                raise PaperCohortError("authoritative_state_mismatch")

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

    async def _load_prepared(
        self, invocation: CohortRunInvocation
    ) -> (
        tuple[
            CanonicalSnapshotPayload,
            list[PaperCohortDecision],
            list[
                tuple[PaperCohortVenueIntent, CanonicalTargetSignal, dict[str, object]]
            ],
        ]
        | None
    ):
        row = await self._session.scalar(
            select(CanonicalMarketSnapshot).where(
                CanonicalMarketSnapshot.cohort_id == invocation.cohort_id,
                CanonicalMarketSnapshot.run_id == invocation.run_id,
                CanonicalMarketSnapshot.round_decision_id
                == invocation.round_decision_id,
            )
        )
        if row is None:
            return None
        snapshot = CanonicalSnapshotPayload.model_validate(row.payload)
        decisions = list(
            (
                await self._session.scalars(
                    select(PaperCohortDecision)
                    .where(
                        PaperCohortDecision.cohort_id == invocation.cohort_id,
                        PaperCohortDecision.run_id == invocation.run_id,
                        PaperCohortDecision.round_decision_id
                        == invocation.round_decision_id,
                    )
                    .order_by(PaperCohortDecision.decision_id)
                )
            ).all()
        )
        by_id = {item.decision_id: item for item in decisions}
        intents = list(
            (
                await self._session.scalars(
                    select(PaperCohortVenueIntent)
                    .where(
                        PaperCohortVenueIntent.cohort_id == invocation.cohort_id,
                        PaperCohortVenueIntent.run_id == invocation.run_id,
                    )
                    .order_by(
                        PaperCohortVenueIntent.decision_id,
                        PaperCohortVenueIntent.venue,
                    )
                )
            ).all()
        )
        active: list[
            tuple[PaperCohortVenueIntent, CanonicalTargetSignal, dict[str, object]]
        ] = []
        for intent in intents:
            decision = by_id.get(intent.decision_id)
            if decision is None:
                raise PaperCohortError("prepared_identity_mismatch")
            signal = CanonicalTargetSignal.model_validate(decision.signal_payload)
            active.append((intent, signal, intent.would_order_evidence))
        if not decisions or not intents:
            raise PaperCohortError("prepared_identity_mismatch")
        return snapshot, decisions, active

    async def _prepare(
        self,
        invocation: CohortRunInvocation,
        cohort: PaperValidationCohort,
        assignments: list[PaperValidationCohortAssignment],
    ) -> tuple[
        CanonicalSnapshotPayload,
        list[PaperCohortDecision],
        list[tuple[PaperCohortVenueIntent, CanonicalTargetSignal, dict[str, object]]],
    ]:
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

        decisions: list[PaperCohortDecision] = []
        signals: list[tuple[str, CanonicalTargetSignal]] = []
        for assignment in assignments:
            for symbol in cohort.symbols:
                signal = compute_target_signal(
                    snapshot, self._signal_input(cohort, assignment, symbol)
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
                decision = PaperCohortDecision(
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
                self._session.add(decision)
                decisions.append(decision)
                signals.append((decision_id, signal))
        await self._session.flush()

        active: list[
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
                        "intent", {"decision_id": decision_id, "venue": venue}
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
                active.append((intent, signal, evidence))
        await self._session.flush()
        return snapshot, decisions, active

    @staticmethod
    def build_request(
        intent: PaperCohortVenueIntent,
        signal: CanonicalTargetSignal,
        evidence: dict[str, object],
        snapshot: CanonicalSnapshotPayload,
    ) -> PaperOrderRequest:
        order = evidence.get("order")
        if not isinstance(order, dict):
            raise PaperCohortError("unsupported_capability")
        venue = Broker(intent.venue)
        return PaperOrderRequest(
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
            qty=None if order.get("qty") is None else Decimal(str(order["qty"])),
            notional=(
                None
                if order.get("notional") is None
                else Decimal(str(order["notional"]))
            ),
            price=(
                None if order.get("price") is None else Decimal(str(order["price"]))
            ),
            market_snapshot_id=snapshot.snapshot_id,
            market_snapshot_hash=snapshot.content_hash,
            market_snapshot_as_of=snapshot.capture_completed_at,
            market_snapshot_source=snapshot.source,
            source_buy_reference=None,
        )

    async def run(self, invocation: CohortRunInvocation) -> CohortRunResult:
        if not self._enablement(invocation.mode):
            raise PaperCohortError("paper_cohort_disabled")
        if invocation.mode is RunMode.PAPER_ACTIVE and self._verifier is None:
            raise PaperCohortError("provenance_verifier_unavailable")
        claim, replay = await self._claim(invocation)
        if replay is not None:
            return replay
        if claim is None:
            raise PaperCohortError("invocation_claim_unavailable")
        cohort, assignments = await self._cohort(invocation.cohort_id)
        if cohort.stop_at is not None and self._clock() >= cohort.stop_at:
            raise PaperCohortError("cohort_stopped")
        await self._validate_authoritative_state(invocation, cohort, assignments)
        prepared = await self._load_prepared(invocation)
        if prepared is None:
            prepared = await self._prepare(invocation, cohort, assignments)
        snapshot, decisions, active_intents = prepared

        completed = CohortRunResult(
            cohort_id=invocation.cohort_id,
            run_id=invocation.run_id,
            round_decision_id=invocation.round_decision_id,
            snapshot_id=snapshot.snapshot_id,
            snapshot_hash=snapshot.content_hash,
            decision_count=len(decisions),
            intent_count=len(active_intents),
        )
        if invocation.mode is RunMode.SHADOW:
            claim.result_payload = completed.model_dump(mode="json")
            claim.completed_at = self._clock()
            await self._session.commit()
            return completed

        # This is the exactly-once boundary: snapshot, decision, quote evidence,
        # intent, and claim are durable before the first broker mutation. Recovery
        # only replays these immutable requests and never captures again.
        await self._session.commit()
        if invocation.mode is RunMode.PAPER_ACTIVE:
            assert self._verifier is not None
            cohort, assignments = await self._cohort(invocation.cohort_id)
            await self._validate_authoritative_state(invocation, cohort, assignments)
            requests = [
                self.build_request(intent, signal, evidence, snapshot)
                for intent, signal, evidence in active_intents
            ]
            # All cohort members and persisted provenance are authorized before
            # the first adapter call. Advisory locks remain held through the
            # final commit, so an abort/hash transition cannot interleave.
            for request in requests:
                await self._verifier.verify(request)
            application = (
                build_paper_execution_application(verifier=self._verifier)
                if self._application_factory is None
                else self._application_factory(self._verifier)
            )
            for (intent, _signal, _evidence), request in zip(
                active_intents, requests, strict=True
            ):
                existing_link = await self._session.scalar(
                    select(PaperRunOrderLink).where(
                        PaperRunOrderLink.cohort_id == intent.cohort_id,
                        PaperRunOrderLink.run_id == intent.run_id,
                        PaperRunOrderLink.decision_id == intent.decision_id,
                        PaperRunOrderLink.venue == intent.venue,
                    )
                )
                if existing_link is not None:
                    continue
                result = await application.submit(request)
                if (
                    result.status is not PaperOperationStatus.SUCCEEDED
                    or result.native_client_order_id is None
                    or result.native_order_id is None
                ):
                    raise PaperCohortError(str(result.reason_code))
                if self._after_submit_hook is not None:
                    await self._after_submit_hook(result)
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
        durable_claim = await self._session.get(PaperCohortRunClaim, claim.id)
        if durable_claim is None:
            raise PaperCohortError("invocation_claim_unavailable")
        durable_claim.result_payload = completed.model_dump(mode="json")
        durable_claim.completed_at = self._clock()
        await self._session.commit()
        return completed


__all__ = ["CohortRunInvocation", "CohortRunResult", "PaperCohortRunner"]
