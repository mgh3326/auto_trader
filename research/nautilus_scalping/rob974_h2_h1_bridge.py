"""ROB-979 (H2, ROB-974 R2) CP6 -- actual merged-origin H1 seam (pure, stdlib).

Bridges the REAL merged H1 feature plane (``rob974_features.py``, ROB-978
PR #1614, merge ``76fb5506``) into the H2-owned immutable DTOs
(``rob974_h2_dtos.py``) the S3/S4 engines consume. This is a genuine
transformation, not the coincidental duck-typing the CP1-CP5 test-only
fixture used against the frozen worker-brief field names (``open_time``) --
H1's real ``MinuteBar`` uses ``ts``/``volume``, and price+VWAP+market-return
data is split across THREE different H1 types (``Bar4h`` for the actual
closing price, ``CommonSnapshot`` for the synchronized 24h market return
``M``, ``SymbolFeature`` for the per-symbol ``vwap24``) that must be joined
on ``close_ts``/``decision_ts`` -- H1 hands back no single flat record H2
can rename fields on.

ultrathink decisions (frozen for CP6; revisit only if orch authority changes
-- see ``/tmp/strategy-worker-rob979-sonnet-checkpoints.md`` CP6 entry):

  * Attribute access ONLY -- every function reads its H1 input via plain
    attribute access (``row.ts``, ``bar.close_ts``, ``snap.M``, ...), never
    ``isinstance`` against ``rob974_features.MinuteBar``/``Bar4h``/
    ``CommonSnapshot``. This mirrors CP1's ``rob974_h2_ingress.py``
    precedent and is what "H2 core is not coupled to H1 concrete class
    identity" concretely means: any object exposing the same attributes
    bridges identically (proven by
    ``test_bridge_is_not_coupled_to_h1_concrete_class_identity``).
  * ``S3CloseFeature.m`` is sourced from H1's UPPERCASE ``CommonSnapshot.M``
    (the 24h median-of-6-bar-return breadth measure), NOT lowercase
    ``.m`` (the 4h common return) -- verified against the research brief's
    own THESIS_EXIT formula (``M_t<=0 OR C<VWAP24``, uppercase) and against
    ``rob974_h2_s3_engine._thesis_exit``'s existing semantics, which this
    checkpoint does NOT change. CP1-CP5's frozen test fixture already used
    this same convention (its raw fixture field was spelled ``M`` even
    though the resulting DTO attribute is lowercase ``m``) -- CP6 confirms
    that convention was correct by wiring it to H1's real, unambiguously
    uppercase field.
  * A ``SymbolFeature`` with ``vwap24 is None`` (H1 legitimately returns
    ``None`` when fewer than 1,440 contiguous prior minutes exist) is
    SKIPPED, not defaulted to 0.0/some placeholder -- an ``S3CloseFeature``
    H2 can't actually use for thesis-exit evaluation is better left absent
    (surfacing as the engine's own ``missing_future_data`` incomplete
    outcome) than silently fabricated.
  * ``from_h1_pair_leg_closes`` only needs ``Bar4h.close`` (S4's MEAN/STALL
    exit reconstructs the beta-neutral spread itself from raw closes, per
    ``rob974_h2_s4_engine``'s own ultrathink log) -- it does not touch
    ``CommonSnapshot``/``SymbolFeature`` at all, unlike the S3 close-feature
    bridge.

No DB/network/app/broker/order/fill/scheduler/random/current-time imports --
pure stdlib, deterministic given its input.
"""

from __future__ import annotations

from collections.abc import Sequence

from rob974_h2_dtos import MinuteBar, S3CloseFeature, S4PairLegClose


def from_h1_minute_bars(symbol: str, rows: Sequence[object]) -> tuple[MinuteBar, ...]:
    """H1 ``MinuteBar(ts, open, high, low, close, volume)`` -> H2 ``MinuteBar``.

    ``open_time = ts``; OHLC copied verbatim; ``volume`` dropped (no H2
    engine consumes it). ``symbol`` is supplied by the caller because H1's
    ``MinuteBar`` carries no symbol field of its own -- it is keyed
    externally by whichever per-symbol sequence it came from.
    """
    return tuple(
        MinuteBar(
            symbol=symbol,
            open_time=row.ts,
            open=row.open,
            high=row.high,
            low=row.low,
            close=row.close,
        )
        for row in rows
    )


def from_h1_close_features(
    symbol: str,
    bars4h: Sequence[object],
    snapshots: Sequence[object],
) -> tuple[S3CloseFeature, ...]:
    """Join H1's per-symbol ``Bar4h`` (for the real closing price) against
    the synchronized ``CommonSnapshot`` sequence (for ``M`` and this
    symbol's ``vwap24``) on ``close_ts == decision_ts``."""
    bars_by_close_ts = {bar.close_ts: bar for bar in bars4h}
    out: list[S3CloseFeature] = []
    for snap in snapshots:
        bar = bars_by_close_ts.get(snap.decision_ts)
        if bar is None:
            continue
        feature = next(f for f in snap.features if f.symbol == symbol)
        if feature.vwap24 is None:
            continue
        out.append(
            S3CloseFeature(
                symbol=symbol,
                close_ts=snap.decision_ts,
                close=bar.close,
                vwap24=feature.vwap24,
                m=snap.M,
            )
        )
    return tuple(out)


def from_h1_pair_leg_closes(
    symbol: str, bars4h: Sequence[object]
) -> tuple[S4PairLegClose, ...]:
    """H1 ``Bar4h.close`` at each ``close_ts`` -> H2 ``S4PairLegClose``."""
    return tuple(
        S4PairLegClose(symbol=symbol, close_ts=bar.close_ts, close=bar.close)
        for bar in bars4h
    )
