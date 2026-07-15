from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.paper_cohort import (
    CanonicalMarketSnapshot,
    PaperCohortDecision,
    PaperCohortRunClaim,
    PaperCohortVenueIntent,
    PaperRunOrderLink,
)
from app.services.paper_cohort.cohort_service import PaperCohortService
from app.services.paper_cohort.contracts import PaperCohortError, RunMode
from app.services.paper_cohort.market_snapshot import (
    CanonicalSnapshotCapture,
    SnapshotCaptureRequest,
)
from app.services.paper_cohort.runner import CohortRunInvocation, PaperCohortRunner
from app.services.paper_cohort.signals import VenueQuote
from tests.services.paper_cohort.test_cohort_service import (
    _activation,
    _assignment,
    _authoritative_history,
    _registry_rows,
)
from tests.services.paper_cohort.test_market_snapshot import (
    CAPTURED_AT,
    FakePublicClient,
)

pytestmark = pytest.mark.integration


@pytest.fixture(autouse=True)
def _enabled_server_flags(monkeypatch) -> None:
    from app.core.config import settings

    monkeypatch.setattr(settings, "PAPER_COHORT_ENABLED", True)
    monkeypatch.setattr(settings, "PAPER_EXECUTION_ENABLED", True)


@dataclass
class FakeCapture:
    calls: list[SnapshotCaptureRequest] = field(default_factory=list)
    fail: bool = False

    async def capture(self, capture_request: SnapshotCaptureRequest):
        self.calls.append(capture_request)
        if self.fail:
            raise PaperCohortError("canonical_provider_error")
        clocks = iter([CAPTURED_AT, CAPTURED_AT + timedelta(milliseconds=200)])
        return await CanonicalSnapshotCapture(
            FakePublicClient(), clock=lambda: next(clocks)
        ).capture(capture_request)


@dataclass
class FakeQuotes:
    session: AsyncSession
    run_id: str
    calls: list[tuple[str, str]] = field(default_factory=list)
    decision_counts_at_call: list[int] = field(default_factory=list)

    async def get_quote(self, venue: str, symbol: str) -> VenueQuote:
        self.calls.append((venue, symbol))
        count = await self.session.scalar(
            select(func.count())
            .select_from(PaperCohortDecision)
            .where(PaperCohortDecision.run_id == self.run_id)
        )
        self.decision_counts_at_call.append(int(count or 0))
        execution_symbol = (
            symbol
            if venue == "binance"
            else {"BTCUSDT": "BTC/USD", "ETHUSDT": "ETH/USD"}[symbol]
        )
        return VenueQuote(
            venue=venue,
            symbol=execution_symbol,
            bid_price=Decimal("100"),
            ask_price=Decimal("101"),
            bid_qty=Decimal("10"),
            ask_qty=Decimal("10"),
            fetched_at=CAPTURED_AT + timedelta(milliseconds=300),
            qty_increment=Decimal("0.0001"),
            min_qty=Decimal("0.0001"),
            min_notional=Decimal("10"),
        )


@dataclass
class TimestampedFakeQuotes(FakeQuotes):
    fetched_at: datetime = CAPTURED_AT

    async def get_quote(self, venue: str, symbol: str) -> VenueQuote:
        quote = await super().get_quote(venue, symbol)
        return quote.model_copy(update={"fetched_at": self.fetched_at})


async def _active_cohort(db_session: AsyncSession, nonce: str) -> str:
    experiment, backtest = await _registry_rows(db_session, nonce)
    activation = _activation(
        (_assignment(experiment, backtest, nonce=nonce),), nonce=nonce
    )
    activation = activation.model_copy(update={"required_lookback": 3})
    activation = activation.model_copy(
        update={"expected_cohort_hash": activation.computed_cohort_hash()}
    )
    await _authoritative_history(db_session, activation)
    await PaperCohortService(db_session).activate(activation)
    await db_session.commit()
    return activation.cohort_id


async def _count_for_run(
    db_session: AsyncSession,
    model: type,
    run_id: str,
) -> int:
    value = await db_session.scalar(
        select(func.count()).select_from(model).where(model.run_id == run_id)
    )
    return int(value or 0)


@pytest.mark.asyncio
async def test_shadow_persists_signal_before_quotes_and_never_mutates_brokers(
    db_session: AsyncSession,
) -> None:
    nonce = uuid4().hex
    cohort_id = await _active_cohort(db_session, nonce)
    run_id = f"run-{nonce}"
    capture = FakeCapture()
    quotes = FakeQuotes(db_session, run_id)
    application_calls: list[str] = []
    native_calls: list[str] = []
    runner = PaperCohortRunner(
        db_session,
        capture=capture,
        quote_provider=quotes,
        application_factory=lambda: application_calls.append("constructed"),
        native_resolver=lambda: native_calls.append("resolved"),
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
        enablement=lambda _mode: True,
    )

    result = await runner.run(
        CohortRunInvocation(
            cohort_id=cohort_id,
            run_id=run_id,
            round_decision_id=f"round-{nonce}",
            mode=RunMode.SHADOW,
        )
    )
    await db_session.commit()

    assert result.snapshot_hash
    assert result.decision_count == 2
    assert result.intent_count == 4
    assert all(count >= 2 for count in quotes.decision_counts_at_call)
    assert application_calls == []
    assert native_calls == []
    assert await _count_for_run(db_session, CanonicalMarketSnapshot, run_id) == 1
    assert await _count_for_run(db_session, PaperCohortDecision, run_id) == 2
    assert await _count_for_run(db_session, PaperCohortVenueIntent, run_id) == 4
    assert await _count_for_run(db_session, PaperRunOrderLink, run_id) == 0
    intents = (
        await db_session.scalars(
            select(PaperCohortVenueIntent)
            .where(PaperCohortVenueIntent.run_id == f"run-{nonce}")
            .order_by(
                PaperCohortVenueIntent.decision_id,
                PaperCohortVenueIntent.venue,
            )
        )
    ).all()
    assert all(intent.would_order_evidence["idempotency_key"] for intent in intents)
    decisions = (
        await db_session.scalars(
            select(PaperCohortDecision).where(
                PaperCohortDecision.run_id == f"run-{nonce}"
            )
        )
    ).all()
    by_symbol: dict[str, set[str]] = {}
    for decision in decisions:
        by_symbol.setdefault(decision.symbol, set()).add(decision.signal_hash)
    assert all(len(hashes) == 1 for hashes in by_symbol.values())


@pytest.mark.asyncio
async def test_capture_failure_leaves_no_snapshot_signal_quote_or_mutation(
    db_session: AsyncSession,
) -> None:
    nonce = uuid4().hex
    cohort_id = await _active_cohort(db_session, nonce)
    run_id = f"run-{nonce}"
    capture = FakeCapture(fail=True)
    quotes = FakeQuotes(db_session, run_id)
    application_calls: list[str] = []
    native_calls: list[str] = []
    runner = PaperCohortRunner(
        db_session,
        capture=capture,
        quote_provider=quotes,
        application_factory=lambda: application_calls.append("constructed"),
        native_resolver=lambda: native_calls.append("resolved"),
        clock=lambda: CAPTURED_AT + timedelta(milliseconds=300),
        enablement=lambda _mode: True,
    )

    with pytest.raises(PaperCohortError) as exc_info:
        await runner.run(
            CohortRunInvocation(
                cohort_id=cohort_id,
                run_id=run_id,
                round_decision_id=f"round-{nonce}",
                mode=RunMode.SHADOW,
            )
        )
    assert exc_info.value.reason_code == "canonical_provider_error"
    assert quotes.calls == []
    assert application_calls == []
    assert native_calls == []
    assert await _count_for_run(db_session, CanonicalMarketSnapshot, run_id) == 0
    assert await _count_for_run(db_session, PaperCohortDecision, run_id) == 0
    assert await _count_for_run(db_session, PaperCohortVenueIntent, run_id) == 0
    assert await _count_for_run(db_session, PaperRunOrderLink, run_id) == 0


@pytest.mark.asyncio
async def test_direct_runner_before_activation_fails_before_capture_or_quote(
    db_session: AsyncSession,
) -> None:
    nonce = uuid4().hex
    experiment, backtest = await _registry_rows(db_session, nonce)
    activation = _activation(
        (_assignment(experiment, backtest, nonce=nonce),), nonce=nonce
    ).model_copy(update={"required_lookback": 3})
    activation = activation.model_copy(
        update={
            "activated_at": CAPTURED_AT + timedelta(minutes=5),
            "stop_at": CAPTURED_AT + timedelta(days=1),
        }
    )
    activation = activation.model_copy(
        update={"expected_cohort_hash": activation.computed_cohort_hash()}
    )
    await _authoritative_history(db_session, activation)
    await PaperCohortService(db_session).activate(activation)
    await db_session.commit()
    capture = FakeCapture()
    run_id = f"future-run-{nonce}"
    quotes = FakeQuotes(db_session, run_id)

    with pytest.raises(PaperCohortError) as exc_info:
        await PaperCohortRunner(
            db_session,
            capture=capture,
            quote_provider=quotes,
            clock=lambda: CAPTURED_AT,
        ).run(
            CohortRunInvocation(
                cohort_id=activation.cohort_id,
                run_id=run_id,
                round_decision_id=f"future-round-{nonce}",
                mode=RunMode.SHADOW,
            )
        )

    assert exc_info.value.reason_code == "cohort_not_active"
    assert capture.calls == []
    assert quotes.calls == []
    assert (
        await db_session.scalar(
            select(func.count())
            .select_from(PaperCohortRunClaim)
            .where(PaperCohortRunClaim.run_id == run_id)
        )
        == 0
    )


@pytest.mark.parametrize(
    "fetched_at",
    [
        CAPTURED_AT - timedelta(seconds=6),
        CAPTURED_AT + timedelta(seconds=3),
    ],
)
@pytest.mark.asyncio
async def test_stale_or_future_venue_quote_fails_before_intent_persistence(
    db_session: AsyncSession,
    fetched_at,
) -> None:
    nonce = uuid4().hex
    cohort_id = await _active_cohort(db_session, nonce)
    run_id = f"quote-run-{nonce}"
    quotes = TimestampedFakeQuotes(db_session, run_id, fetched_at=fetched_at)

    with pytest.raises(PaperCohortError) as exc_info:
        await PaperCohortRunner(
            db_session,
            capture=FakeCapture(),
            quote_provider=quotes,
            clock=lambda: CAPTURED_AT,
        ).run(
            CohortRunInvocation(
                cohort_id=cohort_id,
                run_id=run_id,
                round_decision_id=f"quote-round-{nonce}",
                mode=RunMode.SHADOW,
            )
        )

    assert exc_info.value.reason_code == "venue_quote_provider_error"
    assert await _count_for_run(db_session, PaperCohortVenueIntent, run_id) == 0
