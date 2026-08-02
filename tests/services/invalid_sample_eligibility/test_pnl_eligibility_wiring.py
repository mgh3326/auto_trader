"""ROB-1036 B2 — trade-performance eligibility is wired into the real PnL path.

Covers both Alpaca PnL entry points:

* ``PaperEvaluationPnL.compute_alpaca_view`` — the ledger-row path, driven with
  a fake read-only ledger (no broker, no network).
* ``AuthoritativeEvidenceReader._trade_performance_excluded_row_ids`` — the
  resolver the live ``alpaca_fills`` evidence loader calls before building fills.

The exclusion is keyed on an explicit ``trade_performance_exclude`` decision.
A lifecycle with no decision is ``UNIDENTIFIABLE`` and is left in place, so the
wiring is additive: with no decisions recorded, the PnL inputs are unchanged.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete, text
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import TradeForecast
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

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.integration,
    pytest.mark.usefixtures("investment_reports_cleanup_lock"),
]


@pytest_asyncio.fixture(autouse=True)
async def _cleanup(
    db_session: AsyncSession, investment_reports_cleanup_lock: AsyncSession
):
    for table in (
        "invalid_sample_cleanup_lifecycle_events",
        "invalid_sample_cleanup_bindings",
        "sample_eligibility_decisions",
    ):
        await db_session.execute(
            text(f"ALTER TABLE review.{table} DISABLE TRIGGER USER")
        )
        await db_session.execute(text(f"DELETE FROM review.{table}"))
        await db_session.execute(
            text(f"ALTER TABLE review.{table} ENABLE TRIGGER USER")
        )
    await db_session.execute(delete(TradeForecast))
    await db_session.commit()
    yield


async def _exclude_lifecycle(db_session: AsyncSession, correlation_id: str) -> None:
    service = InvalidSampleEligibilityService(db_session)
    await service.record_decision(
        subject=EligibilitySubject(
            kind=EligibilitySubjectKind.TRADE_LIFECYCLE, ref=correlation_id
        ),
        forecast_outcome_observability=(
            ForecastOutcomeObservability.BLOCKED_PENDING_AUDIT_EVIDENCE
        ),
        calibration_eligibility=CalibrationEligibility.EXCLUDE,
        trade_performance_eligibility=TradePerformanceEligibility.EXCLUDE,
        operational_reliability_eligibility=OperationalReliabilityEligibility.INCLUDE,
        decision_reason="invalid sample cleanup",
        decided_by="test-operator",
        evidence={"source": "uber-d2-execution-result-2026-07-31.md"},
    )
    await db_session.commit()


# --- the service-layer resolver -------------------------------------------


async def test_resolver_reports_only_explicit_exclusions(
    db_session: AsyncSession,
) -> None:
    service = InvalidSampleEligibilityService(db_session)
    await _exclude_lifecycle(db_session, "corr-excluded")

    excluded = await service.list_trade_performance_excluded(
        ["corr-excluded", "corr-undecided"]
    )

    assert excluded == frozenset({"corr-excluded"})
    # Undecided is UNIDENTIFIABLE — never silently reported as excluded.
    assert "corr-undecided" not in excluded


async def test_resolver_ignores_a_calibration_only_exclusion(
    db_session: AsyncSession,
) -> None:
    """The four domains stay independent at the PnL boundary too."""

    service = InvalidSampleEligibilityService(db_session)
    await service.record_decision(
        subject=EligibilitySubject(
            kind=EligibilitySubjectKind.TRADE_LIFECYCLE, ref="corr-cal-only"
        ),
        forecast_outcome_observability=ForecastOutcomeObservability.OBSERVABLE,
        calibration_eligibility=CalibrationEligibility.EXCLUDE,
        trade_performance_eligibility=TradePerformanceEligibility.INCLUDE,
        operational_reliability_eligibility=OperationalReliabilityEligibility.INCLUDE,
        decision_reason="calibration-only exclusion",
        decided_by="test-operator",
    )
    await db_session.commit()

    assert (
        await service.list_trade_performance_excluded(["corr-cal-only"]) == frozenset()
    )


async def test_resolver_is_empty_without_decisions(db_session: AsyncSession) -> None:
    service = InvalidSampleEligibilityService(db_session)
    assert await service.list_trade_performance_excluded(["a", "b"]) == frozenset()
    assert await service.list_trade_performance_excluded([]) == frozenset()


# --- compute_alpaca_view ---------------------------------------------------


def _alpaca_ledger_with_two_lifecycles():
    """Two profitable round trips on two separate lifecycles.

    Reuses the ROB-850 PnL harness so the assertions run through the *real*
    ``compute_alpaca_view``, not a re-implementation of its filter.
    """

    from tests.services.paper_evaluation.test_pnl import (
        FakeAlpacaLedgerReader,
        _make_alpaca_execution_row,
    )

    def roundtrip(corr: str, first_row_id: int):
        return [
            _make_alpaca_execution_row(
                side="buy",
                filled_qty="1",
                filled_price="50000",
                row_id=first_row_id,
                corr_id=corr,
            ),
            _make_alpaca_execution_row(
                side="sell",
                filled_qty="1",
                filled_price="51000",
                row_id=first_row_id + 1,
                corr_id=corr,
            ),
        ]

    return FakeAlpacaLedgerReader(
        rows_by_correlation={
            "corr_kept": roundtrip("corr_kept", 1),
            "corr_excluded": roundtrip("corr_excluded", 3),
        }
    )


async def test_compute_alpaca_view_drops_an_excluded_lifecycle_from_pnl() -> None:
    """§4.2-10 through the production PnL function, not a source assertion.

    Same ledger, same correlation ids; the only difference is the exclusion set.
    The excluded lifecycle's realised P&L must vanish from the result.
    """

    from tests.services.paper_evaluation.test_pnl import _make_service

    service = _make_service()
    correlation_ids = ["corr_kept", "corr_excluded"]

    both = await service.compute_alpaca_view(
        _alpaca_ledger_with_two_lifecycles(), correlation_ids=correlation_ids
    )
    filtered = await service.compute_alpaca_view(
        _alpaca_ledger_with_two_lifecycles(),
        correlation_ids=correlation_ids,
        excluded_correlation_ids={"corr_excluded"},
    )
    only_kept = await service.compute_alpaca_view(
        _alpaca_ledger_with_two_lifecycles(), correlation_ids=["corr_kept"]
    )

    # The excluded lifecycle really did contribute before being filtered...
    assert both.nominal_net_pnl != filtered.nominal_net_pnl
    assert both.turnover > filtered.turnover
    # ...and filtering it is exactly equivalent to never supplying it.
    assert filtered.nominal_net_pnl == only_kept.nominal_net_pnl
    assert filtered.turnover == only_kept.turnover
    assert filtered.fill_count == only_kept.fill_count


async def test_compute_alpaca_view_exclusion_is_additive_by_default() -> None:
    """Omitting the argument reproduces the previous behaviour byte for byte."""

    from tests.services.paper_evaluation.test_pnl import _make_service

    service = _make_service()
    correlation_ids = ["corr_kept", "corr_excluded"]

    default = await service.compute_alpaca_view(
        _alpaca_ledger_with_two_lifecycles(), correlation_ids=correlation_ids
    )
    explicit_empty = await service.compute_alpaca_view(
        _alpaca_ledger_with_two_lifecycles(),
        correlation_ids=correlation_ids,
        excluded_correlation_ids=frozenset(),
    )

    assert default.nominal_net_pnl == explicit_empty.nominal_net_pnl
    assert default.turnover == explicit_empty.turnover
    assert default.fill_count == explicit_empty.fill_count


async def test_compute_alpaca_view_drops_a_foreign_row_carrying_an_excluded_id() -> (
    None
):
    """A ledger row whose own lifecycle id is excluded is dropped too.

    ``list_by_correlation_id`` is a fake-able boundary: a row returned under one
    correlation id may carry another. The per-row guard covers that case, so the
    filter cannot be defeated by a mis-keyed read.
    """

    from tests.services.paper_evaluation.test_pnl import (
        FakeAlpacaLedgerReader,
        _make_alpaca_execution_row,
        _make_service,
    )

    service = _make_service()
    ledger = FakeAlpacaLedgerReader(
        rows_by_correlation={
            "corr_kept": [
                _make_alpaca_execution_row(
                    side="buy",
                    filled_qty="1",
                    filled_price="50000",
                    row_id=1,
                    corr_id="corr_kept",
                ),
                # Same bucket, foreign (excluded) lifecycle id.
                _make_alpaca_execution_row(
                    side="buy",
                    filled_qty="1",
                    filled_price="50000",
                    row_id=2,
                    corr_id="corr_excluded",
                ),
            ]
        }
    )
    metrics = await service.compute_alpaca_view(
        ledger,
        correlation_ids=["corr_kept"],
        excluded_correlation_ids={"corr_excluded"},
    )

    baseline = await service.compute_alpaca_view(
        FakeAlpacaLedgerReader(
            rows_by_correlation={
                "corr_kept": [
                    _make_alpaca_execution_row(
                        side="buy",
                        filled_qty="1",
                        filled_price="50000",
                        row_id=1,
                        corr_id="corr_kept",
                    )
                ]
            }
        ),
        correlation_ids=["corr_kept"],
    )
    assert metrics.turnover == baseline.turnover


# --- the live evidence loader ---------------------------------------------


async def test_evidence_reader_resolves_exclusions_for_alpaca_links(
    db_session: AsyncSession,
) -> None:
    """``AuthoritativeEvidenceReader`` consults the eligibility service."""

    from app.services.paper_evaluation.evidence import AuthoritativeEvidenceReader

    await _exclude_lifecycle(db_session, "corr-excluded")
    reader = AuthoritativeEvidenceReader(db_session)

    @dataclass
    class FakeLink:
        venue: str
        native_ledger_row_id: int

    # Binance links are ignored by this resolver (different ledger/contract).
    rows = [(FakeLink("binance", 999),)]
    assert await reader._trade_performance_excluded_row_ids(rows) == frozenset()


async def test_evidence_loader_filters_the_excluded_alpaca_row(
    db_session: AsyncSession,
) -> None:
    """The live loader skips a ledger row bound to an excluded lifecycle."""

    from app.models.review import AlpacaPaperOrderLedger
    from app.services.paper_evaluation.evidence import AuthoritativeEvidenceReader

    correlation_id = f"corr-{uuid.uuid4().hex[:12]}"
    client_order_id = f"cleanup-{uuid.uuid4().hex[:12]}"
    row = AlpacaPaperOrderLedger(
        client_order_id=client_order_id,
        lifecycle_correlation_id=correlation_id,
        record_kind="execution",
        broker="alpaca",
        account_mode="alpaca_paper_lab",
        lifecycle_state="filled",
        execution_symbol="UBER",
        execution_venue="alpaca_paper",
        instrument_type="equity_us",
        side="sell",
        order_type="market",
        currency="USD",
        filled_qty=Decimal("1"),
        filled_avg_price=Decimal("70"),
        raw_responses={"filled_at": datetime.now(UTC).isoformat()},
    )
    db_session.add(row)
    await db_session.commit()
    await db_session.refresh(row)

    reader = AuthoritativeEvidenceReader(db_session)

    @dataclass
    class FakeLink:
        venue: str
        native_ledger_row_id: int

    links = [(FakeLink("alpaca", row.id),)]

    # Before any decision: UNIDENTIFIABLE, so the row stays (additive).
    assert await reader._trade_performance_excluded_row_ids(links) == frozenset()

    await _exclude_lifecycle(db_session, correlation_id)

    # After an explicit trade-performance exclusion: filtered out. The resolver
    # returns native *row ids* so the ROB-850-guarded loader never handles a
    # correlation id.
    assert await reader._trade_performance_excluded_row_ids(links) == frozenset(
        {row.id}
    )


async def test_evidence_loader_applies_the_filter_in_the_fill_loop() -> None:
    """The resolved set is actually consumed where fills are built."""

    import inspect

    from app.services.paper_evaluation.evidence import AuthoritativeEvidenceReader

    source = inspect.getsource(AuthoritativeEvidenceReader._load_native)
    assert "excluded_row_ids = await self._trade_performance_excluded_row_ids" in source
    assert "row.id in excluded_row_ids" in source
    # ROB-850: the guarded method must stay free of correlation vocabulary.
    assert "lifecycle_correlation_id" not in source
