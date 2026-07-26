"""ROB-1061 H3 — the AP-A1 DATS engine: one pure, STATELESS function
(``run_ap_a1_decision``) tying ``dats_state``/``indicators``/``sizing``/
``reason_codes``/``output_schema`` together into ONE decision's full
per-symbol record set + updated phase state.

Stateless by construction (AC5): this function carries no module-level
mutable state and persists nothing itself — the caller (H4's walk-forward
runner) owns ``prior_state``/``new_state`` across calls, including across
fold boundaries (no reset). Calling this function twice with identical
inputs always produces byte-identical output.

No PnL/return/forward_*/exit_price field anywhere in this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Literal

import configs as cfg
import decision_calendar as dc
import indicators as ind
import pit_universe_alpaca as pu
import sizing
from daily_bars import DailyBar

import dats_state as ds
import output_schema as out
import reason_codes as rc

__all__ = [
    "AP_A1_DecisionResult",
    "AP_A1_PositionState",
    "run_ap_a1_decision",
]

@dataclass(frozen=True)
class AP_A1_PositionState:
    state: Literal["flat", "long"]
    committed_notional: float = 0.0

    def __post_init__(self) -> None:
        if self.state not in ("flat", "long"):
            raise ValueError(f"unknown state {self.state!r}")
        if self.state == "flat" and self.committed_notional != 0.0:
            raise ValueError("a flat position must carry committed_notional == 0.0")
        if self.state == "long" and self.committed_notional <= 0.0:
            raise ValueError("a long position must carry committed_notional > 0.0")


_FLAT_STATE = AP_A1_PositionState(state="flat", committed_notional=0.0)


@dataclass(frozen=True)
class AP_A1_DecisionResult:
    records: tuple[out.SignalRecord, ...]
    new_state: Mapping[str, AP_A1_PositionState]
    reason_histogram: Mapping[str, int]


def _evidence(**kwargs: object) -> dict:
    return {k: v for k, v in kwargs.items() if v is not None}


def run_ap_a1_decision(
    *,
    decision_ts_ms: int,
    config: cfg.ConfigSpec,
    universe: pu.UniverseSnapshot,
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    prior_state: Mapping[str, AP_A1_PositionState],
) -> AP_A1_DecisionResult:
    if config.family != "AP-A1":
        raise ValueError(f"expected an AP-A1 config, got family={config.family!r}")
    if not dc.is_ap_a1_decision_ts(decision_ts_ms):
        raise ValueError(f"{decision_ts_ms} is not an AP-A1 (daily 00:05 UTC) decision")

    f = config.params["f"]
    s = config.params["s"]
    m = config.params["m"]
    threshold = config.params["threshold"]

    _window_start, window_end = dc.prior_completed_day_window(decision_ts_ms)

    all_symbols = sorted(set(universe.eligible_symbols) | set(prior_state.keys()))

    provisional: dict[str, out.SignalRecord] = {}
    new_state: dict[str, AP_A1_PositionState] = {}
    entry_candidates: list[tuple[str, float, float, dict]] = []

    for symbol in all_symbols:
        position = prior_state.get(symbol, _FLAT_STATE)
        raw_bars = bars_by_symbol.get(symbol, ())
        # AC2 look-ahead guard: NEVER consume a bar whose day_end_ms extends
        # past the decision's own prior-completed-day boundary, no matter
        # what the caller passed in -- this filter makes look-ahead
        # structurally unreachable rather than merely "trusted".
        usable_bars = tuple(b for b in raw_bars if b.day_end_ms <= window_end)
        segment = ind.trailing_valid_segment(usable_bars)

        if not segment:
            provisional[symbol] = out.SignalRecord(
                decision_ts_ms=decision_ts_ms,
                strategy="AP-A1",
                config_id=config.config_id,
                symbol=symbol,
                action="NO_ACTION",
                target_notional=0.0,
                reason_code="INVALID_DECISION_DAY",
                evidence_hash=out.evidence_hash(
                    _evidence(symbol=symbol, decision_ts_ms=decision_ts_ms)
                ),
            )
            new_state[symbol] = position
            continue

        closes = [b.close for b in segment]

        if position.state == "long":
            try:
                r = ind.compute_momentum_r(closes, m=m)
                d = ind.compute_trend_d(closes, f=f, s=s)
            except ind.InsufficientPriceHistoryError:
                provisional[symbol] = out.SignalRecord(
                    decision_ts_ms=decision_ts_ms,
                    strategy="AP-A1",
                    config_id=config.config_id,
                    symbol=symbol,
                    action="NO_ACTION",
                    target_notional=0.0,
                    reason_code="INSUFFICIENT_PRICE_HISTORY",
                    evidence_hash=out.evidence_hash(_evidence(symbol=symbol)),
                )
                new_state[symbol] = position
                continue
            outcome = ds.classify_transition(
                state="long", d=d, r=r, threshold=threshold
            )
            evidence = _evidence(symbol=symbol, d=d, r=r, threshold=threshold)
            if outcome.action == "EXIT":
                provisional[symbol] = out.SignalRecord(
                    decision_ts_ms=decision_ts_ms,
                    strategy="AP-A1",
                    config_id=config.config_id,
                    symbol=symbol,
                    action="EXIT",
                    target_notional=0.0,
                    reason_code="EXIT_TRIGGERED",
                    evidence_hash=out.evidence_hash(evidence),
                )
                new_state[symbol] = AP_A1_PositionState(state="flat")
            else:
                provisional[symbol] = out.SignalRecord(
                    decision_ts_ms=decision_ts_ms,
                    strategy="AP-A1",
                    config_id=config.config_id,
                    symbol=symbol,
                    action="HOLD",
                    target_notional=0.0,
                    reason_code="HYSTERESIS_HOLD",
                    evidence_hash=out.evidence_hash(evidence),
                )
                new_state[symbol] = position  # unchanged, still long
            continue

        # position.state == "flat"
        if symbol not in universe.eligible_symbols:
            provisional[symbol] = out.SignalRecord(
                decision_ts_ms=decision_ts_ms,
                strategy="AP-A1",
                config_id=config.config_id,
                symbol=symbol,
                action="NO_ACTION",
                target_notional=0.0,
                reason_code="UNIVERSE_INELIGIBLE",
                evidence_hash=out.evidence_hash(_evidence(symbol=symbol)),
            )
            new_state[symbol] = _FLAT_STATE
            continue

        try:
            r = ind.compute_momentum_r(closes, m=m)
            d = ind.compute_trend_d(closes, f=f, s=s)
        except ind.InsufficientPriceHistoryError:
            provisional[symbol] = out.SignalRecord(
                decision_ts_ms=decision_ts_ms,
                strategy="AP-A1",
                config_id=config.config_id,
                symbol=symbol,
                action="NO_ACTION",
                target_notional=0.0,
                reason_code="INSUFFICIENT_PRICE_HISTORY",
                evidence_hash=out.evidence_hash(_evidence(symbol=symbol)),
            )
            new_state[symbol] = _FLAT_STATE
            continue

        outcome = ds.classify_transition(state="flat", d=d, r=r, threshold=threshold)
        evidence = _evidence(symbol=symbol, d=d, r=r, threshold=threshold)
        if outcome.action != "ENTER":
            provisional[symbol] = out.SignalRecord(
                decision_ts_ms=decision_ts_ms,
                strategy="AP-A1",
                config_id=config.config_id,
                symbol=symbol,
                action="NO_ACTION",
                target_notional=0.0,
                reason_code="NO_ENTRY_SIGNAL",
                evidence_hash=out.evidence_hash(evidence),
            )
            new_state[symbol] = _FLAT_STATE
            continue

        try:
            sigma20 = ind.annualized_sigma20(closes)
        except ind.SigmaInsufficientSampleError:
            provisional[symbol] = out.SignalRecord(
                decision_ts_ms=decision_ts_ms,
                strategy="AP-A1",
                config_id=config.config_id,
                symbol=symbol,
                action="NO_ACTION",
                target_notional=0.0,
                reason_code="SIGMA_INSUFFICIENT_SAMPLE",
                evidence_hash=out.evidence_hash(evidence),
            )
            new_state[symbol] = _FLAT_STATE
            continue

        vol_scale = sizing.compute_vol_scale(sigma20)
        target_notional = sizing.target_notional_ap_a1(vol_scale)
        candidate_evidence = _evidence(
            symbol=symbol, d=d, r=r, sigma20=sigma20, vol_scale=vol_scale
        )
        if not sizing.meets_min_target_notional(target_notional):
            provisional[symbol] = out.SignalRecord(
                decision_ts_ms=decision_ts_ms,
                strategy="AP-A1",
                config_id=config.config_id,
                symbol=symbol,
                action="NO_ACTION",
                target_notional=0.0,
                reason_code="MIN_TARGET_NOTIONAL",
                evidence_hash=out.evidence_hash(candidate_evidence),
            )
            new_state[symbol] = _FLAT_STATE
            continue

        # A real entry candidate -- defer its final record until the shared
        # cash pool has been allocated across every simultaneous candidate
        # (AC12: D descending, symbol ascending tie-break).
        entry_candidates.append((symbol, d, target_notional, candidate_evidence))
        new_state[symbol] = _FLAT_STATE  # provisional; overwritten below if accepted

    # Available cash reflects every symbol's state AFTER today's exits (the
    # `new_state` built above already carries post-exit committed notional
    # for every non-candidate symbol) and BEFORE today's new entries.
    committed = {
        sym: st.committed_notional
        for sym, st in new_state.items()
        if st.state == "long"
    }
    cash = sizing.available_cash(committed)

    allocation = sizing.allocate_cash_constrained(
        [(sym, d, tn) for sym, d, tn, _ev in entry_candidates],
        available_cash=cash,
    )
    by_symbol_candidate = {sym: (d, tn, ev) for sym, d, tn, ev in entry_candidates}

    for symbol in allocation.accepted:
        _d, target_notional, evidence = by_symbol_candidate[symbol]
        provisional[symbol] = out.SignalRecord(
            decision_ts_ms=decision_ts_ms,
            strategy="AP-A1",
            config_id=config.config_id,
            symbol=symbol,
            action="ENTER",
            target_notional=target_notional,
            reason_code="ENTRY_ACCEPTED",
            evidence_hash=out.evidence_hash(evidence),
        )
        new_state[symbol] = AP_A1_PositionState(
            state="long", committed_notional=target_notional
        )

    for symbol in allocation.rejected_insufficient_cash:
        _d, _target_notional, evidence = by_symbol_candidate[symbol]
        provisional[symbol] = out.SignalRecord(
            decision_ts_ms=decision_ts_ms,
            strategy="AP-A1",
            config_id=config.config_id,
            symbol=symbol,
            action="NO_ACTION",
            target_notional=0.0,
            reason_code="INSUFFICIENT_CASH",
            evidence_hash=out.evidence_hash(evidence),
        )
        new_state[symbol] = _FLAT_STATE

    records = out.canonical_sort(tuple(provisional[sym] for sym in all_symbols))
    histogram = rc.reconcile_histogram([r.reason_code for r in records])
    return AP_A1_DecisionResult(
        records=records, new_state=new_state, reason_histogram=histogram
    )
