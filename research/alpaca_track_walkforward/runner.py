"""ROB-1062 H4 — the walk-forward runner: wires fold_schedule + H3's two
signal engines + fill_model + trade_ledger + pnl_views + config_selection +
blind_counts + oos_mask into one continuous, per-fold, per-family,
per-config simulation.

Continuity contract (AC3, Run A SS11.2 "OOS 경계 포지션 리셋 없음"): for one
(fold, family, config), decisions run in ONE unbroken chronological loop
from ``fold.train_start_ms`` through ``fold.oos_end_ms``, skipping the
embargo window entirely (no decision is ever dated inside it — AC4). State
(``prior_state``/``prior_held``) is threaded through this single loop; there
is no reset anywhere between TRAIN and OOS. A fresh, flat state is used only
at the very START of each fold's own TRAIN window (folds do not share state
with each other — each fold is its own independent walk-forward instance,
per the fold_schedule module's own docstring on why TRAIN windows mostly
overlap fold-to-fold rather than continuing a single simulation across
folds).

Trade attribution (a documented, flagged implementation choice — Run A SS15
does not itself resolve what happens to a trade whose ENTRY and EXIT
straddle the TRAIN/OOS boundary): a trade (closed or still-open) is
attributed to whichever phase its ENTRY decision timestamp falls in. A
TRAIN-entered trade that has not yet closed by ``train_end_ms`` is simply
carried forward (never force-closed, never reset) and, if it later closes
during OOS, its REALIZED PnL naturally uses OOS-period price bytes for the
EXIT leg only — this is an unavoidable consequence of multi-day holding
periods in ANY walk-forward design and is NOT the same thing as OOS SIGNAL
information influencing the TRAIN entry decision itself (AC8's concern).
Symmetrically, an OOS-entered trade's ``modeled_entries`` count (H5's own
dry-count input) counts ONLY entries whose OWN decision timestamp is inside
the OOS window — a carried-forward TRAIN position that happens to still be
open during OOS is not itself a new OOS entry and is never counted as one.

Fill-aware state feedback (a real structural finding, not a simplification):
H3's own engines assume every ``ENTER``/``EXIT`` signal they accept executes
INSTANTLY (their ``new_state``/``new_held`` bookkeeping has no concept of a
fill failing) — exactly correct for H3's own PnL-blind scope, but WRONG once
H4's historical fill model is layered on top: an ``ENTRY_UNFILLED`` candidate
never actually became a position, and an ``EXIT_UNFILLED`` position never
actually closed. Feeding the engine's naive state straight into the next
decision would let it emit an EXIT for a symbol that was never really
entered (an unrecoverable "orphan EXIT" — this was caught and fixed during
this module's own development, see ``tests/test_runner.py``'s dedicated
regression test). This runner therefore PATCHES engine state immediately
after every decision, before the next one runs: an unfilled/incomplete ENTER
is corrected back to flat/unheld; an unfilled/incomplete EXIT is corrected
back to the SAME long/held state the symbol had going into that decision.
``trade_ledger.process_entry_signal``/``process_exit_signal`` are the single
source of truth for "did this leg actually fill" — shared with
``trade_ledger.build_trades_for_symbol_config`` (a standalone, tested,
already-consistent-stream batch function) so the fill-outcome logic itself
is never duplicated, only the state-feedback wiring around it.

Known, documented simplification: a within-decision fill failure does NOT
retroactively free the cash/rank-slot capacity it naively consumed for
OTHER candidates evaluated in that SAME decision (H3's engine already
finalized its own allocation before this runner ever learns which legs
filled) — it only affects state CARRIED FORWARD into future decisions. This
mirrors AC16's own "partial fill not modeled" simplification in spirit and
is flagged here rather than silently accepted.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass

import blind_counts as bc
import config_selection as cs
import context_binding as ctxb
import dats_engine as dats_eng
import decision_calendar as dc
import oos_mask as om
import pit_universe_alpaca as pu
import pnl_views as pv
import seal_consumption as h3_seal
import trade_ledger as tl
import wcmb_engine as wcmb_eng
import wf_seal_consumption as wf_seal
from daily_bars import DailyBar, SpotMinute
from fold_schedule import Fold
from output_schema import SignalRecord

__all__ = [
    "ConfigFoldRun",
    "FamilyFoldResult",
    "MinuteBarsProvider",
    "PhaseTradeMetrics",
    "UniverseSnapshotProvider",
    "run_family_fold",
]

UniverseSnapshotProvider = Callable[[int], pu.UniverseSnapshot]
MinuteBarsProvider = Callable[[str, int], Sequence[SpotMinute]]

_PRIMARY_SCENARIO = "C120"


@dataclass(frozen=True)
class PhaseTradeMetrics:
    closed_trades_count: int
    median_trade_e120_bp: float | None
    modeled_entries_count: int
    turnover_p: float
    blind_counts: bc.BlindCounts


@dataclass(frozen=True)
class ConfigFoldRun:
    config_id: str
    family: str
    train_metrics: PhaseTradeMetrics
    oos_blind_counts: bc.BlindCounts
    oos_masked_pnl_by_trade: tuple[om.Masked, ...]
    context_binding_at_oos_start: ctxb.WarmupContextBinding


@dataclass(frozen=True)
class FamilyFoldResult:
    family: str
    fold_id: str
    config_runs: tuple[ConfigFoldRun, ...]
    selection: cs.ConfigSelectionResult


def _decision_timestamps(
    *, family: str, window_start_ms: int, window_end_ms: int
) -> list[int]:
    day_ms = 86_400_000
    check_fn = dc.is_ap_a1_decision_ts if family == "AP-A1" else dc.is_ap_a2_decision_ts
    result = []
    day = window_start_ms
    while day < window_end_ms:
        ts = day + dc.DECISION_MINUTE_UTC * 60_000
        if ts < window_end_ms and check_fn(ts):
            result.append(ts)
        day += day_ms
    return result


@dataclass(frozen=True)
class _ContinuousRunResult:
    all_records: tuple[SignalRecord, ...]
    closed_trades: tuple[tl.Trade, ...]
    open_legs_by_symbol: Mapping[str, tl.OpenLeg]
    fill_attempts: tuple[tl.FillAttempt, ...]


class _RunnerInternalInvariantError(RuntimeError):
    """The engine emitted an EXIT for a symbol this runner has no tracked
    open leg for — this would mean engine state and this runner's own
    fill-aware state patching have desynced. Structurally unreachable given
    the patching this module performs (see module docstring); kept as an
    explicit, fail-closed check rather than a silent ``KeyError``."""


def _reference_close(
    bars_by_symbol: Mapping[str, Sequence[DailyBar]], symbol: str, decision_ts_ms: int
) -> float:
    _start, window_end = dc.prior_completed_day_window(decision_ts_ms)
    for bar in bars_by_symbol.get(symbol, ()):
        if bar.day_end_ms == window_end:
            return bar.close
    raise ValueError(
        f"no reference-close bar for {symbol} at decision {decision_ts_ms} "
        "(window_end has no matching DailyBar.day_end_ms) — the engine "
        "should never have fired ENTER/EXIT without one"
    )


def _phase_of(
    ts: int, *, train_start: int, train_end: int, oos_start: int, oos_end: int
) -> str:
    if train_start <= ts < train_end:
        return "TRAIN"
    if oos_start <= ts < oos_end:
        return "OOS"
    raise ValueError(f"decision_ts {ts} is outside both TRAIN and OOS windows")


def _run_continuous_decisions(
    *,
    config,
    family: str,
    fold: Fold,
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    universe_snapshot_provider: UniverseSnapshotProvider,
    minute_bars_provider: MinuteBarsProvider,
) -> _ContinuousRunResult:
    timestamps = sorted(
        _decision_timestamps(
            family=family,
            window_start_ms=fold.train_start_ms,
            window_end_ms=fold.train_end_ms,
        )
        + _decision_timestamps(
            family=family,
            window_start_ms=fold.oos_start_ms,
            window_end_ms=fold.oos_end_ms,
        )
    )
    state: dict = {}
    all_records: list[SignalRecord] = []
    closed_trades: list[tl.Trade] = []
    open_legs: dict[str, tl.OpenLeg] = {}
    fill_attempts: list[tl.FillAttempt] = []

    for ts in timestamps:
        universe = universe_snapshot_provider(ts)
        prior_state = state
        if family == "AP-A1":
            result = dats_eng.run_ap_a1_decision(
                decision_ts_ms=ts,
                config=config,
                universe=universe,
                bars_by_symbol=bars_by_symbol,
                prior_state=prior_state,
            )
            naive_new_state: dict = dict(result.new_state)
        else:
            result = wcmb_eng.run_ap_a2_decision(
                decision_ts_ms=ts,
                config=config,
                universe=universe,
                bars_by_symbol=bars_by_symbol,
                prior_held=prior_state,
            )
            naive_new_state = dict(result.new_held)

        all_records.extend(result.records)
        patched_state = dict(naive_new_state)

        for record in result.records:
            symbol = record.symbol
            if record.action == "ENTER":
                ref = _reference_close(bars_by_symbol, symbol, ts)
                bars = minute_bars_provider(symbol, ts)
                attempt, maybe_open_leg = tl.process_entry_signal(
                    record, reference_close=ref, minute_bars=bars
                )
                fill_attempts.append(attempt)
                if maybe_open_leg is not None:
                    open_legs[symbol] = maybe_open_leg
                    # naive_new_state already reflects long/held -- keep it.
                elif family == "AP-A1":
                    patched_state[symbol] = dats_eng.AP_A1_PositionState(state="flat")
                else:
                    patched_state.pop(symbol, None)
            elif record.action == "EXIT":
                open_leg = open_legs.get(symbol)
                if open_leg is None:
                    raise _RunnerInternalInvariantError(
                        f"EXIT for {symbol} at {ts} with no tracked open leg"
                    )
                ref = _reference_close(bars_by_symbol, symbol, ts)
                bars = minute_bars_provider(symbol, ts)
                attempt, maybe_trade = tl.process_exit_signal(
                    record, open_leg, reference_close=ref, minute_bars=bars
                )
                fill_attempts.append(attempt)
                if maybe_trade is not None:
                    closed_trades.append(maybe_trade)
                    del open_legs[symbol]
                    # naive_new_state already reflects flat/unheld -- keep it.
                else:
                    # Exit did not actually fill -- restore the position
                    # exactly as it was going into this decision.
                    patched_state[symbol] = prior_state[symbol]
        state = patched_state

    return _ContinuousRunResult(
        all_records=tuple(all_records),
        closed_trades=tuple(closed_trades),
        open_legs_by_symbol=dict(open_legs),
        fill_attempts=tuple(fill_attempts),
    )


def _build_phase_metrics(
    *,
    phase: str,
    run_result: _ContinuousRunResult,
    fold: Fold,
    cost_scenarios_bp: Mapping[str, int],
) -> tuple[PhaseTradeMetrics, tuple[tl.Trade, ...]]:
    """Derive this phase's metrics from the ALREADY-COMPUTED continuous run
    result (``_run_continuous_decisions``), by filtering trades/open-legs/
    fill-attempts by timestamp membership in this phase."""

    def _in_phase(ts: int) -> bool:
        return (
            _phase_of(
                ts,
                train_start=fold.train_start_ms,
                train_end=fold.train_end_ms,
                oos_start=fold.oos_start_ms,
                oos_end=fold.oos_end_ms,
            )
            == phase
        )

    phase_records = tuple(
        r for r in run_result.all_records if _in_phase(r.decision_ts_ms)
    )

    phase_closed_trades = [
        t for t in run_result.closed_trades if _in_phase(t.entry_decision_ts_ms)
    ]
    phase_open_count = sum(
        1
        for open_leg in run_result.open_legs_by_symbol.values()
        if _in_phase(open_leg.entry_decision_ts_ms)
    )
    phase_fill_attempts = [
        a for a in run_result.fill_attempts if _in_phase(a.decision_ts_ms)
    ]

    modeled_entries_count = len(phase_closed_trades) + phase_open_count
    turnover_p = modeled_entries_count / len(phase_records) if phase_records else 0.0

    trade_fills = [
        pv.TradeFill(
            entry_reference_close=t.entry_reference_close,
            entry_fill_price=t.entry_fill.fill_price,
            exit_reference_close=t.exit_reference_close,
            exit_fill_price=t.exit_fill.fill_price,
        )
        for t in phase_closed_trades
    ]
    e120_values = [
        pv.shadow_net_pnl_bp(
            tf, scenario=_PRIMARY_SCENARIO, cost_scenarios_bp=cost_scenarios_bp
        )
        for tf in trade_fills
    ]
    median_e120 = statistics.median(e120_values) if e120_values else None

    blind = bc.compute_blind_counts(
        phase_records,
        closed_trades=phase_closed_trades,
        open_positions_count=phase_open_count,
        fill_attempts=phase_fill_attempts,
    )

    metrics = PhaseTradeMetrics(
        closed_trades_count=len(phase_closed_trades),
        median_trade_e120_bp=median_e120,
        modeled_entries_count=modeled_entries_count,
        turnover_p=turnover_p,
        blind_counts=blind,
    )
    return metrics, tuple(phase_closed_trades)


def run_family_fold(
    *,
    family: str,
    fold_id: str,
    fold: Fold,
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    universe_snapshot_provider: UniverseSnapshotProvider,
    minute_bars_provider: MinuteBarsProvider,
) -> FamilyFoldResult:
    """Run all 8 sealed configs of ``family`` continuously across
    ``fold``'s TRAIN+OOS windows, select the winning config from TRAIN-only
    metrics, and return every config's run with OOS PnL masked-by-default."""
    if family not in ("AP-A1", "AP-A2"):
        raise ValueError(f"unknown family {family!r}")

    bundle = h3_seal.load_sealed_configs_and_params()
    family_configs = [c for c in bundle.configs if c.family == family]
    cost_scenarios_bp = wf_seal.cost_scenarios_bp()
    stress_cap_pct = wf_seal.stress_annual_cost_cap_pct(family)

    config_runs: list[ConfigFoldRun] = []
    train_metrics_by_config: dict[str, cs.ConfigTrainMetrics] = {}

    context_binding = ctxb.compute_warmup_context_binding(
        bars_by_symbol, window_end_ms=fold.oos_start_ms
    )

    for config in family_configs:
        run_result = _run_continuous_decisions(
            config=config,
            family=family,
            fold=fold,
            bars_by_symbol=bars_by_symbol,
            universe_snapshot_provider=universe_snapshot_provider,
            minute_bars_provider=minute_bars_provider,
        )
        train_metrics, _train_trades = _build_phase_metrics(
            phase="TRAIN",
            run_result=run_result,
            fold=fold,
            cost_scenarios_bp=cost_scenarios_bp,
        )
        oos_metrics, oos_trades = _build_phase_metrics(
            phase="OOS",
            run_result=run_result,
            fold=fold,
            cost_scenarios_bp=cost_scenarios_bp,
        )

        train_metrics_by_config[config.config_id] = cs.ConfigTrainMetrics(
            config_id=config.config_id,
            closed_trades_count=train_metrics.closed_trades_count,
            median_trade_e120_bp=train_metrics.median_trade_e120_bp,
            turnover_p=train_metrics.turnover_p,
            annualized_stress_cost_pct=bc.annualized_stress_cost_pct(
                modeled_entries_count=train_metrics.modeled_entries_count,
                window_days=(fold.train_end_ms - fold.train_start_ms) // 86_400_000,
                cost_bp=cost_scenarios_bp[wf_seal.primary_cost_scenario()],
            ),
        )

        oos_masked = tuple(
            om.mask(
                pv.three_view_pnl_bp(
                    pv.TradeFill(
                        entry_reference_close=t.entry_reference_close,
                        entry_fill_price=t.entry_fill.fill_price,
                        exit_reference_close=t.exit_reference_close,
                        exit_fill_price=t.exit_fill.fill_price,
                    ),
                    cost_scenarios_bp=cost_scenarios_bp,
                ),
                fold_id=fold_id,
                family=family,
                config_id=config.config_id,
            )
            for t in oos_trades
        )

        config_runs.append(
            ConfigFoldRun(
                config_id=config.config_id,
                family=family,
                train_metrics=train_metrics,
                oos_blind_counts=oos_metrics.blind_counts,
                oos_masked_pnl_by_trade=oos_masked,
                context_binding_at_oos_start=context_binding,
            )
        )

    selection = cs.select_config(
        [train_metrics_by_config[c.config_id] for c in family_configs],
        data_window="TRAIN",
        stress_cost_cap_pct=stress_cap_pct,
    )
    return FamilyFoldResult(
        family=family,
        fold_id=fold_id,
        config_runs=tuple(config_runs),
        selection=selection,
    )
