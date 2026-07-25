"""ROB-1059 H1 (spec §14.1/AC3) — the immutable per-symbol/corpus manifest.

Records: half-open window ``[start,end)``, symbols, quote_mode, source
(distinguishing checksum-verified archive rows from unchecksummed REST
backfill rows), row counts, expected counts, the missing-row (gap) list,
per-file SHA-256, generator version, schema version. ``content_hash()`` uses
the same canonical, collision-free identity authority as
``research/nautilus_scalping/rob941_manifest.py`` (``research_contracts`` via
the ``canonical_hash`` shim) so re-running the SAME builder over the SAME
already-collected shards reproduces a byte-identical manifest hash without any
new network collection.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import canonical_hash

SCHEMA_VERSION = "rob1059_corpus_manifest.v1"
GENERATOR_VERSION = "rob1059_h1_corpus_builder.v1"

SourceLiteral = Literal["archive_monthly", "archive_daily", "backfill_rest"]

__all__ = [
    "GENERATOR_VERSION",
    "SCHEMA_VERSION",
    "CorpusManifest",
    "ShardSource",
    "SymbolCorpusManifest",
]


@dataclass(frozen=True)
class ShardSource:
    """One contributing fetch — either a checksum-verified archive (monthly or
    daily) or an unchecksummed REST backfill for an archive-uncovered range."""

    source: SourceLiteral
    year: int
    month: int
    day: int | None  # None only for a monthly archive
    url: str
    checksum_sha256: str | None  # None only for backfill_rest (no sidecar exists)

    def to_dict(self) -> dict:
        return {
            "source": self.source,
            "year": self.year,
            "month": self.month,
            "day": self.day,
            "url": self.url,
            "checksum_sha256": self.checksum_sha256,
        }

    @classmethod
    def from_dict(cls, d: dict) -> ShardSource:
        return cls(
            source=d["source"],
            year=d["year"],
            month=d["month"],
            day=d.get("day"),
            url=d["url"],
            checksum_sha256=d.get("checksum_sha256"),
        )

    def __post_init__(self) -> None:
        if self.source in ("archive_monthly", "archive_daily") and (
            self.checksum_sha256 is None
        ):
            raise ValueError(
                f"{self.source} shard must carry a verified checksum_sha256"
            )
        if self.source == "backfill_rest" and self.checksum_sha256 is not None:
            raise ValueError("backfill_rest shard must NOT carry a checksum_sha256")


@dataclass(frozen=True)
class SymbolCorpusManifest:
    symbol: str
    quote_mode: str
    sources: tuple[ShardSource, ...]
    row_count: int
    expected_count: int
    missing_open_times_ms: tuple[int, ...]  # explicit missing-minute list
    normalized_content_sha256: str  # canonical_hash over the normalized row content

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "quote_mode": self.quote_mode,
            "sources": [s.to_dict() for s in self.sources],
            "row_count": self.row_count,
            "expected_count": self.expected_count,
            "missing_open_times_ms": list(self.missing_open_times_ms),
            "normalized_content_sha256": self.normalized_content_sha256,
        }

    @classmethod
    def from_dict(cls, d: dict) -> SymbolCorpusManifest:
        return cls(
            symbol=d["symbol"],
            quote_mode=d["quote_mode"],
            sources=tuple(ShardSource.from_dict(s) for s in d["sources"]),
            row_count=d["row_count"],
            expected_count=d["expected_count"],
            missing_open_times_ms=tuple(d["missing_open_times_ms"]),
            normalized_content_sha256=d["normalized_content_sha256"],
        )


@dataclass(frozen=True)
class CorpusManifest:
    window_start_ms: int
    window_end_ms: int  # exclusive
    symbols: tuple[str, ...]  # canonical lexicographic order
    per_symbol: tuple[SymbolCorpusManifest, ...]  # canonical lexicographic order
    generator_version: str = GENERATOR_VERSION
    schema_version: str = SCHEMA_VERSION

    def __post_init__(self) -> None:
        if self.symbols != tuple(sorted(self.symbols)):
            raise ValueError("symbols must be canonical lexicographic order")
        if len(self.symbols) != len(set(self.symbols)):
            raise ValueError("duplicate symbol in manifest")
        manifest_symbols = tuple(s.symbol for s in self.per_symbol)
        if manifest_symbols != self.symbols:
            raise ValueError(
                f"per_symbol coverage {list(manifest_symbols)} != declared symbols "
                f"{list(self.symbols)} (exact canonical-order match required)"
            )

    def to_dict(self) -> dict:
        return {
            "window_start_ms": self.window_start_ms,
            "window_end_ms": self.window_end_ms,
            "symbols": list(self.symbols),
            "per_symbol": [s.to_dict() for s in self.per_symbol],
            "generator_version": self.generator_version,
            "schema_version": self.schema_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> CorpusManifest:
        return cls(
            window_start_ms=d["window_start_ms"],
            window_end_ms=d["window_end_ms"],
            symbols=tuple(d["symbols"]),
            per_symbol=tuple(
                SymbolCorpusManifest.from_dict(s) for s in d["per_symbol"]
            ),
            generator_version=d.get("generator_version", GENERATOR_VERSION),
            schema_version=d.get("schema_version", SCHEMA_VERSION),
        )

    def content_hash(self) -> str:
        """Immutable identity: canonical SHA-256 over the full manifest
        content. Re-running the SAME builder over the SAME already-collected
        shards (no new network collection) reproduces this exact hash."""
        return canonical_hash.canonical_sha256(self.to_dict())

    def save(self, path: str | Path) -> None:
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True))

    @classmethod
    def load(cls, path: str | Path) -> CorpusManifest:
        return cls.from_dict(json.loads(Path(path).read_text()))
