"""ROB-1062 H4 (Run A SS15, AC4, AC5) — warm-up context provenance binding.

Embargo-window data may be consumed ONLY as warm-up context for the first
OOS decisions (indicator lookback continuity) — it may never itself
generate a signal/entry/trade/PnL/selection metric (AC4: no decision is ever
dated inside an embargo window; the walk-forward runner enforces that by
construction, simply never calling an engine at an embargo-window
timestamp). This module's job is AC5's evidence requirement: bind the EXACT
context range/bytes consumed by each decision to a hash, so that (a) an
OOS-only mutation can be PROVEN never to change a TRAIN decision's
consumed-context hash, and (b) a silent, invisible context substitution
(different bars reaching the same decision without changing anything
else) is structurally impossible — the hash IS the evidence.

Reuses H3's own ``indicators.trailing_valid_segment`` (a pure function, not
a re-declaration of a sealed value) to compute EXACTLY the same trailing
run of bars an engine decision would itself consume for a given
``window_end_ms`` — this module does not reimplement segment-restart logic,
it calls the same one H3 calls.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from types import MappingProxyType

import canonical_hash
import indicators as ind
from daily_bars import DailyBar

__all__ = [
    "WarmupContextBinding",
    "compute_warmup_context_binding",
]


def _bar_tuple(bar: DailyBar) -> tuple:
    return (
        bar.day_start_ms,
        bar.day_end_ms,
        bar.open,
        bar.high,
        bar.low,
        bar.close,
        bar.volume,
        bar.minute_count_observed,
        bar.imputed_minutes,
        bar.max_gap_minutes,
        bar.gap_in_last_60min,
        bar.is_valid,
        bar.is_segment_start,
    )


class WarmupContextBinding:
    """Immutable evidence record: exactly which bars (as a content hash,
    not merely a count/range) a decision's indicators were computed
    from."""

    __slots__ = (
        "window_end_ms",
        "per_symbol_segment_hash",
        "per_symbol_segment_range",
        "source_corpus_hash",
        "feature_input_hash",
        "combined_context_hash",
    )

    def __init__(
        self,
        *,
        window_end_ms: int,
        per_symbol_segment_hash: Mapping[str, str],
        per_symbol_segment_range: Mapping[str, tuple[int | None, int | None]],
        source_corpus_hash: str,
    ) -> None:
        ordered_hashes = dict(sorted(per_symbol_segment_hash.items()))
        ordered_ranges = dict(sorted(per_symbol_segment_range.items()))
        object.__setattr__(self, "window_end_ms", window_end_ms)
        object.__setattr__(
            self,
            "per_symbol_segment_hash",
            MappingProxyType(ordered_hashes),
        )
        object.__setattr__(
            self,
            "per_symbol_segment_range",
            MappingProxyType(ordered_ranges),
        )
        object.__setattr__(self, "source_corpus_hash", source_corpus_hash)
        feature_input_hash = canonical_hash.canonical_sha256(
            {
                "window_end_ms": window_end_ms,
                "per_symbol_segment_hash": ordered_hashes,
                "per_symbol_segment_range": ordered_ranges,
            }
        )
        object.__setattr__(self, "feature_input_hash", feature_input_hash)
        object.__setattr__(
            self,
            "combined_context_hash",
            canonical_hash.canonical_sha256(
                {
                    "window_end_ms": window_end_ms,
                    "source_corpus_hash": source_corpus_hash,
                    "feature_input_hash": feature_input_hash,
                }
            ),
        )

    def __setattr__(self, name: str, value: object) -> None:
        raise AttributeError("WarmupContextBinding is immutable")


def compute_warmup_context_binding(
    bars_by_symbol: Mapping[str, Sequence[DailyBar]], *, window_end_ms: int
) -> WarmupContextBinding:
    """The EXACT per-symbol trailing valid segment (the same one an engine
    decision at this ``window_end_ms`` would consume), content-hashed."""
    per_symbol_hash: dict[str, str] = {}
    per_symbol_range: dict[str, tuple[int | None, int | None]] = {}
    source_payload: dict[str, list[tuple]] = {}
    for symbol in sorted(bars_by_symbol):
        raw_bars = bars_by_symbol[symbol]
        usable = tuple(b for b in raw_bars if b.day_end_ms <= window_end_ms)
        source_payload[symbol] = [_bar_tuple(b) for b in usable]
        segment = ind.trailing_valid_segment(usable)
        per_symbol_hash[symbol] = canonical_hash.canonical_sha256(
            {"segment": [_bar_tuple(b) for b in segment]}
        )
        per_symbol_range[symbol] = (
            segment[0].day_start_ms if segment else None,
            segment[-1].day_end_ms if segment else None,
        )
    return WarmupContextBinding(
        window_end_ms=window_end_ms,
        per_symbol_segment_hash=per_symbol_hash,
        per_symbol_segment_range=per_symbol_range,
        source_corpus_hash=canonical_hash.canonical_sha256(
            {"bars_by_symbol": source_payload}
        ),
    )
