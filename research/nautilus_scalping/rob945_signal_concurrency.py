"""ROB-945 (H5) -- cross-symbol 1m signal-concurrency authority.

Frozen by Fable Q1=A (orch-fable-answer-rob945-20260718.md, 2026-07-18):
signal-active-minute collision rate over the validated, pre-funding/
pre-engine OOS ``SignalEvent`` batch (captured via ``rob945_capture``).

- canonical event key: ``(strategy, fold_id, symbol, signal_ts)``.
- one UTC 1m window is fold-scoped ``(strategy, fold_id, signal_ts)`` --
  a coincidentally identical ``signal_ts`` across two different folds is
  NEVER merged into one minute.
- denominator: unique ``(strategy, fold_id, minute)`` with a valid entry
  signal from >=1 distinct symbol.
- numerator: same set restricted to >=2 distinct symbols.
- rate = numerator / denominator; ``denominator == 0`` -> ``None`` rate +
  stable reason ``no_entry_signal_minutes``.
- per-strategy ``distinct_symbol_count`` histogram over buckets 1..4.
- the overall row is REFERENCE ONLY: it sums per-strategy numerator/
  denominator and carries no separate pass rule.
- S2 pre-execution rejections (``NoTradeRecord``) are not entry signals --
  the input contract is exactly ``SignalEvent`` only; anything else fails
  closed.
- the three independent 13/17/22bp cost scenarios share this SAME signal
  input -- this module has no per-scenario fan-out and must never be fed
  (or asked to count) the same evidence more than once.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import rob941_frozen_scope as frozen
from rob940_engine import SignalEvent

_HISTOGRAM_BUCKETS = (1, 2, 3, 4)
NO_ENTRY_SIGNAL_MINUTES_REASON = "no_entry_signal_minutes"
_REQUIRED_STRATEGIES = ("S1", "S2")


@dataclass(frozen=True)
class StrategyConcurrencyEvidence:
    strategy: str
    numerator: int
    denominator: int | None
    rate: float | None
    reason: str | None
    distinct_symbol_count_histogram: dict[int, int] = field(default_factory=dict)


@dataclass(frozen=True)
class SignalConcurrencyReport:
    per_strategy: tuple[StrategyConcurrencyEvidence, ...]
    overall_numerator: int
    overall_denominator: int

    @property
    def per_strategy_by_name(self) -> dict[str, StrategyConcurrencyEvidence]:
        return {evidence.strategy: evidence for evidence in self.per_strategy}


def _canonical_event_key(signal: SignalEvent) -> tuple[str, str | None, str, int]:
    return (signal.strategy, signal.fold_id, signal.symbol, signal.signal_ts)


def _evidence_for_one_strategy(
    strategy: str, signals: Sequence[SignalEvent]
) -> StrategyConcurrencyEvidence:
    seen_keys: set[tuple[str, str | None, str, int]] = set()
    minute_symbols: dict[tuple[str | None, int], set[str]] = {}
    for signal in signals:
        if not isinstance(signal, SignalEvent):
            raise TypeError(
                "compute_signal_concurrency: input must contain only validated "
                f"SignalEvent objects (entry signals), got {type(signal).__name__!r} "
                "-- a generator's own pre-execution rejection (e.g. S2's "
                "target_direction_invalid NoTradeRecord) is not an entry signal"
            )
        if signal.strategy != strategy:
            raise ValueError(
                f"compute_signal_concurrency: signal claims strategy={signal.strategy!r} "
                f"but was supplied under strategy key {strategy!r}"
            )
        if signal.symbol not in frozen.UNIVERSE:
            raise ValueError(
                f"compute_signal_concurrency: signal symbol {signal.symbol!r} is "
                f"outside the frozen universe {frozen.UNIVERSE!r}"
            )
        key = _canonical_event_key(signal)
        if key in seen_keys:
            raise ValueError(
                "compute_signal_concurrency: duplicate canonical event key "
                f"(strategy, fold_id, symbol, signal_ts)={key!r} -- never silently "
                "collapsed, never double/triple counted"
            )
        seen_keys.add(key)
        minute_key = (signal.fold_id, signal.signal_ts)
        minute_symbols.setdefault(minute_key, set()).add(signal.symbol)

    denominator = len(minute_symbols)
    numerator = sum(1 for symbols in minute_symbols.values() if len(symbols) >= 2)
    histogram = dict.fromkeys(_HISTOGRAM_BUCKETS, 0)
    for symbols in minute_symbols.values():
        count = len(symbols)
        if count in histogram:
            histogram[count] += 1

    if denominator == 0:
        return StrategyConcurrencyEvidence(
            strategy=strategy,
            numerator=0,
            denominator=None,
            rate=None,
            reason=NO_ENTRY_SIGNAL_MINUTES_REASON,
            distinct_symbol_count_histogram=histogram,
        )
    return StrategyConcurrencyEvidence(
        strategy=strategy,
        numerator=numerator,
        denominator=denominator,
        rate=numerator / denominator,
        reason=None,
        distinct_symbol_count_histogram=histogram,
    )


def compute_signal_concurrency(
    strategy_signals: Mapping[str, Sequence[SignalEvent]],
) -> SignalConcurrencyReport:
    if set(strategy_signals.keys()) != set(_REQUIRED_STRATEGIES):
        raise ValueError(
            "compute_signal_concurrency: expected exactly the frozen strategy keys "
            f"{_REQUIRED_STRATEGIES!r}, got {sorted(strategy_signals.keys())!r}"
        )
    # deterministic output order regardless of the caller's mapping insertion order.
    per_strategy = tuple(
        _evidence_for_one_strategy(strategy, strategy_signals[strategy])
        for strategy in _REQUIRED_STRATEGIES
    )
    overall_numerator = sum(evidence.numerator for evidence in per_strategy)
    overall_denominator = sum(evidence.denominator or 0 for evidence in per_strategy)
    return SignalConcurrencyReport(
        per_strategy=per_strategy,
        overall_numerator=overall_numerator,
        overall_denominator=overall_denominator,
    )
