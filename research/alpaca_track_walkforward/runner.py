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

Event-time attribution (2026-07-26 audit-contract freeze): an actual ENTRY
fill belongs to the half-open window containing ``entry_fill_ts_ms`` for
modeled-entry dry count and C120 cost, regardless of whether it later closes.
A closed trade/E120 belongs to a window only when BOTH its entry-fill and
exit-fill timestamps are inside that same window. Cross-boundary positions
remain carried without reset or synthetic exit, but their later exit price
cannot enter an earlier window's closed/E120 statistic.

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
import fold_schedule as fs
import oos_mask as om
import pnl_views as pv
import provider_evidence as pe
import seal_consumption as h3_seal
import trade_ledger as tl
import wcmb_engine as wcmb_eng
import wf_seal_consumption as wf_seal
from daily_bars import DailyBar
from fold_schedule import Fold
from output_schema import SignalRecord

__all__ = [
    "ConfigFoldRun",
    "FamilyFoldResult",
    "MinuteBarsProvider",
    "PhaseTradeMetrics",
    "ProviderTimeBindingError",
    "UniverseSnapshotProvider",
    "run_family_fold",
]

UniverseSnapshotProvider = Callable[[int], pe.UniverseSnapshotEvidence]
MinuteBarsProvider = Callable[[str, int], pe.MinuteBarsEvidence]

_PRIMARY_SCENARIO = "C120"


@dataclass(frozen=True)
class PhaseTradeMetrics:
    closed_trades_count: int
    median_trade_e120_bp: float | None
    modeled_entries_count: int
    turnover_p: float
    modeled_entry_evidence: tuple[tl.ModeledEntryEvidence, ...]
    blind_counts: bc.BlindCounts


@dataclass(frozen=True)
class ConfigFoldRun:
    config_id: str
    family: str
    train_metrics: PhaseTradeMetrics
    oos_blind_counts: bc.BlindCounts
    oos_modeled_entry_evidence: tuple[tl.ModeledEntryEvidence, ...]
    oos_masked_pnl_by_trade: tuple[om.Masked, ...]
    context_binding_at_oos_start: ctxb.WarmupContextBinding
    provider_evidence_binding: pe.RunProviderEvidenceBinding


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
    modeled_entry_evidence: tuple[tl.ModeledEntryEvidence, ...]
    provider_evidence_binding: pe.RunProviderEvidenceBinding


class _RunnerInternalInvariantError(RuntimeError):
    """The engine emitted an EXIT for a symbol this runner has no tracked
    open leg for — this would mean engine state and this runner's own
    fill-aware state patching have desynced. Structurally unreachable given
    the patching this module performs (see module docstring); kept as an
    explicit, fail-closed check rather than a silent ``KeyError``."""


class ProviderTimeBindingError(ValueError):
    """A provider returned evidence for a timestamp other than requested."""


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


def _run_continuous_decisions(
    *,
    config,
    family: str,
    fold: Fold,
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    universe_snapshot_provider: UniverseSnapshotProvider,
    minute_bars_provider: MinuteBarsProvider,
) -> _ContinuousRunResult:
    fs.assert_registered_fold_binding(fold_id=f"fold-{fold.fold_index}", fold=fold)
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
    modeled_entry_evidence: list[tl.ModeledEntryEvidence] = []
    universe_artifacts: list[tuple[int, int, str]] = []
    minute_artifacts: list[tuple[str, int, int, str]] = []

    for ts in timestamps:
        universe_evidence = universe_snapshot_provider(ts)
        if type(universe_evidence) is not pe.UniverseSnapshotEvidence:
            raise TypeError(
                "universe_snapshot_provider must return UniverseSnapshotEvidence"
            )
        try:
            universe_evidence.assert_integrity(requested_ts_ms=ts)
        except pe.ProviderEvidenceError as exc:
            raise ProviderTimeBindingError(str(exc)) from exc
        universe = universe_evidence.snapshot
        universe_artifacts.append(
            (
                ts,
                universe_evidence.source_as_of_ts_ms,
                universe_evidence.source_artifact_hash,
            )
        )
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
                minute_evidence = minute_bars_provider(symbol, ts)
                if type(minute_evidence) is not pe.MinuteBarsEvidence:
                    raise TypeError(
                        "minute_bars_provider must return MinuteBarsEvidence"
                    )
                try:
                    minute_evidence.assert_integrity(symbol=symbol, signal_ts_ms=ts)
                except pe.ProviderEvidenceError as exc:
                    raise ProviderTimeBindingError(str(exc)) from exc
                minute_artifacts.append(
                    (
                        symbol,
                        ts,
                        minute_evidence.source_as_of_ts_ms,
                        minute_evidence.source_artifact_hash,
                    )
                )
                attempt, maybe_open_leg = tl.process_entry_signal(
                    record,
                    reference_close=ref,
                    minute_bars=minute_evidence.bars,
                )
                fill_attempts.append(attempt)
                if maybe_open_leg is not None:
                    open_legs[symbol] = maybe_open_leg
                    modeled_entry_evidence.append(maybe_open_leg.entry_evidence)
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
                minute_evidence = minute_bars_provider(symbol, ts)
                if type(minute_evidence) is not pe.MinuteBarsEvidence:
                    raise TypeError(
                        "minute_bars_provider must return MinuteBarsEvidence"
                    )
                try:
                    minute_evidence.assert_integrity(symbol=symbol, signal_ts_ms=ts)
                except pe.ProviderEvidenceError as exc:
                    raise ProviderTimeBindingError(str(exc)) from exc
                minute_artifacts.append(
                    (
                        symbol,
                        ts,
                        minute_evidence.source_as_of_ts_ms,
                        minute_evidence.source_artifact_hash,
                    )
                )
                attempt, maybe_trade = tl.process_exit_signal(
                    record,
                    open_leg,
                    reference_close=ref,
                    minute_bars=minute_evidence.bars,
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
        modeled_entry_evidence=tuple(modeled_entry_evidence),
        provider_evidence_binding=pe.RunProviderEvidenceBinding(
            universe_artifacts=tuple(universe_artifacts),
            minute_artifacts=tuple(minute_artifacts),
        ),
    )


def _build_phase_metrics(
    *,
    phase: str,
    run_result: _ContinuousRunResult,
    fold: Fold,
    cost_scenarios_bp: Mapping[str, int],
    turnover_capacity_k: int | None,
) -> tuple[PhaseTradeMetrics, tuple[tl.Trade, ...]]:
    """Derive this phase's metrics from the ALREADY-COMPUTED continuous run
    result (``_run_continuous_decisions``), by filtering trades/open-legs/
    fill-attempts by timestamp membership in this phase."""
    if phase == "TRAIN":
        phase_start_ms, phase_end_ms = fold.train_start_ms, fold.train_end_ms
    elif phase == "OOS":
        phase_start_ms, phase_end_ms = fold.oos_start_ms, fold.oos_end_ms
    else:
        raise ValueError(f"unknown phase {phase!r}")

    def _in_phase(ts: int) -> bool:
        return phase_start_ms <= ts < phase_end_ms

    phase_records = tuple(
        r for r in run_result.all_records if _in_phase(r.decision_ts_ms)
    )

    phase_modeled_entries = tuple(
        entry
        for entry in run_result.modeled_entry_evidence
        if _in_phase(entry.entry_fill_ts_ms)
    )
    phase_closed_trades = [
        trade
        for trade in run_result.closed_trades
        if _in_phase(trade.entry_fill_ts_ms) and _in_phase(trade.exit_fill_ts_ms)
    ]
    # Entries that close outside the phase remain open at this phase's end
    # for phase accounting, even if the continuous lifecycle later closes.
    phase_open_count = len(phase_modeled_entries) - len(phase_closed_trades)
    phase_fill_attempts = [
        a for a in run_result.fill_attempts if _in_phase(a.decision_ts_ms)
    ]

    modeled_entries_count = len(phase_modeled_entries)
    if turnover_capacity_k is None:
        turnover_p = (
            modeled_entries_count / len(phase_records) if phase_records else 0.0
        )
    else:
        if type(turnover_capacity_k) is not int or turnover_capacity_k <= 0:
            raise ValueError("turnover_capacity_k must be a positive built-in int")
        weekly_evaluations = len(
            _decision_timestamps(
                family="AP-A2",
                window_start_ms=phase_start_ms,
                window_end_ms=phase_end_ms,
            )
        )
        if weekly_evaluations <= 0:
            raise ValueError("AP-A2 phase must contain scheduled weekly evaluations")
        # Run A §7/§12.6: E[entries/fold] = evaluations * k * p.
        # Therefore p is replacement incidence per available top-k slot,
        # not entries divided by every symbol decision record.
        turnover_p = modeled_entries_count / (weekly_evaluations * turnover_capacity_k)

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
        modeled_entries_count=modeled_entries_count,
    )

    metrics = PhaseTradeMetrics(
        closed_trades_count=len(phase_closed_trades),
        median_trade_e120_bp=median_e120,
        modeled_entries_count=modeled_entries_count,
        turnover_p=turnover_p,
        modeled_entry_evidence=phase_modeled_entries,
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
    fs.assert_registered_fold_binding(fold_id=fold_id, fold=fold)
    wf_seal.assert_policy_matches_schedule_constants(
        oos_folds_const=fs.OOS_FOLDS,
        oos_days_const=fs.OOS_DAYS,
        train_days_const=fs.TRAIN_DAYS,
        embargo_days_const=fs.EMBARGO_DAYS,
        roll_days_const=fs.ROLL_DAYS,
    )
    train_window_days = (fold.train_end_ms - fold.train_start_ms) // fs.DAY_MS
    if train_window_days != wf_seal.train_days():
        raise wf_seal.SealDriftError(
            "canonical TRAIN window must equal the sealed day count"
        )

    bundle = h3_seal.load_sealed_configs_and_params()
    family_configs = [c for c in bundle.configs if c.family == family]
    cost_scenarios_bp = wf_seal.cost_scenarios_bp()
    stress_cap_pct = wf_seal.stress_annual_cost_cap_pct(family)

    config_runs: list[ConfigFoldRun] = []
    train_metrics_by_config: dict[str, cs.ConfigTrainMetrics] = {}

    # Snapshot caller-owned buffers once. Engines and context evidence read
    # these same tuples, so an in-run caller mutation cannot silently
    # substitute bytes after the binding was computed.
    bars_snapshot = {
        symbol: tuple(bars_by_symbol[symbol]) for symbol in sorted(bars_by_symbol)
    }
    context_binding = ctxb.compute_warmup_context_binding(
        bars_snapshot, window_end_ms=fold.oos_start_ms
    )

    for config in family_configs:
        run_result = _run_continuous_decisions(
            config=config,
            family=family,
            fold=fold,
            bars_by_symbol=bars_snapshot,
            universe_snapshot_provider=universe_snapshot_provider,
            minute_bars_provider=minute_bars_provider,
        )
        train_metrics, _train_trades = _build_phase_metrics(
            phase="TRAIN",
            run_result=run_result,
            fold=fold,
            cost_scenarios_bp=cost_scenarios_bp,
            turnover_capacity_k=(
                int(config.params["k"]) if family == "AP-A2" else None
            ),
        )
        oos_metrics, oos_trades = _build_phase_metrics(
            phase="OOS",
            run_result=run_result,
            fold=fold,
            cost_scenarios_bp=cost_scenarios_bp,
            turnover_capacity_k=(
                int(config.params["k"]) if family == "AP-A2" else None
            ),
        )

        train_metrics_by_config[config.config_id] = cs.ConfigTrainMetrics(
            config_id=config.config_id,
            closed_trades_count=train_metrics.closed_trades_count,
            median_trade_e120_bp=train_metrics.median_trade_e120_bp,
            turnover_p=train_metrics.turnover_p,
            annualized_stress_cost_pct=bc.annualized_stress_cost_pct(
                entry_filled_notionals=tuple(
                    entry.entry_filled_notional
                    for entry in train_metrics.modeled_entry_evidence
                ),
                window_days=train_window_days,
                nav_usd=h3_seal.initial_equity_usd(),
                cost_bp=float(cost_scenarios_bp[wf_seal.primary_cost_scenario()]),
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
                dry_counts=oos_metrics.blind_counts,
            )
            for t in oos_trades
        )

        config_runs.append(
            ConfigFoldRun(
                config_id=config.config_id,
                family=family,
                train_metrics=train_metrics,
                oos_blind_counts=oos_metrics.blind_counts,
                oos_modeled_entry_evidence=oos_metrics.modeled_entry_evidence,
                oos_masked_pnl_by_trade=oos_masked,
                context_binding_at_oos_start=context_binding,
                provider_evidence_binding=run_result.provider_evidence_binding,
            )
        )

    selection = cs.select_config(
        [train_metrics_by_config[c.config_id] for c in family_configs],
        data_window="TRAIN",
        stress_cost_cap_pct=stress_cap_pct,
        turnover_band=(wf_seal.ap_a2_turnover_band() if family == "AP-A2" else None),
    )
    return FamilyFoldResult(
        family=family,
        fold_id=fold_id,
        config_runs=tuple(config_runs),
        selection=selection,
    )
