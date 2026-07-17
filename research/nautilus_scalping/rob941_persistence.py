"""ROB-941 (AC6, R1 I1 remediation) — persist raw archives + normalized Parquet
shards under a gitignored artifact root.

The verify-round finding (Important I1) was that the committed manifest only
held a checksum + a normalized-shard SHA-256 *fingerprint*, never the actual
derived bytes — H4/H6 could not load the corpus offline; they would have had
to re-fetch and re-normalize 96 archives from the network to reproduce it.
This module writes the checksum-verified raw ``.zip`` archives and the
normalized kline/funding rows (as Parquet, ``pyarrow`` — already a pinned repo
dependency) under ``artifact_paths.pit_data_root()`` (or
``AUTO_TRADER_RESEARCH_ARTIFACT_ROOT`` when set), so the manifest + artifact
root together are the whole reproducible corpus.

Two distinct hashes are pinned per shard, deliberately not conflated:

- ``normalized_shard_sha256`` (``rob941_manifest``): the SEMANTIC canonical
  hash of the row *content* (``canonical_hash.canonical_sha256``) — stable
  across re-writes, re-compression, or a different pyarrow version.
- ``shard_file_sha256`` (this module): the PHYSICAL SHA-256 of the Parquet
  file's bytes on disk — catches any tamper/corruption of the artifact itself,
  independent of whether the logical content also happens to still decode.

Schema/column order/compression are pinned explicitly here so the write is a
deterministic *transform* (same rows -> same logical table); byte-identical
files across machines/pyarrow versions are NOT claimed or required — each
build computes and pins its own fresh ``shard_file_sha256``.

``ultrathink`` (captain review, atomicity follow-up): every path this module
hands out is CONTENT-ADDRESSED — the raw archive's own verified checksum, and
the shard's semantic ``normalized_shard_sha256`` (a *row-content* hash, NOT a
promise about the physical Parquet bytes a given pyarrow/compression version
would produce), are embedded directly in the relative path (alongside a
human-readable symbol/kind/year-month prefix for operator browsability). The
safety guarantee this buys is NOT "same semantic content always serializes to
byte-identical Parquet" (it may not, across pyarrow versions) — it is
IMMUTABLE NO-OVERWRITE: ``write_kline_shard``/``write_funding_shard``/
``write_raw_archive`` never touch a path that already has a file at it (an
existence check short-circuits before any write), and whatever bytes actually
end up on disk at that path are pinned by their own freshly-computed
``shard_file_sha256``/verified archive checksum every time they're read back
(``rob941_offline_loader``). So a rebuild can only ever (a) skip a path that
already holds *some* file (never inspecting or overwriting it) or (b) write to
a brand-new path for genuinely different content — either way, a prior, still-
published manifest's referenced bytes are structurally never mutated. No
separate generation/staging directory, temp-write, or rename is needed for the
per-shard artifacts — only the single COMMITTED MANIFEST FILE needs an atomic
publish step (``build_rob941_corpus._atomic_save``), since it is the only
mutable pointer ("which shard hashes does the current corpus reference"). This
also makes an identical rerun (same fixture/upstream bytes, same content
hashes) reproduce the exact same relative paths and therefore the exact same
manifest ``content_hash()`` — there is no time/random component anywhere in
this path scheme, and no circularity (every hash a path is keyed on is
computed from in-memory content BEFORE that path is ever built or written to).
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import canonical_hash
import pyarrow as pa
import pyarrow.parquet as pq
import rob941_kline_schema as ks
from funding_oi_archive import FundingRow

_CHUNK = 1 << 20
PARQUET_COMPRESSION = "zstd"

KLINE_COLUMN_ORDER: tuple[str, ...] = (
    "symbol",
    "open_time_ms",
    "open",
    "high",
    "low",
    "close",
    "base_volume",
    "close_time_ms",
    "quote_volume",
    "trade_count",
    "taker_buy_volume",
    "taker_buy_quote_volume",
)
# nullable=False on every field: a NormalizedKline/FundingRow never carries a
# None value, so a NULL appearing anywhere is itself a tamper/corruption signal
# the loader's exact-schema check (types + nullability + order) must catch.
KLINE_SCHEMA = pa.schema(
    [
        pa.field("symbol", pa.string(), nullable=False),
        pa.field("open_time_ms", pa.int64(), nullable=False),
        pa.field("open", pa.float64(), nullable=False),
        pa.field("high", pa.float64(), nullable=False),
        pa.field("low", pa.float64(), nullable=False),
        pa.field("close", pa.float64(), nullable=False),
        pa.field("base_volume", pa.float64(), nullable=False),
        pa.field("close_time_ms", pa.int64(), nullable=False),
        pa.field("quote_volume", pa.float64(), nullable=False),
        pa.field("trade_count", pa.int64(), nullable=False),
        pa.field("taker_buy_volume", pa.float64(), nullable=False),
        pa.field("taker_buy_quote_volume", pa.float64(), nullable=False),
    ]
)

FUNDING_COLUMN_ORDER: tuple[str, ...] = (
    "calc_time",
    "funding_interval_hours",
    "last_funding_rate",
)
FUNDING_SCHEMA = pa.schema(
    [
        pa.field("calc_time", pa.int64(), nullable=False),
        pa.field("funding_interval_hours", pa.int64(), nullable=False),
        pa.field("last_funding_rate", pa.float64(), nullable=False),
    ]
)


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            h.update(chunk)
    return h.hexdigest()


def raw_archive_relative_path(
    symbol: str, kind: str, year: int, month: int, checksum_sha256: str
) -> str:
    """Content-addressed path for a persisted raw archive ``.zip``: the
    archive's OWN verified checksum is embedded in the filename, so
    byte-identical archives always resolve to the same path and any different
    content lands at a different one. ``kind`` mirrors the upstream Binance
    path segment (``klines``/``fundingRate``); ``year``/``month`` are kept as a
    human-readable prefix only (not part of the address)."""
    return (
        f"raw/{kind}/{symbol}/"
        f"{symbol}-{kind}-{year:04d}-{month:02d}.{checksum_sha256}.zip"
    )


def kline_shard_relative_path(
    symbol: str, normalized_shard_sha256: str, interval: str = "1m"
) -> str:
    """Content-addressed: ``normalized_shard_sha256`` (the semantic canonical
    row-content hash) is embedded in the filename."""
    return f"shards/klines/{symbol}-{interval}.{normalized_shard_sha256}.parquet"


def funding_shard_relative_path(symbol: str, normalized_shard_sha256: str) -> str:
    return f"shards/funding/{symbol}-fundingRate.{normalized_shard_sha256}.parquet"


def write_raw_archive(
    artifact_root: Path, symbol: str, kind: str, year: int, month: int, zip_bytes: bytes
) -> str:
    """Persist the exact, already checksum-verified ``zip_bytes`` unmodified at
    its content-addressed path. A pre-existing file at that path is BY
    DEFINITION byte-identical (same checksum -> same path) and is left alone
    (no redundant re-write). Returns the artifact-root-relative POSIX path."""
    checksum = hashlib.sha256(zip_bytes).hexdigest()
    rel = raw_archive_relative_path(symbol, kind, year, month, checksum)
    dest = artifact_root / rel
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(zip_bytes)
    return rel


def write_kline_shard(
    artifact_root: Path,
    symbol: str,
    rows: list[ks.NormalizedKline],
    interval: str = "1m",
) -> tuple[str, str]:
    """Write normalized kline rows as Parquet at their content-addressed path
    (skipped if already present -- same content, same path, by construction).
    Returns ``(relative_path, file_sha256)``."""
    content_hash = canonical_hash.canonical_sha256([r.__dict__ for r in rows])
    rel = kline_shard_relative_path(symbol, content_hash, interval)
    dest = artifact_root / rel
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        columns = {
            name: [getattr(r, name) for r in rows] for name in KLINE_COLUMN_ORDER
        }
        table = pa.table(columns, schema=KLINE_SCHEMA)
        pq.write_table(table, dest, compression=PARQUET_COMPRESSION)
    return rel, sha256_file(dest)


def write_funding_shard(
    artifact_root: Path, symbol: str, rows: list[FundingRow]
) -> tuple[str, str]:
    """Write PIT funding rows as Parquet at their content-addressed path.
    Returns ``(relative_path, file_sha256)``."""
    content_hash = canonical_hash.canonical_sha256([r.__dict__ for r in rows])
    rel = funding_shard_relative_path(symbol, content_hash)
    dest = artifact_root / rel
    if not dest.is_file():
        dest.parent.mkdir(parents=True, exist_ok=True)
        columns = {
            name: [getattr(r, name) for r in rows] for name in FUNDING_COLUMN_ORDER
        }
        table = pa.table(columns, schema=FUNDING_SCHEMA)
        pq.write_table(table, dest, compression=PARQUET_COMPRESSION)
    return rel, sha256_file(dest)
