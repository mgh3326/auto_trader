"""ROB-1061 H3 — the AP-A2 WCM-B engine: one pure, STATELESS function
(``run_ap_a2_decision``) implementing SS12.3's six-step weekly processing
order VERBATIM, in order, never reordered:

    (1) held with Score<=0 OR rank>k+buffer -> exit queued
    (2) exits submitted FIRST
    (3) after exits, from remaining cash buy Score>0 unheld symbols in
        rank order
    (4) stop once k held
    (5) held symbols with rank<=k+buffer AND Score>0 -> no trade (hold)
    (6) fewer than k positive-Score symbols -> remainder stays cash

No existing holding's weight is ever restored/rebalanced (AC16) — this
function only ever EXITS a held symbol (down to zero) or lets it HOLD
unchanged; it never resizes a held position.

Stateless (AC5): the caller (H4) owns ``prior_held``/the returned
``new_held`` across decisions, including across fold boundaries. No PnL/
return/forward-*/exit-price field anywhere in this module.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

import configs as cfg
import decision_calendar as dc
import indicators as ind
import output_schema as out
import pit_universe_alpaca as pu
import reason_codes as rc
import sizing
import wcmb_ranking as wr
from daily_bars import DailyBar

__all__ = [
    "AP_A2_DecisionResult",
    "AP_A2_HeldState",
    "run_ap_a2_decision",
]


@dataclass(frozen=True)
class AP_A2_HeldState:
    committed_notional: float

    def __post_init__(self) -> None:
        if type(self.committed_notional) is not float or self.committed_notional <= 0.0:
            raise ValueError("committed_notional must be a positive float")


@dataclass(frozen=True)
class AP_A2_DecisionResult:
    records: tuple[out.SignalRecord, ...]
    new_held: Mapping[str, AP_A2_HeldState]
    reason_histogram: Mapping[str, int]


def _evidence(**kwargs: object) -> dict:
    return {k: v for k, v in kwargs.items() if v is not None}


def run_ap_a2_decision(
    *,
    decision_ts_ms: int,
    config: cfg.ConfigSpec,
    universe: pu.UniverseSnapshot,
    bars_by_symbol: Mapping[str, Sequence[DailyBar]],
    prior_held: Mapping[str, AP_A2_HeldState],
) -> AP_A2_DecisionResult:
    if config.family != "AP-A2":
        raise ValueError(f"expected an AP-A2 config, got family={config.family!r}")
    if not dc.is_ap_a2_decision_ts(decision_ts_ms):
        raise ValueError(
            f"{decision_ts_ms} is not an AP-A2 (weekly Monday 00:05 UTC) decision"
        )

    ell = config.params["L"]
    k = config.params["k"]
    b = config.params["b"]

    _window_start, window_end = dc.prior_completed_day_window(decision_ts_ms)

    held_symbols = set(prior_held.keys())
    universe_symbols = set(universe.eligible_symbols)
    all_symbols = sorted(held_symbols | universe_symbols)

    provisional: dict[str, out.SignalRecord] = {}
    scored: dict[str, float] = {}
    closes_by_symbol: dict[str, list[float]] = {}

    # ------------------------------------------------------------------- #
    # Step (0), prerequisite to all six: Score every symbol we might need
    # (held ∪ eligible). A symbol whose data cannot support a Score simply
    # never enters `scored` -- its prior held state (if any) carries over
    # UNCHANGED (never forward-filled, never force-exited on a data gap).
    # ------------------------------------------------------------------- #
    for symbol in all_symbols:
        raw_bars = bars_by_symbol.get(symbol, ())
        usable_bars = tuple(bar for bar in raw_bars if bar.day_end_ms <= window_end)
        segment = ind.trailing_valid_segment(usable_bars)
        if not segment:
            provisional[symbol] = out.SignalRecord(
                decision_ts_ms=decision_ts_ms,
                strategy="AP-A2",
                config_id=config.config_id,
                symbol=symbol,
                action="NO_ACTION",
                target_notional=0.0,
                reason_code="INVALID_DECISION_DAY",
                evidence_hash=out.evidence_hash(_evidence(symbol=symbol)),
            )
            continue
        closes = [bar.close for bar in segment]
        try:
            score = ind.compute_score(closes, ell=ell)
        except ind.InsufficientPriceHistoryError:
            provisional[symbol] = out.SignalRecord(
                decision_ts_ms=decision_ts_ms,
                strategy="AP-A2",
                config_id=config.config_id,
                symbol=symbol,
                action="NO_ACTION",
                target_notional=0.0,
                reason_code="INSUFFICIENT_PRICE_HISTORY",
                evidence_hash=out.evidence_hash(_evidence(symbol=symbol)),
            )
            continue
        scored[symbol] = score
        closes_by_symbol[symbol] = closes

    ranks = wr.rank_symbols(scored)

    # ------------------------------------------------------------------- #
    # Step (1): classify every HELD, successfully-scored symbol.
    # ------------------------------------------------------------------- #
    exit_queued: list[str] = []
    hold_pending: list[str] = []
    for symbol in sorted(held_symbols & set(scored)):
        outcome = wr.classify_held_symbol(
            score=scored[symbol], rank=ranks[symbol], k=k, b=b
        )
        (exit_queued if outcome == "EXIT" else hold_pending).append(symbol)

    # ------------------------------------------------------------------- #
    # Step (2): exits submitted FIRST -- this frees cash BEFORE step (3)'s
    # buys are ever considered. Reordering (2) and (3) is a sealed-order
    # violation (AC15/AC16): a buy must never be evaluated against cash
    # that a same-decision exit has not yet freed.
    # ------------------------------------------------------------------- #
    new_held: dict[str, AP_A2_HeldState] = dict(prior_held)
    for symbol in exit_queued:
        provisional[symbol] = out.SignalRecord(
            decision_ts_ms=decision_ts_ms,
            strategy="AP-A2",
            config_id=config.config_id,
            symbol=symbol,
            action="EXIT",
            target_notional=0.0,
            reason_code="RANK_EXCEEDS_BUFFER_EXIT",
            evidence_hash=out.evidence_hash(
                _evidence(symbol=symbol, score=scored[symbol], rank=ranks[symbol])
            ),
        )
        del new_held[symbol]

    available_cash = sizing.available_cash(
        {sym: st.committed_notional for sym, st in new_held.items()}
    )

    # ------------------------------------------------------------------- #
    # Step (3)+(4): buy Score>0 unheld eligible symbols, in rank order,
    # stopping the instant k are held (never re-checking cash for
    # candidates once slots are full).
    #
    # "미보유 종목" (unheld symbols) is scoped to symbols unheld at the
    # START of this decision (`held_symbols`, prior to step (2)'s exits) --
    # NOT merely "not currently in new_held". A symbol that step (2) just
    # exited (rank>k+b, still Score>0) must NOT be reconsidered as a same-
    # decision buy candidate: without this exclusion, such a symbol gets
    # TWO outcomes computed in the same decision (its EXIT, then a later
    # buy-attempt outcome) and only the second silently overwrites the
    # first in `provisional` -- a real bug caught by
    # ``tests/test_wcmb_engine.py``'s exit-frees-cash-for-a-buy fixture,
    # where the exited symbol's own record flipped from EXIT_TRIGGERED to
    # INSUFFICIENT_CASH with no trace of the exit ever having happened.
    # ------------------------------------------------------------------- #
    buy_candidates = [
        symbol
        for symbol in sorted(scored, key=lambda s: ranks[s])
        if symbol in universe_symbols
        and symbol not in held_symbols
        and scored[symbol] > 0.0
    ]
    slots_full = len(new_held) >= k
    for symbol in buy_candidates:
        if slots_full:
            provisional[symbol] = out.SignalRecord(
                decision_ts_ms=decision_ts_ms,
                strategy="AP-A2",
                config_id=config.config_id,
                symbol=symbol,
                action="NO_ACTION",
                target_notional=0.0,
                reason_code="RANK_SLOTS_FULL",
                evidence_hash=out.evidence_hash(
                    _evidence(symbol=symbol, score=scored[symbol], rank=ranks[symbol])
                ),
            )
            continue
        evidence = _evidence(symbol=symbol, score=scored[symbol], rank=ranks[symbol])
        try:
            sigma20 = ind.annualized_sigma20(closes_by_symbol[symbol])
        except ind.SigmaInsufficientSampleError:
            provisional[symbol] = out.SignalRecord(
                decision_ts_ms=decision_ts_ms,
                strategy="AP-A2",
                config_id=config.config_id,
                symbol=symbol,
                action="NO_ACTION",
                target_notional=0.0,
                reason_code="SIGMA_INSUFFICIENT_SAMPLE",
                evidence_hash=out.evidence_hash(evidence),
            )
            continue
        vol_scale = sizing.compute_vol_scale(sigma20)
        uncapped = sizing.ap_a2_base_slot_usd(k) * vol_scale
        floor = sizing.meets_min_target_notional
        if not floor(uncapped):
            provisional[symbol] = out.SignalRecord(
                decision_ts_ms=decision_ts_ms,
                strategy="AP-A2",
                config_id=config.config_id,
                symbol=symbol,
                action="NO_ACTION",
                target_notional=0.0,
                reason_code="MIN_TARGET_NOTIONAL",
                evidence_hash=out.evidence_hash(evidence),
            )
            continue
        if available_cash < uncapped:
            if not floor(available_cash):
                provisional[symbol] = out.SignalRecord(
                    decision_ts_ms=decision_ts_ms,
                    strategy="AP-A2",
                    config_id=config.config_id,
                    symbol=symbol,
                    action="NO_ACTION",
                    target_notional=0.0,
                    reason_code="INSUFFICIENT_CASH",
                    evidence_hash=out.evidence_hash(evidence),
                )
                continue
            target_notional = available_cash
        else:
            target_notional = uncapped

        provisional[symbol] = out.SignalRecord(
            decision_ts_ms=decision_ts_ms,
            strategy="AP-A2",
            config_id=config.config_id,
            symbol=symbol,
            action="ENTER",
            target_notional=target_notional,
            reason_code="RANK_BUY_ACCEPTED",
            evidence_hash=out.evidence_hash(evidence),
        )
        new_held[symbol] = AP_A2_HeldState(committed_notional=target_notional)
        available_cash -= target_notional
        if len(new_held) >= k:
            slots_full = True

    # ------------------------------------------------------------------- #
    # Step (5): held, rank<=k+buffer, Score>0 -> hold, no trade.
    # ------------------------------------------------------------------- #
    for symbol in hold_pending:
        provisional[symbol] = out.SignalRecord(
            decision_ts_ms=decision_ts_ms,
            strategy="AP-A2",
            config_id=config.config_id,
            symbol=symbol,
            action="HOLD",
            target_notional=0.0,
            reason_code="RANK_BUFFER_HOLD",
            evidence_hash=out.evidence_hash(
                _evidence(symbol=symbol, score=scored[symbol], rank=ranks[symbol])
            ),
        )

    # ------------------------------------------------------------------- #
    # Step (6): fewer than k positive-Score symbols -> whatever is left
    # (unheld, eligible, Score<=0, never a buy candidate) stays cash.
    # ------------------------------------------------------------------- #
    for symbol in scored:
        if symbol in provisional:
            continue
        provisional[symbol] = out.SignalRecord(
            decision_ts_ms=decision_ts_ms,
            strategy="AP-A2",
            config_id=config.config_id,
            symbol=symbol,
            action="NO_ACTION",
            target_notional=0.0,
            reason_code="SCORE_NOT_POSITIVE",
            evidence_hash=out.evidence_hash(
                _evidence(symbol=symbol, score=scored[symbol])
            ),
        )

    records = out.canonical_sort(tuple(provisional[sym] for sym in all_symbols))
    histogram = rc.reconcile_histogram([r.reason_code for r in records])
    return AP_A2_DecisionResult(
        records=records, new_held=new_held, reason_histogram=histogram
    )
