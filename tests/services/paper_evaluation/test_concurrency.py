"""ROB-850 concurrency / idempotency tests for PaperEvaluationService.

Tests:
* Replay safety — same ``(epoch_id, idempotency_key, request_hash)``
  returns the existing verdict without re-computing.
* Idempotency conflict — same ``(epoch_id, idempotency_key)`` with a
  different ``request_hash`` raises ``idempotency_conflict``.
* Concurrent evaluation — two concurrent calls with different
  idempotency_keys targeting the same epoch resolve to one winner via
  the ``uq_evaluation_verdict_epoch`` unique constraint; the loser gets
  either the winner's verdict (if it can see the committed row) or a
  ``concurrent_evaluation_conflict`` error.

All tests use ``asyncio.Barrier(2)`` and ``asyncio.gather(...,
return_exceptions=True)`` following the pattern established in
``tests/services/paper_cohort/test_runner_concurrency.py``.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import AsyncSessionLocal
from app.models.paper_cohort import (
    PaperValidationCohort,
    PaperValidationCohortAssignment,
)
from app.models.paper_evaluation import (
    EvaluationConfig as EvaluationConfigDb,
)
from app.models.paper_evaluation import (
    EvaluationEpoch,
    EvaluationVerdict,
)
from app.models.research_backtest import ResearchBacktestRun, ResearchStrategyExperiment
from app.services.paper_evaluation.contracts import (
    EpochIdentity,
    EvaluationConfig,
    EvaluationConfigError,
    ScorecardVerdict,
    ViewName,
)
from app.services.paper_evaluation.service import PaperEvaluationService
from tests.services.paper_evaluation.conftest import make_evaluation_config

pytestmark = pytest.mark.integration


def _make_unique_config(nonce: str, **kwargs: object) -> EvaluationConfig:
    unique_bps = Decimal(int(nonce[:8], 16))
    return make_evaluation_config(slippage_bps=unique_bps, **kwargs)


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Fake readers (same as test_integration.py, kept local for isolation)
# ---------------------------------------------------------------------------


@dataclass
class FakeBinanceRow:
    id: int = 1
    notional_usdt: Decimal | None = None
    extra_metadata: dict[str, Any] | None = None
    lifecycle_state: str = "closed"
    side: str = "BUY"


@dataclass
class FakeBinanceLedgerReader:
    rows: list[FakeBinanceRow] = field(default_factory=list)

    async def closed_rows_since(self, *, since: datetime) -> list[FakeBinanceRow]:
        return list(self.rows)


@dataclass
class FakeAlpacaRow:
    id: int = 1
    record_kind: str = "execution"
    lifecycle_state: str = "filled"
    client_order_id: str = "coid_1"
    lifecycle_correlation_id: str = "corr_1"
    side: str = "buy"
    currency: str = "USD"
    execution_symbol: str = "BTC/USD"
    filled_qty: Decimal | None = None
    filled_avg_price: Decimal | None = None
    position_snapshot: dict[str, Any] | None = None


@dataclass
class FakeAlpacaLedgerReader:
    rows_by_correlation: dict[str, list[FakeAlpacaRow]] = field(
        default_factory=dict
    )

    async def list_by_correlation_id(
        self, lifecycle_correlation_id: str
    ) -> list[FakeAlpacaRow]:
        return list(self.rows_by_correlation.get(lifecycle_correlation_id, []))

    async def find_executed_by_client_order_id(
        self, client_order_id: str
    ) -> FakeAlpacaRow | None:
        for rows in self.rows_by_correlation.values():
            for row in rows:
                if (
                    row.client_order_id == client_order_id
                    and row.record_kind == "execution"
                ):
                    return row
        return None


@dataclass
class FakeMarketSnapshot:
    id: int = 1
    content_hash: str = "a" * 64
    payload: dict[str, Any] = field(default_factory=dict)
    cohort_id: str = "cohort_test"
    snapshot_id: str = "snap_001"


@dataclass
class FakeSnapshotReader:
    snapshots: list[FakeMarketSnapshot] = field(default_factory=list)

    async def list_snapshots(
        self, *, cohort_id: str, since: datetime
    ) -> list[FakeMarketSnapshot]:
        return list(self.snapshots)


# ---------------------------------------------------------------------------
# Data builders
# ---------------------------------------------------------------------------


def _make_binance_row(*, row_id: int = 1) -> FakeBinanceRow:
    return FakeBinanceRow(
        id=row_id,
        notional_usdt=Decimal("1000"),
        extra_metadata={"realized_pnl_usdt": "100"},
    )


def _make_alpaca_pair(
    *, corr_id: str, row_id_start: int = 1
) -> list[FakeAlpacaRow]:
    return [
        FakeAlpacaRow(
            id=row_id_start,
            side="buy",
            execution_symbol="BTC/USD",
            filled_qty=Decimal("1"),
            filled_avg_price=Decimal("50000"),
            client_order_id=f"coid_buy_{row_id_start}",
            lifecycle_correlation_id=corr_id,
        ),
        FakeAlpacaRow(
            id=row_id_start + 1,
            side="sell",
            execution_symbol="BTC/USD",
            filled_qty=Decimal("1"),
            filled_avg_price=Decimal("51000"),
            client_order_id=f"coid_sell_{row_id_start + 1}",
            lifecycle_correlation_id=corr_id,
        ),
    ]


def _make_snapshot_payload() -> dict[str, Any]:
    return {
        "schema_id": "canonical_market_snapshot.v1",
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "candles": [{"close": "50000"}],
            },
            {
                "symbol": "ETHUSDT",
                "candles": [{"close": "3000"}],
            },
        ],
    }


def _build_readers(
    *, cohort_id: str
) -> tuple[FakeBinanceLedgerReader, FakeAlpacaLedgerReader, FakeSnapshotReader]:
    binance_r = FakeBinanceLedgerReader(
        rows=[_make_binance_row(row_id=i) for i in range(1, 4)]
    )
    alpaca_rows: list[FakeAlpacaRow] = []
    for i in range(3):
        alpaca_rows.extend(
            _make_alpaca_pair(corr_id=cohort_id, row_id_start=i * 2 + 1)
        )
    alpaca_r = FakeAlpacaLedgerReader(
        rows_by_correlation={cohort_id: alpaca_rows}
    )
    snap_r = FakeSnapshotReader(
        snapshots=[
            FakeMarketSnapshot(
                id=i,
                content_hash=_hash(f"snap_{i}"),
                payload=_make_snapshot_payload(),
                cohort_id=cohort_id,
                snapshot_id=f"snap_{i}",
            )
            for i in range(1, 4)
        ]
    )
    return binance_r, alpaca_r, snap_r


# ---------------------------------------------------------------------------
# DB setup
# ---------------------------------------------------------------------------


async def _setup_db(
    session: AsyncSession,
    *,
    nonce: str,
    config: EvaluationConfig,
    experiment_hash: str,
    cohort_hash: str,
    started_at: datetime,
) -> tuple[str, str]:
    cohort_id = f"cohort-{nonce}"
    epoch_id = f"epoch-{nonce}"

    experiment = ResearchStrategyExperiment(
        experiment_id=experiment_hash,
        strategy_key=f"strategy-{nonce}",
        strategy_version="strategy-v1",
        strategy_hash=_hash(f"{nonce}:strategy"),
        code_hash=_hash(f"{nonce}:code"),
        params_hash=_hash(f"{nonce}:params"),
        dataset_manifest_hash=_hash(f"{nonce}:dataset"),
        universe_hash=_hash(f"{nonce}:universe"),
        pit_hash=_hash(f"{nonce}:pit"),
        frozen_config_hash=_hash(f"{nonce}:frozen_config"),
        policy_hash=_hash(f"{nonce}:policy"),
        benchmark_hash=_hash(f"{nonce}:benchmark"),
        cost_hash=_hash(f"{nonce}:cost"),
        mdd_hash=_hash(f"{nonce}:mdd"),
        manifest={},
    )
    session.add(experiment)
    await session.flush()

    run = ResearchBacktestRun(
        run_id=f"backtest-{nonce}",
        strategy_name=experiment.strategy_key,
        strategy_version=experiment.strategy_version,
        exchange="binance",
        market="spot",
        timeframe="1m",
        runner="pytest",
        total_trades=10,
        profit_factor=Decimal("1.2"),
        max_drawdown=Decimal("0.1"),
        strategy_experiment_id=experiment.id,
        trial_index=1,
        trial_status="completed",
        trial_idempotency_key=f"trial-{nonce}",
    )
    session.add(run)
    await session.flush()

    session.add(
        PaperValidationCohort(
            cohort_id=cohort_id,
            cohort_hash=cohort_hash,
            venues=["binance", "alpaca"],
            symbols=["BTCUSDT", "ETHUSDT"],
            market="spot",
            leverage=Decimal("1"),
            interval="1m",
            required_lookback=30,
            max_capture_skew_ms=5_000,
            max_ticker_age_ms=5_000,
            capital_notional_usd=Decimal("10000"),
            assignment_count=1,
            activated_at=started_at,
        )
    )
    await session.flush()

    session.add(
        PaperValidationCohortAssignment(
            assignment_id=f"assignment-{nonce}",
            cohort_id=cohort_id,
            ordinal=0,
            role="champion",
            validation_id=f"validation-{nonce}",
            validation_version=1,
            experiment_id=experiment.experiment_id,
            source_backtest_run_id=run.id,
            strategy_version_id=experiment.strategy_version,
            target_weights={"BTCUSDT": "0.5", "ETHUSDT": "0.5"},
            experiment_hash=experiment.experiment_id,
            strategy_hash=experiment.strategy_hash,
            config_hash=experiment.frozen_config_hash,
            policy_hash=experiment.policy_hash,
            input_hash=_hash(f"{nonce}:input"),
        )
    )
    await session.flush()

    config_hash = config.config_hash()
    session.add(
        EvaluationConfigDb(
            config_hash=config_hash,
            schema_id="paper_evaluation_config.v1",
            formula_version="v1",
            currency_conversion_policy="none",
            payload=json.loads(config.model_dump_json()),
        )
    )
    await session.flush()

    session.add(
        EvaluationEpoch(
            epoch_id=epoch_id,
            cohort_id=cohort_id,
            config_hash=config_hash,
            initial_equity={
                ViewName.BINANCE_BROKER.value: str(
                    config.initial_equity[ViewName.BINANCE_BROKER]
                ),
                ViewName.ALPACA_BROKER.value: str(
                    config.initial_equity[ViewName.ALPACA_BROKER]
                ),
                ViewName.CANONICAL_SHADOW.value: str(
                    config.initial_equity[ViewName.CANONICAL_SHADOW]
                ),
            },
            started_at=started_at,
            experiment_hash=experiment_hash,
            cohort_hash=cohort_hash,
        )
    )
    await session.flush()
    await session.commit()
    return cohort_id, epoch_id


def _make_epoch(
    *,
    epoch_id: str,
    cohort_id: str,
    config: EvaluationConfig,
    started_at: datetime,
) -> EpochIdentity:
    return EpochIdentity.model_construct(
        epoch_id=epoch_id,
        cohort_id=cohort_id,
        config_hash=config.config_hash(),
        initial_equity={
            ViewName.BINANCE_BROKER: config.initial_equity[ViewName.BINANCE_BROKER],
            ViewName.ALPACA_BROKER: config.initial_equity[ViewName.ALPACA_BROKER],
            ViewName.CANONICAL_SHADOW: config.initial_equity[
                ViewName.CANONICAL_SHADOW
            ],
        },
        started_at=started_at,
        reset_reason=None,
        prior_epoch_id=None,
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_same_retry_returns_same_verdict(db_session: AsyncSession) -> None:
    """Replay with the same idempotency_key and request → same verdict."""
    nonce = uuid4().hex
    started = datetime(2026, 1, 1, tzinfo=UTC)
    experiment_hash = _hash(f"{nonce}:exp")
    cohort_hash = _hash(f"{nonce}:cohort")
    config = _make_unique_config(nonce, min_observations=2, min_fills=1)

    cohort_id, epoch_id = await _setup_db(
        db_session,
        nonce=nonce,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        started_at=started,
    )
    epoch = _make_epoch(
        epoch_id=epoch_id, cohort_id=cohort_id, config=config, started_at=started
    )
    binance_r, alpaca_r, snap_r = _build_readers(cohort_id=cohort_id)
    service = PaperEvaluationService(
        db_session,
        binance_reader=binance_r,
        alpaca_reader=alpaca_r,
        snapshot_reader=snap_r,
    )
    idem_key = f"idem-{nonce}"
    evaluated_at = datetime(2026, 1, 10, tzinfo=UTC)

    verdict1 = await service.evaluate(
        epoch=epoch,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        idempotency_key=idem_key,
        evaluated_at=evaluated_at,
    )
    await db_session.commit()

    # Replay in a fresh session — the idempotency check reads the
    # committed row and returns it without re-computing.
    async with AsyncSessionLocal() as session2:
        service2 = PaperEvaluationService(
            session2,
            binance_reader=binance_r,
            alpaca_reader=alpaca_r,
            snapshot_reader=snap_r,
        )
        verdict2 = await service2.evaluate(
            epoch=epoch,
            config=config,
            experiment_hash=experiment_hash,
            cohort_hash=cohort_hash,
            idempotency_key=idem_key,
            evaluated_at=evaluated_at,
        )

    assert isinstance(verdict1, ScorecardVerdict)
    assert isinstance(verdict2, ScorecardVerdict)
    assert verdict1.status == verdict2.status
    assert verdict1.evidence_ids == verdict2.evidence_ids
    assert verdict1.reason_code == verdict2.reason_code

    # Only one verdict row in the DB.
    count = await db_session.scalar(
        select(func.count())
        .select_from(EvaluationVerdict)
        .where(EvaluationVerdict.epoch_id == epoch_id)
    )
    assert count == 1


@pytest.mark.asyncio
async def test_conflicting_retry_raises_idempotency_conflict(
    db_session: AsyncSession,
) -> None:
    """Same idempotency_key with a different request → idempotency_conflict."""
    from app.services.paper_evaluation.service import _compute_request_hash

    nonce = uuid4().hex
    started = datetime(2026, 1, 1, tzinfo=UTC)
    experiment_hash = _hash(f"{nonce}:exp")
    cohort_hash = _hash(f"{nonce}:cohort")
    config = _make_unique_config(nonce, min_observations=2, min_fills=1)

    cohort_id, epoch_id = await _setup_db(
        db_session,
        nonce=nonce,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        started_at=started,
    )

    epoch1 = _make_epoch(
        epoch_id=epoch_id, cohort_id=cohort_id, config=config, started_at=started
    )

    binance_r, alpaca_r, snap_r = _build_readers(cohort_id=cohort_id)
    service = PaperEvaluationService(
        db_session,
        binance_reader=binance_r,
        alpaca_reader=alpaca_r,
        snapshot_reader=snap_r,
    )
    idem_key = f"idem-{nonce}"
    evaluated_at = datetime(2026, 1, 10, tzinfo=UTC)

    # First evaluation succeeds.
    await service.evaluate(
        epoch=epoch1,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        idempotency_key=idem_key,
        evaluated_at=evaluated_at,
    )
    await db_session.commit()

    # Second evaluation: SAME epoch_id + SAME idempotency_key but DIFFERENT
    # config_hash in EpochIdentity → different request_hash → conflict.
    epoch2 = EpochIdentity.model_construct(
        epoch_id=epoch_id,
        cohort_id=cohort_id,
        config_hash=_hash(f"{nonce}:stale_config"),
        initial_equity={
            ViewName.BINANCE_BROKER: config.initial_equity[ViewName.BINANCE_BROKER],
            ViewName.ALPACA_BROKER: config.initial_equity[ViewName.ALPACA_BROKER],
            ViewName.CANONICAL_SHADOW: config.initial_equity[
                ViewName.CANONICAL_SHADOW
            ],
        },
        started_at=started,
        reset_reason=None,
        prior_epoch_id=None,
    )

    # Sanity: same epoch_id but different config_hash → different request_hash.
    rh1 = _compute_request_hash(epoch=epoch1, idempotency_key=idem_key)
    rh2 = _compute_request_hash(epoch=epoch2, idempotency_key=idem_key)
    assert rh1 != rh2

    with pytest.raises(EvaluationConfigError) as exc_info:
        await service.evaluate(
            epoch=epoch2,
            config=config,
            experiment_hash=experiment_hash,
            cohort_hash=cohort_hash,
            idempotency_key=idem_key,
            evaluated_at=evaluated_at,
        )
    assert exc_info.value.reason_code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_concurrent_different_idempotency_keys_one_winner(
    db_session: AsyncSession,
) -> None:
    """Two concurrent calls with different idempotency_keys for the same epoch.

    The ``uq_evaluation_verdict_epoch`` constraint allows only one verdict
    per epoch.  Both workers race to insert; one wins, the other gets
    either the winner's committed verdict (if its idempotency pre-check
    sees it) or a ``concurrent_evaluation_conflict`` error.
    """
    nonce = uuid4().hex
    started = datetime(2026, 1, 1, tzinfo=UTC)
    experiment_hash = _hash(f"{nonce}:exp")
    cohort_hash = _hash(f"{nonce}:cohort")
    config = _make_unique_config(nonce, min_observations=2, min_fills=1)

    cohort_id, epoch_id = await _setup_db(
        db_session,
        nonce=nonce,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        started_at=started,
    )
    epoch = _make_epoch(
        epoch_id=epoch_id, cohort_id=cohort_id, config=config, started_at=started
    )
    evaluated_at = datetime(2026, 1, 10, tzinfo=UTC)
    barrier = asyncio.Barrier(2)

    async def worker(*, idem_key: str) -> ScorecardVerdict:
        async with AsyncSessionLocal() as session:
            binance_r, alpaca_r, snap_r = _build_readers(cohort_id=cohort_id)
            service = PaperEvaluationService(
                session,
                binance_reader=binance_r,
                alpaca_reader=alpaca_r,
                snapshot_reader=snap_r,
            )
            await barrier.wait()
            result = await service.evaluate(
                epoch=epoch,
                config=config,
                experiment_hash=experiment_hash,
                cohort_hash=cohort_hash,
                idempotency_key=idem_key,
                evaluated_at=evaluated_at,
            )
            await session.commit()
            return result

    outcomes = await asyncio.gather(
        worker(idem_key=f"idem-{nonce}-a"),
        worker(idem_key=f"idem-{nonce}-b"),
        return_exceptions=True,
    )

    successes = [
        item for item in outcomes if isinstance(item, ScorecardVerdict)
    ]
    conflicts = [
        item
        for item in outcomes
        if isinstance(item, EvaluationConfigError)
    ]

    # Exactly one winner.
    assert len(successes) == 1
    # The loser gets a concurrent_evaluation_conflict (from the unique
    # constraint on epoch_id) or an idempotency-style conflict.
    assert len(conflicts) == 1
    assert conflicts[0].reason_code in (
        "concurrent_evaluation_conflict",
        "idempotency_conflict",
    )

    # Verify only one verdict row exists in the DB.
    async with AsyncSessionLocal() as verify_session:
        count = await verify_session.scalar(
            select(func.count())
            .select_from(EvaluationVerdict)
            .where(EvaluationVerdict.epoch_id == epoch_id)
        )
        assert count == 1


@pytest.mark.asyncio
async def test_concurrent_same_idempotency_key_one_winner(
    db_session: AsyncSession,
) -> None:
    """Two concurrent calls with the SAME idempotency_key — one inserts,
    the other either replays (if it sees the commit) or conflicts on the
    unique constraint.  Either way, exactly one verdict row persists.
    """
    nonce = uuid4().hex
    started = datetime(2026, 1, 1, tzinfo=UTC)
    experiment_hash = _hash(f"{nonce}:exp")
    cohort_hash = _hash(f"{nonce}:cohort")
    config = _make_unique_config(nonce, min_observations=2, min_fills=1)

    cohort_id, epoch_id = await _setup_db(
        db_session,
        nonce=nonce,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        started_at=started,
    )
    epoch = _make_epoch(
        epoch_id=epoch_id, cohort_id=cohort_id, config=config, started_at=started
    )
    evaluated_at = datetime(2026, 1, 10, tzinfo=UTC)
    idem_key = f"idem-{nonce}"
    barrier = asyncio.Barrier(2)

    async def worker() -> ScorecardVerdict:
        async with AsyncSessionLocal() as session:
            binance_r, alpaca_r, snap_r = _build_readers(cohort_id=cohort_id)
            service = PaperEvaluationService(
                session,
                binance_reader=binance_r,
                alpaca_reader=alpaca_r,
                snapshot_reader=snap_r,
            )
            await barrier.wait()
            result = await service.evaluate(
                epoch=epoch,
                config=config,
                experiment_hash=experiment_hash,
                cohort_hash=cohort_hash,
                idempotency_key=idem_key,
                evaluated_at=evaluated_at,
            )
            await session.commit()
            return result

    outcomes = await asyncio.gather(worker(), worker(), return_exceptions=True)

    successes = [
        item for item in outcomes if isinstance(item, ScorecardVerdict)
    ]
    errors = [
        item
        for item in outcomes
        if isinstance(item, (EvaluationConfigError, Exception))
    ]

    # At least one success.
    assert len(successes) >= 1
    # All successful results are equal (same verdict).
    if len(successes) > 1:
        assert all(
            s.evidence_ids == successes[0].evidence_ids for s in successes
        )

    # Exactly one verdict row in the DB.
    async with AsyncSessionLocal() as verify_session:
        count = await verify_session.scalar(
            select(func.count())
            .select_from(EvaluationVerdict)
            .where(EvaluationVerdict.epoch_id == epoch_id)
        )
        assert count == 1

    # Any error must be a recognised conflict, not an unexpected crash.
    for err in errors:
        assert isinstance(err, EvaluationConfigError)
        assert err.reason_code in (
            "concurrent_evaluation_conflict",
            "idempotency_conflict",
        )
