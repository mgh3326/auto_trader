"""Typed, immutable provenance envelopes for H4 data providers.

Provider values are not trusted merely because their public timestamp was
rewritten to the requested instant.  Each response binds its content to the
source artifact's own as-of timestamp and a canonical content hash.  Runner
code validates both on every consumption.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import canonical_hash
import pit_universe_alpaca as pu
from daily_bars import SpotMinute

__all__ = [
    "MinuteBarsEvidence",
    "ProviderEvidenceError",
    "RunProviderEvidenceBinding",
    "UniverseSnapshotEvidence",
    "bind_minute_bars",
    "bind_universe_snapshot",
]


class ProviderEvidenceError(ValueError):
    """Provider evidence is stale, relabeled, reconstructed, or mutated."""


@dataclass(frozen=True, slots=True)
class RunProviderEvidenceBinding:
    """Immutable aggregate bound to the code-pinned canonical run manifest."""

    run_manifest_hash: str
    daily_bars_artifact_hash: str
    universe_grid_hash: str
    minute_grid_hash: str
    universe_artifacts: tuple[tuple[int, int, str], ...]
    minute_artifacts: tuple[tuple[str, int, int, str], ...]
    combined_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(
            self,
            "combined_hash",
            canonical_hash.canonical_sha256(
                {
                    "run_manifest_hash": self.run_manifest_hash,
                    "daily_bars_artifact_hash": self.daily_bars_artifact_hash,
                    "universe_grid_hash": self.universe_grid_hash,
                    "minute_grid_hash": self.minute_grid_hash,
                    "universe_artifacts": [
                        list(item) for item in self.universe_artifacts
                    ],
                    "minute_artifacts": [list(item) for item in self.minute_artifacts],
                }
            ),
        )


def _universe_payload(
    snapshot: pu.UniverseSnapshot, *, source_as_of_ts_ms: int
) -> dict:
    return {
        "source_as_of_ts_ms": source_as_of_ts_ms,
        "snapshot": {
            "decision_ts_ms": snapshot.decision_ts_ms,
            "eligible_symbols": list(snapshot.eligible_symbols),
            "per_symbol": [item.to_dict() for item in snapshot.per_symbol],
            "n_t": snapshot.n_t,
            "meets_min_universe_size": snapshot.meets_min_universe_size,
        },
    }


def _minute_payload(
    *,
    symbol: str,
    signal_ts_ms: int,
    source_as_of_ts_ms: int,
    bars: Sequence[SpotMinute],
) -> dict:
    return {
        "symbol": symbol,
        "signal_ts_ms": signal_ts_ms,
        "source_as_of_ts_ms": source_as_of_ts_ms,
        "bars": [
            {
                "open_time_ms": bar.open_time_ms,
                "open": bar.open,
                "high": bar.high,
                "low": bar.low,
                "close": bar.close,
                "volume": bar.volume,
            }
            for bar in bars
        ],
    }


@dataclass(frozen=True, slots=True)
class UniverseSnapshotEvidence:
    snapshot: pu.UniverseSnapshot
    source_as_of_ts_ms: int
    source_artifact_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.snapshot) is not pu.UniverseSnapshot:
            raise TypeError("snapshot must be an exact UniverseSnapshot")
        if type(self.source_as_of_ts_ms) is not int:
            raise TypeError("source_as_of_ts_ms must be a built-in int")
        object.__setattr__(
            self,
            "source_artifact_hash",
            canonical_hash.canonical_sha256(
                _universe_payload(
                    self.snapshot, source_as_of_ts_ms=self.source_as_of_ts_ms
                )
            ),
        )

    def assert_integrity(self, *, requested_ts_ms: int) -> None:
        expected = canonical_hash.canonical_sha256(
            _universe_payload(self.snapshot, source_as_of_ts_ms=self.source_as_of_ts_ms)
        )
        if expected != self.source_artifact_hash:
            raise ProviderEvidenceError("universe source artifact hash mismatch")
        if self.source_as_of_ts_ms != requested_ts_ms:
            raise ProviderEvidenceError(
                "universe source as-of timestamp does not equal the requested "
                "decision timestamp"
            )
        if self.snapshot.decision_ts_ms != requested_ts_ms:
            raise ProviderEvidenceError(
                "universe snapshot timestamp does not equal the requested "
                "decision timestamp"
            )


@dataclass(frozen=True, slots=True)
class MinuteBarsEvidence:
    symbol: str
    signal_ts_ms: int
    bars: tuple[SpotMinute, ...]
    source_as_of_ts_ms: int
    source_artifact_hash: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.symbol) is not str or not self.symbol:
            raise TypeError("symbol must be a non-empty built-in str")
        if type(self.signal_ts_ms) is not int:
            raise TypeError("signal_ts_ms must be a built-in int")
        if type(self.source_as_of_ts_ms) is not int:
            raise TypeError("source_as_of_ts_ms must be a built-in int")
        if type(self.bars) is not tuple or any(
            type(bar) is not SpotMinute for bar in self.bars
        ):
            raise TypeError("bars must be a tuple of exact SpotMinute values")
        object.__setattr__(
            self,
            "source_artifact_hash",
            canonical_hash.canonical_sha256(
                _minute_payload(
                    symbol=self.symbol,
                    signal_ts_ms=self.signal_ts_ms,
                    source_as_of_ts_ms=self.source_as_of_ts_ms,
                    bars=self.bars,
                )
            ),
        )

    def assert_integrity(self, *, symbol: str, signal_ts_ms: int) -> None:
        expected = canonical_hash.canonical_sha256(
            _minute_payload(
                symbol=self.symbol,
                signal_ts_ms=self.signal_ts_ms,
                source_as_of_ts_ms=self.source_as_of_ts_ms,
                bars=self.bars,
            )
        )
        if expected != self.source_artifact_hash:
            raise ProviderEvidenceError("minute source artifact hash mismatch")
        if self.symbol != symbol or self.signal_ts_ms != signal_ts_ms:
            raise ProviderEvidenceError(
                "minute evidence binding does not match the requested symbol/signal"
            )
        latest_content_ts = max(
            (bar.open_time_ms for bar in self.bars), default=signal_ts_ms
        )
        if self.source_as_of_ts_ms != latest_content_ts:
            raise ProviderEvidenceError(
                "minute source as-of timestamp does not match its actual content"
            )


def bind_universe_snapshot(
    snapshot: pu.UniverseSnapshot, *, source_as_of_ts_ms: int
) -> UniverseSnapshotEvidence:
    return UniverseSnapshotEvidence(
        snapshot=snapshot, source_as_of_ts_ms=source_as_of_ts_ms
    )


def bind_minute_bars(
    *,
    symbol: str,
    signal_ts_ms: int,
    bars: Sequence[SpotMinute],
) -> MinuteBarsEvidence:
    bound_bars = tuple(bars)
    source_as_of_ts_ms = max(
        (bar.open_time_ms for bar in bound_bars), default=signal_ts_ms
    )
    return MinuteBarsEvidence(
        symbol=symbol,
        signal_ts_ms=signal_ts_ms,
        bars=bound_bars,
        source_as_of_ts_ms=source_as_of_ts_ms,
    )
