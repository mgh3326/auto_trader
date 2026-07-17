"""ROB-941 (AC2/AC3/AC6) — the immutable historical-data corpus manifest.

The single citable artifact H4/H6 consume: per symbol, exactly which upstream
archives were checksum-verified (URL + sha256), what the normalized shard
hashes to, its row/gap accounting, and the frozen universe/window/eligibility
scope. ``content_hash`` uses the same canonical, collision-free identity
authority as ``research_contracts`` (via the local ``canonical_hash`` shim) so
the manifest's identity is reproducible and any field change is detectable —
that is the immutability enforcement mechanism, not a promise.

ROB-941 R1 I1 remediation: ``shard_path``/``shard_file_sha256`` point at the
actual persisted Parquet shard (``rob941_persistence.write_kline_shard``/
``write_funding_shard``, under ``artifact_paths.pit_data_root()``) so H4/H6 can
``rob941_offline_loader.load_corpus`` this manifest with zero network — the
manifest alone is a fingerprint, but manifest + artifact root is the whole
reproducible corpus.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

import canonical_hash
import rob941_frozen_scope as frozen
from rob941_archive_fetch import (
    ArchiveProvenance,  # re-exported: single canonical definition
)

__all__ = [
    "ArchiveProvenance",
    "CorpusManifest",
    "SymbolEligibility",
    "SymbolFundingManifest",
    "SymbolKlineManifest",
]

TRANSFORM_VERSION = "rob941_corpus.v2"  # v2 (R1 I1 remediation): shard_path/shard_file_sha256 persist the derived corpus, not just its fingerprint


@dataclass(frozen=True)
class SymbolKlineManifest:
    symbol: str
    interval: str
    archives: tuple[ArchiveProvenance, ...]
    normalized_shard_sha256: str
    row_count: int
    min_open_time_ms: int
    max_open_time_ms: int
    gap_ranges: tuple[tuple[int, int], ...]
    transform_version: str = TRANSFORM_VERSION
    # ROB-941 R1 I1: artifact-root-relative POSIX path to the persisted Parquet
    # shard + its physical file SHA-256 (distinct from normalized_shard_sha256,
    # the SEMANTIC canonical row-content hash) -- both required for offline load.
    shard_path: str | None = None
    shard_file_sha256: str | None = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "interval": self.interval,
            "archives": [a.to_dict() for a in self.archives],
            "normalized_shard_sha256": self.normalized_shard_sha256,
            "row_count": self.row_count,
            "min_open_time_ms": self.min_open_time_ms,
            "max_open_time_ms": self.max_open_time_ms,
            "gap_ranges": [list(g) for g in self.gap_ranges],
            "transform_version": self.transform_version,
            "shard_path": self.shard_path,
            "shard_file_sha256": self.shard_file_sha256,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SymbolKlineManifest:
        return cls(
            symbol=d["symbol"],
            interval=d["interval"],
            archives=tuple(ArchiveProvenance.from_dict(a) for a in d["archives"]),
            normalized_shard_sha256=d["normalized_shard_sha256"],
            row_count=d["row_count"],
            min_open_time_ms=d["min_open_time_ms"],
            max_open_time_ms=d["max_open_time_ms"],
            gap_ranges=tuple(tuple(g) for g in d["gap_ranges"]),
            transform_version=d.get("transform_version", TRANSFORM_VERSION),
            shard_path=d.get("shard_path"),
            shard_file_sha256=d.get("shard_file_sha256"),
        )


@dataclass(frozen=True)
class SymbolFundingManifest:
    symbol: str
    archives: tuple[ArchiveProvenance, ...]
    normalized_shard_sha256: str
    row_count: int
    min_calc_time_ms: int | None
    max_calc_time_ms: int | None
    transform_version: str = TRANSFORM_VERSION
    shard_path: str | None = None
    shard_file_sha256: str | None = None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "archives": [a.to_dict() for a in self.archives],
            "normalized_shard_sha256": self.normalized_shard_sha256,
            "row_count": self.row_count,
            "min_calc_time_ms": self.min_calc_time_ms,
            "max_calc_time_ms": self.max_calc_time_ms,
            "transform_version": self.transform_version,
            "shard_path": self.shard_path,
            "shard_file_sha256": self.shard_file_sha256,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SymbolFundingManifest:
        return cls(
            symbol=d["symbol"],
            archives=tuple(ArchiveProvenance.from_dict(a) for a in d["archives"]),
            normalized_shard_sha256=d["normalized_shard_sha256"],
            row_count=d["row_count"],
            min_calc_time_ms=d["min_calc_time_ms"],
            max_calc_time_ms=d["max_calc_time_ms"],
            transform_version=d.get("transform_version", TRANSFORM_VERSION),
            shard_path=d.get("shard_path"),
            shard_file_sha256=d.get("shard_file_sha256"),
        )


@dataclass(frozen=True)
class SymbolEligibility:
    symbol: str
    historical_only: bool
    demo_execution_eligible: bool
    reason: str | None

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "historical_only": self.historical_only,
            "demo_execution_eligible": self.demo_execution_eligible,
            "reason": self.reason,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SymbolEligibility:
        return cls(
            symbol=d["symbol"],
            historical_only=d["historical_only"],
            demo_execution_eligible=d["demo_execution_eligible"],
            reason=d.get("reason"),
        )


@dataclass(frozen=True)
class CorpusManifest:
    window_start_iso: str
    window_end_iso: str
    universe: tuple[str, ...]
    eligibility: tuple[SymbolEligibility, ...]
    klines: tuple[SymbolKlineManifest, ...]
    funding: tuple[SymbolFundingManifest, ...]

    def to_dict(self) -> dict:
        return {
            "window_start_iso": self.window_start_iso,
            "window_end_iso": self.window_end_iso,
            "universe": list(self.universe),
            "eligibility": [e.to_dict() for e in self.eligibility],
            "klines": [k.to_dict() for k in self.klines],
            "funding": [f.to_dict() for f in self.funding],
        }

    @classmethod
    def from_dict(cls, d: dict) -> CorpusManifest:
        return cls(
            window_start_iso=d["window_start_iso"],
            window_end_iso=d["window_end_iso"],
            universe=tuple(d["universe"]),
            eligibility=tuple(SymbolEligibility.from_dict(e) for e in d["eligibility"]),
            klines=tuple(SymbolKlineManifest.from_dict(k) for k in d["klines"]),
            funding=tuple(SymbolFundingManifest.from_dict(f) for f in d["funding"]),
        )

    def content_hash(self) -> str:
        """Immutable identity: canonical SHA-256 over the full manifest content."""
        return canonical_hash.canonical_sha256(self.to_dict())

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))

    @classmethod
    def load(cls, path: str | Path) -> CorpusManifest:
        return cls.from_dict(json.loads(Path(path).read_text()))

    def validate_frozen_scope(self) -> None:
        """Raise if window/universe/eligibility deviate from ``rob941_frozen_scope``
        — the manifest must describe exactly the D1-D9 approved scope."""
        if (
            self.window_start_iso != frozen.WINDOW_START_ISO
            or self.window_end_iso != frozen.WINDOW_END_ISO
        ):
            raise ValueError(
                f"manifest window [{self.window_start_iso}, {self.window_end_iso}) deviates from "
                f"frozen scope [{frozen.WINDOW_START_ISO}, {frozen.WINDOW_END_ISO})"
            )
        if set(self.universe) != set(frozen.UNIVERSE):
            raise ValueError(
                f"manifest universe {sorted(self.universe)} deviates from frozen universe "
                f"{sorted(frozen.UNIVERSE)}"
            )
        # R1 I1 loader-hardening follow-up: exactly one kline + one funding
        # entry per frozen symbol -- no duplicate/missing/extra. A duplicate
        # symbol would otherwise silently collapse in load_corpus's
        # symbol-keyed dict without ever raising.
        kline_symbols = sorted(k.symbol for k in self.klines)
        if kline_symbols != sorted(frozen.UNIVERSE):
            raise ValueError(
                f"manifest klines cover {kline_symbols}, expected exactly "
                f"{sorted(frozen.UNIVERSE)} (no duplicate/missing/extra)"
            )
        funding_symbols = sorted(f.symbol for f in self.funding)
        if funding_symbols != sorted(frozen.UNIVERSE):
            raise ValueError(
                f"manifest funding covers {funding_symbols}, expected exactly "
                f"{sorted(frozen.UNIVERSE)} (no duplicate/missing/extra)"
            )
        for e in self.eligibility:
            expected = frozen.eligibility(e.symbol)
            actual = (e.historical_only, e.demo_execution_eligible, e.reason)
            expected_tuple = (
                expected["historical_only"],
                expected["demo_execution_eligible"],
                expected["reason"],
            )
            if actual != expected_tuple:
                raise ValueError(
                    f"{e.symbol}: manifest eligibility {actual} deviates from frozen scope {expected_tuple}"
                )
