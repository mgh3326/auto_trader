"""ROB-945 (H5) -- per-scenario independent OOS ledger metrics.

Each cost scenario (base=13bp, primary_stress=17bp, upward_stress=22bp) owns
its own independent H4 ledger (``WalkForwardResult.concatenated_oos_ledgers``)
-- this module NEVER revalues one scenario's ledger to derive another's
numbers; call it once per scenario with that scenario's own ledger.

Frozen by the second Fable ruling (orch-fable-answer-rob945b-20260718.md,
2026-07-18, Q1=A): the strategy-level pass expectancy is an EXACT
four-symbol, equal-weight authority. If any of the four symbols has zero OOS
trades in this scenario, the aggregate expectancy is undefined (never a
fabricated 0bp, never silently narrowed to the eligible subset) and this
scenario's screen evidence is ``incomplete`` with reason
``insufficient_oos_symbol_evidence`` -- a symbol-level ``trade_count`` of
zero is not an execution failure (H6 attempt status/completeness is a
SEPARATE, unaffected concern), so each symbol also carries its own
``signal_count`` (captured OOS entry-signal count, pre-funding/pre-engine)
to distinguish "no signal at all" from "signal existed but was gated out".
"""

from __future__ import annotations

import math
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import rob941_frozen_scope as frozen
from rob940_engine import SignalEvent, TradeRecord

INSUFFICIENT_OOS_SYMBOL_EVIDENCE_REASON = "insufficient_oos_symbol_evidence"
NO_POSITIVE_MONTHS_REASON = "no_positive_months"
MDD_UNAVAILABLE_MISSING_SL_EVIDENCE_REASON = "mdd_unavailable_missing_sl_evidence"

_CLOSED_SIDES = frozenset({"long", "short"})
_CLOSED_EXIT_REASONS = frozenset({"take_profit", "stop_loss", "timeout"})

FROZEN_UNIVERSE: tuple[str, ...] = (
    frozen.UNIVERSE
)  # ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")


def _assert_finite(value: float, *, context: str) -> None:
    if not math.isfinite(value):
        raise ValueError(
            f"compute_scenario_metrics: non-finite raw economic input at {context} "
            f"({value!r}) -- only a DERIVED value (e.g. profit_factor) may ever be "
            "non-finite, raw trade/economic input must be finite"
        )


def _validate_trades(strategy: str, ledger: Sequence[TradeRecord]) -> None:
    seen_identity: set[tuple[str, str, str, str | None, int]] = set()
    for trade in ledger:
        if trade.strategy != strategy:
            raise ValueError(
                f"compute_scenario_metrics: trade claims strategy={trade.strategy!r} "
                f"but ledger was supplied under strategy {strategy!r}"
            )
        if trade.symbol not in FROZEN_UNIVERSE:
            raise ValueError(
                f"compute_scenario_metrics: trade symbol {trade.symbol!r} is outside "
                f"the frozen universe {FROZEN_UNIVERSE!r}"
            )
        identity = (
            trade.strategy,
            trade.config_id,
            trade.symbol,
            trade.fold_id,
            trade.signal_ts,
        )
        if identity in seen_identity:
            raise ValueError(
                f"compute_scenario_metrics: duplicate trade identity {identity!r} in ledger"
            )
        seen_identity.add(identity)
        for field_name in (
            "gross_bps",
            "fee_bps",
            "all_in_bps",
            "funding_bps",
            "net_bps",
        ):
            _assert_finite(getattr(trade, field_name), context=f"trade.{field_name}")
        for price_field in ("entry_price", "exit_price"):
            price = getattr(trade, price_field)
            if not math.isfinite(price) or price <= 0:
                raise ValueError(
                    f"compute_scenario_metrics: trade.{price_field} must be finite and "
                    f"positive, got {price!r}"
                )
        for ts_field in ("signal_ts", "entry_ts", "exit_ts"):
            ts_value = getattr(trade, ts_field)
            if isinstance(ts_value, bool) or not isinstance(ts_value, int):
                raise ValueError(
                    f"compute_scenario_metrics: trade.{ts_field} must be a plain int, "
                    f"got {ts_value!r}"
                )
        if not (trade.signal_ts <= trade.entry_ts <= trade.exit_ts):
            raise ValueError(
                "compute_scenario_metrics: trade timestamps must satisfy "
                f"signal_ts <= entry_ts <= exit_ts, got signal_ts={trade.signal_ts!r}, "
                f"entry_ts={trade.entry_ts!r}, exit_ts={trade.exit_ts!r}"
            )
        if trade.side not in _CLOSED_SIDES:
            raise ValueError(
                f"compute_scenario_metrics: trade.side {trade.side!r} outside the "
                f"closed set {sorted(_CLOSED_SIDES)!r}"
            )
        if trade.exit_reason not in _CLOSED_EXIT_REASONS:
            raise ValueError(
                f"compute_scenario_metrics: trade.exit_reason {trade.exit_reason!r} "
                f"outside the closed set {sorted(_CLOSED_EXIT_REASONS)!r}"
            )


def _validate_signals(strategy: str, captured_signals: Sequence[SignalEvent]) -> None:
    for signal in captured_signals:
        if signal.strategy != strategy:
            raise ValueError(
                f"compute_scenario_metrics: signal claims strategy={signal.strategy!r} "
                f"but captured_signals was supplied under strategy {strategy!r}"
            )
        if signal.symbol not in FROZEN_UNIVERSE:
            raise ValueError(
                f"compute_scenario_metrics: signal symbol {signal.symbol!r} is outside "
                f"the frozen universe {FROZEN_UNIVERSE!r}"
            )


@dataclass(frozen=True)
class SymbolScenarioMetrics:
    symbol: str
    trade_count: int
    signal_count: int
    net_expectancy_bps: float | None
    net_pnl_bps: float


@dataclass(frozen=True)
class StrategyScenarioAggregate:
    strategy: str
    scenario_name: str
    trade_count: int
    net_expectancy_bps: float | None
    pooled_expectancy_bps: float
    profit_factor: float
    win_rate: float | None
    net_pnl_bps: float
    timeout_ratio: float | None
    mdd_r: float | None
    mdd_reason: str | None
    monthly_concentration: float | None
    monthly_concentration_reason: str | None
    symbol_metrics: tuple[SymbolScenarioMetrics, ...] = field(default_factory=tuple)
    incomplete: bool = False
    incomplete_reason: str | None = None


def _canonical_trade_sort_key(
    trade: TradeRecord,
) -> tuple[int, str, str, str | None, int]:
    return (
        trade.entry_ts,
        trade.symbol,
        trade.config_id,
        trade.fold_id,
        trade.signal_ts,
    )


def _profit_factor(gross_profit_bps: float, gross_loss_bps: float) -> float:
    if gross_loss_bps > 0:
        return gross_profit_bps / gross_loss_bps
    if gross_profit_bps > 0:
        return math.inf
    return math.nan


def _monthly_concentration(
    ledger: Sequence[TradeRecord],
) -> tuple[float | None, str | None]:
    monthly_net: dict[tuple[int, int], float] = {}
    for trade in ledger:
        month_key = (
            datetime.fromtimestamp(trade.exit_ts / 1000, tz=UTC).year,
            datetime.fromtimestamp(trade.exit_ts / 1000, tz=UTC).month,
        )
        monthly_net[month_key] = monthly_net.get(month_key, 0.0) + trade.net_bps
    positive_months = [net for net in monthly_net.values() if net > 0]
    if not positive_months:
        return None, NO_POSITIVE_MONTHS_REASON
    return max(positive_months) / sum(positive_months), None


def _build_signal_sl_lookup(
    captured_signals: Sequence[SignalEvent],
) -> dict[tuple[str, str, str, str | None, int], float | None]:
    """Maps frozen signal identity -> ``sl_distance_bps``. Two DIFFERENT
    captured signals claiming the SAME identity with conflicting
    ``sl_distance_bps`` are ambiguous evidence -- the mapping for that
    identity becomes ``None`` (never silently resolved by picking either
    value)."""
    lookup: dict[tuple[str, str, str, str | None, int], float | None] = {}
    for sig in captured_signals:
        identity = (sig.strategy, sig.config_id, sig.symbol, sig.fold_id, sig.signal_ts)
        if identity in lookup and lookup[identity] != sig.sl_distance_bps:
            lookup[identity] = None
        else:
            lookup[identity] = sig.sl_distance_bps
    return lookup


def _mdd_r(
    ledger: Sequence[TradeRecord],
    signal_sl_by_identity: dict[tuple[str, str, str, str | None, int], float],
) -> tuple[float | None, str | None]:
    if not ledger:
        return None, None
    ordered = sorted(ledger, key=_canonical_trade_sort_key)
    cumulative_r = 0.0
    peak_r = 0.0
    max_drawdown_r = 0.0
    for trade in ordered:
        identity = (
            trade.strategy,
            trade.config_id,
            trade.symbol,
            trade.fold_id,
            trade.signal_ts,
        )
        sl_distance_bps = signal_sl_by_identity.get(identity)
        if sl_distance_bps is None or sl_distance_bps <= 0:
            return None, MDD_UNAVAILABLE_MISSING_SL_EVIDENCE_REASON
        cumulative_r += trade.net_bps / sl_distance_bps
        peak_r = max(peak_r, cumulative_r)
        max_drawdown_r = max(max_drawdown_r, peak_r - cumulative_r)
    return max_drawdown_r, None


@dataclass(frozen=True)
class FoldStabilityRow:
    fold_id: str
    selected_config_id: str | None
    trade_count: int
    net_expectancy_bps: float | None
    net_pnl_bps: float
    profit_factor: float | None
    positive: bool | None
    net_pnl_class: str | None  # "positive" | "zero" | "negative"; None if zero trades


def compute_fold_stability(
    *,
    ledger: Sequence[TradeRecord],
    fold_selected_config: dict[str, str | None],
) -> tuple[FoldStabilityRow, ...]:
    """One row per fold in ``fold_selected_config`` (every fold in the
    schedule, even one where no config was ever selected). ``positive``/
    ``net_pnl_class``/``profit_factor`` are ``None`` (undefined) when the
    fold has zero trades -- never coerced to a numeric default, since
    "zero trades" and "traded and lost" are different evidence."""
    rows = []
    for fold_id, selected_config_id in fold_selected_config.items():
        fold_trades = [t for t in ledger if t.fold_id == fold_id]
        trade_count = len(fold_trades)
        net_pnl_bps = sum(t.net_bps for t in fold_trades)
        if trade_count == 0:
            net_expectancy_bps = None
            profit_factor = None
            positive = None
            net_pnl_class = None
        else:
            net_expectancy_bps = net_pnl_bps / trade_count
            gross_profit = sum(t.net_bps for t in fold_trades if t.net_bps > 0)
            gross_loss = -sum(t.net_bps for t in fold_trades if t.net_bps < 0)
            profit_factor = _profit_factor(gross_profit, gross_loss)
            positive = net_pnl_bps > 0
            net_pnl_class = (
                "positive"
                if net_pnl_bps > 0
                else "negative"
                if net_pnl_bps < 0
                else "zero"
            )
        rows.append(
            FoldStabilityRow(
                fold_id=fold_id,
                selected_config_id=selected_config_id,
                trade_count=trade_count,
                net_expectancy_bps=net_expectancy_bps,
                net_pnl_bps=net_pnl_bps,
                profit_factor=profit_factor,
                positive=positive,
                net_pnl_class=net_pnl_class,
            )
        )
    return tuple(rows)


def compute_scenario_metrics(
    *,
    strategy: str,
    scenario_name: str,
    ledger: Sequence[TradeRecord],
    captured_signals: Sequence[SignalEvent],
) -> StrategyScenarioAggregate:
    _validate_trades(strategy, ledger)
    _validate_signals(strategy, captured_signals)
    trade_count = len(ledger)
    net_pnl_bps = sum(trade.net_bps for trade in ledger)
    gross_profit_bps = sum(trade.net_bps for trade in ledger if trade.net_bps > 0)
    gross_loss_bps = -sum(trade.net_bps for trade in ledger if trade.net_bps < 0)
    profit_factor = _profit_factor(gross_profit_bps, gross_loss_bps)
    win_rate = (
        sum(1 for trade in ledger if trade.net_bps > 0) / trade_count
        if trade_count
        else None
    )
    timeout_ratio = (
        sum(1 for trade in ledger if trade.exit_reason == "timeout") / trade_count
        if trade_count
        else None
    )

    signal_sl_by_identity = _build_signal_sl_lookup(captured_signals)
    mdd_r, mdd_reason = _mdd_r(ledger, signal_sl_by_identity)
    monthly_concentration, monthly_reason = _monthly_concentration(ledger)

    symbol_metrics: list[SymbolScenarioMetrics] = []
    per_symbol_expectancy: list[float] = []
    any_zero_trade_symbol = False
    for symbol in FROZEN_UNIVERSE:
        symbol_trades = [t for t in ledger if t.symbol == symbol]
        symbol_signal_count = sum(1 for s in captured_signals if s.symbol == symbol)
        symbol_trade_count = len(symbol_trades)
        if symbol_trade_count == 0:
            any_zero_trade_symbol = True
            symbol_metrics.append(
                SymbolScenarioMetrics(
                    symbol=symbol,
                    trade_count=0,
                    signal_count=symbol_signal_count,
                    net_expectancy_bps=None,
                    net_pnl_bps=0.0,
                )
            )
            continue
        symbol_net_pnl = sum(t.net_bps for t in symbol_trades)
        symbol_expectancy = symbol_net_pnl / symbol_trade_count
        per_symbol_expectancy.append(symbol_expectancy)
        symbol_metrics.append(
            SymbolScenarioMetrics(
                symbol=symbol,
                trade_count=symbol_trade_count,
                signal_count=symbol_signal_count,
                net_expectancy_bps=symbol_expectancy,
                net_pnl_bps=symbol_net_pnl,
            )
        )

    incomplete = any_zero_trade_symbol
    incomplete_reason = INSUFFICIENT_OOS_SYMBOL_EVIDENCE_REASON if incomplete else None
    net_expectancy_bps = (
        None if incomplete else sum(per_symbol_expectancy) / len(per_symbol_expectancy)
    )
    pooled_expectancy_bps = net_pnl_bps / trade_count if trade_count else math.nan

    return StrategyScenarioAggregate(
        strategy=strategy,
        scenario_name=scenario_name,
        trade_count=trade_count,
        net_expectancy_bps=net_expectancy_bps,
        pooled_expectancy_bps=pooled_expectancy_bps,
        profit_factor=profit_factor,
        win_rate=win_rate,
        net_pnl_bps=net_pnl_bps,
        timeout_ratio=timeout_ratio,
        mdd_r=mdd_r,
        mdd_reason=mdd_reason,
        monthly_concentration=monthly_concentration,
        monthly_concentration_reason=monthly_reason,
        symbol_metrics=tuple(symbol_metrics),
        incomplete=incomplete,
        incomplete_reason=incomplete_reason,
    )
