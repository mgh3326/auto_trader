"""ROB-850 end-to-end integration tests for PaperEvaluationService.

These tests exercise the full orchestration path:
    fake readers → 3-view P&L → gates → conjunctive verdict → DB persistence.

All tests use the shared ``db_session`` fixture against ``test_db``.
Prerequisite rows (``PaperValidationCohort``, ``EvaluationConfig`` DB row,
``EvaluationEpoch`` DB row) are created within each test because the
``EvaluationVerdict`` FK chain requires them.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

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
    VerdictStatus,
    ViewCurrency,
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

_STABLE_HASH_A = "a" * 64
_STABLE_HASH_B = "b" * 64


def _hash(label: str) -> str:
    return hashlib.sha256(label.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Fake readers (READ-only — never call any mutation method)
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
    client_order_id: str = "alpaca_coid_1"
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
    content_hash: str = _STABLE_HASH_A
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
# Synthetic data builders
# ---------------------------------------------------------------------------


def _make_binance_row(
    *,
    realized_pnl: str = "100",
    notional: str = "1000",
    row_id: int = 1,
) -> FakeBinanceRow:
    return FakeBinanceRow(
        id=row_id,
        notional_usdt=Decimal(notional),
        extra_metadata={"realized_pnl_usdt": realized_pnl},
    )


def _make_alpaca_pair(
    *,
    symbol: str = "BTC/USD",
    buy_price: str = "50000",
    sell_price: str = "51000",
    qty: str = "1",
    corr_id: str = "cohort_test",
    row_id_start: int = 1,
) -> list[FakeAlpacaRow]:
    """A profitable buy→sell roundtrip pair."""
    return [
        FakeAlpacaRow(
            id=row_id_start,
            record_kind="execution",
            lifecycle_state="filled",
            side="buy",
            currency="USD",
            execution_symbol=symbol,
            filled_qty=Decimal(qty),
            filled_avg_price=Decimal(buy_price),
            client_order_id=f"coid_buy_{row_id_start}",
            lifecycle_correlation_id=corr_id,
        ),
        FakeAlpacaRow(
            id=row_id_start + 1,
            record_kind="execution",
            lifecycle_state="filled",
            side="sell",
            currency="USD",
            execution_symbol=symbol,
            filled_qty=Decimal(qty),
            filled_avg_price=Decimal(sell_price),
            client_order_id=f"coid_sell_{row_id_start + 1}",
            lifecycle_correlation_id=corr_id,
        ),
    ]


def _make_snapshot_payload(
    *,
    btc_close: str = "50000",
    eth_close: str = "3000",
) -> dict[str, Any]:
    return {
        "schema_id": "canonical_market_snapshot.v1",
        "symbols": [
            {
                "symbol": "BTCUSDT",
                "candles": [
                    {
                        "open": "49000",
                        "high": "51000",
                        "low": "48000",
                        "close": btc_close,
                        "base_volume": "100",
                        "quote_volume": "5000000",
                        "trade_count": 1000,
                        "taker_buy_base_volume": "50",
                        "taker_buy_quote_volume": "2500000",
                    }
                ],
                "ticker": {
                    "bid_price": "49990",
                    "bid_qty": "1",
                    "ask_price": "50010",
                    "ask_qty": "1",
                },
            },
            {
                "symbol": "ETHUSDT",
                "candles": [
                    {
                        "open": "2950",
                        "high": "3050",
                        "low": "2900",
                        "close": eth_close,
                        "base_volume": "200",
                        "quote_volume": "600000",
                        "trade_count": 500,
                        "taker_buy_base_volume": "100",
                        "taker_buy_quote_volume": "300000",
                    }
                ],
                "ticker": {
                    "bid_price": "2995",
                    "bid_qty": "1",
                    "ask_price": "3005",
                    "ask_qty": "1",
                },
            },
        ],
    }


def _make_snapshot(
    *,
    btc_close: str = "50000",
    eth_close: str = "3000",
    snap_id: int = 1,
    cohort_id: str = "cohort_test",
) -> FakeMarketSnapshot:
    return FakeMarketSnapshot(
        id=snap_id,
        content_hash=_hash(f"snap_{snap_id}"),
        payload=_make_snapshot_payload(btc_close=btc_close, eth_close=eth_close),
        cohort_id=cohort_id,
        snapshot_id=f"snap_{snap_id}",
    )


# ---------------------------------------------------------------------------
# DB setup helper
# ---------------------------------------------------------------------------


async def _setup_prerequisites(
    session: AsyncSession,
    *,
    nonce: str,
    config: EvaluationConfig,
    experiment_hash: str,
    cohort_hash: str,
    started_at: datetime,
) -> tuple[str, str]:
    """Create PaperValidationCohort + EvaluationConfig + EvaluationEpoch rows.

    The ``PaperValidationCohort`` trigger requires exactly one champion
    assignment, so ``ResearchStrategyExperiment``, ``ResearchBacktestRun``,
    and ``PaperValidationCohortAssignment`` rows are created first.

    Returns ``(cohort_id, epoch_id)``.
    """
    cohort_id = f"cohort-{nonce}"
    epoch_id = f"epoch-{nonce}"

    # Registry rows (FK targets for the assignment).
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
    """Build an EpochIdentity matching the DB epoch row."""
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


def _build_readers(
    *,
    cohort_id: str,
    binance_fill_count: int = 5,
    alpaca_pair_count: int = 3,
    snapshot_count: int = 3,
) -> tuple[
    FakeBinanceLedgerReader, FakeAlpacaLedgerReader, FakeSnapshotReader
]:
    """Build fake readers with synthetic fill data."""
    binance_rows = [
        _make_binance_row(realized_pnl="100", notional="1000", row_id=i)
        for i in range(1, binance_fill_count + 1)
    ]
    binance_reader = FakeBinanceLedgerReader(rows=binance_rows)

    alpaca_rows: list[FakeAlpacaRow] = []
    for i in range(alpaca_pair_count):
        alpaca_rows.extend(
            _make_alpaca_pair(
                buy_price="50000",
                sell_price="51000",
                qty="1",
                corr_id=cohort_id,
                row_id_start=i * 2 + 1,
            )
        )
    alpaca_reader = FakeAlpacaLedgerReader(
        rows_by_correlation={cohort_id: alpaca_rows}
    )

    snapshots = [
        _make_snapshot(snap_id=i, cohort_id=cohort_id)
        for i in range(1, snapshot_count + 1)
    ]
    snapshot_reader = FakeSnapshotReader(snapshots=snapshots)

    return binance_reader, alpaca_reader, snapshot_reader


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_evaluate_returns_verdict_with_three_views_and_correct_currencies(
    db_session: AsyncSession,
) -> None:
    """Full evaluation returns a ScorecardVerdict with all 3 views."""
    nonce = uuid4().hex
    started = datetime(2026, 1, 1, tzinfo=UTC)
    experiment_hash = _hash(f"{nonce}:exp")
    cohort_hash = _hash(f"{nonce}:cohort")
    config = _make_unique_config(nonce, min_observations=2, min_fills=1)

    cohort_id, epoch_id = await _setup_prerequisites(
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

    verdict = await service.evaluate(
        epoch=epoch,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        idempotency_key=f"idem-{nonce}",
        evaluated_at=datetime(2026, 1, 10, tzinfo=UTC),
    )

    # All 3 views present.
    assert set(verdict.view_metrics) == {
        ViewName.BINANCE_BROKER,
        ViewName.ALPACA_BROKER,
        ViewName.CANONICAL_SHADOW,
    }

    # Correct currencies per view.
    assert (
        verdict.view_metrics[ViewName.BINANCE_BROKER].currency
        is ViewCurrency.USDT
    )
    assert (
        verdict.view_metrics[ViewName.ALPACA_BROKER].currency is ViewCurrency.USD
    )
    assert (
        verdict.view_metrics[ViewName.CANONICAL_SHADOW].currency
        is ViewCurrency.USDT
    )

    # No cross-view nominal P&L total field exists on the verdict.
    assert not hasattr(verdict, "total_nominal_pnl")
    assert not hasattr(verdict, "aggregate_nominal_pnl")

    # Evidence IDs are deterministic (non-empty tuple).
    assert len(verdict.evidence_ids) >= 4  # 3 views + 1 aggregate
    for eid in verdict.evidence_ids:
        assert isinstance(eid, str) and len(eid) > 0


@pytest.mark.asyncio
async def test_evaluate_persists_verdict_to_db(
    db_session: AsyncSession,
) -> None:
    """The verdict is persisted to evaluation_verdicts and queryable."""
    nonce = uuid4().hex
    started = datetime(2026, 1, 1, tzinfo=UTC)
    experiment_hash = _hash(f"{nonce}:exp")
    cohort_hash = _hash(f"{nonce}:cohort")
    config = _make_unique_config(nonce, min_observations=2, min_fills=1)

    cohort_id, epoch_id = await _setup_prerequisites(
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
    verdict = await service.evaluate(
        epoch=epoch,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        idempotency_key=idem_key,
        evaluated_at=datetime(2026, 1, 10, tzinfo=UTC),
    )
    await db_session.commit()

    # Query the persisted row.
    result = await db_session.execute(
        select(EvaluationVerdict).where(
            EvaluationVerdict.epoch_id == epoch_id,
            EvaluationVerdict.idempotency_key == idem_key,
        )
    )
    row = result.scalar_one()
    assert row.verdict_status == verdict.status.value
    assert row.config_hash == verdict.config_hash
    assert row.experiment_hash == experiment_hash
    assert row.cohort_hash == cohort_hash


@pytest.mark.asyncio
async def test_evaluate_insufficient_evidence_status(
    db_session: AsyncSession,
) -> None:
    """Missing observations under fail_close → INSUFFICIENT_EVIDENCE."""
    nonce = uuid4().hex
    started = datetime(2026, 1, 1, tzinfo=UTC)
    experiment_hash = _hash(f"{nonce}:exp")
    cohort_hash = _hash(f"{nonce}:cohort")
    # High thresholds that synthetic data won't meet.
    config = _make_unique_config(nonce, min_observations=100, min_fills=50)

    cohort_id, epoch_id = await _setup_prerequisites(
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

    # Minimal data — far below thresholds.
    binance_r = FakeBinanceLedgerReader(rows=[])
    alpaca_r = FakeAlpacaLedgerReader(rows_by_correlation={})
    snap_r = FakeSnapshotReader(snapshots=[])

    service = PaperEvaluationService(
        db_session,
        binance_reader=binance_r,
        alpaca_reader=alpaca_r,
        snapshot_reader=snap_r,
    )

    verdict = await service.evaluate(
        epoch=epoch,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        idempotency_key=f"idem-{nonce}",
        evaluated_at=datetime(2026, 1, 10, tzinfo=UTC),
    )

    assert verdict.status is VerdictStatus.INSUFFICIENT_EVIDENCE
    assert verdict.reason_code == "insufficient_evidence"


@pytest.mark.asyncio
async def test_evaluate_shadow_gate_blocked(
    db_session: AsyncSession,
) -> None:
    """Shadow soak < 7 days with sufficient data → GATE_BLOCKED."""
    nonce = uuid4().hex
    started = datetime(2026, 1, 1, tzinfo=UTC)
    experiment_hash = _hash(f"{nonce}:exp")
    cohort_hash = _hash(f"{nonce}:cohort")
    config = _make_unique_config(nonce, min_observations=2, min_fills=1)

    cohort_id, epoch_id = await _setup_prerequisites(
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

    # shadow_started_at only 3 days before evaluated_at → < 7 days.
    verdict = await service.evaluate(
        epoch=epoch,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        idempotency_key=f"idem-{nonce}",
        shadow_started_at=datetime(2026, 1, 7, tzinfo=UTC),
        evaluated_at=datetime(2026, 1, 10, tzinfo=UTC),
    )

    assert verdict.status is VerdictStatus.GATE_BLOCKED
    assert verdict.reason_code == "shadow_gate_blocked"
    assert verdict.shadow_gate is not None
    assert verdict.shadow_gate.passed is False


@pytest.mark.asyncio
async def test_evaluate_benchmark_not_beaten(
    db_session: AsyncSession,
) -> None:
    """Shadow view cost-only P&L < 0 → BENCHMARK_NOT_BEATEN.

    The canonical shadow view applies fees/spread/slippage costs without
    price-appreciation gains in the current V1 P&L model, so its
    benchmark delta is always negative when snapshots are present.
    With the shadow gate passed (8 days), the verdict proceeds past the
    gate to the benchmark check and fails there.
    """
    nonce = uuid4().hex
    started = datetime(2026, 1, 1, tzinfo=UTC)
    experiment_hash = _hash(f"{nonce}:exp")
    cohort_hash = _hash(f"{nonce}:cohort")
    config = _make_unique_config(nonce, min_observations=2, min_fills=1)

    cohort_id, epoch_id = await _setup_prerequisites(
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

    # Shadow started 8 days before evaluation → passes 7-day gate.
    verdict = await service.evaluate(
        epoch=epoch,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        idempotency_key=f"idem-{nonce}",
        shadow_started_at=datetime(2026, 1, 1, tzinfo=UTC),
        evaluated_at=datetime(2026, 1, 9, tzinfo=UTC),
    )

    assert verdict.status is VerdictStatus.BENCHMARK_NOT_BEATEN
    assert "benchmark_not_beaten" in verdict.reason_code
    # Shadow gate should have passed.
    assert verdict.shadow_gate is not None
    assert verdict.shadow_gate.passed is True
    # The shadow view should have negative delta (costs).
    shadow_delta = verdict.view_metrics[
        ViewName.CANONICAL_SHADOW
    ].btc_eth_benchmark_delta_pct
    assert shadow_delta <= 0


@pytest.mark.asyncio
async def test_evaluate_replay_returns_same_verdict(
    db_session: AsyncSession,
) -> None:
    """Calling evaluate twice with the same idempotency_key returns the same verdict."""
    nonce = uuid4().hex
    started = datetime(2026, 1, 1, tzinfo=UTC)
    experiment_hash = _hash(f"{nonce}:exp")
    cohort_hash = _hash(f"{nonce}:cohort")
    config = _make_unique_config(nonce, min_observations=2, min_fills=1)

    cohort_id, epoch_id = await _setup_prerequisites(
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
    verdict1 = await service.evaluate(
        epoch=epoch,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        idempotency_key=idem_key,
        evaluated_at=datetime(2026, 1, 10, tzinfo=UTC),
    )
    await db_session.commit()

    # Second call — same idempotency_key, same request.
    verdict2 = await service.evaluate(
        epoch=epoch,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        idempotency_key=idem_key,
        evaluated_at=datetime(2026, 1, 10, tzinfo=UTC),
    )

    assert verdict1.status == verdict2.status
    assert verdict1.evidence_ids == verdict2.evidence_ids
    assert verdict1.reason_code == verdict2.reason_code


@pytest.mark.asyncio
async def test_evaluate_evidence_ids_are_deterministic(
    db_session: AsyncSession,
) -> None:
    """Evidence IDs are deterministic across two evaluations of identical data."""
    nonce = uuid4().hex
    started = datetime(2026, 1, 1, tzinfo=UTC)
    experiment_hash = _hash(f"{nonce}:exp")
    cohort_hash = _hash(f"{nonce}:cohort")
    config = _make_unique_config(nonce, min_observations=2, min_fills=1)

    cohort_id, epoch_id = await _setup_prerequisites(
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
    verdict = await service.evaluate(
        epoch=epoch,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        idempotency_key=idem_key,
        evaluated_at=datetime(2026, 1, 10, tzinfo=UTC),
    )
    await db_session.commit()

    # Replay — evidence IDs should match.
    replay = await service.evaluate(
        epoch=epoch,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        idempotency_key=idem_key,
        evaluated_at=datetime(2026, 1, 10, tzinfo=UTC),
    )
    assert replay.evidence_ids == verdict.evidence_ids


@pytest.mark.asyncio
async def test_evaluate_idempotency_conflict_raises(
    db_session: AsyncSession,
) -> None:
    """Same idempotency_key with a different request → EvaluationConfigError."""
    from app.services.paper_evaluation.service import _compute_request_hash

    nonce = uuid4().hex
    started = datetime(2026, 1, 1, tzinfo=UTC)
    experiment_hash = _hash(f"{nonce}:exp")
    cohort_hash = _hash(f"{nonce}:cohort")
    config = _make_unique_config(nonce, min_observations=2, min_fills=1)

    cohort_id, epoch_id = await _setup_prerequisites(
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
    # First evaluation.
    await service.evaluate(
        epoch=epoch,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        idempotency_key=idem_key,
        evaluated_at=datetime(2026, 1, 10, tzinfo=UTC),
    )
    await db_session.commit()

    # Second call: SAME epoch_id + SAME idempotency_key but DIFFERENT
    # config_hash in the EpochIdentity → different request_hash → conflict.
    # This simulates a caller error: reusing an idempotency_key with a
    # stale or inconsistent epoch identity.
    epoch_conflict = EpochIdentity.model_construct(
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

    # Sanity: the two epochs produce different request hashes.
    rh1 = _compute_request_hash(epoch=epoch, idempotency_key=idem_key)
    rh2 = _compute_request_hash(epoch=epoch_conflict, idempotency_key=idem_key)
    assert rh1 != rh2

    with pytest.raises(EvaluationConfigError) as exc_info:
        await service.evaluate(
            epoch=epoch_conflict,
            config=config,
            experiment_hash=experiment_hash,
            cohort_hash=cohort_hash,
            idempotency_key=idem_key,
            evaluated_at=datetime(2026, 1, 10, tzinfo=UTC),
        )
    assert exc_info.value.reason_code == "idempotency_conflict"


@pytest.mark.asyncio
async def test_evaluate_no_usdt_usd_conversion(
    db_session: AsyncSession,
) -> None:
    """No USDT/USD conversion — each view keeps its native currency."""
    nonce = uuid4().hex
    started = datetime(2026, 1, 1, tzinfo=UTC)
    experiment_hash = _hash(f"{nonce}:exp")
    cohort_hash = _hash(f"{nonce}:cohort")
    config = _make_unique_config(nonce, min_observations=2, min_fills=1)

    cohort_id, epoch_id = await _setup_prerequisites(
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

    verdict = await service.evaluate(
        epoch=epoch,
        config=config,
        experiment_hash=experiment_hash,
        cohort_hash=cohort_hash,
        idempotency_key=f"idem-{nonce}",
        evaluated_at=datetime(2026, 1, 10, tzinfo=UTC),
    )

    binance_m = verdict.view_metrics[ViewName.BINANCE_BROKER]
    alpaca_m = verdict.view_metrics[ViewName.ALPACA_BROKER]
    shadow_m = verdict.view_metrics[ViewName.CANONICAL_SHADOW]

    # Binance and shadow are USDT; Alpaca is USD. They are never mixed.
    assert binance_m.currency is ViewCurrency.USDT
    assert shadow_m.currency is ViewCurrency.USDT
    assert alpaca_m.currency is ViewCurrency.USD

    # Binance nominal P&L is in USDT, Alpaca in USD — they are never
    # summed or converted.
    assert binance_m.nominal_net_pnl > 0  # positive realized P&L
    assert alpaca_m.nominal_net_pnl > 0  # profitable roundtrip
    assert shadow_m.nominal_net_pnl <= 0  # costs only
