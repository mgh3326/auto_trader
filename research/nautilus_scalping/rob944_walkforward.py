"""ROB-944 (H4, ROB-940) — walk-forward runner (pure, stdlib).

Orchestrates, per strategy, the frozen 120d/3h/28d/28d rolling walk-forward
across the 12 frozen configs and 4 symbols:

  * prevalidates that ``bars_1m``/``funding_sidecars``/``gap_ranges`` cover
    EXACTLY the frozen 4-symbol universe -- a missing symbol's row is a
    terminal invalid-data condition (``MissingSymbolDataError``), never a
    silent <4-symbol partial campaign;
  * per fold: generate signals against that fold's TRAIN-only bar slice for
    each (config, symbol); apply the funding PIT entry gate (Q3, final)
    BEFORE H2 (H2 has no funding gate itself, only a ``funding_lookup`` PnL
    hook); run the primary-stress (17bp) scenario ONLY for TRAIN evidence
    (the frozen selection authority); if ANY resulting trade's
    ``[entry_ts, exit_ts)`` window overlaps a data gap, the WHOLE scenario
    trial is terminal-``rejected`` (``rejected:data_gap_in_position``) --
    the entire ledger is discarded, never partially salvaged, because the
    engine's own daily-stop/cooldown state accounting already treated the
    now-invalidated trade as real before this check runs;
  * hand exactly 12 ``ConfigTrainCandidate``s to ``rob944_selection.
    select_fold_config`` -- the ONLY selection authority, TRAIN evidence
    only, never re-consulted after OOS is known;
  * if a fold selects a winner, run that config's OOS (all 4 symbols) through
    all THREE independent cost-scenario runs (``rob940_engine.
    run_symbol_stream`` — each its own fresh invocation, never a shared-path
    revaluation), same funding-gate/gap-rejection treatment;
  * concatenate each scenario's OOS ledger across ALL folds, in canonical
    ``(entry_ts, symbol, config_id, exit_ts, side)`` order;
  * aggregate per-config ATTEMPT evidence (H6's "one logical attempt = one
    config's full walk-forward invocation" unit): a config's attempt is
    ``crashed``/``timeout`` if ANY child (signal generation or engine)
    invocation actually raised anywhere across any fold/symbol/scenario (a
    crash NEVER silently vanishes -- the config still appears, with its
    crash log), ``rejected`` if it was never train-eligible in ANY fold
    (never had a chance to be selected), else ``completed`` (evidence
    generation succeeded -- NOT a strategy PASS verdict, ROB-846/946's
    "completed != PASS" discipline). Data-gap rejections are tracked
    SEPARATELY from code crashes (they are a data-quality fact, not a bug)
    and never by themselves flip a config's attempt to ``crashed``.

This module takes bars/signals/fold-schedule as PLAIN INPUT (dicts/tuples of
already-pure H1/H2/H3 types) -- it has no opinion on how those were loaded;
the CLI/H6-adapter layer is responsible for actually loading the real corpus
via ``rob941_offline_loader`` (network-0) before calling this.

No DB/network/app/broker/random/current-time imports -- pure stdlib plus the
sibling rob940_*/rob941_*/rob944_* modules, deterministic given its input.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Literal

import rob941_frozen_scope as frozen
from rob940_bars_agg import Bar1m
from rob940_cost_model import COST_SCENARIO_PRIMARY_STRESS, COST_SCENARIOS, CostScenario
from rob940_engine import (
    EngineResult,
    NoTradeRecord,
    SignalEvent,
    TradeRecord,
    run_symbol_stream,
)
from rob941_funding_sidecar import FundingSidecar
from rob944_folds import Fold
from rob944_gap_funding import (
    REASON_DATA_GAP_IN_POSITION,
    build_funding_lookup,
    evaluate_funding_entry_gate,
    is_trade_gap_in_position,
)
from rob944_scenario_evidence import ScenarioRunOutcome
from rob944_scenario_evidence import (
    no_trade_reason_counts as _no_trade_reason_counts,
)
from rob944_scenario_evidence import (
    scenario_run_outcome_from_engine_result as _outcome_from_result,
)
from rob944_selection import (
    ConfigTrainCandidate,
    FoldSelectionTrace,
    SymbolTrainEvidence,
    select_fold_config,
)
from rob944_signal_ordering import sort_signals_canonically

from research_contracts.canonical_hash import canonical_sha256

_MS_PER_MINUTE = 60_000
_EXPECTED_CONFIGS_PER_STRATEGY = 12

AttemptStatus = Literal["completed", "rejected", "crashed", "timeout"]
AggregateScenarioStatus = Literal[
    "completed", "rejected", "crashed", "timeout", "never_selected"
]

# Captain security/determinism correction (2026-07-17): raw exception/log
# text must NEVER flow into a persisted reason_code OR into any persisted
# artifact hash's INPUT (a hash bound to `str(exc)` would make identical
# fixed-failure-classes vary with secrets/paths/runtime wording, and the
# input itself -- even hashed -- should never have depended on it). Every
# non-completed/non-rejected attempt or scenario gets one of these FIXED,
# stable codes instead -- the raw message stays only in
# `ConfigAttemptResult.crash_log`/`ScenarioRunOutcome.error_reason`,
# in-memory, in-process research diagnostics that are never persisted to a
# DB row, never hashed, and never included in `ConfigAttemptEvidenceSummary`.
REASON_CHILD_EXECUTION_CRASHED = "child_execution_crashed"
REASON_CHILD_EXECUTION_TIMEOUT = "child_execution_timeout"
REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS = "insufficient_train_evidence_all_folds"
REASON_NEVER_SELECTED_IN_ANY_FOLD = "never_selected_in_any_fold"
# A GLOBAL (not per-config) failure -- corpus loading, manifest validation,
# or any other precondition that must succeed before ANY per-strategy
# walk-forward can run at all. Captain correction (2026-07-17): once the 24
# experiment keys are predeclared (post-registration), such a failure must
# still yield a full 24-entry terminal batch (all "crashed" with this fixed
# reason), never zero attempts -- see the CLI's fallback-batch builder.
REASON_GLOBAL_CORPUS_LOAD_FAILED = "global_corpus_load_failed"

# The complete, closed allowlist of reason codes this module (and its H6
# evidence-conversion callers) may ever persist. Any other value -- in
# particular raw exception/log text -- must be rejected, never passed
# through, by any caller building persisted evidence from a
# ConfigAttemptEvidenceSummary/ScenarioEvidenceSummary.
KNOWN_REASON_CODES = frozenset(
    {
        REASON_CHILD_EXECUTION_CRASHED,
        REASON_CHILD_EXECUTION_TIMEOUT,
        REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS,
        REASON_DATA_GAP_IN_POSITION,
        REASON_NEVER_SELECTED_IN_ANY_FOLD,
        REASON_GLOBAL_CORPUS_LOAD_FAILED,
    }
)


@dataclass(frozen=True)
class GeneratedSignalBatch:
    """Captain Fable condition 1 correction (2026-07-17): a generator's full
    output -- the accepted ``signals`` PLUS its own pre-execution rejection
    evidence (``rejections``, e.g. H3 S2's ``target_direction_invalid``/
    ``tp_above_max``/etc, or H1's ``next_bar_unavailable``/``confirmation_failed``)
    as canonical ``NoTradeRecord``s. Discarding ``rejections`` (the real
    S2 adapter previously did exactly this) means H3's own rejection reason
    counts never reach any independent scenario result, the TRAIN input
    hash, or the operator-visible CLI summary -- Fable condition 1 requires
    they survive end-to-end."""

    signals: tuple[SignalEvent, ...]
    rejections: tuple[NoTradeRecord, ...] = ()


# A generator may legitimately return either the older bare-tuple shape
# (every existing S1/test-fixture generator) or the richer
# GeneratedSignalBatch (real H3 adapters with rejection evidence) --
# _normalize_generated_batch below accepts either, so this stays backward
# compatible with every existing generator/test fixture.
SignalGenerator = Callable[
    [str, tuple[Bar1m, ...], str | None],
    "GeneratedSignalBatch | tuple[SignalEvent, ...]",
]


def _normalize_generated_batch(result) -> GeneratedSignalBatch:
    """Accept either the older bare ``tuple[SignalEvent, ...]`` (every
    existing generator/test fixture) or a ``GeneratedSignalBatch`` (real H3
    adapters carrying rejection evidence) -- never a breaking change to the
    generator contract."""
    if isinstance(result, GeneratedSignalBatch):
        return result
    return GeneratedSignalBatch(signals=tuple(result), rejections=())


# Captain GeneratedSignalBatch checklist (2026-07-17): the CLOSED set of
# reasons a generator's OWN pre-execution rejection evidence may carry --
# H3 S2's exact 6-code set (rob940_signal_s2.py:142-150,213,227; H3 source
# stays untouched -- this is a literal, hand-verified duplicate, same
# pattern as run_rob944_campaign._known_no_trade_reasons). Distinct from
# KNOWN_REASON_CODES (ATTEMPT-level terminal reasons) -- a different
# concern at a different layer.
H3_GENERATOR_REJECTION_REASONS: frozenset[str] = frozenset(
    {
        "confirmation_failed",
        "next_bar_unavailable",
        "target_direction_invalid",
        "tp_above_max",
        "tp_below_r_min_sl",
        "tp_below_abs_floor",
    }
)


def _rejection_sort_key(rec: NoTradeRecord) -> tuple[int, str, str, str]:
    """Mirrors ``rob944_signal_ordering.canonical_signal_sort_key`` for
    ``NoTradeRecord`` -- a generator's own (implementation-detail) rejection
    ORDER must never change any hash it feeds into."""
    return (rec.signal_ts, rec.symbol, rec.config_id, rec.strategy)


def _validate_generated_rejections(
    rejections: Sequence[NoTradeRecord],
    *,
    strategy: str,
    config_id: str,
    symbol: str,
    fold_id: str | None,
    window_start_ms: int,
    window_end_ms: int,
) -> tuple[NoTradeRecord, ...]:
    """Captain GeneratedSignalBatch checklist (2026-07-17): fail closed on
    a non-``NoTradeRecord``, an invalid ``side``, a reason outside H3's
    exact closed 6-code set, forged identity/out-of-window ``signal_ts``, a
    non-int/bool ``signal_ts``, or a duplicate/colliding record (same
    ``signal_ts`` -- mirrors the accepted-signals uniqueness rule) -- the
    SAME identity/window forgery discipline ``_validate_generated_signals``
    applies to accepted signals, applied here to a generator's own
    pre-execution rejection evidence, which is just as untrusted. Returns
    the rejections in CANONICAL (sorted) order -- callers must use the
    returned tuple, never the raw input order, for any downstream
    hashing/merging.
    """
    seen_ts: set[int] = set()
    for rec in rejections:
        if not isinstance(rec, NoTradeRecord):
            raise ForgedSignalError(
                f"rejection for {strategy}/{config_id}/{symbol}/{fold_id} is not a "
                "NoTradeRecord -- refusing to trust an arbitrary object"
            )
        if rec.side not in ("long", "short"):
            raise ForgedSignalError(
                f"rejection for {strategy}/{config_id}/{symbol}/{fold_id} has an invalid side "
                "-- refusing to trust"
            )
        if rec.reason not in H3_GENERATOR_REJECTION_REASONS:
            raise ForgedSignalError(
                f"rejection for {strategy}/{config_id}/{symbol}/{fold_id} has a reason outside "
                "the closed H3 generator-rejection allowlist -- refusing to trust"
            )
        if isinstance(rec.signal_ts, bool) or not isinstance(rec.signal_ts, int):
            raise ForgedSignalError(
                f"rejection for {strategy}/{config_id}/{symbol}/{fold_id} has a non-int "
                "signal_ts -- refusing to trust"
            )
        if (
            rec.strategy != strategy
            or rec.config_id != config_id
            or rec.symbol != symbol
            or rec.fold_id != fold_id
        ):
            raise ForgedSignalError(
                f"forged rejection identity: requested (strategy={strategy!r}, "
                f"config_id={config_id!r}, symbol={symbol!r}, fold_id={fold_id!r}) but "
                f"rejection claims (strategy={rec.strategy!r}, config_id={rec.config_id!r}, "
                f"symbol={rec.symbol!r}, fold_id={rec.fold_id!r})"
            )
        if not (window_start_ms <= rec.signal_ts <= window_end_ms):
            raise ForgedSignalError(
                f"rejection signal_ts {rec.signal_ts} outside requested window "
                f"[{window_start_ms}, {window_end_ms}] for {strategy}/{config_id}/{symbol}/{fold_id}"
            )
        if rec.signal_ts in seen_ts:
            raise ForgedSignalError(
                f"duplicate/colliding rejection signal_ts for {strategy}/{config_id}/{symbol}/"
                f"{fold_id} -- refusing to trust"
            )
        seen_ts.add(rec.signal_ts)
    return tuple(sorted(rejections, key=_rejection_sort_key))


def _assert_no_signal_rejection_ts_collision(
    signals: Sequence[SignalEvent],
    rejections: Sequence[NoTradeRecord],
    *,
    strategy: str,
    config_id: str,
    symbol: str,
    fold_id: str | None,
) -> None:
    """Captain GeneratedSignalBatch fail-closed seam (2026-07-17):
    ``_validate_generated_signals``/``_validate_generated_rejections`` each
    maintain their OWN ``seen_ts`` set -- neither ever checks the OTHER
    list, so a forged/buggy generator callback could emit BOTH an accepted
    signal AND a rejection at the SAME ``signal_ts``. A single timestamp
    can never be both emitted and rejected; called AFTER both individual
    validators succeed, on their already-validated (canonical) outputs."""
    signal_ts_values = {s.signal_ts for s in signals}
    rejection_ts_values = {r.signal_ts for r in rejections}
    overlap = signal_ts_values & rejection_ts_values
    if overlap:
        raise ForgedSignalError(
            f"signal_ts collision between accepted signals and rejections for "
            f"{strategy}/{config_id}/{symbol}/{fold_id} -- a timestamp cannot be both "
            "emitted and rejected"
        )


# Severity order for combining multiple fold-level ScenarioRunOutcome
# statuses into ONE aggregate per (config, scenario): the most severe wins.
_STATUS_SEVERITY: dict[str, int] = {
    "completed": 0,
    "rejected": 1,
    "timeout": 2,
    "crashed": 3,
}


class MissingSymbolDataError(ValueError):
    """``bars_1m``/``funding_sidecars``/``gap_ranges`` does not cover
    EXACTLY the frozen 4-symbol universe -- ROB-944 requires four exact
    independent streams; a missing symbol's row must never silently
    collapse into a <4-symbol partial campaign."""


class ForgedSignalError(ValueError):
    """A generator returned a ``SignalEvent`` whose identity fields do not
    match what was REQUESTED (strategy/config_id/symbol/fold_id), or whose
    ``signal_ts`` falls outside the exact half-open window it was given --
    stable terminal invalid-data evidence, never silently trusted or
    relabeled under the caller's requested identity."""


def _validate_generated_signals(
    signals: Sequence[SignalEvent],
    *,
    strategy: str,
    config_id: str,
    symbol: str,
    fold_id: str | None,
    window_start_ms: int,
    window_end_ms: int,
) -> None:
    """Fail closed BEFORE H2 unless every signal's identity matches what was
    REQUESTED and its ``signal_ts`` is temporally derivable from the exact
    window it was generated against.

    The upper bound is INCLUSIVE (``signal_ts <= window_end_ms``): an
    aggregated bar's ``close_ts`` can legitimately equal ``window_end_ms``
    (train_end/oos_end) -- a real close-boundary H3 signal, not a forgery.
    The 1m execution slice passed to H2 is ``[window_start_ms,
    window_end_ms)`` (half-open, exclusive of the end), so a bar AT
    ``signal_ts == window_end_ms`` is never present in it; H2's own
    ``resolve_entry`` naturally reports ``next_bar_unavailable`` for that
    signal rather than crossing into the embargo/next fold -- this function
    must NOT reject that case as forged. Only ``signal_ts < window_start_ms``
    or ``signal_ts > window_end_ms`` is a genuine temporal violation.
    """
    for sig in signals:
        if (
            sig.strategy != strategy
            or sig.config_id != config_id
            or sig.symbol != symbol
            or sig.fold_id != fold_id
        ):
            raise ForgedSignalError(
                f"forged signal identity: requested (strategy={strategy!r}, "
                f"config_id={config_id!r}, symbol={symbol!r}, fold_id={fold_id!r}) but "
                f"signal claims (strategy={sig.strategy!r}, config_id={sig.config_id!r}, "
                f"symbol={sig.symbol!r}, fold_id={sig.fold_id!r})"
            )
        if not (window_start_ms <= sig.signal_ts <= window_end_ms):
            raise ForgedSignalError(
                f"signal_ts {sig.signal_ts} outside requested window "
                f"[{window_start_ms}, {window_end_ms}] for {strategy}/{config_id}/{symbol}/{fold_id}"
            )


@dataclass(frozen=True)
class ConfigSpec:
    """One config's signal-generation entry point, already bound to its
    frozen params (and, for S2, its target-validity gates) -- the runner
    never inspects strategy-specific math, only calls this callable."""

    config_id: str
    generate_signals: SignalGenerator


@dataclass(frozen=True)
class ConfigAttemptResult:
    """H6's "one logical attempt" unit: one config's full walk-forward
    invocation, across every fold. ``status="completed"`` means evidence
    generation succeeded, NOT a strategy PASS verdict. ``gap_rejection_log``
    is tracked SEPARATELY from ``crash_log`` -- a data-quality gap rejection
    is never itself a code crash, but must still be exposed/counted, never
    silently dropped."""

    strategy: str
    config_id: str
    status: AttemptStatus
    reason_code: str | None
    selected_in_folds: tuple[str, ...]
    crash_log: tuple[str, ...]
    gap_rejection_log: tuple[str, ...]


@dataclass(frozen=True)
class FoldWalkForwardResult:
    fold: Fold
    selection_trace: FoldSelectionTrace
    oos_outcomes: tuple[
        ScenarioRunOutcome, ...
    ]  # per (symbol, scenario) of the winner; () if none selected


@dataclass(frozen=True)
class WalkForwardResult:
    strategy: str
    folds: tuple[FoldWalkForwardResult, ...]
    config_attempts: tuple[ConfigAttemptResult, ...]  # exactly 12, input order
    concatenated_oos_ledgers: dict[
        str, tuple[TradeRecord, ...]
    ]  # scenario_name -> ledger


def _validate_exact_universe_coverage(
    bars_1m: dict[str, tuple[Bar1m, ...]],
    funding_sidecars: dict[str, FundingSidecar],
    gap_ranges: dict[str, tuple[tuple[int, int], ...]],
) -> None:
    """Fail closed BEFORE any fold work unless all three inputs cover
    EXACTLY the frozen 4-symbol universe -- no missing, no silently-defaulted
    symbol. A partial (<4-symbol) run is never a valid ROB-940 campaign."""
    expected = set(frozen.UNIVERSE)
    for name, mapping in (
        ("bars_1m", bars_1m),
        ("funding_sidecars", funding_sidecars),
        ("gap_ranges", gap_ranges),
    ):
        actual = set(mapping.keys())
        if actual != expected:
            missing = sorted(expected - actual)
            extra = sorted(actual - expected)
            raise MissingSymbolDataError(
                f"{name} must cover EXACTLY the frozen universe {sorted(expected)} "
                f"(missing={missing}, extra={extra})"
            )


def _slice_bars(bars: Sequence[Bar1m], start_ms: int, end_ms: int) -> tuple[Bar1m, ...]:
    """Half-open ``[start_ms, end_ms)`` slice -- the ONLY boundary between
    train/embargo/OOS phases; embargo bars are simply never sliced into
    either phase, so they can never leak into scoring or execution."""
    return tuple(b for b in bars if start_ms <= b.ts < end_ms)


def _canonical_trade_key(t: TradeRecord) -> tuple:
    return (t.entry_ts, t.symbol, t.config_id, t.exit_ts, t.side)


def _apply_funding_gate(
    bars_slice: tuple[Bar1m, ...],
    signals: Sequence[SignalEvent],
    sidecar: FundingSidecar,
) -> tuple[tuple[SignalEvent, ...], tuple[NoTradeRecord, ...]]:
    """Filter signals through the Q3 PIT funding entry gate BEFORE H2 (H2 has
    no funding-cost gate of its own). A signal whose entry can't resolve at
    all is passed through unfiltered -- H2 will reject it as
    ``next_bar_unavailable``, the existing/expected reason for that case."""
    index = {b.ts: i for i, b in enumerate(bars_slice)}
    eligible: list[SignalEvent] = []
    rejections: list[NoTradeRecord] = []
    for sig in signals:
        entry_idx = index.get(sig.signal_ts)
        if entry_idx is None:
            eligible.append(sig)
            continue
        entry_ts = bars_slice[entry_idx].ts
        max_hold_ms = sig.timeout_bars * _MS_PER_MINUTE
        gate = evaluate_funding_entry_gate(
            sidecar, side=sig.side, entry_ts_ms=entry_ts, max_hold_ms=max_hold_ms
        )
        if gate.passed:
            eligible.append(sig)
        else:
            rejections.append(
                NoTradeRecord(
                    strategy=sig.strategy,
                    config_id=sig.config_id,
                    symbol=sig.symbol,
                    side=sig.side,
                    signal_ts=sig.signal_ts,
                    reason=gate.rejection_reason,
                    fold_id=sig.fold_id,
                )
            )
    return tuple(eligible), tuple(rejections)


def _stable_terminal_hash(
    *,
    strategy: str,
    config_id: str,
    symbol: str,
    fold_id: str,
    scope: str,
    status: str,
    reason_code: str,
) -> str:
    """A deterministic hash built ONLY from stable identity + a FIXED status/
    reason_code -- never from raw exception/log text. Two different
    secret-bearing exception messages for the "same" failure class (same
    identity + same status + same reason_code) hash IDENTICALLY."""
    return canonical_sha256(
        {
            "strategy": strategy,
            "config_id": config_id,
            "symbol": symbol,
            "fold_id": fold_id,
            "scope": scope,
            "status": status,
            "reason_code": reason_code,
        }
    )


def _crash_outcome(
    exc: Exception,
    *,
    strategy: str,
    config_id: str,
    symbol: str,
    fold_id: str,
    scenario_name: str,
    no_trade_reason_counts: dict[str, int] | None = None,
) -> ScenarioRunOutcome:
    """Any child (signal-generation or engine) exception becomes terminal
    evidence -- never a silent skip. A ``TimeoutError`` (or subclass) is
    reported as ``status="timeout"``; every other exception is
    ``status="crashed"``. The persisted ``artifact_hash`` is bound ONLY to
    stable identity + status + reason_code (PLUS any already-preserved
    no_trade_reason_counts, captain P1 correction 2026-07-17) -- NEVER to
    ``str(exc)`` -- so two different secret-bearing exception messages for
    the same failure class hash identically; the raw message survives only
    in ``error_reason`` (in-memory only, never re-hashed/persisted)."""
    status: Literal["crashed", "timeout"] = (
        "timeout" if isinstance(exc, TimeoutError) else "crashed"
    )
    reason_code = (
        REASON_CHILD_EXECUTION_TIMEOUT
        if status == "timeout"
        else REASON_CHILD_EXECUTION_CRASHED
    )
    base_hash = _stable_terminal_hash(
        strategy=strategy,
        config_id=config_id,
        symbol=symbol,
        fold_id=fold_id,
        scope=scenario_name,
        status=status,
        reason_code=reason_code,
    )
    counts = no_trade_reason_counts or {}
    artifact_hash = canonical_sha256(
        {"base_hash": base_hash, "no_trade_reason_counts": counts}
    )
    return ScenarioRunOutcome(
        scenario_name=scenario_name,
        status=status,
        trade_count=0,
        artifact_hash=artifact_hash,
        error_reason=str(exc)[:500],
        no_trade_reason_counts=counts,
    )


def _rejected_gap_outcome(
    *,
    strategy: str,
    config_id: str,
    symbol: str,
    fold_id: str,
    scenario_name: str,
    no_trade_reason_counts: dict[str, int] | None = None,
) -> ScenarioRunOutcome:
    """The WHOLE scenario trial is invalidated: at least one trade in this
    run touched a data gap, so the engine's own daily-stop/cooldown state
    accounting downstream of that (now-invalid) trade is untrustworthy --
    the entire ledger is discarded, never partially salvaged.

    Captain independent Fable e2e audit correction (2026-07-17): the
    funding entry gate runs BEFORE the gap check within one scenario
    invocation (see ``_run_scenario``) -- its own no-trade rejections
    (``funding_evidence_unavailable``/``expected_funding_cost_above_3bps``)
    are REAL, already-observed evidence for this run and must not be
    silently dropped just because the run is ALSO gap-rejected. They are
    preserved on ``no_trade_reason_counts`` AND folded into the artifact
    hash (combined with the stable identity+status+reason base hash) --
    Fable Q3 requires these stay separately countable in every terminal
    scenario evidence path, not only the completed one.
    """
    base_hash = _stable_terminal_hash(
        strategy=strategy,
        config_id=config_id,
        symbol=symbol,
        fold_id=fold_id,
        scope=scenario_name,
        status="rejected",
        reason_code=REASON_DATA_GAP_IN_POSITION,
    )
    counts = no_trade_reason_counts or {}
    artifact_hash = canonical_sha256(
        {"base_hash": base_hash, "no_trade_reason_counts": counts}
    )
    return ScenarioRunOutcome(
        scenario_name=scenario_name,
        status="rejected",
        trade_count=0,
        artifact_hash=artifact_hash,
        error_reason=REASON_DATA_GAP_IN_POSITION,
        no_trade_reason_counts=counts,
    )


def _run_scenario(
    bars_slice: tuple[Bar1m, ...],
    signals: Sequence[SignalEvent],
    cost_scenario: CostScenario,
    sidecar: FundingSidecar,
    gap_ranges: Sequence[tuple[int, int]],
    *,
    strategy: str,
    config_id: str,
    symbol: str,
    fold_id: str,
    pre_execution_rejections: Sequence[NoTradeRecord] = (),
) -> tuple[ScenarioRunOutcome, EngineResult | None]:
    """Run ONE independent scenario invocation (fresh engine state, ROB-942
    R1 discipline).

    Any exception becomes a ``crashed``/``timeout`` terminal outcome, never
    a silent skip. If ANY resulting trade touches a data gap, the WHOLE
    trial is terminal-``rejected`` and its entire ledger discarded -- the
    engine's daily-stop/cooldown/entry-cap state was already computed AS IF
    the now-invalidated trade were real, so no partial ledger from this run
    can be trusted, even trades that don't themselves touch the gap.

    ``pre_execution_rejections`` (Fable condition 1): the generator's OWN
    pre-execution rejection evidence (e.g. H3 S2's
    ``target_direction_invalid``/``tp_above_max``/etc) -- real, already-
    observed evidence for this run, merged into ``no_trades`` alongside the
    funding gate's own rejections, in EVERY terminal path (completed AND
    gap-rejected), never silently dropped.
    """
    # Captain P1 correction (2026-07-17): split into two stages so a crash
    # can preserve whatever no-trade evidence was ALREADY known at the
    # point of failure -- pre_execution_rejections (the generator's own,
    # known from the very start) survive EITHER crash point; funding_rejections
    # (only known once the funding gate itself has actually run) additionally
    # survive an engine-stage crash.
    try:
        eligible_signals, funding_rejections = _apply_funding_gate(
            bars_slice, signals, sidecar
        )
    except Exception as exc:  # noqa: BLE001 -- deliberate: any child failure becomes terminal crashed/timeout evidence, never a silent skip
        preserved = _no_trade_reason_counts(
            EngineResult(trades=(), no_trades=tuple(pre_execution_rejections))
        )
        return (
            _crash_outcome(
                exc,
                strategy=strategy,
                config_id=config_id,
                symbol=symbol,
                fold_id=fold_id,
                scenario_name=cost_scenario.name,
                no_trade_reason_counts=preserved,
            ),
            None,
        )

    try:
        ordered_signals = sort_signals_canonically(eligible_signals)
        funding_lookup = build_funding_lookup({symbol: sidecar})
        engine_result = run_symbol_stream(
            bars_slice, ordered_signals, cost_scenario, funding_lookup=funding_lookup
        )
    except Exception as exc:  # noqa: BLE001 -- deliberate: any child failure becomes terminal crashed/timeout evidence, never a silent skip
        preserved = _no_trade_reason_counts(
            EngineResult(
                trades=(),
                no_trades=funding_rejections + tuple(pre_execution_rejections),
            )
        )
        return (
            _crash_outcome(
                exc,
                strategy=strategy,
                config_id=config_id,
                symbol=symbol,
                fold_id=fold_id,
                scenario_name=cost_scenario.name,
                no_trade_reason_counts=preserved,
            ),
            None,
        )

    combined_no_trades = (
        engine_result.no_trades + funding_rejections + tuple(pre_execution_rejections)
    )

    if any(is_trade_gap_in_position(t, gap_ranges) for t in engine_result.trades):
        # The funding gate AND the generator's own pre-execution rejections
        # already ran/were produced (and are real, observed evidence)
        # BEFORE this gap check -- preserve them rather than silently
        # dropping them just because the run is ALSO gap-rejected (Fable
        # Q3: separately countable in every path).
        preserved_no_trade_counts = _no_trade_reason_counts(
            EngineResult(
                trades=(),
                no_trades=funding_rejections + tuple(pre_execution_rejections),
            )
        )
        return (
            _rejected_gap_outcome(
                strategy=strategy,
                config_id=config_id,
                symbol=symbol,
                fold_id=fold_id,
                scenario_name=cost_scenario.name,
                no_trade_reason_counts=preserved_no_trade_counts,
            ),
            None,
        )

    merged = EngineResult(trades=engine_result.trades, no_trades=combined_no_trades)
    outcome = _outcome_from_result(
        merged,
        strategy=strategy,
        config_id=config_id,
        symbol=symbol,
        fold_id=fold_id,
        scenario_name=cost_scenario.name,
    )
    return outcome, merged


def _record_crash_note(
    crash_notes: dict[str, list[tuple[str, bool]]],
    config_id: str,
    message: str,
    *,
    is_timeout: bool,
) -> None:
    crash_notes.setdefault(config_id, []).append((message, is_timeout))


def _record_gap_note(
    gap_notes: dict[str, list[str]], config_id: str, message: str
) -> None:
    gap_notes.setdefault(config_id, []).append(message)


def _json_safe_funding_rate(rate: float) -> float | str:
    """``canonical_sha256``/``encode_canonical`` fail-closed (raise
    ``TypeError``) on a non-finite float -- JSONB cannot represent NaN/±Inf.
    A malformed/non-finite ``last_funding_rate`` is a real state
    ``evaluate_funding_entry_gate`` already handles (fails closed as
    ``funding_evidence_unavailable``), so it must not instead escape as an
    UNACCOUNTED ``TypeError`` from this fingerprint computation -- map it to
    a stable string sentinel distinct from any finite numeric value, so the
    fingerprint (and therefore ``train_artifact_hash``) still deterministically
    reflects it without ever calling ``canonical_sha256`` on the raw
    non-finite float itself."""
    if math.isfinite(rate):
        return rate
    if math.isnan(rate):
        return "nonfinite:nan"
    return "nonfinite:inf" if rate > 0 else "nonfinite:-inf"


def _train_relevant_funding_rows(
    sidecar: FundingSidecar, *, train_start_ms: int, train_end_ms: int
) -> list:
    """Captain PIT-scope correction (2026-07-17): only the funding evidence
    the TRAIN-window entry gate could ever actually consult -- the single
    last-known row visible AT ``train_start_ms`` (covers every entry from
    the very start of the window until a newer row appears), plus any row
    strictly inside ``(train_start_ms, train_end_ms)``. A row at/after
    ``train_end_ms`` (OOS-only) is NEVER included -- hashing the WHOLE
    sidecar would make a future OOS-only funding change spuriously alter a
    TRAIN input hash/selection trace, which never actually saw it."""
    last_at_start = sidecar.last_known_rate(train_start_ms)
    window_rows = [
        r for r in sidecar.rows if train_start_ms < r.calc_time < train_end_ms
    ]
    return ([last_at_start] if last_at_start is not None else []) + window_rows


def _train_relevant_gap_ranges(
    gap_ranges_for_symbol: Sequence[tuple[int, int]],
    *,
    train_start_ms: int,
    train_end_ms: int,
) -> list[tuple[int, int]]:
    """Only gap ranges that actually INTERSECT ``[train_start_ms, train_end_ms)``
    -- an OOS-only gap must never alter a TRAIN input hash. Independent
    audit correction (2026-07-17): hash the CLIPPED intersection
    (``[max(start, train_start_ms), min(end, train_end_ms)]``), never the
    gap's own original (possibly partly-OOS) endpoints -- otherwise
    changing ONLY the OOS-side tail of a gap that merely touches the train
    window would still alter the TRAIN hash, even though the train-visible
    portion is unchanged."""
    return sorted(
        (max(start, train_start_ms), min(end, train_end_ms))
        for start, end in gap_ranges_for_symbol
        if start < train_end_ms and end > train_start_ms
    )


def _train_static_fingerprint(
    bars: Sequence[Bar1m],
    sidecar: FundingSidecar,
    gap_ranges_for_symbol: Sequence[tuple[int, int]],
    *,
    train_start_ms: int,
    train_end_ms: int,
) -> str:
    """Captain freeze-audit addendum (2026-07-17, item A) + train-input
    completeness/PIT-scope/performance follow-ups: ``train_artifact_hash``
    was previously derived ONLY from the scenario OUTPUT (trades +
    no_trade_reason_counts) -- two materially DIFFERENT raw inputs (e.g. two
    distinct ``signal_ts`` values, or identical bars/signals under two
    different funding-sidecar/gap-range states) that both happen to produce
    zero trades with the identical no-trade-reason histogram hashed
    IDENTICALLY, so ``train_input_hash``/the selection trace never actually
    bound the real INPUT.

    This is the CONFIG-INDEPENDENT half of that fix: the bar slice, the
    PIT-visible/train-relevant funding evidence (never OOS-only future rows,
    see ``_train_relevant_funding_rows``), the train-intersecting gap ranges
    (never OOS-only gaps, see ``_train_relevant_gap_ranges``), and the FIXED
    primary scenario identity/cost -- ALL of which are IDENTICAL across
    every config for a given (fold, symbol), so this is computed ONCE per
    (fold, symbol) by the caller (``_evaluate_fold_train``, 4 calls per
    fold, never 12x4=48) and reused for every config via
    ``_combine_static_and_signals`` (cheap: just the small per-config signal
    list). No production-global mutable call-counter lives here -- a test
    that needs to prove the exact call count wraps THIS function with its
    own local ``monkeypatch`` spy instead, so runtime behavior carries no
    extra mutable instrumentation state.
    """
    relevant_funding_rows = _train_relevant_funding_rows(
        sidecar, train_start_ms=train_start_ms, train_end_ms=train_end_ms
    )
    relevant_gap_ranges = _train_relevant_gap_ranges(
        gap_ranges_for_symbol, train_start_ms=train_start_ms, train_end_ms=train_end_ms
    )
    return canonical_sha256(
        {
            "bars": [(b.ts, b.open, b.high, b.low, b.close, b.volume) for b in bars],
            "funding_symbol": sidecar.symbol,
            "funding_rows": [
                (
                    r.calc_time,
                    r.funding_interval_hours,
                    _json_safe_funding_rate(r.last_funding_rate),
                )
                for r in relevant_funding_rows
            ],
            "gap_ranges": relevant_gap_ranges,
            "scenario_name": COST_SCENARIO_PRIMARY_STRESS.name,
            "scenario_all_in_bps": COST_SCENARIO_PRIMARY_STRESS.all_in_bps,
        }
    )


def _combine_static_and_signals(
    static_hash: str,
    signals: Sequence[SignalEvent],
    rejections: Sequence[NoTradeRecord] = (),
) -> str:
    """The CONFIG-SPECIFIC half: cheaply combines the precomputed
    config-independent ``static_hash`` (see ``_train_static_fingerprint``)
    with this config's own (small) generated-signal list AND its own
    generator-produced rejection evidence (Fable condition 1 -- canonical,
    already-validated/sorted by the caller) -- never re-hashes the shared
    bars/funding/gaps per config."""
    return canonical_sha256(
        {
            "static_hash": static_hash,
            "signals": [
                (
                    s.strategy,
                    s.config_id,
                    s.symbol,
                    s.signal_ts,
                    s.side,
                    s.sl_distance_bps,
                    s.tp_distance_bps,
                    s.tp_target_price,
                    s.timeout_bars,
                    s.cooldown_bars,
                    s.fold_id,
                )
                for s in signals
            ],
            "rejections": [
                (
                    r.strategy,
                    r.config_id,
                    r.symbol,
                    r.side,
                    r.signal_ts,
                    r.reason,
                    r.fold_id,
                )
                for r in rejections
            ],
        }
    )


def _bind_train_input(output_hash: str, input_fingerprint: str) -> str:
    """Combine the (already-computed) output-side hash with the raw-input
    fingerprint into the FINAL ``train_artifact_hash`` -- deliberately
    combines rather than replaces, so this stays sensitive to BOTH what was
    fed in and what came out."""
    return canonical_sha256(
        {"input_fingerprint": input_fingerprint, "output_hash": output_hash}
    )


def _zero_evidence(
    symbol: str,
    *,
    train_artifact_hash: str,
    no_trade_reason_counts: dict[str, int] | None = None,
) -> SymbolTrainEvidence:
    """Captain P1 correction (2026-07-17): rejected/crashed/timeout TRAIN
    outcomes previously always defaulted to an EMPTY no_trade_reason_counts
    here, silently dropping known no-trade evidence (e.g.
    target_direction_invalid) that ``outcome`` had already preserved."""
    return SymbolTrainEvidence(
        symbol=symbol,
        completed_trades=0,
        net_expectancy_bps=0.0,
        gross_profit_bps=0.0,
        gross_loss_bps=0.0,
        train_artifact_hash=train_artifact_hash,
        no_trade_reason_counts=no_trade_reason_counts or {},
    )


def _train_evidence_for_symbol(
    strategy: str,
    config_spec: ConfigSpec,
    symbol: str,
    fold: Fold,
    train_bars: tuple[Bar1m, ...],
    static_input_hash: str,
    sidecar: FundingSidecar,
    gap_ranges: dict[str, tuple[tuple[int, int], ...]],
    crash_notes: dict[str, list[tuple[str, bool]]],
    gap_notes: dict[str, list[str]],
) -> SymbolTrainEvidence:
    """``train_bars`` (already sliced) and ``static_input_hash`` (the
    config-independent half of the train-input fingerprint) are precomputed
    ONCE per (fold, symbol) by the caller -- see ``_train_static_fingerprint``
    for why (performance: identical across every config for this symbol)."""
    try:
        raw_result = config_spec.generate_signals(symbol, train_bars, fold.fold_id)
        batch = _normalize_generated_batch(raw_result)
        signals = batch.signals
        _validate_generated_signals(
            signals,
            strategy=strategy,
            config_id=config_spec.config_id,
            symbol=symbol,
            fold_id=fold.fold_id,
            window_start_ms=fold.train_start_ms,
            window_end_ms=fold.train_end_ms,
        )
        rejections = _validate_generated_rejections(
            batch.rejections,
            strategy=strategy,
            config_id=config_spec.config_id,
            symbol=symbol,
            fold_id=fold.fold_id,
            window_start_ms=fold.train_start_ms,
            window_end_ms=fold.train_end_ms,
        )
        _assert_no_signal_rejection_ts_collision(
            signals,
            rejections,
            strategy=strategy,
            config_id=config_spec.config_id,
            symbol=symbol,
            fold_id=fold.fold_id,
        )
        # Independent audit correction (2026-07-17): canonicalize BEFORE
        # hashing/execution -- a generator's own (implementation-detail)
        # return order must never change train_artifact_hash, and a
        # duplicate signal_ts is a terminal failure here too (consistent
        # with the OOS path's own sort_signals_canonically call), never a
        # silent pass-through.
        signals = sort_signals_canonically(signals)
    except Exception as exc:  # noqa: BLE001 -- child signal-generation failure (incl. forged/out-of-window identity/duplicate signal_ts) is terminal crash evidence
        _record_crash_note(
            crash_notes,
            config_spec.config_id,
            f"{fold.fold_id}/{symbol}/train/generate_signals: {exc}",
            is_timeout=isinstance(exc, TimeoutError),
        )
        status = "timeout" if isinstance(exc, TimeoutError) else "crashed"
        reason_code = (
            REASON_CHILD_EXECUTION_TIMEOUT
            if status == "timeout"
            else REASON_CHILD_EXECUTION_CRASHED
        )
        sentinel_hash = _stable_terminal_hash(
            strategy=strategy,
            config_id=config_spec.config_id,
            symbol=symbol,
            fold_id=fold.fold_id,
            scope="train",
            status=status,
            reason_code=reason_code,
        )
        # Captain follow-up (2026-07-17): a bare identity+status+reason
        # sentinel means two DIFFERENT train bars/funding/gaps that happen
        # to hit the SAME generator failure class would still collide --
        # bind the already-computed (config-independent) static_input_hash
        # too, so distinct train-relevant inputs still diverge, while raw
        # exception/log text (``exc``) still never enters any hash input.
        return _zero_evidence(
            symbol,
            train_artifact_hash=_bind_train_input(sentinel_hash, static_input_hash),
        )

    # Cheap: combines the precomputed (per-symbol, config-independent)
    # static_input_hash with THIS config's own small signal list AND its
    # own canonical (sorted) rejection evidence -- never re-hashes the
    # shared bars/funding/gaps.
    input_fingerprint = _combine_static_and_signals(
        static_input_hash, signals, rejections
    )

    outcome, filtered = _run_scenario(
        train_bars,
        signals,
        COST_SCENARIO_PRIMARY_STRESS,
        sidecar,
        gap_ranges[symbol],
        strategy=strategy,
        config_id=config_spec.config_id,
        symbol=symbol,
        fold_id=fold.fold_id,
        pre_execution_rejections=rejections,
    )
    if outcome.status == "rejected":
        _record_gap_note(
            gap_notes,
            config_spec.config_id,
            f"{fold.fold_id}/{symbol}/train/{outcome.scenario_name}: {outcome.error_reason}",
        )
        return _zero_evidence(
            symbol,
            train_artifact_hash=_bind_train_input(
                outcome.artifact_hash, input_fingerprint
            ),
            no_trade_reason_counts=outcome.no_trade_reason_counts,
        )
    if outcome.status != "completed" or filtered is None:
        _record_crash_note(
            crash_notes,
            config_spec.config_id,
            f"{fold.fold_id}/{symbol}/train/{outcome.scenario_name}: {outcome.error_reason}",
            is_timeout=(outcome.status == "timeout"),
        )
        return _zero_evidence(
            symbol,
            train_artifact_hash=_bind_train_input(
                outcome.artifact_hash, input_fingerprint
            ),
            no_trade_reason_counts=outcome.no_trade_reason_counts,
        )

    trades = filtered.trades
    completed = len(trades)
    net_expectancy = (sum(t.net_bps for t in trades) / completed) if completed else 0.0
    gross_profit = sum(t.net_bps for t in trades if t.net_bps > 0)
    gross_loss = sum(-t.net_bps for t in trades if t.net_bps < 0)
    return SymbolTrainEvidence(
        symbol=symbol,
        completed_trades=completed,
        net_expectancy_bps=net_expectancy,
        gross_profit_bps=gross_profit,
        gross_loss_bps=gross_loss,
        train_artifact_hash=_bind_train_input(outcome.artifact_hash, input_fingerprint),
        no_trade_reason_counts=outcome.no_trade_reason_counts,
    )


def _evaluate_fold_train(
    strategy: str,
    configs: Sequence[ConfigSpec],
    bars_1m: dict[str, tuple[Bar1m, ...]],
    funding_sidecars: dict[str, FundingSidecar],
    gap_ranges: dict[str, tuple[tuple[int, int], ...]],
    fold: Fold,
    crash_notes: dict[str, list[tuple[str, bool]]],
    gap_notes: dict[str, list[str]],
) -> list[ConfigTrainCandidate]:
    # Captain performance correction (2026-07-17): bar-slicing and the
    # config-independent static train-input fingerprint are IDENTICAL across
    # every config for a given (fold, symbol) -- computed ONCE per symbol
    # here (4 times per fold), never once per (config, symbol) (48 times
    # per fold), which is what made a naive per-config recomputation
    # prohibitive for real 120-day 1m slices.
    evidence_by_config: dict[str, list[SymbolTrainEvidence]] = {
        spec.config_id: [] for spec in configs
    }
    for symbol in frozen.UNIVERSE:
        train_bars = _slice_bars(
            bars_1m[symbol], fold.train_start_ms, fold.train_end_ms
        )
        sidecar = funding_sidecars[symbol]
        static_input_hash = _train_static_fingerprint(
            train_bars,
            sidecar,
            gap_ranges[symbol],
            train_start_ms=fold.train_start_ms,
            train_end_ms=fold.train_end_ms,
        )
        for spec in configs:
            evidence_by_config[spec.config_id].append(
                _train_evidence_for_symbol(
                    strategy,
                    spec,
                    symbol,
                    fold,
                    train_bars,
                    static_input_hash,
                    sidecar,
                    gap_ranges,
                    crash_notes,
                    gap_notes,
                )
            )
    return [
        ConfigTrainCandidate(
            config_id=spec.config_id,
            symbol_evidence=tuple(evidence_by_config[spec.config_id]),
        )
        for spec in configs
    ]


def _evaluate_fold_oos(
    strategy: str,
    config_spec: ConfigSpec,
    bars_1m: dict[str, tuple[Bar1m, ...]],
    funding_sidecars: dict[str, FundingSidecar],
    gap_ranges: dict[str, tuple[tuple[int, int], ...]],
    fold: Fold,
    crash_notes: dict[str, list[tuple[str, bool]]],
    gap_notes: dict[str, list[str]],
) -> tuple[tuple[ScenarioRunOutcome, ...], dict[str, list[TradeRecord]]]:
    outcomes: list[ScenarioRunOutcome] = []
    ledgers: dict[str, list[TradeRecord]] = {s.name: [] for s in COST_SCENARIOS}

    for symbol in frozen.UNIVERSE:
        oos_bars = _slice_bars(bars_1m[symbol], fold.oos_start_ms, fold.oos_end_ms)
        sidecar = funding_sidecars[symbol]
        try:
            raw_result = config_spec.generate_signals(symbol, oos_bars, fold.fold_id)
            batch = _normalize_generated_batch(raw_result)
            signals = batch.signals
            _validate_generated_signals(
                signals,
                strategy=strategy,
                config_id=config_spec.config_id,
                symbol=symbol,
                fold_id=fold.fold_id,
                window_start_ms=fold.oos_start_ms,
                window_end_ms=fold.oos_end_ms,
            )
            rejections = _validate_generated_rejections(
                batch.rejections,
                strategy=strategy,
                config_id=config_spec.config_id,
                symbol=symbol,
                fold_id=fold.fold_id,
                window_start_ms=fold.oos_start_ms,
                window_end_ms=fold.oos_end_ms,
            )
            _assert_no_signal_rejection_ts_collision(
                signals,
                rejections,
                strategy=strategy,
                config_id=config_spec.config_id,
                symbol=symbol,
                fold_id=fold.fold_id,
            )
        except Exception as exc:  # noqa: BLE001 -- child signal-generation failure (incl. forged/out-of-window identity) is terminal crash evidence
            _record_crash_note(
                crash_notes,
                config_spec.config_id,
                f"{fold.fold_id}/{symbol}/oos/generate_signals: {exc}",
                is_timeout=isinstance(exc, TimeoutError),
            )
            for scenario in COST_SCENARIOS:
                outcomes.append(
                    _crash_outcome(
                        exc,
                        strategy=strategy,
                        config_id=config_spec.config_id,
                        symbol=symbol,
                        fold_id=fold.fold_id,
                        scenario_name=scenario.name,
                    )
                )
            continue

        for scenario in COST_SCENARIOS:
            # Fable condition 1: this generator's own pre-execution
            # rejection evidence (e.g. H3 S2's target_direction_invalid) is
            # merged into EACH independent fresh scenario run alongside the
            # funding gate's/engine's own no-trades -- never a shared-path
            # revaluation, matching the 3-independent-runs discipline.
            outcome, filtered = _run_scenario(
                oos_bars,
                signals,
                scenario,
                sidecar,
                gap_ranges[symbol],
                strategy=strategy,
                config_id=config_spec.config_id,
                symbol=symbol,
                fold_id=fold.fold_id,
                pre_execution_rejections=rejections,
            )
            outcomes.append(outcome)
            if outcome.status == "rejected":
                _record_gap_note(
                    gap_notes,
                    config_spec.config_id,
                    f"{fold.fold_id}/{symbol}/oos/{scenario.name}: {outcome.error_reason}",
                )
                continue
            if outcome.status != "completed" or filtered is None:
                _record_crash_note(
                    crash_notes,
                    config_spec.config_id,
                    f"{fold.fold_id}/{symbol}/oos/{scenario.name}: {outcome.error_reason}",
                    is_timeout=(outcome.status == "timeout"),
                )
                continue
            ledgers[scenario.name].extend(filtered.trades)

    return tuple(outcomes), ledgers


def run_walkforward(
    *,
    strategy: str,
    configs: tuple[ConfigSpec, ...],
    bars_1m: dict[str, tuple[Bar1m, ...]],
    funding_sidecars: dict[str, FundingSidecar],
    gap_ranges: dict[str, tuple[tuple[int, int], ...]],
    fold_schedule: tuple[Fold, ...],
) -> WalkForwardResult:
    """Run the full walk-forward for ONE strategy's 12 frozen configs.

    Fails closed immediately (``MissingSymbolDataError``) unless
    ``bars_1m``/``funding_sidecars``/``gap_ranges`` cover EXACTLY the frozen
    4-symbol universe -- no fold work happens otherwise. Selection uses
    TRAIN evidence only and is immutable once made for a fold; OOS
    execution/ledgers are NEVER fed back into selection. Every one of the
    (exactly 12) configs gets exactly one ``ConfigAttemptResult``,
    regardless of whether it ever won a fold.
    """
    _validate_exact_universe_coverage(bars_1m, funding_sidecars, gap_ranges)

    if len(configs) != _EXPECTED_CONFIGS_PER_STRATEGY:
        raise ValueError(
            f"{strategy}: expected exactly {_EXPECTED_CONFIGS_PER_STRATEGY} configs, "
            f"got {len(configs)}"
        )
    config_ids = [c.config_id for c in configs]
    if len(set(config_ids)) != len(config_ids):
        raise ValueError(f"{strategy}: duplicate config_id in configs")

    fold_results: list[FoldWalkForwardResult] = []
    selected_in_folds: dict[str, list[str]] = {cid: [] for cid in config_ids}
    crash_notes: dict[str, list[tuple[str, bool]]] = {cid: [] for cid in config_ids}
    gap_notes: dict[str, list[str]] = {cid: [] for cid in config_ids}
    ever_eligible: dict[str, bool] = dict.fromkeys(config_ids, False)
    concatenated_by_scenario: dict[str, list[TradeRecord]] = {
        s.name: [] for s in COST_SCENARIOS
    }

    for fold in fold_schedule:
        candidates = _evaluate_fold_train(
            strategy,
            configs,
            bars_1m,
            funding_sidecars,
            gap_ranges,
            fold,
            crash_notes,
            gap_notes,
        )
        trace = select_fold_config(strategy, candidates)
        for outcome in trace.candidates:
            if not outcome.rejected:
                ever_eligible[outcome.config_id] = True

        oos_outcomes: tuple[ScenarioRunOutcome, ...] = ()
        if trace.selected_config_id is not None:
            winner_spec = next(
                c for c in configs if c.config_id == trace.selected_config_id
            )
            oos_outcomes, ledgers = _evaluate_fold_oos(
                strategy,
                winner_spec,
                bars_1m,
                funding_sidecars,
                gap_ranges,
                fold,
                crash_notes,
                gap_notes,
            )
            selected_in_folds[trace.selected_config_id].append(fold.fold_id)
            for scenario_name, trades in ledgers.items():
                concatenated_by_scenario[scenario_name].extend(trades)

        fold_results.append(
            FoldWalkForwardResult(
                fold=fold, selection_trace=trace, oos_outcomes=oos_outcomes
            )
        )

    config_attempts = []
    for cid in config_ids:
        entries = crash_notes[cid]
        crash_log = tuple(message for message, _is_timeout in entries)
        gap_rejection_log = tuple(gap_notes[cid])
        # Priority (most severe first): a genuine code crash/timeout always
        # wins; else ANY data-gap rejection anywhere (train or OOS) makes
        # the WHOLE attempt "rejected:data_gap_in_position" -- never
        # "completed" merely because the config was otherwise eligible
        # elsewhere (captain correction: a gap-contaminated trial must not
        # be silently absorbed into an otherwise-clean attempt); else
        # "rejected" if never train-eligible in any fold; else "completed".
        if entries:
            # "timeout" only if EVERY failure for this config was a timeout;
            # any non-timeout crash makes the whole attempt "crashed" (the
            # more generic, non-recoverable-by-waiting-longer classification).
            status: AttemptStatus = (
                "timeout" if all(is_to for _m, is_to in entries) else "crashed"
            )
            reason_code = None
        elif gap_rejection_log:
            status = "rejected"
            reason_code = REASON_DATA_GAP_IN_POSITION
        elif not ever_eligible[cid]:
            status = "rejected"
            reason_code = REASON_INSUFFICIENT_TRAIN_EVIDENCE_ALL_FOLDS
        else:
            status = "completed"
            reason_code = None
        config_attempts.append(
            ConfigAttemptResult(
                strategy=strategy,
                config_id=cid,
                status=status,
                reason_code=reason_code,
                selected_in_folds=tuple(selected_in_folds[cid]),
                crash_log=crash_log,
                gap_rejection_log=gap_rejection_log,
            )
        )

    concatenated_oos_ledgers = {
        scenario_name: tuple(sorted(trades, key=_canonical_trade_key))
        for scenario_name, trades in concatenated_by_scenario.items()
    }

    return WalkForwardResult(
        strategy=strategy,
        folds=tuple(fold_results),
        config_attempts=tuple(config_attempts),
        concatenated_oos_ledgers=concatenated_oos_ledgers,
    )


def _json_safe_float_or_sentinel(value: float | None) -> float | str | None:
    """Captain P1 correction (2026-07-17): a rejected candidate's
    ``profit_factor`` is ``math.nan`` and a legitimate zero-loss winner's
    can be ``math.inf`` -- both are real, meaningful values that must still
    be BOUND into the hash (never silently dropped), but ``canonical_sha256``
    fails closed on non-finite floats (JSONB cannot represent them). Maps
    non-finite values to a stable, JSON/canonical-safe string sentinel;
    ``None`` (e.g. a rejected candidate's ``equal_weight_expectancy_bps``)
    and finite floats pass through unchanged."""
    if value is None:
        return None
    if math.isfinite(value):
        return value
    if math.isnan(value):
        return "nonfinite:nan"
    return "nonfinite:inf" if value > 0 else "nonfinite:-inf"


@dataclass(frozen=True)
class FoldSelectionEvidenceSummary:
    """Captain P1 end-to-end provenance correction (2026-07-17): a compact,
    canonical per-config/per-fold TRAIN selection trace --
    ``summarize_config_attempts_for_h6`` previously only consulted
    ``FoldSelectionTrace.selected_config_id`` (who won), never each config's
    own ``ConfigSelectionOutcome`` (eligible/excluded symbols, expectancy,
    profit factor, rejection reason, ``train_input_hash``, no-trade counts)
    -- so a TRAIN-only mutation (e.g. a price change that alters
    ``train_input_hash`` but does not change which config eventually won
    OOS in every fold) was silently INVISIBLE to the final
    ``fold_evidence_hash``/``run_identity``. One entry per fold (fold order),
    for every config, whether or not that config won that fold."""

    fold_id: str
    fold_selected_config_id: str | None
    eligible_symbols: tuple[str, ...]
    excluded_symbols: tuple[tuple[str, str], ...]
    equal_weight_expectancy_bps: float | None
    pooled_expectancy_bps: float | None
    profit_factor: float
    rejected: bool
    rejection_reason: str | None
    train_input_hash: str
    no_trade_reason_counts: dict[str, int]


@dataclass(frozen=True)
class ScenarioEvidenceSummary:
    """One config's ONE scenario's aggregate evidence across every fold it
    was selected in. Captain correction (2026-07-17): H6's ``ScenarioEvidence``
    DTO has no status field, so a hash alone is not REPORT EXPOSURE of which
    of base/17/22 failed -- this H4-owned summary preserves ``status``,
    a FIXED ``reason_code`` (never raw text), ``trade_count``, the combined
    ``artifact_hash``, AND the combined ``no_trade_reason_counts`` histogram
    (Fable Q3: funding_evidence_unavailable/expected_funding_cost_above_3bps
    must stay separately countable, not just hash-committed) -- readable
    through the whole H4->H6/CLI conversion boundary, independent of H6's
    own (unmodified) schema.
    """

    scenario_name: str
    status: AggregateScenarioStatus
    reason_code: str | None
    trade_count: int
    artifact_hash: str
    no_trade_reason_counts: dict[str, int]


@dataclass(frozen=True)
class ConfigAttemptEvidenceSummary:
    """One config's H6-ready terminal evidence, aggregated ACROSS every fold
    it was selected in (never selected -> a deterministic sentinel per
    scenario, not a missing/None value -- H6's ``ScenarioEvidence`` schema
    requires exactly 3 rows regardless). ``reason_code`` is always a FIXED,
    stable code (never raw exception/log text). This summary deliberately
    has NO ``run_identity`` field: that requires lineage facts (full-campaign
    hash, campaign_run_id, canonical experiment_id, retry_index) this pure
    module has no way to know -- the caller (app-side controller/CLI, which
    DOES know them) computes the final ``run_identity`` when building
    ``AttemptEvidence``.
    """

    strategy: str
    config_id: str
    status: AttemptStatus
    reason_code: str | None
    scenario_summaries: tuple[ScenarioEvidenceSummary, ...]
    fold_selection_trace: tuple[FoldSelectionEvidenceSummary, ...] = ()


def _combine_scenario_outcomes(
    strategy: str,
    config_id: str,
    scenario_name: str,
    outcomes: list[ScenarioRunOutcome],
) -> ScenarioEvidenceSummary:
    if not outcomes:
        artifact_hash = canonical_sha256(
            {
                "strategy": strategy,
                "config_id": config_id,
                "scenario_name": scenario_name,
                "note": REASON_NEVER_SELECTED_IN_ANY_FOLD,
            }
        )
        return ScenarioEvidenceSummary(
            scenario_name=scenario_name,
            status="never_selected",
            reason_code=REASON_NEVER_SELECTED_IN_ANY_FOLD,
            trade_count=0,
            artifact_hash=artifact_hash,
            no_trade_reason_counts={},
        )

    worst = max(outcomes, key=lambda o: _STATUS_SEVERITY[o.status])
    status: AggregateScenarioStatus = worst.status
    if status == "crashed":
        reason_code = REASON_CHILD_EXECUTION_CRASHED
    elif status == "timeout":
        reason_code = REASON_CHILD_EXECUTION_TIMEOUT
    elif status == "rejected":
        reason_code = REASON_DATA_GAP_IN_POSITION
    else:
        reason_code = None

    total_trades = sum(o.trade_count for o in outcomes)
    combined_reason_counts: dict[str, int] = {}
    for o in outcomes:
        for reason, count in o.no_trade_reason_counts.items():
            combined_reason_counts[reason] = (
                combined_reason_counts.get(reason, 0) + count
            )
    combined_hash = canonical_sha256(
        {
            "strategy": strategy,
            "config_id": config_id,
            "scenario_name": scenario_name,
            "status": status,
            "fold_artifact_hashes": [o.artifact_hash for o in outcomes],
        }
    )
    return ScenarioEvidenceSummary(
        scenario_name=scenario_name,
        status=status,
        reason_code=reason_code,
        trade_count=total_trades,
        artifact_hash=combined_hash,
        no_trade_reason_counts=combined_reason_counts,
    )


def summarize_config_attempts_for_h6(
    result: WalkForwardResult,
) -> tuple[ConfigAttemptEvidenceSummary, ...]:
    """Pure aggregation from a completed ``run_walkforward`` result into the
    exactly-3-scenario-rows-per-config shape H6's ``AttemptEvidence`` needs,
    while preserving PER-SCENARIO status/reason/count/artifact/no-trade-
    reason-histogram evidence (see ``ScenarioEvidenceSummary``).

    For each config, every fold it WON contributes that fold's 4-symbols x
    per-scenario ``ScenarioRunOutcome``s; these are combined per scenario
    (most-severe-status-wins) into one aggregate row. A config never
    selected in any fold gets a deterministic ``never_selected`` sentinel
    per scenario, with ``trade_count=0`` -- never a missing/None row.

    Security: only ``ScenarioRunOutcome.artifact_hash`` (a SHA-256 digest,
    preimage-resistant, itself bound only to stable identity/status/reason,
    never raw exception text) and fixed reason codes cross into the returned
    summary -- raw exception/log text (``ConfigAttemptResult.crash_log``,
    ``ScenarioRunOutcome.error_reason``) never does.
    """
    by_config_scenario: dict[str, dict[str, list[ScenarioRunOutcome]]] = {
        a.config_id: {s.name: [] for s in COST_SCENARIOS}
        for a in result.config_attempts
    }
    for fold_result in result.folds:
        winner = fold_result.selection_trace.selected_config_id
        if winner is None:
            continue
        for outcome in fold_result.oos_outcomes:
            by_config_scenario[winner][outcome.scenario_name].append(outcome)

    summaries: list[ConfigAttemptEvidenceSummary] = []
    for attempt in result.config_attempts:
        scenario_rows = tuple(
            _combine_scenario_outcomes(
                result.strategy,
                attempt.config_id,
                scenario.name,
                by_config_scenario[attempt.config_id][scenario.name],
            )
            for scenario in COST_SCENARIOS
        )

        if attempt.status == "crashed":
            reason_code = REASON_CHILD_EXECUTION_CRASHED
        elif attempt.status == "timeout":
            reason_code = REASON_CHILD_EXECUTION_TIMEOUT
        else:
            reason_code = (
                attempt.reason_code
            )  # "rejected" reason, or None for "completed"

        # Captain P1 correction: this config's OWN ConfigSelectionOutcome,
        # per fold (fold order) -- makes TRAIN-only evidence (eligible/
        # excluded, expectancy, profit factor, rejection reason,
        # train_input_hash, no-trade counts) part of this config's
        # returned evidence, not silently dropped after selection.
        fold_selection_trace = []
        for fold_result in result.folds:
            candidate = next(
                (
                    c
                    for c in fold_result.selection_trace.candidates
                    if c.config_id == attempt.config_id
                ),
                None,
            )
            if candidate is None:
                continue
            fold_selection_trace.append(
                FoldSelectionEvidenceSummary(
                    fold_id=fold_result.fold.fold_id,
                    fold_selected_config_id=fold_result.selection_trace.selected_config_id,
                    eligible_symbols=candidate.eligible_symbols,
                    excluded_symbols=candidate.excluded_symbols,
                    equal_weight_expectancy_bps=candidate.equal_weight_expectancy_bps,
                    pooled_expectancy_bps=candidate.pooled_expectancy_bps,
                    profit_factor=candidate.profit_factor,
                    rejected=candidate.rejected,
                    rejection_reason=candidate.rejection_reason,
                    train_input_hash=candidate.train_input_hash,
                    no_trade_reason_counts=candidate.no_trade_reason_counts,
                )
            )

        summaries.append(
            ConfigAttemptEvidenceSummary(
                strategy=result.strategy,
                config_id=attempt.config_id,
                status=attempt.status,
                reason_code=reason_code,
                scenario_summaries=scenario_rows,
                fold_selection_trace=tuple(fold_selection_trace),
            )
        )
    return tuple(summaries)
