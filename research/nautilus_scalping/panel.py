"""ROB-351 (eng-review Issue 6 + Codex as-of-rebalance PIT) — lazy cross-sections.

Cross-sectional strategies need, at each rebalance, the lookback return of every
symbol tradeable AS OF that rebalance. This generator yields ONE rebalance at a
time so the caller never materializes a full (time x symbol) matrix (the OOM risk
flagged in eng-review Issue 6), and consults the PIT manifest per rebalance so a
delisted/unlisted symbol cannot leak in (Codex hardening).

Inputs are bar-level (not tick): ``closes_by_symbol`` maps symbol -> chronological
``[(ts, close), ...]``. A symbol is included at a rebalance only if it is
PIT-tradeable there AND has both a bar at the rebalance and a bar at/just-before
``ts - lookback`` to form a return.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence

from pit_universe import PITManifest


def _close_at_or_before(series: Sequence[tuple[int, float]], ts: int) -> float | None:
    """Most recent close at or before ``ts`` (series assumed chronological)."""
    found: float | None = None
    for s_ts, close in series:
        if s_ts > ts:
            break
        found = close
    return found


def iter_rebalance_cross_sections(
    closes_by_symbol: dict[str, Sequence[tuple[int, float]]],
    rebalances: Sequence[int],
    lookback: int,
    manifest: PITManifest | None = None,
    min_seasoning: int = 0,
) -> Iterator[tuple[int, dict[str, float]]]:
    """Yield ``(rebalance_ts, {symbol: lookback_return})`` lazily, PIT-filtered."""
    for ts in rebalances:
        eligible = (
            manifest.universe_as_of(ts, min_seasoning)
            if manifest is not None
            else set(closes_by_symbol)
        )
        xs: dict[str, float] = {}
        for symbol, series in closes_by_symbol.items():
            if symbol not in eligible:
                continue
            now = _close_at_or_before(series, ts)
            then = _close_at_or_before(series, ts - lookback)
            # require a genuine prior anchor: a bar must exist at/before ts-lookback
            has_prior = series and series[0][0] <= ts - lookback
            if now is None or then is None or then == 0.0 or not has_prior:
                continue
            xs[symbol] = now / then - 1.0
        yield ts, xs
