"""ROB-850 read-only 3-view P&L computation.

Computes :class:`ViewMetrics` for each of the three independent evaluation views:
    1. Binance broker P&L (native USDT) — reads ``BinanceDemoLedgerService``
    2. Alpaca broker P&L (native USD) — reads ``AlpacaPaperLedgerService``
    3. Canonical shadow P&L (native USDT) — reads ``CanonicalMarketSnapshot`` rows

This module is strictly READ-ONLY. It must NEVER call any ``record_*``,
``claim_*``, ``reserve_*``, ``resolve_or_create_instrument``, ``_transition``,
or ``update_state`` method on any ledger service.

Currency separation is enforced: Binance=USDT, Alpaca=USD, Shadow=USDT.
No USDT/USD conversion is performed. No cross-view nominal P&L total is emitted.
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any, Protocol

from app.services.paper_evaluation.contracts import (
    EpochIdentity,
    EvaluationConfig,
    EvaluationConfigError,
    ViewCurrency,
    ViewMetrics,
    ViewName,
    ViewSource,
)

if TYPE_CHECKING:

    from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
    from app.models.paper_cohort import CanonicalMarketSnapshot
    from app.models.review import AlpacaPaperOrderLedger


# ---------------------------------------------------------------------------
# Protocols for read-only access (avoids hard import of mutable services)
# ---------------------------------------------------------------------------


class _BinanceLedgerReader(Protocol):
    """Read-only protocol for ``BinanceDemoLedgerService``."""

    async def closed_rows_since(
        self, *, since: datetime
    ) -> Sequence[BinanceDemoOrderLedger]: ...


class _AlpacaLedgerReader(Protocol):
    """Read-only protocol for ``AlpacaPaperLedgerService``."""

    async def list_by_correlation_id(
        self, lifecycle_correlation_id: str
    ) -> Sequence[AlpacaPaperOrderLedger]: ...

    async def find_executed_by_client_order_id(
        self, client_order_id: str
    ) -> AlpacaPaperOrderLedger | None: ...


class _SnapshotReader(Protocol):
    """Read-only protocol for snapshot retrieval."""

    async def list_snapshots(
        self, *, cohort_id: str, since: datetime
    ) -> Sequence[CanonicalMarketSnapshot]: ...


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

_ZERO = Decimal("0")
_ONE = Decimal("1")
_HUNDRED = Decimal("100")
_MIN_SHARPE_SAMPLES = 3


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------


def _validate_finite(value: Decimal, context: str) -> Decimal:
    """Validate that ``value`` is a finite ``Decimal``.

    Raises ``EvaluationConfigError("non_finite_value")`` if the value is
    NaN, Inf, or otherwise non-finite.
    """
    if not isinstance(value, Decimal):
        try:
            value = Decimal(value)
        except Exception:
            raise EvaluationConfigError(
                "non_finite_value",
                f"{context}: cannot coerce {value!r} to Decimal",
            )
    if not value.is_finite():
        raise EvaluationConfigError(
            "non_finite_value",
            f"{context}: value {value} is not finite",
        )
    return value


def _to_decimal_safe(raw: Any, context: str) -> Decimal:
    """Convert a raw value (str/int/float/Decimal) to a finite Decimal.

    Raises ``EvaluationConfigError("non_finite_value")`` on failure.
    """
    if raw is None:
        raise EvaluationConfigError(
            "non_finite_value", f"{context}: value is None"
        )
    try:
        result = Decimal(str(raw))
    except Exception:
        raise EvaluationConfigError(
            "non_finite_value",
            f"{context}: cannot parse {raw!r} as Decimal",
        )
    return _validate_finite(result, context)


# ---------------------------------------------------------------------------
# Statistical helpers
# ---------------------------------------------------------------------------


def _compute_max_drawdown_pct(equity_curve: Sequence[Decimal]) -> Decimal:
    """Compute peak-to-trough maximum drawdown percentage.

    Returns ``Decimal("0")`` if the curve is empty, has a single point,
    or no drawdown occurred.
    """
    if len(equity_curve) < 2:
        return _ZERO
    peak = equity_curve[0]
    max_dd = _ZERO
    for value in equity_curve[1:]:
        if value > peak:
            peak = value
        if peak > _ZERO:
            dd = (peak - value) / peak * _HUNDRED
            if dd > max_dd:
                max_dd = dd
    return max_dd


def _compute_sharpe(
    returns: Sequence[Decimal],
    periods_per_year: int,
    risk_free_rate_pct: Decimal,
) -> Decimal | None:
    """Compute an annualized Sharpe ratio from per-period returns.

    * ``returns`` are percentage returns per period.
    * ``periods_per_year`` annualizes the result.
    * ``risk_free_rate_pct`` is the annual risk-free rate in percent.

    Returns ``None`` if fewer than ``_MIN_SHARPE_SAMPLES`` returns are given
    or if the standard deviation is zero.
    """
    if len(returns) < _MIN_SHARPE_SAMPLES:
        return None
    n = Decimal(len(returns))
    mean_return = sum(returns, start=_ZERO) / n

    squared_deviations: list[Decimal] = []
    for r in returns:
        diff = r - mean_return
        squared_deviations.append(diff * diff)
    variance = sum(squared_deviations, start=_ZERO) / n

    if variance <= _ZERO:
        return None
    std_dev = Decimal(str(math.sqrt(float(variance))))
    if std_dev == _ZERO:
        return None

    # annualize: mean_return * periods_per_year gives annualized return %
    annualized_return = mean_return * Decimal(periods_per_year)
    annualized_std = std_dev * Decimal(str(math.sqrt(periods_per_year)))

    if annualized_std == _ZERO:
        return None

    excess_return = annualized_return - risk_free_rate_pct
    return excess_return / annualized_std


def _compute_exposure(
    positions: Sequence[Decimal], equity: Decimal
) -> Decimal:
    """Compute average absolute position notional / equity, clamped to [0, 1]."""
    if equity <= _ZERO or len(positions) == 0:
        return _ZERO
    total = sum((abs(p) for p in positions), start=_ZERO)
    avg = total / Decimal(len(positions))
    ratio = avg / equity
    # Clamp to [0, 1]
    if ratio < _ZERO:
        return _ZERO
    if ratio > _ONE:
        return _ONE
    return ratio


def _compute_benchmark_deltas(
    net_return_pct: Decimal,
    cash_benchmark_return_pct: Decimal,
    btc_eth_benchmark_return_pct: Decimal,
) -> tuple[Decimal, Decimal]:
    """Compute benchmark deltas: strategy return minus benchmark return."""
    cash_delta = net_return_pct - cash_benchmark_return_pct
    btc_eth_delta = net_return_pct - btc_eth_benchmark_return_pct
    return cash_delta, btc_eth_delta


def _build_equity_curve(
    initial_equity: Decimal,
    pnl_samples: Sequence[Decimal],
) -> list[Decimal]:
    """Build a cumulative equity curve from per-trade P&L samples."""
    curve = [initial_equity]
    cumulative = initial_equity
    for pnl in pnl_samples:
        cumulative = cumulative + pnl
        curve.append(cumulative)
    return curve


# ---------------------------------------------------------------------------
# Per-row extractors
# ---------------------------------------------------------------------------


def _extract_realized_pnl_usdt(
    extra_metadata: dict[str, Any] | None,
) -> Decimal | None:
    """Extract ``realized_pnl_usdt`` from extra_metadata.

    Returns ``None`` if missing or non-finite (counts as missing observation).
    """
    if extra_metadata is None:
        return None
    raw = extra_metadata.get("realized_pnl_usdt")
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except Exception:
        return None
    if not value.is_finite():
        return None
    return value


def _extract_fee_usdt(
    extra_metadata: dict[str, Any] | None,
) -> Decimal | None:
    """Extract fee from extra_metadata if present."""
    if extra_metadata is None:
        return None
    raw = extra_metadata.get("fee_usdt")
    if raw is None:
        raw = extra_metadata.get("commission_usdt")
    if raw is None:
        return None
    try:
        value = Decimal(str(raw))
    except Exception:
        return None
    if not value.is_finite():
        return None
    return value


def _is_partial_fill(extra_metadata: dict[str, Any] | None) -> bool:
    """Detect partial-fill evidence from extra_metadata."""
    if extra_metadata is None:
        return False
    raw = extra_metadata.get("is_partial_fill")
    if isinstance(raw, bool):
        return raw
    if isinstance(raw, str):
        return raw.lower() in ("true", "1", "yes")
    return False


# ---------------------------------------------------------------------------
# Main service class
# ---------------------------------------------------------------------------


class PaperEvaluationPnL:
    """Read-only 3-view P&L computation service.

    Computes :class:`ViewMetrics` for each evaluation view without performing
    any writes, mutations, or state transitions on any ledger service.
    """

    def __init__(
        self,
        config: EvaluationConfig,
        epoch: EpochIdentity,
        *,
        experiment_hash: str,
        cohort_hash: str,
    ) -> None:
        self._config = config
        self._epoch = epoch
        self._experiment_hash = experiment_hash
        self._cohort_hash = cohort_hash

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def compute_binance_view(
        self,
        ledger: _BinanceLedgerReader,
        *,
        since: datetime,
    ) -> ViewMetrics:
        """Compute View 1: Binance broker P&L (native USDT).

        Reads closed rows via ``ledger.closed_rows_since(since)``.
        Extracts ``realized_pnl_usdt`` from each row's ``extra_metadata``.
        Missing or non-finite values increment ``missing_observation_count``.
        """
        rows = await ledger.closed_rows_since(since=since)

        pnl_samples: list[Decimal] = []
        fees_total = _ZERO
        turnover = _ZERO
        fill_count = 0
        partial_fill_count = 0
        missing_observation_count = 0
        position_notionals: list[Decimal] = []

        for row in rows:
            notional = _ZERO
            if row.notional_usdt is not None:
                try:
                    notional = Decimal(str(row.notional_usdt))
                    if not notional.is_finite():
                        notional = _ZERO
                except Exception:
                    notional = _ZERO
            turnover += abs(notional)
            position_notionals.append(notional)

            realized = _extract_realized_pnl_usdt(row.extra_metadata)
            if realized is None:
                missing_observation_count += 1
                continue

            realized = _validate_finite(realized, f"binance row {row.id}")
            pnl_samples.append(realized)
            fill_count += 1

            if _is_partial_fill(row.extra_metadata):
                partial_fill_count += 1

            fee = _extract_fee_usdt(row.extra_metadata)
            if fee is not None:
                fees_total += fee
            elif notional > _ZERO:
                fee_rate = self._config.fill_cost_policy.fee_rate_bps
                fees_total += notional * fee_rate / Decimal("10000")

        nominal_net_pnl = sum(pnl_samples, start=_ZERO)
        mapping = self._config.views[ViewName.BINANCE_BROKER]
        initial_equity = self._epoch.initial_equity[ViewName.BINANCE_BROKER]
        ending_equity = initial_equity + nominal_net_pnl
        net_return_pct = self._safe_return_pct(ending_equity, initial_equity)

        equity_curve = _build_equity_curve(initial_equity, pnl_samples)
        max_dd = _compute_max_drawdown_pct(equity_curve)
        exposure = _compute_exposure(position_notionals, initial_equity)

        per_trade_returns = self._pnl_to_returns(pnl_samples, initial_equity)
        sharpe = _compute_sharpe(
            per_trade_returns,
            self._config.annualization.periods_per_year,
            self._config.annualization.risk_free_rate_pct,
        )

        cash_bench = _ZERO
        btc_eth_bench = _ZERO
        cash_delta, btc_eth_delta = _compute_benchmark_deltas(
            net_return_pct, cash_bench, btc_eth_bench
        )

        return ViewMetrics(
            view_name=ViewName.BINANCE_BROKER,
            currency=ViewCurrency.USDT,
            source=ViewSource.BINANCE_DEMO_LEDGER,
            symbol_mapping=mapping.symbols,
            initial_equity=initial_equity,
            ending_equity=ending_equity,
            nominal_net_pnl=nominal_net_pnl,
            fees=fees_total,
            net_return_pct=net_return_pct,
            max_drawdown_pct=max_dd,
            turnover=turnover,
            exposure=exposure,
            sharpe_reference=sharpe,
            fill_count=fill_count,
            partial_fill_count=partial_fill_count,
            missing_observation_count=missing_observation_count,
            cash_benchmark_return_pct=cash_bench,
            cash_benchmark_delta_pct=cash_delta,
            btc_eth_benchmark_return_pct=btc_eth_bench,
            btc_eth_benchmark_delta_pct=btc_eth_delta,
            canonical_snapshot_hashes=(),
            experiment_hash=self._experiment_hash,
            cohort_hash=self._cohort_hash,
            epoch_id=self._epoch.epoch_id,
            config_hash=self._epoch.config_hash,
        )

    async def compute_alpaca_view(
        self,
        ledger: _AlpacaLedgerReader,
        *,
        correlation_ids: Sequence[str],
    ) -> ViewMetrics:
        """Compute View 2: Alpaca broker P&L (native USD).

        Reads execution rows via ``ledger.list_by_correlation_id``.
        Filters to ``record_kind='execution'`` and executed lifecycle states.
        Filters to ``currency='USD'``.
        """
        _EXECUTED_LIFECYCLE_STATES = frozenset(
            {
                "submitted",
                "filled",
                "position_reconciled",
                "sell_validated",
                "closed",
                "final_reconciled",
            }
        )
        _RECORD_KIND_EXECUTION = "execution"

        buy_executions: list[AlpacaPaperOrderLedger] = []
        sell_executions: list[AlpacaPaperOrderLedger] = []
        fee_rows: list[AlpacaPaperOrderLedger] = []
        missing_observation_count = 0
        partial_fill_count = 0
        turnover = _ZERO
        position_notionals: list[Decimal] = []

        for corr_id in correlation_ids:
            rows = await ledger.list_by_correlation_id(corr_id)
            for row in rows:
                if row.record_kind != _RECORD_KIND_EXECUTION:
                    if row.record_kind == "reconcile" and row.lifecycle_state == "final_reconciled":
                        fee_rows.append(row)
                    continue
                if row.lifecycle_state not in _EXECUTED_LIFECYCLE_STATES:
                    continue

                currency = getattr(row, "currency", None) or "USD"
                if currency != "USD":
                    raise EvaluationConfigError(
                        "currency_mismatch",
                        f"alpaca row currency={currency!r} must be USD",
                    )

                filled_qty_raw = getattr(row, "filled_qty", None)
                filled_price_raw = getattr(row, "filled_avg_price", None)

                if filled_qty_raw is None or filled_price_raw is None:
                    missing_observation_count += 1
                    continue

                try:
                    filled_qty = Decimal(str(filled_qty_raw))
                    filled_price = Decimal(str(filled_price_raw))
                except Exception:
                    missing_observation_count += 1
                    continue

                if not filled_qty.is_finite() or not filled_price.is_finite():
                    missing_observation_count += 1
                    continue

                notional = filled_qty * filled_price
                turnover += abs(notional)
                position_notionals.append(notional)

                side_lower = (row.side or "").lower()
                if side_lower == "buy":
                    buy_executions.append(row)
                elif side_lower == "sell":
                    sell_executions.append(row)

                partial_meta = getattr(row, "position_snapshot", None)
                if isinstance(partial_meta, dict):
                    if _is_partial_fill(partial_meta):
                        partial_fill_count += 1

        nominal_net_pnl = self._compute_alpaca_roundtrip_pnl(
            buy_executions, sell_executions
        )

        fees_total = _ZERO
        for fee_row in fee_rows:
            fee_amount = getattr(fee_row, "fee_amount", None)
            if fee_amount is not None:
                try:
                    fee = Decimal(str(fee_amount))
                    if fee.is_finite():
                        fees_total += fee
                except Exception:
                    missing_observation_count += 1

        mapping = self._config.views[ViewName.ALPACA_BROKER]
        initial_equity = self._epoch.initial_equity[ViewName.ALPACA_BROKER]
        ending_equity = initial_equity + nominal_net_pnl
        net_return_pct = self._safe_return_pct(ending_equity, initial_equity)

        pnl_samples = self._extract_alpaca_pnl_samples(
            buy_executions, sell_executions
        )
        equity_curve = _build_equity_curve(initial_equity, pnl_samples)
        max_dd = _compute_max_drawdown_pct(equity_curve)
        exposure = _compute_exposure(position_notionals, initial_equity)

        per_trade_returns = self._pnl_to_returns(pnl_samples, initial_equity)
        sharpe = _compute_sharpe(
            per_trade_returns,
            self._config.annualization.periods_per_year,
            self._config.annualization.risk_free_rate_pct,
        )

        cash_bench = _ZERO
        btc_eth_bench = _ZERO
        cash_delta, btc_eth_delta = _compute_benchmark_deltas(
            net_return_pct, cash_bench, btc_eth_bench
        )

        fill_count = len(buy_executions) + len(sell_executions)

        return ViewMetrics(
            view_name=ViewName.ALPACA_BROKER,
            currency=ViewCurrency.USD,
            source=ViewSource.ALPACA_PAPER_LEDGER,
            symbol_mapping=mapping.symbols,
            initial_equity=initial_equity,
            ending_equity=ending_equity,
            nominal_net_pnl=nominal_net_pnl,
            fees=fees_total,
            net_return_pct=net_return_pct,
            max_drawdown_pct=max_dd,
            turnover=turnover,
            exposure=exposure,
            sharpe_reference=sharpe,
            fill_count=fill_count,
            partial_fill_count=partial_fill_count,
            missing_observation_count=missing_observation_count,
            cash_benchmark_return_pct=cash_bench,
            cash_benchmark_delta_pct=cash_delta,
            btc_eth_benchmark_return_pct=btc_eth_bench,
            btc_eth_benchmark_delta_pct=btc_eth_delta,
            canonical_snapshot_hashes=(),
            experiment_hash=self._experiment_hash,
            cohort_hash=self._cohort_hash,
            epoch_id=self._epoch.epoch_id,
            config_hash=self._epoch.config_hash,
        )

    async def compute_shadow_view(
        self,
        snapshot_reader: _SnapshotReader,
        target_weights: dict[str, Decimal],
        *,
        cohort_id: str,
        since: datetime,
    ) -> ViewMetrics:
        """Compute View 3: Canonical shadow P&L (native USDT).

        Reads ``CanonicalMarketSnapshot`` rows and applies the frozen
        fill/cost model from :class:`FillCostPolicy`.
        """
        snapshots = await snapshot_reader.list_snapshots(
            cohort_id=cohort_id, since=since
        )

        fcp = self._config.fill_cost_policy
        pnl_samples: list[Decimal] = []
        turnover = _ZERO
        missing_observation_count = 0
        position_notionals: list[Decimal] = []
        consumed_hashes: list[str] = []
        fees_total = _ZERO

        for snap in snapshots:
            payload = snap.payload
            content_hash = snap.content_hash

            if not isinstance(payload, dict):
                missing_observation_count += 1
                continue

            symbols_data = payload.get("symbols")
            if not isinstance(symbols_data, (list, tuple)) or len(symbols_data) < 1:
                missing_observation_count += 1
                continue

            snapshot_pnl = _ZERO
            snapshot_notional = _ZERO

            for sym_data in symbols_data:
                if not isinstance(sym_data, dict):
                    continue
                symbol = sym_data.get("symbol")
                if symbol is None:
                    continue

                candles = sym_data.get("candles")
                if not isinstance(candles, (list, tuple)) or len(candles) == 0:
                    missing_observation_count += 1
                    continue

                last_candle = candles[-1]
                if not isinstance(last_candle, dict):
                    missing_observation_count += 1
                    continue

                close_raw = last_candle.get("close")
                if close_raw is None:
                    missing_observation_count += 1
                    continue

                try:
                    canonical_close = Decimal(str(close_raw))
                except Exception:
                    missing_observation_count += 1
                    continue

                if not canonical_close.is_finite() or canonical_close <= _ZERO:
                    missing_observation_count += 1
                    continue

                weight = target_weights.get(symbol, _ZERO)
                if weight <= _ZERO:
                    continue

                initial_equity_shadow = self._epoch.initial_equity[
                    ViewName.CANONICAL_SHADOW
                ]
                target_notional = initial_equity_shadow * weight
                snapshot_notional += abs(target_notional)

                fill_price = self._apply_slippage(
                    canonical_close, fcp.slippage_bps, is_buy=True
                )
                slippage_cost = abs(fill_price - canonical_close) * weight
                fee = target_notional * fcp.fee_rate_bps / Decimal("10000")
                spread_cost = target_notional * fcp.spread_bps / Decimal("10000")

                fees_total += fee
                snapshot_pnl += -(fee + spread_cost + slippage_cost)

            if snapshot_notional > _ZERO:
                turnover += snapshot_notional
                position_notionals.append(snapshot_notional)

            pnl_samples.append(snapshot_pnl)
            consumed_hashes.append(content_hash)

        nominal_net_pnl = sum(pnl_samples, start=_ZERO)
        mapping = self._config.views[ViewName.CANONICAL_SHADOW]
        initial_equity = self._epoch.initial_equity[ViewName.CANONICAL_SHADOW]
        ending_equity = initial_equity + nominal_net_pnl
        net_return_pct = self._safe_return_pct(ending_equity, initial_equity)

        equity_curve = _build_equity_curve(initial_equity, pnl_samples)
        max_dd = _compute_max_drawdown_pct(equity_curve)
        exposure = _compute_exposure(position_notionals, initial_equity)

        per_trade_returns = self._pnl_to_returns(pnl_samples, initial_equity)
        sharpe = _compute_sharpe(
            per_trade_returns,
            self._config.annualization.periods_per_year,
            self._config.annualization.risk_free_rate_pct,
        )

        cash_bench = _ZERO
        btc_eth_bench = _ZERO
        cash_delta, btc_eth_delta = _compute_benchmark_deltas(
            net_return_pct, cash_bench, btc_eth_bench
        )

        fill_count = len(pnl_samples)
        partial_fill_count = 0

        return ViewMetrics(
            view_name=ViewName.CANONICAL_SHADOW,
            currency=ViewCurrency.USDT,
            source=ViewSource.CANONICAL_MARKET_SNAPSHOT,
            symbol_mapping=mapping.symbols,
            initial_equity=initial_equity,
            ending_equity=ending_equity,
            nominal_net_pnl=nominal_net_pnl,
            fees=fees_total,
            net_return_pct=net_return_pct,
            max_drawdown_pct=max_dd,
            turnover=turnover,
            exposure=exposure,
            sharpe_reference=sharpe,
            fill_count=fill_count,
            partial_fill_count=partial_fill_count,
            missing_observation_count=missing_observation_count,
            cash_benchmark_return_pct=cash_bench,
            cash_benchmark_delta_pct=cash_delta,
            btc_eth_benchmark_return_pct=btc_eth_bench,
            btc_eth_benchmark_delta_pct=btc_eth_delta,
            canonical_snapshot_hashes=tuple(consumed_hashes),
            experiment_hash=self._experiment_hash,
            cohort_hash=self._cohort_hash,
            epoch_id=self._epoch.epoch_id,
            config_hash=self._epoch.config_hash,
        )

    # ------------------------------------------------------------------
    # Benchmark computation
    # ------------------------------------------------------------------

    def compute_cash_benchmark(
        self,
        *,
        elapsed_periods: int,
    ) -> Decimal:
        """Compute the risk-free cash benchmark return percentage.

        The cash benchmark is the annualized risk-free rate prorated
        for the elapsed number of periods.
        """
        rf = self._config.annualization.risk_free_rate_pct
        ppy = self._config.annualization.periods_per_year
        if ppy <= 0:
            return _ZERO
        per_period = rf / Decimal(ppy)
        return per_period * Decimal(elapsed_periods)

    def compute_btc_eth_benchmark(
        self,
        *,
        btc_returns_pct: Sequence[Decimal],
        eth_returns_pct: Sequence[Decimal],
    ) -> Decimal:
        """Compute BTC/ETH equal-weight benchmark return percentage.

        Uses the frozen ``BenchmarkWeights`` from the config.
        """
        bw = self._config.benchmark_weights
        btc_total = sum(btc_returns_pct, start=_ZERO)
        eth_total = sum(eth_returns_pct, start=_ZERO)
        return btc_total * bw.btc_weight + eth_total * bw.eth_weight

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _safe_return_pct(ending: Decimal, initial: Decimal) -> Decimal:
        """Compute (ending - initial) / initial * 100, guard against zero."""
        if initial == _ZERO:
            return _ZERO
        return (ending - initial) / initial * _HUNDRED

    @staticmethod
    def _pnl_to_returns(
        pnl_samples: Sequence[Decimal], initial_equity: Decimal
    ) -> list[Decimal]:
        """Convert absolute P&L samples to percentage returns."""
        if initial_equity == _ZERO:
            return [_ZERO] * len(pnl_samples)
        return [pnl / initial_equity * _HUNDRED for pnl in pnl_samples]

    @staticmethod
    def _apply_slippage(
        price: Decimal, slippage_bps: Decimal, *, is_buy: bool
    ) -> Decimal:
        """Apply slippage to a fill price.

        Buys: price * (1 + slippage_bps/10000)
        Sells: price * (1 - slippage_bps/10000)
        """
        factor = _ONE + slippage_bps / Decimal("10000") if is_buy else _ONE - slippage_bps / Decimal("10000")
        return price * factor

    def _compute_alpaca_roundtrip_pnl(
        self,
        buys: Sequence[AlpacaPaperOrderLedger],
        sells: Sequence[AlpacaPaperOrderLedger],
    ) -> Decimal:
        """Compute nominal P&L from buy/sell roundtrip pairs.

        Pairs buys and sells by symbol; for each roundtrip:
        P&L = sell_notional - buy_notional (positive = profit)
        """
        from collections import defaultdict

        buy_by_symbol: dict[str, list[Decimal]] = defaultdict(list)
        sell_by_symbol: dict[str, list[Decimal]] = defaultdict(list)

        for buy in buys:
            symbol = getattr(buy, "execution_symbol", "") or ""
            filled_qty_raw = getattr(buy, "filled_qty", None)
            filled_price_raw = getattr(buy, "filled_avg_price", None)
            if filled_qty_raw is not None and filled_price_raw is not None:
                try:
                    qty = Decimal(str(filled_qty_raw))
                    price = Decimal(str(filled_price_raw))
                    if qty.is_finite() and price.is_finite():
                        buy_by_symbol[symbol].append(qty * price)
                except Exception:
                    pass

        for sell in sells:
            symbol = getattr(sell, "execution_symbol", "") or ""
            filled_qty_raw = getattr(sell, "filled_qty", None)
            filled_price_raw = getattr(sell, "filled_avg_price", None)
            if filled_qty_raw is not None and filled_price_raw is not None:
                try:
                    qty = Decimal(str(filled_qty_raw))
                    price = Decimal(str(filled_price_raw))
                    if qty.is_finite() and price.is_finite():
                        sell_by_symbol[symbol].append(qty * price)
                except Exception:
                    pass

        total_pnl = _ZERO
        all_symbols = set(buy_by_symbol) | set(sell_by_symbol)
        for symbol in all_symbols:
            buy_notional = sum(buy_by_symbol[symbol], start=_ZERO)
            sell_notional = sum(sell_by_symbol[symbol], start=_ZERO)
            total_pnl += sell_notional - buy_notional

        return total_pnl

    def _extract_alpaca_pnl_samples(
        self,
        buys: Sequence[AlpacaPaperOrderLedger],
        sells: Sequence[AlpacaPaperOrderLedger],
    ) -> list[Decimal]:
        """Extract per-roundtrip P&L samples for equity curve / Sharpe."""
        from collections import defaultdict

        buy_by_symbol: dict[str, list[tuple[Decimal, Decimal]]] = defaultdict(list)
        sell_by_symbol: dict[str, list[tuple[Decimal, Decimal]]] = defaultdict(list)

        for buy in buys:
            symbol = getattr(buy, "execution_symbol", "") or ""
            qty_raw = getattr(buy, "filled_qty", None)
            price_raw = getattr(buy, "filled_avg_price", None)
            if qty_raw is not None and price_raw is not None:
                try:
                    qty = Decimal(str(qty_raw))
                    price = Decimal(str(price_raw))
                    if qty.is_finite() and price.is_finite():
                        buy_by_symbol[symbol].append((qty, price))
                except Exception:
                    pass

        for sell in sells:
            symbol = getattr(sell, "execution_symbol", "") or ""
            qty_raw = getattr(sell, "filled_qty", None)
            price_raw = getattr(sell, "filled_avg_price", None)
            if qty_raw is not None and price_raw is not None:
                try:
                    qty = Decimal(str(qty_raw))
                    price = Decimal(str(price_raw))
                    if qty.is_finite() and price.is_finite():
                        sell_by_symbol[symbol].append((qty, price))
                except Exception:
                    pass

        samples: list[Decimal] = []
        all_symbols = set(buy_by_symbol) | set(sell_by_symbol)
        for symbol in all_symbols:
            buys_list = buy_by_symbol[symbol]
            sells_list = sell_by_symbol[symbol]
            buy_total_notional = sum(
                (qty * price for qty, price in buys_list), start=_ZERO
            )
            buy_total_qty = sum((qty for qty, _ in buys_list), start=_ZERO)
            sell_total_notional = sum(
                (qty * price for qty, price in sells_list), start=_ZERO
            )
            sell_total_qty = sum((qty for qty, _ in sells_list), start=_ZERO)

            if buy_total_qty > _ZERO and sell_total_qty > _ZERO:
                avg_buy_price = buy_total_notional / buy_total_qty
                avg_sell_price = sell_total_notional / sell_total_qty
                matched_qty = min(buy_total_qty, sell_total_qty)
                samples.append(
                    (avg_sell_price - avg_buy_price) * matched_qty
                )
            elif buy_total_qty > _ZERO:
                samples.append(-buy_total_notional)
            elif sell_total_qty > _ZERO:
                samples.append(sell_total_notional)

        return samples


__all__ = [
    "PaperEvaluationPnL",
]
