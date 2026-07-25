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
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime

import rob941_frozen_scope as frozen
from rob940_cost_model import COST_SCENARIOS, FEE_ROUND_TRIP_BPS
from rob940_cost_model import gross_bps as _cost_model_gross_bps
from rob940_cost_model import net_bps as _cost_model_net_bps
from rob940_engine import SignalEvent, TradeRecord

INSUFFICIENT_OOS_SYMBOL_EVIDENCE_REASON = "insufficient_oos_symbol_evidence"
NO_POSITIVE_MONTHS_REASON = "no_positive_months"
MDD_UNAVAILABLE_MISSING_SL_EVIDENCE_REASON = "mdd_unavailable_missing_sl_evidence"

_CLOSED_SIDES = frozenset({"long", "short"})
_CLOSED_EXIT_REASONS = frozenset({"take_profit", "stop_loss", "timeout"})

FROZEN_UNIVERSE: tuple[str, ...] = (
    frozen.UNIVERSE
)  # ("BTCUSDT", "XRPUSDT", "DOGEUSDT", "SOLUSDT")

# Frozen scenario -> all_in_bps authority (rob940_cost_model.COST_SCENARIOS).
_ALL_IN_BPS_BY_SCENARIO: dict[str, float] = {
    s.name: s.all_in_bps for s in COST_SCENARIOS
}
_COST_SCENARIO_BY_NAME = {s.name: s for s in COST_SCENARIOS}

# The closed set of no_trade_reason_counts KEYS this system can ever
# legitimately produce -- literal hand-verified duplicate of the same
# closed set established in rob945_h6_summary_contract.py (H2's bare-string
# no-fill reasons, H4's funding-gate reasons, H3 S2's 6-code rejection set)
# -- an arbitrary caller-injected key must never be hashed/exposed.
_KNOWN_NO_TRADE_REASONS: frozenset[str] = frozenset(
    {
        "next_bar_unavailable",
        "daily_stop_active",
        "daily_entry_cap",
        "cooldown_active",
        "tp_below_min_distance",
        "funding_evidence_unavailable",
        "expected_funding_cost_above_3bps",
        "confirmation_failed",
        "target_direction_invalid",
        "tp_above_max",
        "tp_below_r_min_sl",
        "tp_below_abs_floor",
    }
)


def _assert_finite(value: float, *, context: str) -> None:
    # Captain scope reminder (Task 3 final-fix): exact/plain runtime types,
    # not only math.isfinite()/isinstance() -- a bool IS finite and IS an
    # int/float subclass (so it would silently pass isfinite AND could
    # coincidentally equal an expected value via `True == 1.0`), a plain
    # int can masquerade as float and pass a `==`-based expected-value
    # check (`10 == 10.0`), and a str raw input would otherwise leak an
    # uncontrolled TypeError out of math.isfinite() itself. type(x) is
    # float (never isinstance) is checked FIRST, before finiteness.
    if type(value) is not float:
        raise ValueError(
            f"compute_scenario_metrics: raw economic input at {context} must be an "
            "exact float"
        )
    if not math.isfinite(value):
        raise ValueError(
            f"compute_scenario_metrics: non-finite raw economic input at {context} "
            f"({value!r}) -- only a DERIVED value (e.g. profit_factor) may ever be "
            "non-finite, raw trade/economic input must be finite"
        )


def _validate_trades(
    strategy: str,
    scenario_name: str,
    ledger: Sequence[TradeRecord],
    *,
    fold_selected_config: Mapping[str, str],
) -> None:
    expected_all_in_bps = _ALL_IN_BPS_BY_SCENARIO[scenario_name]
    cost_scenario = _COST_SCENARIO_BY_NAME[scenario_name]
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
            if type(price) is not float:
                raise ValueError(
                    f"compute_scenario_metrics: trade.{price_field} must be an exact "
                    "float"
                )
            if not math.isfinite(price) or price <= 0:
                raise ValueError(
                    f"compute_scenario_metrics: trade.{price_field} must be finite and "
                    f"positive, got {price!r}"
                )
        for ts_field in ("signal_ts", "entry_ts", "exit_ts"):
            ts_value = getattr(trade, ts_field)
            # type(...) is not int (never isinstance) rejects bool AND any
            # int subclass uniformly.
            if type(ts_value) is not int:
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

        # Task 3 final-fix: economic identity re-derivation -- fee_bps/
        # all_in_bps must match the frozen scenario authority exactly, and
        # gross_bps/net_bps must equal what rob940_cost_model would
        # actually derive from this trade's own prices/side/funding --
        # never a caller-forged/double-fee-subtracted value.
        if trade.fee_bps != FEE_ROUND_TRIP_BPS:
            raise ValueError(
                f"compute_scenario_metrics: trade.fee_bps must be exactly "
                f"{FEE_ROUND_TRIP_BPS!r}, got {trade.fee_bps!r}"
            )
        if trade.all_in_bps != expected_all_in_bps:
            raise ValueError(
                f"compute_scenario_metrics: trade.all_in_bps must be exactly "
                f"{expected_all_in_bps!r} for scenario {scenario_name!r}, got "
                f"{trade.all_in_bps!r}"
            )
        recomputed_gross_bps = _cost_model_gross_bps(
            trade.side, trade.entry_price, trade.exit_price
        )
        if trade.gross_bps != recomputed_gross_bps:
            raise ValueError(
                "compute_scenario_metrics: trade.gross_bps does not match the "
                "recomputed rob940_cost_model.gross_bps derivation from its own "
                "side/entry_price/exit_price"
            )
        recomputed_net_bps = _cost_model_net_bps(
            trade.gross_bps, cost_scenario, trade.funding_bps
        )
        if trade.net_bps != recomputed_net_bps:
            raise ValueError(
                "compute_scenario_metrics: trade.net_bps does not match the "
                "recomputed rob940_cost_model.net_bps derivation -- no double-fee "
                "subtraction or forged value is trusted"
            )

        # Every trade must use a fold in the exact frozen fold map and the
        # exact config selected for that fold -- reject unregistered folds/
        # Every trade must use a fold in the exact frozen fold map and the
        # exact config selected for that fold -- fail closed (never
        # opt-in/skippable): an unregistered fold or config-drifted trade is
        # rejected outright, rather than silently dropped.
        if trade.fold_id not in fold_selected_config:
            raise ValueError(
                f"compute_scenario_metrics: trade.fold_id {trade.fold_id!r} is not "
                "a registered fold in fold_selected_config"
            )
        if trade.config_id != fold_selected_config[trade.fold_id]:
            raise ValueError(
                f"compute_scenario_metrics: trade.config_id {trade.config_id!r} does "
                f"not match fold {trade.fold_id!r}'s selected config "
                f"{fold_selected_config[trade.fold_id]!r} -- config drift rejected"
            )


def _validate_signals(
    strategy: str,
    captured_signals: Sequence[SignalEvent],
    *,
    fold_selected_config: Mapping[str, str],
) -> None:
    seen_identity: set[tuple[str, str, str, str | None, int]] = set()
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
        # Captain scope reminder: exact/plain runtime types -- a bool is
        # both a valid int (signal_ts) and a valid float (sl_distance_bps)
        # to isinstance/math.isfinite, so type(x) is T (never isinstance)
        # is required here too, matching the trade-side hardening above.
        if type(signal.signal_ts) is not int:
            raise ValueError(
                f"compute_scenario_metrics: signal.signal_ts must be a plain int, got "
                f"{signal.signal_ts!r}"
            )
        if type(signal.sl_distance_bps) is not float:
            raise ValueError(
                "compute_scenario_metrics: signal.sl_distance_bps must be an exact float"
            )
        # Task 3 final-fix: captured signals are held to the SAME fail-closed
        # fold/config registration as trades -- caller omission of usable
        # evidence must never bypass validation.
        if signal.fold_id not in fold_selected_config:
            raise ValueError(
                f"compute_scenario_metrics: signal.fold_id {signal.fold_id!r} is not a "
                "registered fold in fold_selected_config"
            )
        if signal.config_id != fold_selected_config[signal.fold_id]:
            raise ValueError(
                f"compute_scenario_metrics: signal.config_id {signal.config_id!r} does "
                f"not match fold {signal.fold_id!r}'s selected config "
                f"{fold_selected_config[signal.fold_id]!r} -- config drift rejected"
            )
        # Task 3 final-fix: a duplicate captured-signal identity fails
        # closed UNCONDITIONALLY -- even when both copies agree on
        # sl_distance_bps (previously only a VALUE mismatch was flagged, as
        # merely "ambiguous" evidence in ``_build_signal_sl_lookup``).
        identity = (
            signal.strategy,
            signal.config_id,
            signal.symbol,
            signal.fold_id,
            signal.signal_ts,
        )
        if identity in seen_identity:
            raise ValueError(
                f"compute_scenario_metrics: duplicate captured_signals identity "
                f"{identity!r}"
            )
        seen_identity.add(identity)


def _validate_no_trade_reason_counts(
    no_trade_reason_counts: Mapping[str, int] | None,
) -> dict[str, int]:
    if no_trade_reason_counts is None:
        return {}
    normalized: dict[str, int] = {}
    for key, value in dict(no_trade_reason_counts).items():
        if type(key) is not str or key not in _KNOWN_NO_TRADE_REASONS:
            raise ValueError(
                "compute_scenario_metrics: no_trade_reason_counts has a key outside "
                "the closed known-reasons allowlist"
            )
        if type(value) is not int or value < 0:
            raise ValueError(
                f"compute_scenario_metrics: no_trade_reason_counts[{key!r}] must be a "
                f"nonnegative plain int, got {value!r}"
            )
        normalized[key] = value
    return normalized


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
    no_trade_reason_counts: dict[str, int] = field(default_factory=dict)


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
    fold_selected_config: Mapping[str, str],
    no_trade_reason_counts: Mapping[str, int] | None = None,
) -> StrategyScenarioAggregate:
    """``fold_selected_config`` is REQUIRED and fail-closed (Task 3
    final-fix, captain scope reminder): every trade AND every captured
    signal must use a fold in this exact frozen fold map and the exact
    config selected for that fold. There is no opt-in/omission path -- a
    caller cannot bypass registration validation merely by not supplying
    the map."""
    if scenario_name not in _ALL_IN_BPS_BY_SCENARIO:
        raise ValueError(
            f"compute_scenario_metrics: scenario_name must be one of "
            f"{sorted(_ALL_IN_BPS_BY_SCENARIO)!r}, got {scenario_name!r}"
        )
    _validate_trades(
        strategy, scenario_name, ledger, fold_selected_config=fold_selected_config
    )
    _validate_signals(
        strategy, captured_signals, fold_selected_config=fold_selected_config
    )
    validated_no_trade_reason_counts = _validate_no_trade_reason_counts(
        no_trade_reason_counts
    )
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
        no_trade_reason_counts=validated_no_trade_reason_counts,
    )
