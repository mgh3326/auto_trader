"""ROB-1059 H1 (AC23) — persist + offline-verify a symbol's normalized kline
shard under a gitignored artifact root.

Writing DELEGATES to ``rob941_persistence.write_kline_shard`` by direct import
(composition, not a fork) — ``rob941_kline_schema.NormalizedKline`` is the same
row type ``corpus_builder.build_symbol_corpus`` already returns, so the exact
same content-addressed Parquet writer applies unmodified.

Loading is this module's OWN fail-closed verification chain (a NEW module, not
an edit of ``rob941_offline_loader``, which is keyed to that module's own
``SymbolKlineManifest``/frozen-scope contract): physical file SHA-256 ->
EXACT pinned Parquet schema -> canonical row-content hash -> row count. Every
failure mode raises a distinct ``ShardLoadError`` subclass, mirroring the
``rob941_offline_loader`` discipline.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import canonical_hash
import pyarrow as pa
import pyarrow.parquet as pq
import rob941_kline_schema as ks
import rob941_persistence as rp

write_symbol_shard = rp.write_kline_shard  # direct re-export: composition, not a fork


class ShardLoadError(RuntimeError):
    """Base class for every offline-load fail-closed rejection."""


class ShardPathEscapesArtifactRootError(ShardLoadError):
    """A relative shard path is absolute or resolves outside ``artifact_root``
    — refused before any file I/O."""


class ShardFileMissingError(ShardLoadError):
    """The named shard file is absent on disk."""


class ShardFileTamperedError(ShardLoadError):
    """The on-disk file's physical SHA-256 does not match the recorded value."""


class ShardSchemaMismatchError(ShardLoadError):
    """The Parquet file's schema does not exactly match the pinned schema."""


class ShardContentTamperedError(ShardLoadError):
    """The decoded rows' canonical content hash does not match the recorded
    ``normalized_content_sha256``."""


class ShardRowCountMismatchError(ShardLoadError):
    """Decoded row count does not match the recorded ``row_count``."""


def _resolve_within_root(artifact_root: Path, relative_path: str) -> Path:
    root = artifact_root.resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ShardPathEscapesArtifactRootError(
            f"path {relative_path!r} escapes artifact root {root}"
        )
    return candidate


def load_symbol_shard(
    artifact_root: Path,
    relative_path: str,
    *,
    expected_file_sha256: str,
    expected_content_sha256: str,
    expected_row_count: int,
) -> list[ks.NormalizedKline]:
    """Load+verify one symbol's persisted kline shard fully offline. Raises a
    ``ShardLoadError`` subclass on any tamper/missing/mismatch condition —
    zero network access, zero DB access.
    """
    path = _resolve_within_root(artifact_root, relative_path)
    if not path.is_file():
        raise ShardFileMissingError(f"shard file missing at {path}")

    # CodeRabbit fix: hash and parse the SAME in-memory bytes. `rp.sha256_file`
    # opens+streams the file once, then `pq.read_table(path)` used to open it
    # a SECOND time -- if the file changed between those two independent
    # reads (e.g. a concurrent overwrite), bytes that never matched
    # `expected_file_sha256` could still be parsed and pass every later
    # schema/content-hash check. Reading once and verifying+parsing that
    # exact buffer closes the gap.
    data = path.read_bytes()
    actual_file_sha256 = hashlib.sha256(data).hexdigest()
    if actual_file_sha256 != expected_file_sha256:
        raise ShardFileTamperedError(
            f"shard file SHA-256 mismatch (expected {expected_file_sha256}, "
            f"got {actual_file_sha256})"
        )

    table = pq.read_table(pa.BufferReader(data))
    if not table.schema.equals(rp.KLINE_SCHEMA, check_metadata=False):
        raise ShardSchemaMismatchError(
            f"shard schema {table.schema} != expected {rp.KLINE_SCHEMA}"
        )
    rows = [ks.NormalizedKline(**d) for d in table.to_pylist()]

    actual_content_sha256 = canonical_hash.canonical_sha256([r.__dict__ for r in rows])
    if actual_content_sha256 != expected_content_sha256:
        raise ShardContentTamperedError(
            f"normalized content hash mismatch (expected {expected_content_sha256}, "
            f"got {actual_content_sha256})"
        )

    if len(rows) != expected_row_count:
        raise ShardRowCountMismatchError(
            f"row_count mismatch (expected {expected_row_count}, loaded {len(rows)})"
        )
    return rows
