"""ROB-1036 D-2 — the exclusion is proved in the real evidence/PnL path.

Every other ROB-1036 test either drives ``compute_alpaca_view`` with a fake
ledger, calls the ``_trade_performance_excluded_row_ids`` resolver directly, or
asserts on ``inspect.getsource`` text.  None of them execute the one line that
actually removes an excluded row from the evidence set::

    if row is not None and row.id in excluded_row_ids:
        continue

Appending ``and False`` to that condition left all nine of them green.  The
tests here close that hole: the lineage is built by the production
:class:`PaperCohortRunner`, ``AuthoritativeEvidenceReader.load()`` is driven end
to end against real DB rows, and the loaded evidence is fed into the real
``PaperEvaluationPnL.compute_native_evidence_view``.  Every assertion is on a
concrete value.

What the exclusion actually does to an assignment
-------------------------------------------------
The normal fresh path reserves each target before it submits, so a normal
single-link lineage fails closed if an excluded row was its only mark. Recovery
is different: it resolves a durable prepared intent against the native ledger
without rechecking the target reservation. The tests below pin both observable
contracts. In particular, the recovery case proves that a DB-valid second
Alpaca link can keep the expected mark series complete after the original row
is excluded.
"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_cohort import (
    PaperCohortRunClaim,
    PaperCohortTargetReservation,
    PaperRunOrderLink,
    PaperValidationCohortAssignment,
)
from app.models.review import AlpacaPaperOrderLedger
from app.services.alpaca_paper_submit_service import (
    build_canonical_payload,
    derive_automated_key,
)
from app.services.brokers.paper.contracts import (
    PaperOrderRequest,
    VerifiedExperimentProvenance,
    derive_paper_idempotency_key,
)
from app.services.invalid_sample_eligibility.contract import (
    CalibrationEligibility,
    EligibilitySubject,
    EligibilitySubjectKind,
    ForecastOutcomeObservability,
    OperationalReliabilityEligibility,
    TradePerformanceEligibility,
)
from app.services.invalid_sample_eligibility.service import (
    InvalidSampleEligibilityService,
)
from app.services.paper_cohort.contracts import PaperCohortError, RunMode
from app.services.paper_cohort.native_links import NativeOrderResolver
from app.services.paper_cohort.provenance import PaperCohortProvenanceVerifier
from app.services.paper_cohort.runner import CohortRunInvocation, PaperCohortRunner
from app.services.paper_evaluation.contracts import EvaluationConfigError, ViewName
from app.services.paper_evaluation.evidence import (
    AuthoritativeEvidenceReader,
    EvaluationEvidence,
    NativeFill,
)
from app.services.paper_evaluation.pnl import PaperEvaluationPnL
from app.services.paper_validation.contracts import ActorRole
from app.services.paper_validation.service import PaperValidationService
from tests.services.paper_cohort.test_market_snapshot import CAPTURED_AT
from tests.services.paper_cohort.test_runner_shadow import FakeCapture, FakeQuotes
from tests.services.paper_evaluation.test_evidence_native_binance import (
    _build_lineage,
    _evaluated_at,
)
from tests.services.paper_validation.conftest import (
    FakeActorRoleProvider,
    FakeFrozenInputHashProvider,
    FakePolicyHashProvider,
)

# ``_build_lineage`` writes review.alpaca_paper_order_ledger rows through the
# global ``db_session``, so this module holds the ROB-968 cleanup advisory lock
# for the same reason test_evidence_native_binance.py does.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
    pytest.mark.usefixtures("_serialize_alpaca_paper_db_suites"),
]

# ``_LedgerBackedNativeResolver`` fills every Alpaca leg at 1 @ 100 with a zero
# fee, and ``FakeQuotes`` marks at (100 + 101) / 2.
_ROW_QTY = Decimal("1")
_ROW_PRICE = Decimal("100")
_ROW_NOTIONAL = _ROW_QTY * _ROW_PRICE
# ``FakeQuotes`` stamps every venue quote at this instant, so it is both the
# mark time and the only fill time that satisfies the loader (fill at or before
# the resolved as-of mark) and the native P&L view (a mark at or before every
# fill) at once.
MARKED_AT = CAPTURED_AT + timedelta(milliseconds=300)
_D2_NATIVE_EVIDENCE_CLIENT_ORDER_PREFIX = "rob1036-native-evidence-"
_RECOVERY_SEED_CORRELATION_PREFIX = "rob1036-recovery-seed-"


@pytest.fixture(autouse=True)
def _enabled_server_flags(monkeypatch) -> None:
    """``_build_lineage`` drives the real runner, which is default-disabled."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXECUTION_ENABLED", True)


@pytest_asyncio.fixture(autouse=True)
async def _cleanup_owned_alpaca_rows(
    db_session: AsyncSession,
    investment_reports_cleanup_lock: AsyncSession,
    _serialize_alpaca_paper_db_suites,
):
    """Keep this module's committed Alpaca fixtures out of pending scans."""
    del investment_reports_cleanup_lock, _serialize_alpaca_paper_db_suites
    statement = delete(AlpacaPaperOrderLedger).where(
        AlpacaPaperOrderLedger.client_order_id.like(
            f"{_D2_NATIVE_EVIDENCE_CLIENT_ORDER_PREFIX}%"
        )
        | AlpacaPaperOrderLedger.lifecycle_correlation_id.like(
            f"{_RECOVERY_SEED_CORRELATION_PREFIX}%"
        )
    )
    await db_session.execute(statement)
    await db_session.commit()
    yield
    await db_session.execute(statement)
    await db_session.commit()


async def _build_d2_lineage(session: AsyncSession):
    """Build a real lineage with rows this module can clean by ownership."""
    return await _build_lineage(
        session,
        filled_at=MARKED_AT,
        client_order_id_prefix=_D2_NATIVE_EVIDENCE_CLIENT_ORDER_PREFIX,
    )


async def _record_trade_performance_exclusion(
    session: AsyncSession, correlation_id: str
) -> None:
    """Explicitly exclude one lifecycle from trade performance."""
    await InvalidSampleEligibilityService(session).record_decision(
        subject=EligibilitySubject(
            kind=EligibilitySubjectKind.TRADE_LIFECYCLE, ref=correlation_id
        ),
        forecast_outcome_observability=(
            ForecastOutcomeObservability.BLOCKED_PENDING_AUDIT_EVIDENCE
        ),
        calibration_eligibility=CalibrationEligibility.EXCLUDE,
        trade_performance_eligibility=TradePerformanceEligibility.EXCLUDE,
        operational_reliability_eligibility=OperationalReliabilityEligibility.INCLUDE,
        decision_reason="ROB-1036 invalid sample cleanup",
        decided_by="test-operator",
        evidence={"source": "uber-d2-execution-result-2026-07-31.md"},
    )
    await session.commit()


async def _record_calibration_only_exclusion(
    session: AsyncSession, correlation_id: str
) -> None:
    """Exclude the same lifecycle from calibration but *not* from performance."""
    await InvalidSampleEligibilityService(session).record_decision(
        subject=EligibilitySubject(
            kind=EligibilitySubjectKind.TRADE_LIFECYCLE, ref=correlation_id
        ),
        forecast_outcome_observability=ForecastOutcomeObservability.OBSERVABLE,
        calibration_eligibility=CalibrationEligibility.EXCLUDE,
        trade_performance_eligibility=TradePerformanceEligibility.INCLUDE,
        operational_reliability_eligibility=OperationalReliabilityEligibility.INCLUDE,
        decision_reason="ROB-1036 calibration-only exclusion",
        decided_by="test-operator",
    )
    await session.commit()


def _alpaca_link(
    links: tuple[PaperRunOrderLink, ...], symbol: str
) -> PaperRunOrderLink:
    """The normal-lineage Alpaca link for one cohort symbol."""
    matches = [
        link for link in links if link.venue == "alpaca" and link.symbol == symbol
    ]
    # One per (cohort, assignment, symbol, venue) — see the module docstring.
    assert len(matches) == 1
    return matches[0]


def _alpaca_view(evidence: EvaluationEvidence, fills: list[NativeFill] | None = None):
    """Run loaded evidence through the production native-view P&L function."""
    return PaperEvaluationPnL(
        evidence.config,
        evidence.epoch,
        experiment_hash=evidence.epoch.experiment_hash,
        cohort_hash=evidence.epoch.cohort_hash,
    ).compute_native_evidence_view(
        view_name=ViewName.ALPACA_BROKER,
        fills=list(evidence.alpaca_fills) if fills is None else fills,
        marks=evidence.alpaca_marks,
        window=evidence.paper_window,
    )


async def test_excluded_alpaca_row_leaves_load_and_takes_its_pnl_with_it(
    db_session: AsyncSession,
) -> None:
    """An explicit exclusion removes the row from the evaluated evidence.

    Three things are established with real values from one lineage:

    1. Before any decision the row is in ``load()``'s output — the wiring is
       additive, an undecided lifecycle is ``UNIDENTIFIABLE`` and stays.
    2. The row carries real P&L: dropping it from
       ``compute_native_evidence_view`` moves turnover by exactly its notional
       and moves the net P&L. It is a performance data point, not a no-op.
    3. Once excluded, ``load()`` will not hand that row to the P&L function at
       all — the assignment fails closed on the mark the excluded row would
       have supplied, so no aggregate containing it can be produced.
    """
    lineage = await _build_d2_lineage(db_session)
    reader = AuthoritativeEvidenceReader(db_session)

    victim_link = _alpaca_link(lineage.links, "BTCUSDT")
    other_link = _alpaca_link(lineage.links, "ETHUSDT")
    victim = await db_session.get(
        AlpacaPaperOrderLedger, victim_link.native_ledger_row_id
    )
    assert victim is not None

    # --- 1. baseline: no decision on record, the row is evidence ---------
    baseline = await reader.load(
        evaluated_at=_evaluated_at(),
        cohort_id=lineage.cohort_id,
        assignment_id=lineage.assignment_id,
    )
    assert {fill.native_row_id for fill in baseline.alpaca_fills} == {
        victim_link.native_ledger_row_id,
        other_link.native_ledger_row_id,
    }

    # --- 2. what that row is worth in the real P&L function -------------
    with_victim = _alpaca_view(baseline)
    without_victim = _alpaca_view(
        baseline,
        fills=[
            fill
            for fill in baseline.alpaca_fills
            if fill.native_row_id != victim_link.native_ledger_row_id
        ],
    )
    assert with_victim.fill_count == 2
    assert without_victim.fill_count == 1
    assert with_victim.turnover == _ROW_NOTIONAL * 2
    assert with_victim.turnover - without_victim.turnover == _ROW_NOTIONAL
    assert with_victim.nominal_net_pnl != without_victim.nominal_net_pnl
    assert with_victim.ending_equity != without_victim.ending_equity

    # --- 3. after the exclusion the loader refuses to produce evidence ---
    await _record_trade_performance_exclusion(
        db_session, victim.lifecycle_correlation_id
    )

    with pytest.raises(EvaluationConfigError) as exc:
        await reader.load(
            evaluated_at=_evaluated_at(),
            cohort_id=lineage.cohort_id,
            assignment_id=lineage.assignment_id,
        )
    assert exc.value.reason_code == "missing_evidence"
    # The excluded row was this symbol's only Alpaca leg, so its mark goes with
    # it. ETH/USD is untouched and never reports missing.
    assert str(exc.value) == "missing native mark for alpaca:BTC/USD"

    # The invalid sample stays a real operational record; it merely stopped
    # being a performance data point.
    assert (
        await db_session.get(AlpacaPaperOrderLedger, victim_link.native_ledger_row_id)
        is not None
    )


async def test_a_calibration_only_exclusion_leaves_the_evidence_intact(
    db_session: AsyncSession,
) -> None:
    """The four eligibility domains stay independent inside the loader.

    A lifecycle excluded from *calibration* is still a valid trade-performance
    sample. Broadening the loader's predicate to "any exclusion" would drop it
    and turn this red.
    """
    lineage = await _build_d2_lineage(db_session)
    reader = AuthoritativeEvidenceReader(db_session)

    link = _alpaca_link(lineage.links, "BTCUSDT")
    row = await db_session.get(AlpacaPaperOrderLedger, link.native_ledger_row_id)
    assert row is not None
    await _record_calibration_only_exclusion(db_session, row.lifecycle_correlation_id)

    evidence = await reader.load(
        evaluated_at=_evaluated_at(),
        cohort_id=lineage.cohort_id,
        assignment_id=lineage.assignment_id,
    )

    assert link.native_ledger_row_id in {
        fill.native_row_id for fill in evidence.alpaca_fills
    }
    assert _alpaca_view(evidence).fill_count == 2


async def test_an_exclusion_for_another_lifecycle_does_not_touch_this_assignment(
    db_session: AsyncSession,
) -> None:
    """Only the decided lifecycle is filtered — not the whole cohort.

    Guards the row-id resolution in ``_trade_performance_excluded_row_ids``: an
    exclusion keyed on an unrelated correlation id must not leak into this
    assignment's evidence.
    """
    lineage = await _build_d2_lineage(db_session)
    reader = AuthoritativeEvidenceReader(db_session)

    await _record_trade_performance_exclusion(
        db_session, f"corr-unrelated-{uuid.uuid4().hex[:12]}"
    )

    evidence = await reader.load(
        evaluated_at=_evaluated_at(),
        cohort_id=lineage.cohort_id,
        assignment_id=lineage.assignment_id,
    )

    assert {fill.native_row_id for fill in evidence.alpaca_fills} == {
        _alpaca_link(lineage.links, symbol).native_ledger_row_id
        for symbol in ("BTCUSDT", "ETHUSDT")
    }
    assert _alpaca_view(evidence).fill_count == 2


@dataclass
class _MutableClock:
    now: datetime

    def __call__(self) -> datetime:
        return self.now


@dataclass
class _StopAfterPrepareBeforeReservation:
    """Test-only crash seam that leaves the runner's durable plan untouched."""

    delegate: PaperCohortProvenanceVerifier

    async def verify(self, request: PaperOrderRequest) -> VerifiedExperimentProvenance:
        del request
        raise PaperCohortError("test_stop_after_prepare_before_reservation")

    async def verify_persisted(
        self, request: PaperOrderRequest
    ) -> VerifiedExperimentProvenance:
        return await self.delegate.verify_persisted(request)


def _deterministic_alpaca_client_order_id(
    request: PaperOrderRequest,
    provenance: VerifiedExperimentProvenance,
) -> str:
    """Use the same production helper calls as ``resolve_prepared``.

    A native row has to exist before the resolver can return it.  This exposes
    its deterministic lookup key without replacing the resolver: recovery
    below still calls the real ``NativeOrderResolver.resolve_prepared``.
    """
    idempotency_key = derive_paper_idempotency_key(provenance)
    canonical = build_canonical_payload(
        symbol=request.symbol,
        side=request.side,
        type=request.order_type,
        time_in_force=request.time_in_force,
        qty=request.qty,
        notional=request.notional,
        limit_price=request.price,
        asset_class="crypto",
    )
    return derive_automated_key(
        correlation_id=hashlib.sha256(idempotency_key.encode()).hexdigest(),
        snapshot_id=request.market_snapshot_id,
        canonical=canonical,
    )


@pytest.mark.asyncio
async def test_recovery_link_keeps_excluded_alpaca_row_out_of_completed_native_view(
    db_session: AsyncSession,
) -> None:
    """A recovery-only duplicate link preserves the non-excluded BTC mark.

    The test uses a normal four-link lineage first, then a distinct paper-active
    run/round for the same cohort and assignment.  The second invocation is
    interrupted only after ``PaperCohortRunner`` has durably prepared its
    intents and before fresh-path target reservation.  Its claim lease then
    expires and public ``recover()`` is allowed to reconcile the prepared data.
    """
    lineage = await _build_d2_lineage(db_session)
    victim_link = _alpaca_link(lineage.links, "BTCUSDT")
    other_link = _alpaca_link(lineage.links, "ETHUSDT")
    victim = await db_session.get(
        AlpacaPaperOrderLedger, victim_link.native_ledger_row_id
    )
    assert victim is not None

    assignment = await db_session.scalar(
        select(PaperValidationCohortAssignment).where(
            PaperValidationCohortAssignment.assignment_id == lineage.assignment_id
        )
    )
    assert assignment is not None
    clock = _MutableClock(MARKED_AT)
    validation = PaperValidationService(
        db_session,
        actor_role_provider=FakeActorRoleProvider(
            {"paper-cohort-runner": ActorRole.SYSTEM}
        ),
        frozen_input_provider=FakeFrozenInputHashProvider(assignment.input_hash),
        policy_provider=FakePolicyHashProvider(assignment.policy_hash),
    )
    durable_verifier = PaperCohortProvenanceVerifier(
        db_session,
        validation_service=validation,
        caller_id="paper-cohort-runner",
        clock=clock,
    )
    native_resolver = NativeOrderResolver(db_session)
    invocation = CohortRunInvocation(
        cohort_id=lineage.cohort_id,
        run_id=f"recovery-only-run-{uuid.uuid4().hex}",
        round_decision_id=f"recovery-only-round-{uuid.uuid4().hex}",
        mode=RunMode.PAPER_ACTIVE,
    )
    assert invocation.run_id != victim_link.run_id
    assert invocation.round_decision_id != victim_link.round_decision_id

    # Drive the public fresh execution path until its durable preparation is
    # committed. The test seam models a crash at the first provenance check,
    # immediately before the normal ``_reserve_target`` call.
    with pytest.raises(PaperCohortError, match="test_stop_after_prepare"):
        await PaperCohortRunner(
            db_session,
            capture=FakeCapture(),
            quote_provider=FakeQuotes(db_session),
            verifier=_StopAfterPrepareBeforeReservation(durable_verifier),
            native_resolver=native_resolver,
            clock=clock,
            enablement=lambda _mode: True,
        ).run(invocation)
    await db_session.rollback()

    claim = await db_session.scalar(
        select(PaperCohortRunClaim).where(
            PaperCohortRunClaim.cohort_id == invocation.cohort_id,
            PaperCohortRunClaim.run_id == invocation.run_id,
            PaperCohortRunClaim.round_decision_id == invocation.round_decision_id,
        )
    )
    assert claim is not None
    assert claim.claim_status == "in_progress"
    assert claim.lease_expires_at > clock()
    assert (
        await db_session.scalar(
            select(PaperCohortTargetReservation.id).where(
                PaperCohortTargetReservation.run_id == invocation.run_id
            )
        )
    ) is None

    prepared = await PaperCohortRunner(
        db_session,
        capture=FakeCapture(),
        quote_provider=FakeQuotes(db_session),
        verifier=durable_verifier,
        native_resolver=native_resolver,
        clock=clock,
        enablement=lambda _mode: True,
    )._load_prepared(invocation)
    assert prepared is not None
    snapshot, _decisions, active_intents = prepared
    recovery_candidates = [
        item
        for item in active_intents
        if item[0].venue == "alpaca" and item[0].symbol == "BTCUSDT"
    ]
    assert len(recovery_candidates) == 1
    recovery_intent, recovery_signal, recovery_evidence = recovery_candidates[0]
    request = PaperCohortRunner.build_request(
        recovery_intent, recovery_signal, recovery_evidence, snapshot
    )
    assert request.symbol == "BTC/USD"
    provenance = await durable_verifier.verify_persisted(request)
    client_order_id = _deterministic_alpaca_client_order_id(request, provenance)

    # Intentional isolated-test-DB seed: a filled Alpaca execution for this
    # already-prepared second BTC intent, carrying the exact deterministic
    # client id that production ``resolve_prepared`` looks up. This is DB-valid:
    # PaperRunOrderLink has no (cohort, assignment, symbol, venue) uniqueness
    # constraint and no FK to PaperCohortTargetReservation, while this ledger
    # row itself satisfies the resolver's execution/account/broker-id shape.
    # It models the real crash window after durable prepare but before fresh
    # reservation. Everything after the seed — resolver lookup, recovery link
    # creation, evidence load, and PnL aggregation — is production code.
    recovery_row = AlpacaPaperOrderLedger(
        client_order_id=client_order_id,
        broker_order_id=f"recovery-broker-{uuid.uuid4().hex}",
        lifecycle_correlation_id=(
            f"{_RECOVERY_SEED_CORRELATION_PREFIX}{uuid.uuid4().hex}"
        ),
        record_kind="execution",
        broker="alpaca",
        account_mode="alpaca_paper",
        lifecycle_state="filled",
        execution_symbol=request.symbol,
        execution_venue="alpaca_paper",
        instrument_type="equity_us",
        side="buy",
        order_type="market",
        currency="USD",
        requested_qty=Decimal("1"),
        filled_qty=Decimal("1"),
        filled_avg_price=Decimal("100"),
        fee_amount=Decimal("0"),
        fee_currency="USD",
        raw_responses={"filled_at": MARKED_AT.isoformat()},
    )
    db_session.add(recovery_row)
    await db_session.flush()
    resolved = await native_resolver.resolve_prepared(request, provenance)
    assert resolved.ledger_row_id == recovery_row.id
    assert resolved.client_order_id == client_order_id
    await db_session.commit()

    # The fresh claim is no longer live. Recovery sees only the matching seeded
    # BTC/Alpaca row; the three deliberately absent native rows make its final
    # result ``recovery_incomplete`` after the recovered link has been committed.
    clock.now = claim.lease_expires_at + timedelta(seconds=1)
    with pytest.raises(PaperCohortError, match="recovery_incomplete"):
        await PaperCohortRunner(
            db_session,
            capture=FakeCapture(),
            quote_provider=FakeQuotes(db_session),
            verifier=durable_verifier,
            native_resolver=native_resolver,
            clock=clock,
            enablement=lambda _mode: True,
        ).recover(invocation)
    await db_session.rollback()
    assert (
        await db_session.scalar(
            select(PaperCohortTargetReservation.id).where(
                PaperCohortTargetReservation.run_id == invocation.run_id
            )
        )
    ) is None

    duplicate_btc_links = list(
        (
            await db_session.scalars(
                select(PaperRunOrderLink).where(
                    PaperRunOrderLink.cohort_id == lineage.cohort_id,
                    PaperRunOrderLink.assignment_id == lineage.assignment_id,
                    PaperRunOrderLink.symbol == "BTCUSDT",
                    PaperRunOrderLink.venue == "alpaca",
                )
            )
        ).all()
    )
    assert {link.native_ledger_row_id for link in duplicate_btc_links} == {
        victim_link.native_ledger_row_id,
        recovery_row.id,
    }
    assert len(duplicate_btc_links) == 2

    reader = AuthoritativeEvidenceReader(db_session)
    before_exclusion = await reader.load(
        evaluated_at=_evaluated_at(),
        cohort_id=lineage.cohort_id,
        assignment_id=lineage.assignment_id,
    )
    before_row_ids = {fill.native_row_id for fill in before_exclusion.alpaca_fills}
    assert victim_link.native_ledger_row_id in before_row_ids
    assert recovery_row.id in before_row_ids
    assert before_row_ids == {
        victim_link.native_ledger_row_id,
        other_link.native_ledger_row_id,
        recovery_row.id,
    }

    await _record_trade_performance_exclusion(
        db_session, victim.lifecycle_correlation_id
    )
    after_exclusion = await reader.load(
        evaluated_at=_evaluated_at(),
        cohort_id=lineage.cohort_id,
        assignment_id=lineage.assignment_id,
    )
    load_row_ids = {fill.native_row_id for fill in after_exclusion.alpaca_fills}
    assert victim_link.native_ledger_row_id not in load_row_ids
    assert recovery_row.id in load_row_ids
    assert load_row_ids == {other_link.native_ledger_row_id, recovery_row.id}

    # ``ViewMetrics`` intentionally records aggregates only, so pin the exact
    # native-row set supplied to the real production PnL function as well.
    native_view_fills = tuple(after_exclusion.alpaca_fills)
    native_view_row_ids = {fill.native_row_id for fill in native_view_fills}
    assert victim_link.native_ledger_row_id not in native_view_row_ids
    assert native_view_row_ids == load_row_ids
    native_view = PaperEvaluationPnL(
        after_exclusion.config,
        after_exclusion.epoch,
        experiment_hash=after_exclusion.epoch.experiment_hash,
        cohort_hash=after_exclusion.epoch.cohort_hash,
    ).compute_native_evidence_view(
        view_name=ViewName.ALPACA_BROKER,
        fills=native_view_fills,
        marks=after_exclusion.alpaca_marks,
        window=after_exclusion.paper_window,
    )
    assert native_view.fill_count == len(native_view_row_ids)
    assert native_view.fill_count == 2
