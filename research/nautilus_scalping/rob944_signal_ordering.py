"""ROB-944 (H4, ROB-940) — canonical signal ordering + duplicate-signal_ts
guard (pure, stdlib).

``rob940_engine.run_symbol_stream``'s ONLY tie-break for same-``signal_ts``
signals is Python's stable sort on input order (see its module docstring's
caller-precondition note) -- H4 owns presenting signals in a REPRODUCIBLE
canonical order so AC1's "same bars/config -> same bytes" determinism claim
actually holds regardless of dict/set/filesystem iteration order upstream.

``assert_unique_signal_ts_per_symbol_config`` is defense-in-depth on top of
H3's own per-generator-call uniqueness guard
(``rob940_signal_s1._assert_unique_signal_ts`` / the S2 equivalent): it
re-validates uniqueness scoped to (strategy, config_id, symbol) at the point
H4 actually hands a combined signal list to the engine, so a caller that
merges signal lists from multiple sources (or replays a stale one) cannot
silently reintroduce a same-bar double entry.

No DB/network/app/broker/random/current-time imports -- pure stdlib,
deterministic given its input.
"""

from __future__ import annotations

from collections.abc import Sequence

from rob940_engine import SignalEvent


class DuplicateSignalTimestampError(ValueError):
    """Two signals share ``signal_ts`` within the same (strategy, config_id,
    symbol) stream -- a single-position stream must never see two candidate
    entries for the same bar."""


def canonical_signal_sort_key(signal: SignalEvent) -> tuple[int, str, str, str]:
    """``(signal_ts, symbol, config_id, strategy)`` -- the canonical,
    reproducible tie-break order H4 guarantees before calling H2."""
    return (signal.signal_ts, signal.symbol, signal.config_id, signal.strategy)


def assert_unique_signal_ts_per_symbol_config(
    signals: Sequence[SignalEvent],
) -> None:
    """Fail closed if any (strategy, config_id, symbol) stream carries two
    signals with the same ``signal_ts``. Same ``signal_ts`` across DIFFERENT
    symbols/configs/strategies is expected and allowed (that's the normal
    case of several independent streams firing near the same instant)."""
    seen: dict[tuple[str, str, str], set[int]] = {}
    for sig in signals:
        key = (sig.strategy, sig.config_id, sig.symbol)
        bucket = seen.setdefault(key, set())
        if sig.signal_ts in bucket:
            raise DuplicateSignalTimestampError(
                f"duplicate signal_ts {sig.signal_ts} for strategy={sig.strategy!r} "
                f"config_id={sig.config_id!r} symbol={sig.symbol!r} -- a "
                "single-position stream must have at most one signal per bar"
            )
        bucket.add(sig.signal_ts)


def sort_signals_canonically(
    signals: Sequence[SignalEvent],
) -> tuple[SignalEvent, ...]:
    """Validate uniqueness, then return signals in the canonical stable
    order -- byte-identical regardless of the caller's input order."""
    assert_unique_signal_ts_per_symbol_config(signals)
    return tuple(sorted(signals, key=canonical_signal_sort_key))
