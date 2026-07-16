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
from collections import defaultdict
from collections.abc import Mapping, Sequence
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
from app.services.paper_evaluation.epoch import compute_calendar_days

if TYPE_CHECKING:
    from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
    from app.models.paper_cohort import CanonicalMarketSnapshot
    from app.models.review import AlpacaPaperOrderLedger
    from app.services.paper_evaluation.evidence import (
        EvaluationWindow,
        NativeFill,
        NativeMark,
        ShadowObservation,
    )


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


def _benchmark_return_pct(
    marks: Mapping[str, tuple[Decimal, Decimal]],
    weights: Mapping[str, Decimal],
) -> Decimal:
    """Compute a frozen-weight first-mark to last-mark return."""
    total = _ZERO
    for symbol, weight in weights.items():
        try:
            start, end = marks[symbol]
        except KeyError as exc:
            raise EvaluationConfigError(
                "insufficient_evidence", f"missing benchmark marks for {symbol}"
            ) from exc
        start = _validate_finite(start, f"{symbol} benchmark start")
        end = _validate_finite(end, f"{symbol} benchmark end")
        if start <= 0 or end <= 0:
            raise EvaluationConfigError(
                "insufficient_evidence", f"non-positive benchmark mark for {symbol}"
            )
        total += weight * ((end / start) - _ONE) * _HUNDRED
    return total


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
        raise EvaluationConfigError("non_finite_value", f"{context}: value is None")
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


def _compute_exposure(positions: Sequence[Decimal], equity: Decimal) -> Decimal:
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
        evaluated_at: datetime | None = None,
        benchmark_marks: Mapping[str, tuple[Decimal, Decimal]] | None = None,
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
        if evaluated_at is not None:
            days = compute_calendar_days(since, evaluated_at)
            cash_bench = (
                self._config.annualization.risk_free_rate_pct
                * Decimal(days)
                / Decimal("365")
            )
        if benchmark_marks is not None:
            symbols = mapping.benchmark_symbols
            btc_eth_bench = _benchmark_return_pct(
                benchmark_marks,
                {
                    symbols[0]: self._config.benchmark_weights.btc_weight,
                    symbols[1]: self._config.benchmark_weights.eth_weight,
                },
            )
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
        evaluated_at: datetime | None = None,
        native_marks: Mapping[str, Decimal] | None = None,
        benchmark_marks: Mapping[str, tuple[Decimal, Decimal]] | None = None,
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
                "canceled",
            }
        )
        _RECORD_KIND_EXECUTION = "execution"

        buy_executions: list[AlpacaPaperOrderLedger] = []
        sell_executions: list[AlpacaPaperOrderLedger] = []
        execution_rows: list[AlpacaPaperOrderLedger] = []
        fee_rows: list[AlpacaPaperOrderLedger] = []
        missing_observation_count = 0
        partial_fill_count = 0
        turnover = _ZERO

        for corr_id in correlation_ids:
            rows = await ledger.list_by_correlation_id(corr_id)
            for row in rows:
                if row.record_kind != _RECORD_KIND_EXECUTION:
                    if (
                        row.record_kind == "reconcile"
                        and row.lifecycle_state == "final_reconciled"
                    ):
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

                # Parse qty safely
                filled_qty = None
                qty_invalid = False
                if filled_qty_raw is not None:
                    try:
                        filled_qty = Decimal(str(filled_qty_raw))
                        if not filled_qty.is_finite():
                            qty_invalid = True
                    except Exception:
                        qty_invalid = True

                # If qty is invalid, then it's a missing/invalid observation for both canceled and normal states.
                if qty_invalid:
                    missing_observation_count += 1
                    continue

                # Zero-fill cancels have: row.lifecycle_state == "canceled" AND (qty is None or qty == 0)
                is_zero_fill_cancel = row.lifecycle_state == "canceled" and (
                    filled_qty is None or filled_qty == 0
                )

                if is_zero_fill_cancel:
                    # benign zero-fill cancel: skip completely without incrementing missing_observation_count
                    continue

                # For any other case, both qty and price must be valid and positive.
                # If either is missing/invalid, increment missing_observation_count
                if filled_qty is None or filled_price_raw is None:
                    missing_observation_count += 1
                    continue

                try:
                    filled_price = Decimal(str(filled_price_raw))
                except Exception:
                    missing_observation_count += 1
                    continue

                if not filled_price.is_finite() or filled_qty <= 0 or filled_price <= 0:
                    missing_observation_count += 1
                    continue

                notional = filled_qty * filled_price
                turnover += abs(notional)
                execution_rows.append(row)

                side_lower = (row.side or "").lower()
                if side_lower == "buy":
                    buy_executions.append(row)
                elif side_lower == "sell":
                    sell_executions.append(row)

                partial_meta = getattr(row, "position_snapshot", None)
                if isinstance(partial_meta, dict):
                    if _is_partial_fill(partial_meta):
                        partial_fill_count += 1

        mapping = self._config.views[ViewName.ALPACA_BROKER]
        initial_equity = self._epoch.initial_equity[ViewName.ALPACA_BROKER]
        cash = initial_equity
        inventory: dict[str, Decimal] = {}
        last_fill_price: dict[str, Decimal] = {}
        fees_total = _ZERO
        executions = execution_rows
        for row in executions:
            symbol = row.execution_symbol
            qty = _to_decimal_safe(row.filled_qty, f"alpaca row {row.id} qty")
            price = _to_decimal_safe(row.filled_avg_price, f"alpaca row {row.id} price")
            fee = _ZERO
            if row.fee_amount is not None:
                fee = _to_decimal_safe(row.fee_amount, f"alpaca row {row.id} fee")
                if fee < 0 or (row.fee_currency or "USD") != "USD":
                    raise EvaluationConfigError(
                        "currency_mismatch", "Alpaca fees must be non-negative USD"
                    )
            last_fill_price[symbol] = price
            current_qty = inventory.get(symbol, _ZERO)
            if row.side.lower() == "buy":
                cash -= qty * price + fee
                inventory[symbol] = current_qty + qty
            else:
                if qty > current_qty:
                    missing_observation_count += 1
                    continue
                cash += qty * price - fee
                inventory[symbol] = current_qty - qty
            fees_total += fee

        for fee_row in fee_rows:
            fee_amount = getattr(fee_row, "fee_amount", None)
            if fee_amount is not None:
                try:
                    fee = Decimal(str(fee_amount))
                    if fee.is_finite():
                        fees_total += fee
                        cash -= fee
                except Exception:
                    missing_observation_count += 1

        ending_equity = cash
        open_notionals: list[Decimal] = []
        for symbol, qty in inventory.items():
            if qty == 0:
                continue
            mark = None if native_marks is None else native_marks.get(symbol)
            if mark is None:
                missing_observation_count += 1
                mark = last_fill_price[symbol]
            mark = _validate_finite(mark, f"Alpaca native USD mark {symbol}")
            if mark <= 0:
                raise EvaluationConfigError(
                    "insufficient_evidence", f"invalid Alpaca mark for {symbol}"
                )
            notional = qty * mark
            ending_equity += notional
            open_notionals.append(abs(notional))
        nominal_net_pnl = ending_equity - initial_equity
        net_return_pct = self._safe_return_pct(ending_equity, initial_equity)

        pnl_samples = [nominal_net_pnl] if executions else []
        equity_curve = _build_equity_curve(initial_equity, pnl_samples)
        max_dd = _compute_max_drawdown_pct(equity_curve)
        exposure = _compute_exposure(open_notionals, initial_equity)

        per_trade_returns = self._pnl_to_returns(pnl_samples, initial_equity)
        sharpe = _compute_sharpe(
            per_trade_returns,
            self._config.annualization.periods_per_year,
            self._config.annualization.risk_free_rate_pct,
        )

        cash_bench = _ZERO
        btc_eth_bench = _ZERO
        if evaluated_at is not None:
            days = compute_calendar_days(self._epoch.started_at, evaluated_at)
            cash_bench = (
                self._config.annualization.risk_free_rate_pct
                * Decimal(days)
                / Decimal("365")
            )
        if benchmark_marks is not None:
            symbols = mapping.benchmark_symbols
            btc_eth_bench = _benchmark_return_pct(
                benchmark_marks,
                {
                    symbols[0]: self._config.benchmark_weights.btc_weight,
                    symbols[1]: self._config.benchmark_weights.eth_weight,
                },
            )
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
        target_weights: Mapping[str, Decimal],
        *,
        cohort_id: str,
        since: datetime,
        evaluated_at: datetime | None = None,
    ) -> ViewMetrics:
        """Compute View 3: Canonical shadow P&L (native USDT).

        Reads ``CanonicalMarketSnapshot`` rows and applies the frozen
        fill/cost model from :class:`FillCostPolicy`.
        """
        snapshots = await snapshot_reader.list_snapshots(
            cohort_id=cohort_id, since=since
        )

        fcp = self._config.fill_cost_policy
        parsed: list[tuple[str, dict[str, Decimal], dict[str, Decimal]]] = []
        seen_hashes: set[str] = set()
        missing_observation_count = 0
        for snap in snapshots:
            if snap.content_hash in seen_hashes:
                raise EvaluationConfigError(
                    "invalid_evidence", "duplicate canonical snapshot hash"
                )
            seen_hashes.add(snap.content_hash)
            payload = snap.payload
            symbols_data = payload.get("symbols") if isinstance(payload, dict) else None
            if not isinstance(symbols_data, (list, tuple)) or not symbols_data:
                missing_observation_count += 1
                continue
            closes: dict[str, Decimal] = {}
            opens: dict[str, Decimal] = {}
            malformed = False
            for sym_data in symbols_data:
                if not isinstance(sym_data, dict) or not isinstance(
                    sym_data.get("symbol"), str
                ):
                    malformed = True
                    continue
                candles = sym_data.get("candles")
                if not isinstance(candles, (list, tuple)) or not candles:
                    malformed = True
                    continue
                candle = candles[-1]
                if not isinstance(candle, dict):
                    malformed = True
                    continue
                try:
                    close = _to_decimal_safe(
                        candle.get("close"), f"{sym_data['symbol']} close"
                    )
                    open_price = _to_decimal_safe(
                        candle.get("open"), f"{sym_data['symbol']} open"
                    )
                except EvaluationConfigError:
                    malformed = True
                    continue
                if close <= 0 or open_price <= 0:
                    malformed = True
                    continue
                closes[sym_data["symbol"]] = close
                opens[sym_data["symbol"]] = open_price
            if malformed or any(symbol not in closes for symbol in target_weights):
                missing_observation_count += 1
                continue
            parsed.append((snap.content_hash, closes, opens))

        mapping = self._config.views[ViewName.CANONICAL_SHADOW]
        initial_equity = self._epoch.initial_equity[ViewName.CANONICAL_SHADOW]
        cash = initial_equity
        positions = dict.fromkeys(target_weights, _ZERO)
        equity_curve = [initial_equity]
        position_notionals: list[Decimal] = []
        consumed_hashes: list[str] = []
        turnover = _ZERO
        fees_total = _ZERO
        fill_count = 0
        partial_fill_count = 0

        for index, (content_hash, closes, _opens) in enumerate(parsed):
            equity = cash + sum(
                positions[symbol] * closes[symbol] for symbol in positions
            )
            deltas: dict[str, Decimal] = {}
            for symbol, weight in target_weights.items():
                weight = _validate_finite(weight, f"target weight {symbol}")
                if weight < 0:
                    raise EvaluationConfigError(
                        "invalid_evidence", f"negative target weight for {symbol}"
                    )
                deltas[symbol] = equity * weight / closes[symbol] - positions[symbol]

            has_delta = any(delta != 0 for delta in deltas.values())
            fill_prices = closes
            valuation_prices = closes
            if (
                has_delta
                and self._config.mark_fill_timing.fill_timing == "next_bar_open"
            ):
                if index + 1 >= len(parsed):
                    missing_observation_count += 1
                    consumed_hashes.append(content_hash)
                    equity_curve.append(equity)
                    continue
                _, valuation_prices, next_opens = parsed[index + 1]
                fill_prices = next_opens

            if has_delta:
                ratio = _ONE
                if fcp.partial_fill_policy.value == "accept_partial_with_evidence":
                    ratio = fcp.partial_fill_ratio
                    partial_fill_count += 1
                total_bps = fcp.fee_rate_bps + fcp.spread_bps + fcp.slippage_bps
                for symbol, requested_delta in deltas.items():
                    delta = requested_delta * ratio
                    if delta == 0:
                        continue
                    fill_price = fill_prices[symbol]
                    executed_notional = abs(delta) * fill_price
                    cost = executed_notional * total_bps / Decimal("10000")
                    fee = executed_notional * fcp.fee_rate_bps / Decimal("10000")
                    cash -= delta * fill_price + cost
                    positions[symbol] += delta
                    turnover += executed_notional
                    fees_total += fee
                fill_count += 1

            ending_at_observation = cash + sum(
                positions[symbol] * valuation_prices[symbol] for symbol in positions
            )
            equity_curve.append(ending_at_observation)
            position_notionals.append(
                sum(
                    abs(positions[symbol] * valuation_prices[symbol])
                    for symbol in positions
                )
            )
            consumed_hashes.append(content_hash)

        ending_equity = equity_curve[-1]
        nominal_net_pnl = ending_equity - initial_equity
        net_return_pct = self._safe_return_pct(ending_equity, initial_equity)

        max_dd = _compute_max_drawdown_pct(equity_curve)
        exposure = _compute_exposure(position_notionals, initial_equity)

        pnl_samples = [
            equity_curve[index] - equity_curve[index - 1]
            for index in range(1, len(equity_curve))
        ]
        per_trade_returns = self._pnl_to_returns(pnl_samples, initial_equity)
        sharpe = _compute_sharpe(
            per_trade_returns,
            self._config.annualization.periods_per_year,
            self._config.annualization.risk_free_rate_pct,
        )

        cash_bench = _ZERO
        if evaluated_at is not None:
            cash_bench = (
                self._config.annualization.risk_free_rate_pct
                * Decimal(compute_calendar_days(since, evaluated_at))
                / Decimal("365")
            )
        btc_eth_bench = _ZERO
        if len(parsed) >= 2:
            symbols = mapping.benchmark_symbols
            btc_eth_bench = _benchmark_return_pct(
                {
                    symbol: (parsed[0][1][symbol], parsed[-1][1][symbol])
                    for symbol in symbols
                },
                {
                    symbols[0]: self._config.benchmark_weights.btc_weight,
                    symbols[1]: self._config.benchmark_weights.eth_weight,
                },
            )
        cash_delta, btc_eth_delta = _compute_benchmark_deltas(
            net_return_pct, cash_bench, btc_eth_bench
        )

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

    def compute_native_evidence_view(
        self,
        *,
        view_name: ViewName,
        fills: Sequence[NativeFill],
        marks: Sequence[NativeMark],
        window: EvaluationWindow,
    ) -> ViewMetrics:
        """Account for exact linked native fills and mark open inventory."""
        if view_name not in {ViewName.BINANCE_BROKER, ViewName.ALPACA_BROKER}:
            raise EvaluationConfigError("invalid_view_mapping")
        mapping = self._config.views[view_name]
        initial = self._epoch.initial_equity[view_name]
        cash = initial
        positions: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        turnover = fees = _ZERO
        partial_count = 0
        equity_curve = [initial]
        by_symbol: dict[str, list[NativeMark]] = defaultdict(list)
        for mark in marks:
            if not window.start <= mark.marked_at <= window.end:
                raise EvaluationConfigError("invalid_evaluation_window")
            by_symbol[mark.symbol].append(mark)
        for symbol in mapping.benchmark_symbols:
            by_symbol[symbol].sort(key=lambda item: item.marked_at)
            if not by_symbol[symbol]:
                raise EvaluationConfigError(
                    "insufficient_evidence",
                    f"missing native mark for {symbol}",
                )
        for fill in fills:
            if not window.start <= fill.filled_at <= window.end:
                raise EvaluationConfigError("invalid_evaluation_window")
            notional = fill.quantity * fill.price
            turnover += notional
            fees += fill.fee
            if fill.side == "buy":
                cash -= notional + fill.fee
                positions[fill.symbol] += fill.quantity
            else:
                if positions[fill.symbol] < fill.quantity:
                    raise EvaluationConfigError(
                        "insufficient_evidence", "sell exceeds linked inventory"
                    )
                cash += notional - fill.fee
                positions[fill.symbol] -= fill.quantity
            partial_count += int(fill.partial)
            asof_marks: dict[str, Decimal] = {}
            for symbol, quantity in positions.items():
                if not quantity:
                    continue
                candidates = [
                    mark.price
                    for mark in by_symbol[symbol]
                    if mark.marked_at <= fill.filled_at
                ]
                if candidates:
                    asof_marks[symbol] = candidates[-1]
                elif symbol == fill.symbol:
                    asof_marks[symbol] = fill.price
                else:
                    raise EvaluationConfigError(
                        "insufficient_evidence",
                        f"missing as-of native mark for {symbol}",
                    )
            equity_curve.append(
                cash
                + sum(
                    quantity * asof_marks.get(symbol, _ZERO)
                    for symbol, quantity in positions.items()
                )
            )
        latest_marks = {
            symbol: symbol_marks[-1].price
            for symbol, symbol_marks in by_symbol.items()
            if symbol_marks
        }
        for symbol, quantity in positions.items():
            if quantity and symbol not in latest_marks:
                raise EvaluationConfigError(
                    "insufficient_evidence", f"missing native mark for {symbol}"
                )
        ending = cash + sum(
            quantity * latest_marks.get(symbol, _ZERO)
            for symbol, quantity in positions.items()
        )
        equity_curve.append(ending)
        net_return = self._safe_return_pct(ending, initial)
        symbols = mapping.benchmark_symbols
        benchmark = _benchmark_return_pct(
            {
                symbol: (
                    by_symbol[symbol][0].price,
                    by_symbol[symbol][-1].price,
                )
                for symbol in symbols
                if by_symbol[symbol]
            },
            {
                symbols[0]: self._config.benchmark_weights.btc_weight,
                symbols[1]: self._config.benchmark_weights.eth_weight,
            },
        )
        cash_benchmark = (
            self._config.annualization.risk_free_rate_pct
            * Decimal(compute_calendar_days(window.start, window.end))
            / Decimal("365")
        )
        cash_delta, benchmark_delta = _compute_benchmark_deltas(
            net_return, cash_benchmark, benchmark
        )
        notionals = [
            quantity * latest_marks.get(symbol, _ZERO)
            for symbol, quantity in positions.items()
        ]
        return ViewMetrics(
            view_name=view_name,
            currency=mapping.currency,
            source=mapping.source,
            symbol_mapping=mapping.symbols,
            initial_equity=initial,
            ending_equity=ending,
            nominal_net_pnl=ending - initial,
            fees=fees,
            net_return_pct=net_return,
            max_drawdown_pct=_compute_max_drawdown_pct(equity_curve),
            turnover=turnover,
            exposure=_compute_exposure(notionals, initial),
            fill_count=len(fills),
            observation_count=len(fills),
            partial_fill_count=partial_count,
            missing_observation_count=0,
            cash_benchmark_return_pct=cash_benchmark,
            cash_benchmark_delta_pct=cash_delta,
            btc_eth_benchmark_return_pct=benchmark,
            btc_eth_benchmark_delta_pct=benchmark_delta,
            experiment_hash=self._experiment_hash,
            cohort_hash=self._cohort_hash,
            epoch_id=self._epoch.epoch_id,
            config_hash=self._epoch.config_hash,
        )

    def compute_shadow_evidence_view(
        self,
        *,
        observations: Sequence[ShadowObservation],
        window: EvaluationWindow,
    ) -> ViewMetrics:
        """Rebalance target deltas through time and mark positions each snapshot."""
        mapping = self._config.views[ViewName.CANONICAL_SHADOW]
        initial = self._epoch.initial_equity[ViewName.CANONICAL_SHADOW]
        cash = initial
        positions: dict[str, Decimal] = defaultdict(lambda: _ZERO)
        fees = turnover = _ZERO
        fill_count = partial_count = 0
        curve = [initial]
        consumed: list[str] = []
        timing = self._config.mark_fill_timing.fill_timing
        canonical_bars: dict[
            tuple[datetime, datetime],
            tuple[dict[str, Decimal], dict[str, Decimal]],
        ] = {}
        for observation in observations:
            source_bars = observation.candle_bars or (
                (
                    observation.candle_open_at,
                    observation.candle_close_at,
                    observation.opens,
                    observation.closes,
                ),
            )
            for opened_at, closed_at, opens, closes in source_bars:
                key = (opened_at, closed_at)
                value = (dict(opens), dict(closes))
                previous = canonical_bars.get(key)
                if previous is not None and previous != value:
                    raise EvaluationConfigError(
                        "cross_wired_evidence", "conflicting canonical candle"
                    )
                canonical_bars[key] = value
        ordered_bars = sorted(canonical_bars.items())
        last_mark_prices: dict[str, Decimal] | None = None
        for observation in observations:
            if not window.start <= observation.observed_at < window.end:
                raise EvaluationConfigError("invalid_evaluation_window")
            closes = dict(observation.closes)
            equity = cash + sum(
                quantity * closes[symbol] for symbol, quantity in positions.items()
            )
            weights = dict(observation.target_weights)
            deltas: dict[str, Decimal] = {}
            for symbol in mapping.symbols:
                target_qty = equity * weights.get(symbol, _ZERO) / closes[symbol]
                deltas[symbol] = target_qty - positions[symbol]
            if timing == "next_bar_open" and any(deltas.values()):
                next_bar = next(
                    (
                        (key, prices)
                        for key, prices in ordered_bars
                        if key[0] > observation.candle_close_at
                    ),
                    None,
                )
                if next_bar is None:
                    raise EvaluationConfigError(
                        "insufficient_evidence", "next canonical bar missing"
                    )
                (next_open_at, _next_close_at), (fill_prices, mark_prices) = next_bar
                gap = next_open_at - observation.candle_close_at
                if gap.total_seconds() < 0 or gap.total_seconds() > 1:
                    raise EvaluationConfigError(
                        "insufficient_evidence",
                        "next canonical candle is not contiguous",
                    )
            else:
                fill_prices = closes
                mark_prices = closes
            for symbol, requested_delta in deltas.items():
                if requested_delta == 0:
                    continue
                ratio = _ONE
                if (
                    self._config.fill_cost_policy.partial_fill_policy.value
                    == "accept_partial_with_evidence"
                ):
                    ratio = self._config.fill_cost_policy.partial_fill_ratio
                delta = requested_delta * ratio
                partial_count += int(ratio < _ONE)
                fill_price = fill_prices[symbol]
                notional = abs(delta) * fill_price
                cost = (
                    notional
                    * (
                        self._config.fill_cost_policy.fee_rate_bps
                        + self._config.fill_cost_policy.spread_bps
                        + self._config.fill_cost_policy.slippage_bps
                    )
                    / Decimal("10000")
                )
                cash -= delta * fill_price + cost
                positions[symbol] += delta
                fees += cost
                turnover += notional
                fill_count += 1
            curve.append(
                cash
                + sum(
                    quantity * mark_prices[symbol]
                    for symbol, quantity in positions.items()
                )
            )
            last_mark_prices = mark_prices
            consumed.append(observation.snapshot_hash)
        if not observations:
            raise EvaluationConfigError(
                "insufficient_evidence", "shadow evidence missing"
            )
        final_closes = last_mark_prices or dict(observations[-1].closes)
        ending = cash + sum(
            quantity * final_closes[symbol] for symbol, quantity in positions.items()
        )
        net_return = self._safe_return_pct(ending, initial)
        symbols = mapping.benchmark_symbols
        first, last = dict(observations[0].closes), dict(observations[-1].closes)
        benchmark = _benchmark_return_pct(
            {symbol: (first[symbol], last[symbol]) for symbol in symbols},
            {
                symbols[0]: self._config.benchmark_weights.btc_weight,
                symbols[1]: self._config.benchmark_weights.eth_weight,
            },
        )
        cash_benchmark = (
            self._config.annualization.risk_free_rate_pct
            * Decimal(compute_calendar_days(window.start, window.end))
            / Decimal("365")
        )
        cash_delta, benchmark_delta = _compute_benchmark_deltas(
            net_return, cash_benchmark, benchmark
        )
        return ViewMetrics(
            view_name=ViewName.CANONICAL_SHADOW,
            currency=mapping.currency,
            source=mapping.source,
            symbol_mapping=mapping.symbols,
            initial_equity=initial,
            ending_equity=ending,
            nominal_net_pnl=ending - initial,
            fees=fees,
            net_return_pct=net_return,
            max_drawdown_pct=_compute_max_drawdown_pct(curve),
            turnover=turnover,
            exposure=_compute_exposure(
                [positions[s] * final_closes[s] for s in mapping.symbols], initial
            ),
            fill_count=fill_count,
            observation_count=len(observations),
            partial_fill_count=partial_count,
            missing_observation_count=0,
            cash_benchmark_return_pct=cash_benchmark,
            cash_benchmark_delta_pct=cash_delta,
            btc_eth_benchmark_return_pct=benchmark,
            btc_eth_benchmark_delta_pct=benchmark_delta,
            canonical_snapshot_hashes=tuple(consumed),
            experiment_hash=self._experiment_hash,
            cohort_hash=self._cohort_hash,
            epoch_id=self._epoch.epoch_id,
            config_hash=self._epoch.config_hash,
        )

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
        factor = (
            _ONE + slippage_bps / Decimal("10000")
            if is_buy
            else _ONE - slippage_bps / Decimal("10000")
        )
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
                samples.append((avg_sell_price - avg_buy_price) * matched_qty)
            elif buy_total_qty > _ZERO:
                samples.append(-buy_total_notional)
            elif sell_total_qty > _ZERO:
                samples.append(sell_total_notional)

        return samples


__all__ = [
    "PaperEvaluationPnL",
]
