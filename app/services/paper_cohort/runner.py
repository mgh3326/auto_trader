"""Deterministic canonical-snapshot cohort orchestration."""

from __future__ import annotations

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
    PaperCohortTargetReservation,
    PaperCohortTerminalFence,
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
from app.services.paper_validation.locking import lock_validation_streams
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

    async def resolve_prepared(
        self,
        request: PaperOrderRequest,
        provenance: object,
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
        # The injected predicate is a test/embedding seam that may only narrow
        # the server-owned feature gates.  It must never turn a disabled paper
        # execution surface on.
        self._enablement = enablement or (lambda _mode: True)

    @staticmethod
    def _settings_enablement(mode: RunMode) -> bool:
        return settings.PAPER_COHORT_ENABLED and (
            mode is RunMode.SHADOW or settings.PAPER_EXECUTION_ENABLED
        )

    async def _claim(
        self,
        invocation: CohortRunInvocation,
        *,
        allow_reconciliation_retry: bool = False,
        allow_live_terminal_takeover: bool = False,
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
            select(PaperCohortRunClaim)
            .where(
                PaperCohortRunClaim.cohort_id == invocation.cohort_id,
                PaperCohortRunClaim.run_id == invocation.run_id,
                PaperCohortRunClaim.round_decision_id == invocation.round_decision_id,
            )
            .with_for_update()
        )
        if existing is None:
            raise PaperCohortError("invocation_claim_unavailable")
        if existing.request_hash != request_hash:
            raise PaperCohortError("invocation_conflict")
        if existing.claim_status == "completed":
            if existing.result_payload is None:
                raise PaperCohortError("invocation_claim_unavailable")
            return None, CohortRunResult.model_validate(existing.result_payload)
        if existing.claim_status == "blocked":
            raise PaperCohortError(
                existing.terminal_reason or "invocation_claim_unavailable"
            )
        resuming_reconciliation = existing.claim_status == "reconciliation_required"
        if resuming_reconciliation:
            if not allow_reconciliation_retry:
                raise PaperCohortError(
                    existing.terminal_reason or "invocation_claim_unavailable"
                )
            existing.claim_status = "in_progress"
            existing.terminal_reason = None
            existing.terminal_at = None
        if (
            existing.lease_expires_at > now
            and not resuming_reconciliation
            and not allow_live_terminal_takeover
        ):
            raise PaperCohortError("invocation_in_progress")
        existing.owner_token = owner_token
        existing.lease_expires_at = now + timedelta(minutes=5)
        return existing, None

    async def _lock_owned_claim(
        self, claim_id: int, owner_token: str
    ) -> tuple[PaperCohortRunClaim | None, CohortRunResult | None]:
        claim = await self._session.scalar(
            select(PaperCohortRunClaim)
            .where(PaperCohortRunClaim.id == claim_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if claim is None:
            raise PaperCohortError("invocation_claim_unavailable")
        if claim.claim_status == "completed":
            if claim.result_payload is None:
                raise PaperCohortError("invocation_claim_unavailable")
            return None, CohortRunResult.model_validate(claim.result_payload)
        if claim.claim_status in {"blocked", "reconciliation_required"}:
            raise PaperCohortError(
                claim.terminal_reason or "invocation_claim_unavailable"
            )
        if claim.owner_token != owner_token:
            raise PaperCohortError("invocation_owner_mismatch")
        claim.lease_expires_at = self._clock() + timedelta(minutes=5)
        await self._session.flush()
        return claim, None

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

    async def _lock_execution_boundary(
        self,
        cohort_id: str,
        assignments: list[PaperValidationCohortAssignment],
    ) -> None:
        # Total order: runner-owned claim row -> sorted/deduplicated validation
        # locks -> cohort lock. Activation and kill use the validation/cohort
        # suffix and never request a claim row, so no reverse edge exists.
        await lock_validation_streams(
            self._session, (item.validation_id for item in assignments)
        )
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:key, 0))"),
            {"key": f"paper-cohort:{cohort_id}"},
        )

    async def _has_terminal_fence(self, cohort_id: str) -> bool:
        return (
            await self._session.scalar(
                select(PaperCohortTerminalFence.id).where(
                    PaperCohortTerminalFence.cohort_id == cohort_id
                )
            )
            is not None
        )

    async def _precheck_submission_boundary(
        self, cohort: PaperValidationCohort
    ) -> None:
        now = self._clock()
        if now < cohort.activated_at:
            raise PaperCohortError("cohort_not_active")
        if cohort.stop_at is not None and now >= cohort.stop_at:
            raise PaperCohortError("cohort_stopped")
        if await self._has_terminal_fence(cohort.cohort_id):
            raise PaperCohortError("cohort_stopped")

    async def _authoritative_state_matches(
        self,
        invocation: CohortRunInvocation,
        cohort: PaperValidationCohort,
        assignments: list[PaperValidationCohortAssignment],
    ) -> bool:
        expected_state = (
            "paper_active" if invocation.mode is RunMode.PAPER_ACTIVE else "shadow_soak"
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
                return False
        return True

    async def _checkpoint_boundary(
        self,
        invocation: CohortRunInvocation,
        cohort: PaperValidationCohort,
        assignments: list[PaperValidationCohortAssignment],
        *,
        recovery_only: bool,
    ) -> bool:
        """Lock and revalidate every durable boundary before native work.

        Returns whether the invocation is behind a terminal boundary. Recovery
        may reconcile persisted native truth behind that boundary, while fresh
        submission always fails closed.
        """

        await self._lock_execution_boundary(cohort.cohort_id, assignments)
        fenced = await self._has_terminal_fence(cohort.cohort_id)
        now = self._clock()
        not_active = now < cohort.activated_at
        stopped = cohort.stop_at is not None and now >= cohort.stop_at
        state_matches = await self._authoritative_state_matches(
            invocation, cohort, assignments
        )
        if not recovery_only:
            if not_active:
                raise PaperCohortError("cohort_not_active")
            if fenced or stopped:
                raise PaperCohortError("cohort_stopped")
            if not state_matches:
                raise PaperCohortError("authoritative_state_mismatch")
        return bool(fenced or stopped or not state_matches)

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

    @staticmethod
    def _validate_venue_quote(
        quote: VenueQuote,
        *,
        observed_at: datetime,
        max_age_ms: int,
        max_future_skew_ms: int,
    ) -> None:
        if observed_at.tzinfo is None or quote.fetched_at.tzinfo is None:
            raise PaperCohortError("venue_quote_provider_error")
        age = observed_at - quote.fetched_at
        if age > timedelta(milliseconds=max_age_ms) or age < -timedelta(
            milliseconds=max_future_skew_ms
        ):
            raise PaperCohortError("venue_quote_provider_error")

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
                        PaperCohortVenueIntent.round_decision_id
                        == invocation.round_decision_id,
                    )
                    .order_by(PaperCohortVenueIntent.execution_ordinal)
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
            if not all(
                (
                    intent.round_decision_id == invocation.round_decision_id,
                    intent.assignment_id == decision.assignment_id,
                    intent.symbol == decision.symbol,
                    intent.snapshot_id == decision.snapshot_id,
                    intent.snapshot_hash == decision.snapshot_hash,
                )
            ):
                raise PaperCohortError("prepared_identity_mismatch")
            signal = CanonicalTargetSignal.model_validate(decision.signal_payload)
            active.append((intent, signal, intent.would_order_evidence))
        if not decisions or not intents:
            raise PaperCohortError("prepared_identity_mismatch")
        if [intent.execution_ordinal for intent in intents] != list(
            range(len(intents))
        ):
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
        signals: list[
            tuple[
                str,
                CanonicalTargetSignal,
                PaperValidationCohortAssignment,
            ]
        ] = []
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
                signals.append((decision_id, signal, assignment))
        await self._session.flush()

        active: list[
            tuple[PaperCohortVenueIntent, CanonicalTargetSignal, dict[str, object]]
        ] = []
        for decision_id, signal, assignment in signals:
            symbol_index = cohort.symbols.index(signal.symbol)
            for venue_index, venue in enumerate(cohort.venues):
                quote = await self._quote_provider.get_quote(venue, signal.symbol)
                self._validate_venue_quote(
                    quote,
                    observed_at=self._clock(),
                    max_age_ms=cohort.max_ticker_age_ms,
                    max_future_skew_ms=cohort.max_capture_skew_ms,
                )
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
                    round_decision_id=invocation.round_decision_id,
                    decision_id=decision_id,
                    assignment_id=assignment.assignment_id,
                    symbol=signal.symbol,
                    snapshot_id=snapshot.snapshot_id,
                    snapshot_hash=snapshot.content_hash,
                    venue=venue,
                    execution_ordinal=(
                        assignment.ordinal * 4 + symbol_index * 2 + venue_index
                    ),
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

    async def _reserve_target(self, intent: PaperCohortVenueIntent) -> bool:
        values = {
            "cohort_id": intent.cohort_id,
            "run_id": intent.run_id,
            "round_decision_id": intent.round_decision_id,
            "intent_id": intent.intent_id,
            "decision_id": intent.decision_id,
            "assignment_id": intent.assignment_id,
            "symbol": intent.symbol,
            "snapshot_id": intent.snapshot_id,
            "snapshot_hash": intent.snapshot_hash,
            "venue": intent.venue,
            "execution_ordinal": intent.execution_ordinal,
        }
        inserted_id = await self._session.scalar(
            pg_insert(PaperCohortTargetReservation)
            .values(**values)
            .on_conflict_do_nothing()
            .returning(PaperCohortTargetReservation.id)
        )
        if inserted_id is not None:
            await self._session.commit()
            return True
        reservation = await self._session.scalar(
            select(PaperCohortTargetReservation).where(
                PaperCohortTargetReservation.cohort_id == intent.cohort_id,
                PaperCohortTargetReservation.assignment_id == intent.assignment_id,
                PaperCohortTargetReservation.symbol == intent.symbol,
                PaperCohortTargetReservation.venue == intent.venue,
            )
        )
        if reservation is None:
            raise PaperCohortError("target_reservation_unavailable")
        same_origin = all(
            getattr(reservation, field) == value for field, value in values.items()
        )
        await self._session.commit()
        return same_origin

    @staticmethod
    def _link_row(
        intent: PaperCohortVenueIntent, native: NativeOrderIdentity
    ) -> PaperRunOrderLink:
        return PaperRunOrderLink(
            cohort_id=intent.cohort_id,
            run_id=intent.run_id,
            round_decision_id=intent.round_decision_id,
            intent_id=intent.intent_id,
            decision_id=intent.decision_id,
            assignment_id=intent.assignment_id,
            symbol=intent.symbol,
            snapshot_id=intent.snapshot_id,
            snapshot_hash=intent.snapshot_hash,
            venue=intent.venue,
            native_ledger_kind=native.ledger_kind,
            native_ledger_row_id=native.ledger_row_id,
            client_order_id=native.client_order_id,
            broker_order_id=native.broker_order_id,
        )

    async def _terminalize_claim(
        self,
        claim: PaperCohortRunClaim,
        *,
        status: str,
        reason: str,
    ) -> None:
        terminal_at = self._clock()
        claim.claim_status = status
        claim.terminal_reason = reason
        claim.terminal_at = terminal_at
        claim.lease_expires_at = terminal_at
        await self._session.commit()

    async def run(self, invocation: CohortRunInvocation) -> CohortRunResult:
        return await self._execute(invocation, recovery_only=False)

    async def recover(self, invocation: CohortRunInvocation) -> CohortRunResult:
        """Reconcile an immutable prepared invocation without creating intent."""

        return await self._execute(invocation, recovery_only=True)

    async def _execute(
        self,
        invocation: CohortRunInvocation,
        *,
        recovery_only: bool,
    ) -> CohortRunResult:
        if not recovery_only and (
            not self._settings_enablement(invocation.mode)
            or not self._enablement(invocation.mode)
        ):
            raise PaperCohortError("paper_cohort_disabled")
        if invocation.mode is RunMode.PAPER_ACTIVE and self._verifier is None:
            raise PaperCohortError("provenance_verifier_unavailable")

        cohort, assignments = await self._cohort(invocation.cohort_id)
        if not recovery_only:
            # Avoid creating a dangling in-progress claim for a boundary that is
            # already known to be terminal. The locked checkpoint below closes
            # the read/check race after the claim is acquired.
            await self._precheck_submission_boundary(cohort)
        allow_live_terminal_takeover = bool(
            recovery_only
            and (
                await self._has_terminal_fence(cohort.cohort_id)
                or (cohort.stop_at is not None and self._clock() >= cohort.stop_at)
            )
        )
        claim, replay = await self._claim(
            invocation,
            allow_reconciliation_retry=recovery_only,
            allow_live_terminal_takeover=allow_live_terminal_takeover,
        )
        if replay is not None:
            return replay
        if claim is None:
            raise PaperCohortError("invocation_claim_unavailable")
        claim_id = claim.id
        owner_token = claim.owner_token
        terminal_boundary = await self._checkpoint_boundary(
            invocation,
            cohort,
            assignments,
            recovery_only=recovery_only,
        )
        prepared = await self._load_prepared(invocation)
        if prepared is None:
            if recovery_only:
                raise PaperCohortError("prepared_invocation_required")
            await self._prepare(invocation, cohort, assignments)
            # The exact execution plan is durable before it is consumed. Both
            # fresh execution and retry therefore use the same persisted order.
            await self._session.commit()
            claim, replay = await self._lock_owned_claim(claim_id, owner_token)
            if replay is not None:
                return replay
            if claim is None:
                raise PaperCohortError("invocation_claim_unavailable")
            prepared = await self._load_prepared(invocation)
            if prepared is None:
                raise PaperCohortError("prepared_identity_mismatch")
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
            claim, replay = await self._lock_owned_claim(claim_id, owner_token)
            if replay is not None:
                return replay
            if claim is None:
                raise PaperCohortError("invocation_claim_unavailable")
            cohort, assignments = await self._cohort(invocation.cohort_id)
            await self._checkpoint_boundary(
                invocation,
                cohort,
                assignments,
                recovery_only=recovery_only,
            )
            claim.result_payload = completed.model_dump(mode="json")
            claim.completed_at = self._clock()
            claim.claim_status = "completed"
            claim.lease_expires_at = claim.completed_at
            await self._session.commit()
            return completed

        # Persist an expired-claim takeover and release preparation locks. Every
        # intent below reacquires the strict owner and full execution boundary.
        await self._session.commit()
        verifier = self._verifier
        if verifier is None:
            raise PaperCohortError("provenance_verifier_unavailable")
        verify_persisted = getattr(verifier, "verify_persisted", None)
        if recovery_only and verify_persisted is None:
            raise PaperCohortError("recovery_verifier_unavailable")
        application = None
        if not recovery_only:
            application = (
                build_paper_execution_application(verifier=verifier)
                if self._application_factory is None
                else self._application_factory(verifier)
            )

        unresolved_intents = 0
        requests = [
            self.build_request(intent, signal, evidence, snapshot)
            for intent, signal, evidence in active_intents
        ]
        for (intent, _signal, _evidence), request in zip(
            active_intents, requests, strict=True
        ):
            claim, replay = await self._lock_owned_claim(claim_id, owner_token)
            if replay is not None:
                return replay
            if claim is None:
                raise PaperCohortError("invocation_claim_unavailable")
            cohort, assignments = await self._cohort(invocation.cohort_id)
            terminal_boundary = (
                await self._checkpoint_boundary(
                    invocation,
                    cohort,
                    assignments,
                    recovery_only=recovery_only,
                )
                or terminal_boundary
            )
            provenance = (
                await verify_persisted(request)  # type: ignore[misc]
                if recovery_only
                else await verifier.verify(request)
            )
            existing_link = await self._session.scalar(
                select(PaperRunOrderLink).where(
                    PaperRunOrderLink.intent_id == intent.intent_id
                )
            )
            if existing_link is not None:
                native = await self._native_resolver.resolve(
                    existing_link.venue,
                    existing_link.client_order_id,
                    existing_link.broker_order_id,
                )
                if not all(
                    (
                        native.ledger_kind == existing_link.native_ledger_kind,
                        native.ledger_row_id == existing_link.native_ledger_row_id,
                        native.client_order_id == existing_link.client_order_id,
                        native.broker_order_id == existing_link.broker_order_id,
                    )
                ):
                    raise PaperCohortError("native_order_identity_mismatch")
                await self._session.commit()
                continue

            if recovery_only:
                try:
                    native = await self._native_resolver.resolve_prepared(
                        request, provenance
                    )
                except PaperCohortError as exc:
                    if exc.reason_code != "native_order_not_found":
                        raise
                    native = None
                if native is None:
                    unresolved_intents += 1
                    await self._session.commit()
                    continue
                self._session.add(self._link_row(intent, native))
                await self._session.flush()
                await self._session.commit()
                continue

            # The unique target allocation is committed before the POST. A
            # later round observes the reservation and performs no mutation.
            if not await self._reserve_target(intent):
                continue

            # Reservation commit released every transaction lock. Reacquire the
            # strict owner, state/fence locks, and fresh provenance immediately
            # before resolving or submitting the native request.
            claim, replay = await self._lock_owned_claim(claim_id, owner_token)
            if replay is not None:
                return replay
            if claim is None:
                raise PaperCohortError("invocation_claim_unavailable")
            cohort, assignments = await self._cohort(invocation.cohort_id)
            await self._checkpoint_boundary(
                invocation,
                cohort,
                assignments,
                recovery_only=False,
            )
            provenance = await verifier.verify(request)
            try:
                native = await self._native_resolver.resolve_prepared(
                    request, provenance
                )
            except PaperCohortError as exc:
                if exc.reason_code != "native_order_not_found":
                    raise
                native = None

            result = None
            if native is None:
                if application is None:
                    raise PaperCohortError("paper_application_unavailable")
                result = await application.submit(request)
                if result.status is PaperOperationStatus.BLOCKED:
                    reason = str(result.reason_code)
                    await self._terminalize_claim(
                        claim, status="blocked", reason=reason
                    )
                    raise PaperCohortError(reason)
                if result.status is not PaperOperationStatus.SUCCEEDED:
                    raise PaperCohortError(str(result.reason_code))
                if (
                    result.native_client_order_id is None
                    or result.native_order_id is None
                ):
                    raise PaperCohortError("native_order_identity_mismatch")
                if self._after_submit_hook is not None:
                    await self._after_submit_hook(result)
                native = await self._native_resolver.resolve(
                    intent.venue,
                    result.native_client_order_id,
                    result.native_order_id,
                )
            self._session.add(self._link_row(intent, native))
            await self._session.flush()
            await self._session.commit()

        if recovery_only and unresolved_intents:
            if terminal_boundary:
                claim, replay = await self._lock_owned_claim(claim_id, owner_token)
                if replay is not None:
                    return replay
                if claim is None:
                    raise PaperCohortError("invocation_claim_unavailable")
                await self._terminalize_claim(
                    claim,
                    status="reconciliation_required",
                    reason="reconciliation_required",
                )
                raise PaperCohortError("reconciliation_required")
            raise PaperCohortError("recovery_incomplete")

        claim, replay = await self._lock_owned_claim(claim_id, owner_token)
        if replay is not None:
            return replay
        if claim is None:
            raise PaperCohortError("invocation_claim_unavailable")
        completed_at = self._clock()
        completion = await self._session.execute(
            update(PaperCohortRunClaim)
            .where(
                PaperCohortRunClaim.id == claim_id,
                PaperCohortRunClaim.owner_token == owner_token,
                PaperCohortRunClaim.claim_status == "in_progress",
            )
            .values(
                claim_status="completed",
                result_payload=completed.model_dump(mode="json"),
                completed_at=completed_at,
                lease_expires_at=completed_at,
            )
        )
        if completion.rowcount != 1:
            raise PaperCohortError("invocation_claim_unavailable")
        await self._session.commit()
        return completed


__all__ = ["CohortRunInvocation", "CohortRunResult", "PaperCohortRunner"]
