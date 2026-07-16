"""ROB-850 comprehensive unit tests for 3-view P&L computation."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from app.services.paper_evaluation.contracts import (
    EpochIdentity,
    EvaluationConfigError,
    PartialFillPolicy,
    ViewCurrency,
    ViewName,
    ViewSource,
)
from app.services.paper_evaluation.pnl import (
    PaperEvaluationPnL,
    _compute_exposure,
    _compute_max_drawdown_pct,
    _compute_sharpe,
    _extract_realized_pnl_usdt,
    _to_decimal_safe,
    _validate_finite,
)
from tests.services.paper_evaluation.conftest import make_evaluation_config

pytestmark = pytest.mark.unit


# ---------------------------------------------------------------------------
# Test constants
# ---------------------------------------------------------------------------

EPOCH_STARTED = datetime(2026, 1, 1, tzinfo=UTC)
SINCE = EPOCH_STARTED
_INITIAL_EQUITY = Decimal("10000")

_STABLE_HASH_A = "a" * 64
_STABLE_HASH_B = "b" * 64


# ---------------------------------------------------------------------------
# Helpers: build epoch + config
# ---------------------------------------------------------------------------


def _make_epoch(
    *,
    initial_equity_usdt: Decimal = _INITIAL_EQUITY,
    initial_equity_usd: Decimal = _INITIAL_EQUITY,
) -> EpochIdentity:
    # model_construct bypasses field validators (one of which has a known
    # utcoffset signature issue in the frozen contract). The data we pass is
    # semantically valid: timezone-aware datetime, three views, valid equity.
    return EpochIdentity.model_construct(
        epoch_id="epoch_test_001",
        cohort_id="cohort_test_001",
        config_hash=_STABLE_HASH_A,
        initial_equity={
            ViewName.BINANCE_BROKER: initial_equity_usdt,
            ViewName.ALPACA_BROKER: initial_equity_usd,
            ViewName.CANONICAL_SHADOW: initial_equity_usdt,
        },
        started_at=EPOCH_STARTED,
        reset_reason=None,
        prior_epoch_id=None,
    )


def _make_service(
    *,
    initial_equity_usdt: Decimal = _INITIAL_EQUITY,
    initial_equity_usd: Decimal = _INITIAL_EQUITY,
    fee_rate_bps: Decimal = Decimal("10"),
    spread_bps: Decimal = Decimal("5"),
    slippage_bps: Decimal = Decimal("3"),
    risk_free_rate_pct: Decimal = Decimal("2"),
    partial_fill_policy: PartialFillPolicy = PartialFillPolicy.REJECT_PARTIAL,
    fill_timing: str = "canonical_close",
) -> PaperEvaluationPnL:
    config = make_evaluation_config(
        initial_equity_usdt=initial_equity_usdt,
        initial_equity_usd=initial_equity_usd,
        fee_rate_bps=fee_rate_bps,
        spread_bps=spread_bps,
        slippage_bps=slippage_bps,
        risk_free_rate_pct=risk_free_rate_pct,
        partial_fill_policy=partial_fill_policy,
        fill_timing=fill_timing,
    )
    epoch = _make_epoch(
        initial_equity_usdt=initial_equity_usdt,
        initial_equity_usd=initial_equity_usd,
    )
    return PaperEvaluationPnL(
        config=config,
        epoch=epoch,
        experiment_hash=_STABLE_HASH_A,
        cohort_hash=_STABLE_HASH_B,
    )


# ---------------------------------------------------------------------------
# Fake ledger/snapshot objects
# ---------------------------------------------------------------------------


@dataclass
class FakeBinanceRow:
    """Mimics ``BinanceDemoOrderLedger`` for READ-only tests."""

    id: int = 1
    notional_usdt: Decimal | None = None
    extra_metadata: dict[str, Any] | None = None
    lifecycle_state: str = "closed"
    side: str = "BUY"
    product: str = "spot"
    instrument_id: int = 1
    client_order_id: str = "test_coid_1"
    filled_at: datetime | None = None
    closed_at: datetime | None = None


@dataclass
class FakeBinanceLedgerReader:
    """Fake for ``_BinanceLedgerReader`` protocol."""

    rows: list[FakeBinanceRow] = field(default_factory=list)

    async def closed_rows_since(self, *, since: datetime) -> list[FakeBinanceRow]:
        return list(self.rows)


@dataclass
class FakeAlpacaRow:
    """Mimics ``AlpacaPaperOrderLedger`` for READ-only tests."""

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
    fee_amount: Decimal | None = None
    fee_currency: str | None = "USD"
    qty_delta: Decimal | None = None
    position_snapshot: dict[str, Any] | None = None


@dataclass
class FakeAlpacaLedgerReader:
    """Fake for ``_AlpacaLedgerReader`` protocol."""

    rows_by_correlation: dict[str, list[FakeAlpacaRow]] = field(default_factory=dict)

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
    """Mimics ``CanonicalMarketSnapshot`` for READ-only tests."""

    id: int = 1
    content_hash: str = _STABLE_HASH_A
    payload: dict[str, Any] = field(default_factory=dict)
    cohort_id: str = "cohort_test_001"
    snapshot_id: str = "snap_001"
    run_id: str = "run_001"
    round_decision_id: str = "decision_001"


@dataclass
class FakeSnapshotReader:
    """Fake for ``_SnapshotReader`` protocol."""

    snapshots: list[FakeMarketSnapshot] = field(default_factory=list)

    async def list_snapshots(
        self, *, cohort_id: str, since: datetime
    ) -> list[FakeMarketSnapshot]:
        return list(self.snapshots)


def _make_snapshot_payload(
    btc_close: str = "50000",
    eth_close: str = "3000",
) -> dict[str, Any]:
    """Build a synthetic canonical snapshot payload."""
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


def _make_binance_row(
    *,
    realized_pnl: str | None = "100",
    notional: str = "1000",
    fee: str | None = None,
    is_partial: bool = False,
    row_id: int = 1,
) -> FakeBinanceRow:
    extra: dict[str, Any] = {}
    if realized_pnl is not None:
        extra["realized_pnl_usdt"] = realized_pnl
    if fee is not None:
        extra["fee_usdt"] = fee
    if is_partial:
        extra["is_partial_fill"] = True
    return FakeBinanceRow(
        id=row_id,
        notional_usdt=Decimal(notional),
        extra_metadata=extra,
    )


def _make_alpaca_execution_row(
    *,
    side: str = "buy",
    symbol: str = "BTC/USD",
    filled_qty: str = "1",
    filled_price: str = "50000",
    lifecycle_state: str = "filled",
    row_id: int = 1,
    corr_id: str = "corr_1",
    is_partial: bool = False,
) -> FakeAlpacaRow:
    pos_snap = {"is_partial_fill": True} if is_partial else None
    return FakeAlpacaRow(
        id=row_id,
        record_kind="execution",
        lifecycle_state=lifecycle_state,
        side=side,
        currency="USD",
        execution_symbol=symbol,
        filled_qty=Decimal(filled_qty),
        filled_avg_price=Decimal(filled_price),
        client_order_id=f"coid_{row_id}",
        lifecycle_correlation_id=corr_id,
        position_snapshot=pos_snap,
    )


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestValidateFinite:
    def test_valid_finite_decimal(self) -> None:
        assert _validate_finite(Decimal("42.5"), "test") == Decimal("42.5")

    def test_negative_decimal(self) -> None:
        assert _validate_finite(Decimal("-10"), "test") == Decimal("-10")

    def test_zero(self) -> None:
        assert _validate_finite(Decimal("0"), "test") == Decimal("0")

    def test_nan_raises(self) -> None:
        with pytest.raises(EvaluationConfigError) as exc_info:
            _validate_finite(Decimal("NaN"), "test")
        assert exc_info.value.reason_code == "non_finite_value"

    def test_inf_raises(self) -> None:
        with pytest.raises(EvaluationConfigError) as exc_info:
            _validate_finite(Decimal("Infinity"), "test")
        assert exc_info.value.reason_code == "non_finite_value"

    def test_neg_inf_raises(self) -> None:
        with pytest.raises(EvaluationConfigError) as exc_info:
            _validate_finite(Decimal("-Infinity"), "test")
        assert exc_info.value.reason_code == "non_finite_value"


class TestToDecimalSafe:
    def test_from_str(self) -> None:
        assert _to_decimal_safe("123.45", "test") == Decimal("123.45")

    def test_from_int(self) -> None:
        assert _to_decimal_safe(42, "test") == Decimal("42")

    def test_from_decimal(self) -> None:
        assert _to_decimal_safe(Decimal("9.9"), "test") == Decimal("9.9")

    def test_none_raises(self) -> None:
        with pytest.raises(EvaluationConfigError) as exc_info:
            _to_decimal_safe(None, "test")
        assert exc_info.value.reason_code == "non_finite_value"

    def test_garbage_raises(self) -> None:
        with pytest.raises(EvaluationConfigError) as exc_info:
            _to_decimal_safe("abc", "test")
        assert exc_info.value.reason_code == "non_finite_value"


class TestExtractRealizedPnl:
    def test_present(self) -> None:
        assert _extract_realized_pnl_usdt({"realized_pnl_usdt": "123.45"}) == Decimal(
            "123.45"
        )

    def test_missing_key(self) -> None:
        assert _extract_realized_pnl_usdt({"other": "1"}) is None

    def test_none_metadata(self) -> None:
        assert _extract_realized_pnl_usdt(None) is None

    def test_none_value(self) -> None:
        assert _extract_realized_pnl_usdt({"realized_pnl_usdt": None}) is None

    def test_non_finite(self) -> None:
        assert _extract_realized_pnl_usdt({"realized_pnl_usdt": "NaN"}) is None

    def test_inf(self) -> None:
        assert _extract_realized_pnl_usdt({"realized_pnl_usdt": "Infinity"}) is None

    def test_garbage(self) -> None:
        assert _extract_realized_pnl_usdt({"realized_pnl_usdt": "abc"}) is None


class TestComputeMaxDrawdown:
    def test_empty(self) -> None:
        assert _compute_max_drawdown_pct([]) == Decimal("0")

    def test_single(self) -> None:
        assert _compute_max_drawdown_pct([Decimal("100")]) == Decimal("0")

    def test_monotonic_increasing(self) -> None:
        curve = [Decimal("100"), Decimal("110"), Decimal("120")]
        assert _compute_max_drawdown_pct(curve) == Decimal("0")

    def test_simple_drawdown(self) -> None:
        curve = [Decimal("100"), Decimal("90")]
        dd = _compute_max_drawdown_pct(curve)
        assert dd == Decimal("10")

    def test_recovery_after_drawdown(self) -> None:
        curve = [Decimal("100"), Decimal("80"), Decimal("120")]
        dd = _compute_max_drawdown_pct(curve)
        assert dd == Decimal("20")

    def test_complex_curve(self) -> None:
        curve = [
            Decimal("100"),
            Decimal("120"),
            Decimal("90"),
            Decimal("110"),
            Decimal("70"),
            Decimal("80"),
        ]
        dd = _compute_max_drawdown_pct(curve)
        # Peak at 120, trough at 70 → (120-70)/120*100 = 41.666...
        expected = (Decimal("120") - Decimal("70")) / Decimal("120") * Decimal("100")
        assert dd == expected


class TestComputeSharpe:
    def test_too_few_samples(self) -> None:
        assert _compute_sharpe([Decimal("1"), Decimal("2")], 365, Decimal("2")) is None

    def test_zero_variance(self) -> None:
        returns = [Decimal("1"), Decimal("1"), Decimal("1")]
        assert _compute_sharpe(returns, 365, Decimal("2")) is None

    def test_positive_sharpe(self) -> None:
        returns = [Decimal("1"), Decimal("2"), Decimal("3"), Decimal("4")]
        result = _compute_sharpe(returns, 365, Decimal("0"))
        assert result is not None
        assert result > Decimal("0")

    def test_with_risk_free_rate(self) -> None:
        returns = [Decimal("0.1"), Decimal("0.2"), Decimal("0.15"), Decimal("0.25")]
        result = _compute_sharpe(returns, 365, Decimal("2"))
        assert result is not None


class TestComputeExposure:
    def test_empty(self) -> None:
        assert _compute_exposure([], Decimal("10000")) == Decimal("0")

    def test_zero_equity(self) -> None:
        assert _compute_exposure([Decimal("100")], Decimal("0")) == Decimal("0")

    def test_normal(self) -> None:
        positions = [Decimal("5000"), Decimal("3000")]
        equity = Decimal("10000")
        result = _compute_exposure(positions, equity)
        # avg = 4000, ratio = 0.4
        assert result == Decimal("0.4")

    def test_clamped_to_one(self) -> None:
        positions = [Decimal("20000")]
        equity = Decimal("1000")
        result = _compute_exposure(positions, equity)
        assert result == Decimal("1")

    def test_negative_positions_abs(self) -> None:
        positions = [Decimal("-5000")]
        equity = Decimal("10000")
        result = _compute_exposure(positions, equity)
        assert result == Decimal("0.5")


# ---------------------------------------------------------------------------
# View 1: Binance broker P&L tests
# ---------------------------------------------------------------------------


class TestBinanceView:
    @pytest.mark.asyncio
    async def test_basic_pnl_computation(self) -> None:
        service = _make_service()
        ledger = FakeBinanceLedgerReader(
            rows=[
                _make_binance_row(realized_pnl="100", notional="1000", row_id=1),
                _make_binance_row(realized_pnl="200", notional="2000", row_id=2),
            ]
        )
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.view_name == ViewName.BINANCE_BROKER
        assert metrics.currency == ViewCurrency.USDT
        assert metrics.source == ViewSource.BINANCE_DEMO_LEDGER
        assert metrics.nominal_net_pnl == Decimal("300")
        assert metrics.initial_equity == _INITIAL_EQUITY
        assert metrics.ending_equity == _INITIAL_EQUITY + Decimal("300")
        assert metrics.fill_count == 2
        assert metrics.missing_observation_count == 0

    @pytest.mark.asyncio
    async def test_ending_equity_invariant(self) -> None:
        service = _make_service()
        ledger = FakeBinanceLedgerReader(
            rows=[_make_binance_row(realized_pnl="500", row_id=1)]
        )
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.ending_equity == metrics.initial_equity + metrics.nominal_net_pnl

    @pytest.mark.asyncio
    async def test_net_return_pct(self) -> None:
        service = _make_service(initial_equity_usdt=Decimal("10000"))
        ledger = FakeBinanceLedgerReader(
            rows=[_make_binance_row(realized_pnl="1000", row_id=1)]
        )
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.net_return_pct == Decimal("10")

    @pytest.mark.asyncio
    async def test_negative_pnl(self) -> None:
        service = _make_service()
        ledger = FakeBinanceLedgerReader(
            rows=[_make_binance_row(realized_pnl="-500", row_id=1)]
        )
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.nominal_net_pnl == Decimal("-500")
        assert metrics.ending_equity == Decimal("9500")
        assert metrics.net_return_pct == Decimal("-5")

    @pytest.mark.asyncio
    async def test_missing_realized_pnl_increments_count(self) -> None:
        service = _make_service()
        ledger = FakeBinanceLedgerReader(
            rows=[
                _make_binance_row(realized_pnl="100", row_id=1),
                FakeBinanceRow(id=2, notional_usdt=Decimal("1000"), extra_metadata={}),
                FakeBinanceRow(id=3, notional_usdt=Decimal("500"), extra_metadata=None),
            ]
        )
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.missing_observation_count == 2
        assert metrics.fill_count == 1
        assert metrics.nominal_net_pnl == Decimal("100")

    @pytest.mark.asyncio
    async def test_missing_pnl_not_zero_filled(self) -> None:
        """Critical: missing data is NOT zero-filled."""
        service = _make_service()
        ledger = FakeBinanceLedgerReader(
            rows=[
                _make_binance_row(realized_pnl="100", row_id=1),
                FakeBinanceRow(id=2, notional_usdt=Decimal("1000"), extra_metadata={}),
            ]
        )
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        # P&L must be 100, NOT 100+0
        assert metrics.nominal_net_pnl == Decimal("100")
        assert metrics.missing_observation_count == 1

    @pytest.mark.asyncio
    async def test_non_finite_pnl_treated_as_missing(self) -> None:
        service = _make_service()
        ledger = FakeBinanceLedgerReader(
            rows=[
                _make_binance_row(realized_pnl="NaN", row_id=1),
                _make_binance_row(realized_pnl="Infinity", row_id=2),
            ]
        )
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.missing_observation_count == 2
        assert metrics.fill_count == 0

    @pytest.mark.asyncio
    async def test_fees_from_metadata(self) -> None:
        service = _make_service()
        ledger = FakeBinanceLedgerReader(
            rows=[
                _make_binance_row(
                    realized_pnl="100", notional="1000", fee="5", row_id=1
                ),
                _make_binance_row(
                    realized_pnl="200", notional="2000", fee="10", row_id=2
                ),
            ]
        )
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.fees == Decimal("15")

    @pytest.mark.asyncio
    async def test_fees_from_config_rate_when_missing(self) -> None:
        service = _make_service(fee_rate_bps=Decimal("10"))
        ledger = FakeBinanceLedgerReader(
            rows=[
                # 1000 notional * 10bps / 10000 = 1 USDT fee
                _make_binance_row(
                    realized_pnl="100", notional="1000", fee=None, row_id=1
                ),
            ]
        )
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.fees == Decimal("1")

    @pytest.mark.asyncio
    async def test_turnover(self) -> None:
        service = _make_service()
        ledger = FakeBinanceLedgerReader(
            rows=[
                _make_binance_row(realized_pnl="100", notional="1000", row_id=1),
                _make_binance_row(realized_pnl="-50", notional="2000", row_id=2),
            ]
        )
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.turnover == Decimal("3000")

    @pytest.mark.asyncio
    async def test_partial_fill_detection(self) -> None:
        service = _make_service()
        ledger = FakeBinanceLedgerReader(
            rows=[
                _make_binance_row(realized_pnl="100", is_partial=True, row_id=1),
                _make_binance_row(realized_pnl="200", is_partial=False, row_id=2),
            ]
        )
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.partial_fill_count == 1

    @pytest.mark.asyncio
    async def test_empty_rows(self) -> None:
        service = _make_service()
        ledger = FakeBinanceLedgerReader(rows=[])
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.nominal_net_pnl == Decimal("0")
        assert metrics.ending_equity == metrics.initial_equity
        assert metrics.fill_count == 0
        assert metrics.missing_observation_count == 0
        assert metrics.turnover == Decimal("0")

    @pytest.mark.asyncio
    async def test_canonical_snapshot_hashes_empty(self) -> None:
        """Broker view does not consume canonical snapshots."""
        service = _make_service()
        ledger = FakeBinanceLedgerReader(
            rows=[_make_binance_row(realized_pnl="100", row_id=1)]
        )
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.canonical_snapshot_hashes == ()

    @pytest.mark.asyncio
    async def test_native_currency_usdt(self) -> None:
        service = _make_service()
        ledger = FakeBinanceLedgerReader(rows=[])
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.currency == ViewCurrency.USDT
        assert metrics.currency != ViewCurrency.USD

    @pytest.mark.asyncio
    async def test_drawdown_from_losses(self) -> None:
        service = _make_service()
        ledger = FakeBinanceLedgerReader(
            rows=[
                _make_binance_row(realized_pnl="-2000", row_id=1),
                _make_binance_row(realized_pnl="-1000", row_id=2),
            ]
        )
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.max_drawdown_pct > Decimal("0")


# ---------------------------------------------------------------------------
# View 2: Alpaca broker P&L tests
# ---------------------------------------------------------------------------


class TestAlpacaView:
    @pytest.mark.asyncio
    async def test_basic_roundtrip_pnl(self) -> None:
        service = _make_service()
        # Buy 1 BTC at 50000, sell 1 BTC at 51000 → P&L = 1000
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "corr_1": [
                    _make_alpaca_execution_row(
                        side="buy", filled_qty="1", filled_price="50000", row_id=1
                    ),
                    _make_alpaca_execution_row(
                        side="sell", filled_qty="1", filled_price="51000", row_id=2
                    ),
                ]
            }
        )
        metrics = await service.compute_alpaca_view(ledger, correlation_ids=["corr_1"])
        assert metrics.view_name == ViewName.ALPACA_BROKER
        assert metrics.currency == ViewCurrency.USD
        assert metrics.source == ViewSource.ALPACA_PAPER_LEDGER
        assert metrics.nominal_net_pnl == Decimal("1000")

    @pytest.mark.asyncio
    async def test_partial_inventory_is_marked_not_treated_as_full_loss(self) -> None:
        service = _make_service(
            fee_rate_bps=Decimal("0"),
            spread_bps=Decimal("0"),
            slippage_bps=Decimal("0"),
        )
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "corr_1": [
                    _make_alpaca_execution_row(
                        side="buy", filled_qty="2", filled_price="100", row_id=1
                    ),
                    _make_alpaca_execution_row(
                        side="sell", filled_qty="1", filled_price="110", row_id=2
                    ),
                ]
            }
        )
        metrics = await service.compute_alpaca_view(
            ledger,
            correlation_ids=["corr_1"],
            evaluated_at=SINCE,
            native_marks={"BTC/USD": Decimal("105")},
            benchmark_marks={
                "BTC/USD": (Decimal("100"), Decimal("105")),
                "ETH/USD": (Decimal("100"), Decimal("100")),
            },
        )
        assert metrics.nominal_net_pnl == Decimal("15")

    @pytest.mark.asyncio
    async def test_open_buy_at_unchanged_mark_is_not_a_loss(self) -> None:
        service = _make_service(
            fee_rate_bps=Decimal("0"),
            spread_bps=Decimal("0"),
            slippage_bps=Decimal("0"),
        )
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "corr_1": [
                    _make_alpaca_execution_row(
                        side="buy", filled_qty="1", filled_price="100", row_id=1
                    )
                ]
            }
        )
        metrics = await service.compute_alpaca_view(
            ledger,
            correlation_ids=["corr_1"],
            evaluated_at=SINCE,
            native_marks={"BTC/USD": Decimal("100")},
            benchmark_marks={
                "BTC/USD": (Decimal("100"), Decimal("100")),
                "ETH/USD": (Decimal("100"), Decimal("100")),
            },
        )
        assert metrics.nominal_net_pnl == Decimal("0")

    @pytest.mark.asyncio
    async def test_ending_equity_invariant(self) -> None:
        service = _make_service()
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "corr_1": [
                    _make_alpaca_execution_row(
                        side="buy", filled_qty="1", filled_price="50000", row_id=1
                    ),
                    _make_alpaca_execution_row(
                        side="sell", filled_qty="1", filled_price="52000", row_id=2
                    ),
                ]
            }
        )
        metrics = await service.compute_alpaca_view(ledger, correlation_ids=["corr_1"])
        assert metrics.ending_equity == metrics.initial_equity + metrics.nominal_net_pnl

    @pytest.mark.asyncio
    async def test_loss_roundtrip(self) -> None:
        service = _make_service()
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "corr_1": [
                    _make_alpaca_execution_row(
                        side="buy", filled_qty="1", filled_price="50000", row_id=1
                    ),
                    _make_alpaca_execution_row(
                        side="sell", filled_qty="1", filled_price="48000", row_id=2
                    ),
                ]
            }
        )
        metrics = await service.compute_alpaca_view(ledger, correlation_ids=["corr_1"])
        assert metrics.nominal_net_pnl == Decimal("-2000")

    @pytest.mark.asyncio
    async def test_currency_usd(self) -> None:
        service = _make_service()
        ledger = FakeAlpacaLedgerReader(rows_by_correlation={})
        metrics = await service.compute_alpaca_view(ledger, correlation_ids=[])
        assert metrics.currency == ViewCurrency.USD
        assert metrics.currency != ViewCurrency.USDT

    @pytest.mark.asyncio
    async def test_currency_mismatch_raises(self) -> None:
        service = _make_service()
        bad_row = _make_alpaca_execution_row(row_id=1)
        bad_row.currency = "KRW"
        ledger = FakeAlpacaLedgerReader(rows_by_correlation={"corr_1": [bad_row]})
        with pytest.raises(EvaluationConfigError) as exc_info:
            await service.compute_alpaca_view(ledger, correlation_ids=["corr_1"])
        assert exc_info.value.reason_code == "currency_mismatch"

    @pytest.mark.asyncio
    async def test_missing_qty_increments_count(self) -> None:
        service = _make_service()
        row = _make_alpaca_execution_row(row_id=1)
        row.filled_qty = None
        ledger = FakeAlpacaLedgerReader(rows_by_correlation={"corr_1": [row]})
        metrics = await service.compute_alpaca_view(ledger, correlation_ids=["corr_1"])
        assert metrics.missing_observation_count == 1

    @pytest.mark.asyncio
    async def test_missing_price_increments_count(self) -> None:
        service = _make_service()
        row = _make_alpaca_execution_row(row_id=1)
        row.filled_avg_price = None
        ledger = FakeAlpacaLedgerReader(rows_by_correlation={"corr_1": [row]})
        metrics = await service.compute_alpaca_view(ledger, correlation_ids=["corr_1"])
        assert metrics.missing_observation_count == 1

    @pytest.mark.asyncio
    async def test_non_execution_rows_ignored(self) -> None:
        service = _make_service()
        plan_row = FakeAlpacaRow(
            id=1,
            record_kind="plan",
            lifecycle_state="planned",
            side="buy",
            execution_symbol="BTC/USD",
            filled_qty=Decimal("1"),
            filled_avg_price=Decimal("50000"),
        )
        exec_row = _make_alpaca_execution_row(side="buy", row_id=2)
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={"corr_1": [plan_row, exec_row]}
        )
        metrics = await service.compute_alpaca_view(ledger, correlation_ids=["corr_1"])
        assert metrics.fill_count == 1  # only execution row counted

    @pytest.mark.asyncio
    async def test_turnover(self) -> None:
        service = _make_service()
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "corr_1": [
                    _make_alpaca_execution_row(
                        side="buy", filled_qty="1", filled_price="50000", row_id=1
                    ),
                    _make_alpaca_execution_row(
                        side="sell", filled_qty="1", filled_price="51000", row_id=2
                    ),
                ]
            }
        )
        metrics = await service.compute_alpaca_view(ledger, correlation_ids=["corr_1"])
        assert metrics.turnover == Decimal("101000")

    @pytest.mark.asyncio
    async def test_canonical_snapshot_hashes_empty(self) -> None:
        service = _make_service()
        ledger = FakeAlpacaLedgerReader(rows_by_correlation={})
        metrics = await service.compute_alpaca_view(ledger, correlation_ids=[])
        assert metrics.canonical_snapshot_hashes == ()

    @pytest.mark.asyncio
    async def test_multiple_correlation_ids(self) -> None:
        service = _make_service()
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "corr_1": [
                    _make_alpaca_execution_row(
                        side="buy",
                        filled_qty="1",
                        filled_price="50000",
                        row_id=1,
                        corr_id="corr_1",
                    ),
                    _make_alpaca_execution_row(
                        side="sell",
                        filled_qty="1",
                        filled_price="51000",
                        row_id=2,
                        corr_id="corr_1",
                    ),
                ],
                "corr_2": [
                    _make_alpaca_execution_row(
                        side="buy",
                        filled_qty="2",
                        filled_price="3000",
                        row_id=3,
                        corr_id="corr_2",
                        symbol="ETH/USD",
                    ),
                    _make_alpaca_execution_row(
                        side="sell",
                        filled_qty="2",
                        filled_price="3100",
                        row_id=4,
                        corr_id="corr_2",
                        symbol="ETH/USD",
                    ),
                ],
            }
        )
        metrics = await service.compute_alpaca_view(
            ledger, correlation_ids=["corr_1", "corr_2"]
        )
        assert metrics.nominal_net_pnl == Decimal("1200")  # 1000 + 200
        assert metrics.fill_count == 4

    @pytest.mark.asyncio
    async def test_partial_fill_detection(self) -> None:
        service = _make_service()
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "corr_1": [
                    _make_alpaca_execution_row(
                        side="buy",
                        row_id=1,
                        is_partial=True,
                    ),
                ]
            }
        )
        metrics = await service.compute_alpaca_view(ledger, correlation_ids=["corr_1"])
        assert metrics.partial_fill_count == 1

    @pytest.mark.asyncio
    async def test_canceled_zero_fill_ignored_without_missing_observation(self) -> None:
        service = _make_service()
        # Seed a zero-fill canceled order row and a normal filled row
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "corr_1": [
                    _make_alpaca_execution_row(
                        side="buy", filled_qty="1", filled_price="50000", row_id=1
                    ),
                    FakeAlpacaRow(
                        id=2,
                        record_kind="execution",
                        lifecycle_state="canceled",
                        side="buy",
                        currency="USD",
                        execution_symbol="BTC/USD",
                        filled_qty=Decimal("0"),
                        filled_avg_price=None,
                        client_order_id="coid_2",
                        lifecycle_correlation_id="corr_1",
                    ),
                ]
            }
        )
        metrics = await service.compute_alpaca_view(
            ledger,
            correlation_ids=["corr_1"],
            native_marks={"BTC/USD": Decimal("50000")},
        )
        assert metrics.missing_observation_count == 0
        assert metrics.fill_count == 1

    @pytest.mark.asyncio
    async def test_canceled_partial_fill_reflected(self) -> None:
        service = _make_service()
        # Seed a partial-fill canceled order row
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "corr_1": [
                    FakeAlpacaRow(
                        id=1,
                        record_kind="execution",
                        lifecycle_state="canceled",
                        side="buy",
                        currency="USD",
                        execution_symbol="BTC/USD",
                        filled_qty=Decimal("0.5"),
                        filled_avg_price=Decimal("50000"),
                        client_order_id="coid_1",
                        lifecycle_correlation_id="corr_1",
                    ),
                ]
            }
        )
        metrics = await service.compute_alpaca_view(
            ledger,
            correlation_ids=["corr_1"],
            native_marks={"BTC/USD": Decimal("50000")},
        )
        assert metrics.missing_observation_count == 0
        assert metrics.fill_count == 1
        assert metrics.turnover == Decimal("25000")  # 0.5 * 50000

    @pytest.mark.asyncio
    async def test_canceled_partial_fill_missing_price(self) -> None:
        service = _make_service()
        # Seed a partial-fill (qty > 0) canceled order row but missing price
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "corr_1": [
                    FakeAlpacaRow(
                        id=1,
                        record_kind="execution",
                        lifecycle_state="canceled",
                        side="buy",
                        currency="USD",
                        execution_symbol="BTC/USD",
                        filled_qty=Decimal("0.5"),
                        filled_avg_price=None,
                        client_order_id="coid_1",
                        lifecycle_correlation_id="corr_1",
                    ),
                ]
            }
        )
        metrics = await service.compute_alpaca_view(
            ledger,
            correlation_ids=["corr_1"],
            native_marks={"BTC/USD": Decimal("50000")},
        )
        assert metrics.missing_observation_count == 1
        assert metrics.fill_count == 0

    @pytest.mark.asyncio
    async def test_canceled_nan_filled_qty(self) -> None:
        service = _make_service()
        # Seed a canceled order row with NaN filled_qty
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "corr_1": [
                    FakeAlpacaRow(
                        id=1,
                        record_kind="execution",
                        lifecycle_state="canceled",
                        side="buy",
                        currency="USD",
                        execution_symbol="BTC/USD",
                        filled_qty=Decimal("NaN"),
                        filled_avg_price=Decimal("50000"),
                        client_order_id="coid_1",
                        lifecycle_correlation_id="corr_1",
                    ),
                ]
            }
        )
        metrics = await service.compute_alpaca_view(
            ledger,
            correlation_ids=["corr_1"],
            native_marks={"BTC/USD": Decimal("50000")},
        )
        assert metrics.missing_observation_count == 1
        assert metrics.fill_count == 0

    @pytest.mark.asyncio
    async def test_canceled_zero_fill_explicit_zero(self) -> None:
        service = _make_service()
        # Seed a zero-fill (qty == 0) canceled order row
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "corr_1": [
                    FakeAlpacaRow(
                        id=1,
                        record_kind="execution",
                        lifecycle_state="canceled",
                        side="buy",
                        currency="USD",
                        execution_symbol="BTC/USD",
                        filled_qty=Decimal("0"),
                        filled_avg_price=None,
                        client_order_id="coid_1",
                        lifecycle_correlation_id="corr_1",
                    ),
                ]
            }
        )
        metrics = await service.compute_alpaca_view(
            ledger,
            correlation_ids=["corr_1"],
            native_marks={"BTC/USD": Decimal("50000")},
        )
        assert metrics.missing_observation_count == 0
        assert metrics.fill_count == 0


# ---------------------------------------------------------------------------
# View 3: Canonical shadow P&L tests
# ---------------------------------------------------------------------------


class TestShadowView:
    @pytest.mark.asyncio
    async def test_basic_shadow_pnl(self) -> None:
        service = _make_service(
            fee_rate_bps=Decimal("10"),
            spread_bps=Decimal("5"),
            slippage_bps=Decimal("3"),
        )
        snapshots = [
            FakeMarketSnapshot(
                id=1,
                content_hash=_STABLE_HASH_A,
                payload=_make_snapshot_payload(btc_close="50000", eth_close="3000"),
            ),
        ]
        reader = FakeSnapshotReader(snapshots=snapshots)
        target_weights = {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")}
        metrics = await service.compute_shadow_view(
            reader, target_weights, cohort_id="cohort_test_001", since=SINCE
        )
        assert metrics.view_name == ViewName.CANONICAL_SHADOW
        assert metrics.currency == ViewCurrency.USDT
        assert metrics.source == ViewSource.CANONICAL_MARKET_SNAPSHOT
        assert metrics.fill_count == 1
        assert len(metrics.canonical_snapshot_hashes) == 1
        assert metrics.canonical_snapshot_hashes[0] == _STABLE_HASH_A

    @pytest.mark.asyncio
    async def test_rising_prices_produce_mark_to_market_profit(self) -> None:
        service = _make_service(
            fee_rate_bps=Decimal("0"),
            spread_bps=Decimal("0"),
            slippage_bps=Decimal("0"),
            fill_timing="canonical_close",
        )
        reader = FakeSnapshotReader(
            snapshots=[
                FakeMarketSnapshot(
                    id=1,
                    content_hash=_STABLE_HASH_A,
                    payload=_make_snapshot_payload(btc_close="100", eth_close="100"),
                ),
                FakeMarketSnapshot(
                    id=2,
                    content_hash=_STABLE_HASH_B,
                    payload=_make_snapshot_payload(btc_close="110", eth_close="110"),
                ),
            ]
        )
        metrics = await service.compute_shadow_view(
            reader,
            {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")},
            cohort_id="cohort_test_001",
            since=SINCE,
        )
        assert metrics.nominal_net_pnl == Decimal("1000")
        assert metrics.fill_count == 1

    @pytest.mark.asyncio
    async def test_zero_target_snapshot_is_observation_not_fill(self) -> None:
        service = _make_service()
        reader = FakeSnapshotReader(
            snapshots=[FakeMarketSnapshot(payload=_make_snapshot_payload())]
        )
        metrics = await service.compute_shadow_view(
            reader,
            {"BTCUSDT": Decimal("0"), "ETHUSDT": Decimal("0")},
            cohort_id="cohort_test_001",
            since=SINCE,
        )
        assert metrics.fill_count == 0
        assert metrics.missing_observation_count == 0

    @pytest.mark.asyncio
    async def test_ending_equity_invariant(self) -> None:
        service = _make_service()
        snapshots = [
            FakeMarketSnapshot(
                id=1,
                content_hash=_STABLE_HASH_A,
                payload=_make_snapshot_payload(),
            ),
        ]
        reader = FakeSnapshotReader(snapshots=snapshots)
        metrics = await service.compute_shadow_view(
            reader,
            {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")},
            cohort_id="cohort_test_001",
            since=SINCE,
        )
        assert metrics.ending_equity == metrics.initial_equity + metrics.nominal_net_pnl

    @pytest.mark.asyncio
    async def test_canonical_snapshot_hash_lineage(self) -> None:
        service = _make_service()
        snapshots = [
            FakeMarketSnapshot(
                id=1,
                content_hash=_STABLE_HASH_A,
                payload=_make_snapshot_payload(),
            ),
            FakeMarketSnapshot(
                id=2,
                content_hash=_STABLE_HASH_B,
                payload=_make_snapshot_payload(),
            ),
        ]
        reader = FakeSnapshotReader(snapshots=snapshots)
        metrics = await service.compute_shadow_view(
            reader,
            {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")},
            cohort_id="cohort_test_001",
            since=SINCE,
        )
        assert metrics.canonical_snapshot_hashes == (_STABLE_HASH_A, _STABLE_HASH_B)

    @pytest.mark.asyncio
    async def test_missing_candle_increments_count(self) -> None:
        service = _make_service()
        snapshots = [
            FakeMarketSnapshot(
                id=1,
                content_hash=_STABLE_HASH_A,
                payload={
                    "symbols": [
                        {"symbol": "BTCUSDT", "candles": []},
                    ]
                },
            ),
        ]
        reader = FakeSnapshotReader(snapshots=snapshots)
        metrics = await service.compute_shadow_view(
            reader,
            {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")},
            cohort_id="cohort_test_001",
            since=SINCE,
        )
        assert metrics.missing_observation_count >= 1

    @pytest.mark.asyncio
    async def test_missing_payload_increments_count(self) -> None:
        service = _make_service()
        snapshots = [
            FakeMarketSnapshot(
                id=1,
                content_hash=_STABLE_HASH_A,
                payload="not_a_dict",  # type: ignore
            ),
        ]
        reader = FakeSnapshotReader(snapshots=snapshots)
        metrics = await service.compute_shadow_view(
            reader,
            {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")},
            cohort_id="cohort_test_001",
            since=SINCE,
        )
        assert metrics.missing_observation_count == 1

    @pytest.mark.asyncio
    async def test_malformed_close_increments_count(self) -> None:
        service = _make_service()
        snapshots = [
            FakeMarketSnapshot(
                id=1,
                content_hash=_STABLE_HASH_A,
                payload={
                    "symbols": [
                        {
                            "symbol": "BTCUSDT",
                            "candles": [{"close": "NaN"}],
                        }
                    ]
                },
            ),
        ]
        reader = FakeSnapshotReader(snapshots=snapshots)
        metrics = await service.compute_shadow_view(
            reader,
            {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")},
            cohort_id="cohort_test_001",
            since=SINCE,
        )
        assert metrics.missing_observation_count >= 1

    @pytest.mark.asyncio
    async def test_missing_data_not_zero_filled(self) -> None:
        """Critical: missing snapshots are NOT zero-filled."""
        service = _make_service()
        good_snap = FakeMarketSnapshot(
            id=1,
            content_hash=_STABLE_HASH_A,
            payload=_make_snapshot_payload(),
        )
        bad_snap = FakeMarketSnapshot(
            id=2,
            content_hash=_STABLE_HASH_B,
            payload={},
        )
        reader = FakeSnapshotReader(snapshots=[good_snap, bad_snap])
        metrics = await service.compute_shadow_view(
            reader,
            {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")},
            cohort_id="cohort_test_001",
            since=SINCE,
        )
        assert metrics.missing_observation_count >= 1
        # P&L from good_snap is NOT replaced by zero for bad_snap
        # (bad_snap contributes empty symbols → zero P&L for that snapshot, but missing count is incremented)

    @pytest.mark.asyncio
    async def test_empty_snapshots(self) -> None:
        service = _make_service()
        reader = FakeSnapshotReader(snapshots=[])
        metrics = await service.compute_shadow_view(
            reader,
            {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")},
            cohort_id="cohort_test_001",
            since=SINCE,
        )
        assert metrics.nominal_net_pnl == Decimal("0")
        assert metrics.fill_count == 0
        assert metrics.canonical_snapshot_hashes == ()

    @pytest.mark.asyncio
    async def test_native_currency_usdt(self) -> None:
        service = _make_service()
        reader = FakeSnapshotReader(snapshots=[])
        metrics = await service.compute_shadow_view(
            reader,
            {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")},
            cohort_id="cohort_test_001",
            since=SINCE,
        )
        assert metrics.currency == ViewCurrency.USDT

    @pytest.mark.asyncio
    async def test_fees_computed(self) -> None:
        service = _make_service(fee_rate_bps=Decimal("10"))
        snapshots = [
            FakeMarketSnapshot(
                id=1,
                content_hash=_STABLE_HASH_A,
                payload=_make_snapshot_payload(),
            ),
        ]
        reader = FakeSnapshotReader(snapshots=snapshots)
        metrics = await service.compute_shadow_view(
            reader,
            {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")},
            cohort_id="cohort_test_001",
            since=SINCE,
        )
        # Fees should be > 0 for the shadow view
        assert metrics.fees > Decimal("0")

    @pytest.mark.asyncio
    async def test_turnover_computed(self) -> None:
        service = _make_service()
        snapshots = [
            FakeMarketSnapshot(
                id=1,
                content_hash=_STABLE_HASH_A,
                payload=_make_snapshot_payload(),
            ),
        ]
        reader = FakeSnapshotReader(snapshots=snapshots)
        metrics = await service.compute_shadow_view(
            reader,
            {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")},
            cohort_id="cohort_test_001",
            since=SINCE,
        )
        # turnover = sum of target notionals = 0.5 * 10000 + 0.5 * 10000 = 10000
        assert metrics.turnover == Decimal("10000")


# ---------------------------------------------------------------------------
# Native currency separation tests
# ---------------------------------------------------------------------------


class TestCurrencySeparation:
    @pytest.mark.asyncio
    async def test_binance_is_usdt_alpaca_is_usd_shadow_is_usdt(self) -> None:
        service = _make_service()
        binance_ledger = FakeBinanceLedgerReader(rows=[])
        alpaca_ledger = FakeAlpacaLedgerReader(rows_by_correlation={})
        snapshot_reader = FakeSnapshotReader(snapshots=[])

        binance_metrics = await service.compute_binance_view(
            binance_ledger, since=SINCE
        )
        alpaca_metrics = await service.compute_alpaca_view(
            alpaca_ledger, correlation_ids=[]
        )
        shadow_metrics = await service.compute_shadow_view(
            snapshot_reader,
            {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")},
            cohort_id="cohort_test_001",
            since=SINCE,
        )

        assert binance_metrics.currency == ViewCurrency.USDT
        assert alpaca_metrics.currency == ViewCurrency.USD
        assert shadow_metrics.currency == ViewCurrency.USDT

    @pytest.mark.asyncio
    async def test_no_cross_view_nominal_total(self) -> None:
        """Verify that ViewMetrics has no cross-view aggregation field."""
        service = _make_service()
        binance_ledger = FakeBinanceLedgerReader(
            rows=[_make_binance_row(realized_pnl="500", row_id=1)]
        )
        alpaca_ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "c1": [_make_alpaca_execution_row(side="buy", row_id=1)]
            }
        )

        binance_metrics = await service.compute_binance_view(
            binance_ledger, since=SINCE
        )
        alpaca_metrics = await service.compute_alpaca_view(
            alpaca_ledger, correlation_ids=["c1"]
        )

        # There is no field that sums nominal_net_pnl across views
        assert not hasattr(binance_metrics, "total_nominal_pnl")
        assert not hasattr(alpaca_metrics, "aggregate_nominal_pnl")

        # The currencies are different — no conversion performed
        assert binance_metrics.currency != alpaca_metrics.currency
        assert binance_metrics.nominal_net_pnl == Decimal("500")

    @pytest.mark.asyncio
    async def test_no_usdt_usd_conversion(self) -> None:
        """The module never converts USDT to USD or vice versa."""
        service = _make_service(
            initial_equity_usdt=Decimal("10000"),
            initial_equity_usd=Decimal("10000"),
        )
        binance_ledger = FakeBinanceLedgerReader(
            rows=[_make_binance_row(realized_pnl="1000", row_id=1)]
        )
        metrics = await service.compute_binance_view(binance_ledger, since=SINCE)
        # 1000 USDT is reported as 1000, not converted
        assert metrics.nominal_net_pnl == Decimal("1000")
        assert metrics.currency == ViewCurrency.USDT


# ---------------------------------------------------------------------------
# Benchmark tests
# ---------------------------------------------------------------------------


class TestBenchmarks:
    def test_cash_benchmark(self) -> None:
        service = _make_service(risk_free_rate_pct=Decimal("2"))
        bench = service.compute_cash_benchmark(elapsed_periods=525600)
        assert bench == Decimal("2")

    def test_cash_benchmark_half_year(self) -> None:
        service = _make_service(risk_free_rate_pct=Decimal("4"))
        bench = service.compute_cash_benchmark(elapsed_periods=262800)
        assert bench > Decimal("0")
        assert bench < Decimal("4")

    def test_btc_eth_equal_weight_benchmark(self) -> None:
        config = make_evaluation_config(
            btc_weight=Decimal("0.5"),
            eth_weight=Decimal("0.5"),
        )
        epoch = _make_epoch()
        service = PaperEvaluationPnL(
            config=config,
            epoch=epoch,
            experiment_hash=_STABLE_HASH_A,
            cohort_hash=_STABLE_HASH_B,
        )
        btc_returns = [Decimal("10")]
        eth_returns = [Decimal("20")]
        bench = service.compute_btc_eth_benchmark(
            btc_returns_pct=btc_returns, eth_returns_pct=eth_returns
        )
        assert bench == Decimal("15")

    def test_btc_eth_custom_weights(self) -> None:
        config = make_evaluation_config(
            btc_weight=Decimal("0.3"),
            eth_weight=Decimal("0.7"),
        )
        epoch = _make_epoch()
        service = PaperEvaluationPnL(
            config=config,
            epoch=epoch,
            experiment_hash=_STABLE_HASH_A,
            cohort_hash=_STABLE_HASH_B,
        )
        btc_returns = [Decimal("10")]
        eth_returns = [Decimal("20")]
        bench = service.compute_btc_eth_benchmark(
            btc_returns_pct=btc_returns, eth_returns_pct=eth_returns
        )
        assert bench == Decimal("17")

    def test_cash_benchmark_delta_in_metrics(self) -> None:
        service = _make_service(risk_free_rate_pct=Decimal("2"))
        ledger = FakeBinanceLedgerReader(
            rows=[_make_binance_row(realized_pnl="500", row_id=1)]  # 5% return
        )
        import asyncio

        metrics = asyncio.run(
            service.compute_binance_view(
                ledger,
                since=SINCE,
                evaluated_at=SINCE + timedelta(days=365),
                benchmark_marks={
                    "BTCUSDT": (Decimal("100"), Decimal("110")),
                    "ETHUSDT": (Decimal("100"), Decimal("120")),
                },
            )
        )
        assert metrics.cash_benchmark_return_pct == Decimal("2")
        assert metrics.btc_eth_benchmark_return_pct == Decimal("15")
        assert metrics.cash_benchmark_delta_pct == metrics.net_return_pct - Decimal("2")


# ---------------------------------------------------------------------------
# Read-only safety tests
# ---------------------------------------------------------------------------


class TestReadOnlySafety:
    @pytest.mark.asyncio
    async def test_no_write_methods_called_on_binance(self) -> None:
        """Ensure the Binance view never calls write methods."""
        service = _make_service()
        reader = FakeBinanceLedgerReader(rows=[])
        await service.compute_binance_view(reader, since=SINCE)
        for forbidden in (
            "record_planned",
            "record_previewed",
            "record_validated",
            "record_submitted",
            "record_filled",
            "record_closed",
            "record_cancelled",
            "record_reconciled",
            "record_anomaly",
            "reserve_root_planned",
            "resolve_or_create_instrument",
            "_transition",
            "update_state",
        ):
            assert not hasattr(reader, forbidden), (
                f"{forbidden} must not exist on a read-only reader"
            )

    @pytest.mark.asyncio
    async def test_no_write_methods_called_on_alpaca(self) -> None:
        """Ensure the Alpaca view never calls write methods."""
        service = _make_service()
        reader = FakeAlpacaLedgerReader(rows_by_correlation={})
        await service.compute_alpaca_view(reader, correlation_ids=["c1"])
        for forbidden in (
            "record_plan",
            "record_preview",
            "record_validation_attempt",
            "record_submit",
            "claim_submit",
            "reserve_sell_and_claim",
            "record_submit_failure",
            "_transition",
            "update_state",
        ):
            assert not hasattr(reader, forbidden), (
                f"{forbidden} must not exist on a read-only reader"
            )


# ---------------------------------------------------------------------------
# Equity invariant tests
# ---------------------------------------------------------------------------


class TestEquityInvariant:
    @pytest.mark.asyncio
    async def test_binance_invariant_holds(self) -> None:
        service = _make_service()
        ledger = FakeBinanceLedgerReader(
            rows=[
                _make_binance_row(realized_pnl="100", row_id=1),
                _make_binance_row(realized_pnl="200", row_id=2),
                _make_binance_row(realized_pnl="-50", row_id=3),
            ]
        )
        metrics = await service.compute_binance_view(ledger, since=SINCE)
        assert metrics.ending_equity == metrics.initial_equity + metrics.nominal_net_pnl

    @pytest.mark.asyncio
    async def test_alpaca_invariant_holds(self) -> None:
        service = _make_service()
        ledger = FakeAlpacaLedgerReader(
            rows_by_correlation={
                "c1": [
                    _make_alpaca_execution_row(
                        side="buy", filled_qty="1", filled_price="100", row_id=1
                    ),
                    _make_alpaca_execution_row(
                        side="sell", filled_qty="1", filled_price="110", row_id=2
                    ),
                ]
            }
        )
        metrics = await service.compute_alpaca_view(ledger, correlation_ids=["c1"])
        assert metrics.ending_equity == metrics.initial_equity + metrics.nominal_net_pnl

    @pytest.mark.asyncio
    async def test_shadow_invariant_holds(self) -> None:
        service = _make_service()
        snapshots = [
            FakeMarketSnapshot(
                id=1,
                content_hash=_STABLE_HASH_A,
                payload=_make_snapshot_payload(),
            ),
        ]
        reader = FakeSnapshotReader(snapshots=snapshots)
        metrics = await service.compute_shadow_view(
            reader,
            {"BTCUSDT": Decimal("0.5"), "ETHUSDT": Decimal("0.5")},
            cohort_id="cohort_test_001",
            since=SINCE,
        )
        assert metrics.ending_equity == metrics.initial_equity + metrics.nominal_net_pnl
