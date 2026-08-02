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
The ``continue`` fires *before* the venue mark is appended, and
``_load_native`` requires a mark for every ``expected_symbols`` entry.  A cohort
assignment holds at most one Alpaca link per symbol — ``_reserve_target``
commits a unique ``(cohort_id, assignment_id, symbol, venue)`` reservation
before the order is placed, so "a later round observes the reservation and
performs no mutation" (``app/services/paper_cohort/runner.py``).  Excluding an
Alpaca row therefore removes that symbol's *only* mark and the evaluation fails
closed with ``missing_evidence`` rather than completing with a smaller
aggregate.

That is the observable contract, so it is what these tests pin, and it is
enough to kill the mutant: with ``and False`` the row is not skipped, ``load()``
succeeds, and ``pytest.raises`` fails.  The size of the P&L the excluded row
carried is measured separately, through the real
``compute_native_evidence_view``, so "this row was a genuine performance data
point" is a number here rather than a claim.
"""

from __future__ import annotations

import uuid
from datetime import timedelta
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_cohort import PaperRunOrderLink
from app.models.review import AlpacaPaperOrderLedger
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
from app.services.paper_evaluation.contracts import EvaluationConfigError, ViewName
from app.services.paper_evaluation.evidence import (
    AuthoritativeEvidenceReader,
    EvaluationEvidence,
    NativeFill,
)
from app.services.paper_evaluation.pnl import PaperEvaluationPnL
from tests.services.paper_cohort.test_market_snapshot import CAPTURED_AT
from tests.services.paper_evaluation.test_evidence_native_binance import (
    _build_lineage,
    _evaluated_at,
)

# ``_build_lineage`` writes review.alpaca_paper_order_ledger rows through the
# global ``db_session``, so this module holds the ROB-968 cleanup advisory lock
# for the same reason test_evidence_native_binance.py does.
pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
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


@pytest.fixture(autouse=True)
def _enabled_server_flags(monkeypatch) -> None:
    """``_build_lineage`` drives the real runner, which is default-disabled."""
    from app.core.config import settings

    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXECUTION_ENABLED", True)


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
    """The single Alpaca link this assignment holds for one cohort symbol."""
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
    lineage = await _build_lineage(db_session, filled_at=MARKED_AT)
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
    lineage = await _build_lineage(db_session, filled_at=MARKED_AT)
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
    lineage = await _build_lineage(db_session, filled_at=MARKED_AT)
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
