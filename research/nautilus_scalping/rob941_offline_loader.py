"""ROB-941 (AC6, R1 I1 remediation) — offline, network-0 loader for the
persisted corpus.

Given only a ``CorpusManifest`` and the ``artifact_root`` it was materialized
under (``artifact_paths.pit_data_root()``), H4/H6 can load every symbol's 1m
kline + funding rows with ZERO network access. This module imports no network
opener (enforced structurally by
``tests/test_rob941_offline_loader.py::test_offline_loader_module_never_imports_urllib_or_network_openers``)
and never touches ``rob941_archive_fetch.urllib_opener``.

Fail-closed verification chain, per symbol shard (``load_kline_shard``/
``load_funding_shard``): every raw archive's local path/existence/physical
SHA-256 -> derived-shard path traversal -> file existence -> physical file
SHA-256 -> EXACT Parquet schema (field names, types, nullability, and order —
not names alone) -> canonical row-content hash -> row count/min/max. At the
corpus level (``load_corpus``, ``ultrathink`` captain review): exactly one
kline + one funding entry per frozen symbol and window/universe/eligibility
(``CorpusManifest.validate_frozen_scope``), every loaded row's timestamp
inside the frozen half-open window, and a from-scratch recomputation of each
symbol's ``gap_ranges`` that must equal the manifest's declared value. Every
failure mode raises a distinct, stable ``ShardLoadError`` subclass — tamper,
missing data, and a wrong artifact root are never confused with a generic
exception.

Per-symbol vs. corpus-level split is deliberate: ``load_kline_shard``/
``load_funding_shard`` only have that ONE symbol's manifest in hand, so they
verify everything self-contained to it (including its own raw archives).
Window-membership and gap-range recomputation need the shared corpus window
and reuse ``rob941_gaps`` (the same algorithm ``rob941_corpus_builder`` used to
produce the manifest in the first place), so they live in ``load_corpus``,
the entry point H4/H6 actually call.
"""

from __future__ import annotations

from pathlib import Path

import canonical_hash
import pyarrow.parquet as pq
import rob941_frozen_scope as frozen
import rob941_gaps as gaps
import rob941_kline_schema as ks
import rob941_persistence as persist
from funding_oi_archive import FundingRow
from rob941_manifest import (
    ArchiveProvenance,
    CorpusManifest,
    SymbolFundingManifest,
    SymbolKlineManifest,
)


class ShardLoadError(RuntimeError):
    """Base class for every offline-load fail-closed rejection."""


class ShardPathEscapesArtifactRootError(ShardLoadError):
    """A ``shard_path``/archive ``local_path`` is absolute or resolves outside
    ``artifact_root`` — refused before any file I/O, never followed."""


class ShardFileMissingError(ShardLoadError):
    """The manifest has no ``shard_path`` (never materialized), or the file it
    names is absent on disk."""


class ShardFileTamperedError(ShardLoadError):
    """The on-disk shard file's SHA-256 does not match ``shard_file_sha256``."""


class ShardSchemaMismatchError(ShardLoadError):
    """The Parquet file's schema (field names, types, nullability, or order)
    does not exactly match the pinned schema — a names-only match is not
    sufficient (a tampered file could keep names but change types)."""


class ShardContentTamperedError(ShardLoadError):
    """The file's bytes are intact, but the decoded rows' canonical content hash
    does not match ``normalized_shard_sha256`` — the manifest's semantic-content
    pin and the file's actual content have diverged."""


class ShardRowCountMismatchError(ShardLoadError):
    """Decoded row count does not match the manifest's declared ``row_count``."""


class ShardTimeRangeMismatchError(ShardLoadError):
    """Decoded min/max timestamp does not match the manifest's declared bound."""


class ArchiveFileMissingError(ShardLoadError):
    """A raw archive's manifest ``local_path`` is ``None`` (never materialized),
    or the file it names is absent on disk."""


class ArchiveFileTamperedError(ShardLoadError):
    """A raw archive's on-disk SHA-256 does not match its manifest-recorded,
    already-verified ``checksum_sha256``."""


class ShardWindowViolationError(ShardLoadError):
    """A loaded row's timestamp lies outside the corpus's frozen half-open
    window — the manifest's own declared min/max can be internally consistent
    and still violate the corpus-level window contract."""


class ShardGapRangesMismatchError(ShardLoadError):
    """Recomputing gap ranges from the loaded rows (the same algorithm that
    produced the manifest at build time) does not reproduce the manifest's
    declared ``gap_ranges`` — the declared gaps and the actual data disagree."""


def _resolve_within_root(artifact_root: Path, relative_path: str) -> Path:
    root = artifact_root.resolve()
    candidate = (root / relative_path).resolve()
    if candidate != root and root not in candidate.parents:
        raise ShardPathEscapesArtifactRootError(
            f"path {relative_path!r} escapes artifact root {root}"
        )
    return candidate


def verify_archives(
    archives: tuple[ArchiveProvenance, ...], artifact_root: Path, *, symbol: str
) -> None:
    """Fail-closed offline verification of every raw archive a shard cites:
    path traversal -> file existence -> physical SHA-256 == the archive's own
    already-verified ``checksum_sha256``. Network-0; never re-fetches."""
    for a in archives:
        if a.local_path is None:
            raise ArchiveFileMissingError(
                f"{symbol}: archive {a.url!r} has no local_path (never materialized)"
            )
        path = _resolve_within_root(artifact_root, a.local_path)
        if not path.is_file():
            raise ArchiveFileMissingError(
                f"{symbol}: archive file missing at {path} (url={a.url!r})"
            )
        actual = persist.sha256_file(path)
        if actual != a.checksum_sha256:
            raise ArchiveFileTamperedError(
                f"{symbol}: archive {a.local_path!r} SHA-256 mismatch "
                f"(expected {a.checksum_sha256}, got {actual})"
            )


def load_kline_shard(
    manifest: SymbolKlineManifest, artifact_root: Path
) -> list[ks.NormalizedKline]:
    """Load+verify one symbol's kline shard fully offline. Raises a
    ``ShardLoadError`` subclass on any tamper/missing/mismatch condition."""
    verify_archives(manifest.archives, artifact_root, symbol=manifest.symbol)

    if manifest.shard_path is None:
        raise ShardFileMissingError(
            f"{manifest.symbol}: manifest has no shard_path (never materialized)"
        )
    path = _resolve_within_root(artifact_root, manifest.shard_path)
    if not path.is_file():
        raise ShardFileMissingError(f"{manifest.symbol}: shard file missing at {path}")

    actual_file_sha256 = persist.sha256_file(path)
    if actual_file_sha256 != manifest.shard_file_sha256:
        raise ShardFileTamperedError(
            f"{manifest.symbol}: shard file SHA-256 mismatch "
            f"(expected {manifest.shard_file_sha256}, got {actual_file_sha256})"
        )

    table = pq.read_table(path)
    if not table.schema.equals(persist.KLINE_SCHEMA, check_metadata=False):
        raise ShardSchemaMismatchError(
            f"{manifest.symbol}: shard schema {table.schema} != expected "
            f"{persist.KLINE_SCHEMA}"
        )
    rows = [ks.NormalizedKline(**d) for d in table.to_pylist()]

    actual_row_hash = canonical_hash.canonical_sha256([r.__dict__ for r in rows])
    if actual_row_hash != manifest.normalized_shard_sha256:
        raise ShardContentTamperedError(
            f"{manifest.symbol}: normalized row-content hash mismatch "
            f"(expected {manifest.normalized_shard_sha256}, got {actual_row_hash})"
        )

    if len(rows) != manifest.row_count:
        raise ShardRowCountMismatchError(
            f"{manifest.symbol}: row_count mismatch (manifest={manifest.row_count}, "
            f"loaded={len(rows)})"
        )
    if rows and (
        rows[0].open_time_ms != manifest.min_open_time_ms
        or rows[-1].open_time_ms != manifest.max_open_time_ms
    ):
        raise ShardTimeRangeMismatchError(
            f"{manifest.symbol}: min/max open_time_ms mismatch "
            f"(manifest=[{manifest.min_open_time_ms}, {manifest.max_open_time_ms}], "
            f"loaded=[{rows[0].open_time_ms}, {rows[-1].open_time_ms}])"
        )
    return rows


def load_funding_shard(
    manifest: SymbolFundingManifest, artifact_root: Path
) -> list[FundingRow]:
    """Load+verify one symbol's PIT funding shard fully offline. Same fail-closed
    chain as :func:`load_kline_shard`."""
    verify_archives(manifest.archives, artifact_root, symbol=manifest.symbol)

    if manifest.shard_path is None:
        raise ShardFileMissingError(
            f"{manifest.symbol}: manifest has no shard_path (never materialized)"
        )
    path = _resolve_within_root(artifact_root, manifest.shard_path)
    if not path.is_file():
        raise ShardFileMissingError(f"{manifest.symbol}: shard file missing at {path}")

    actual_file_sha256 = persist.sha256_file(path)
    if actual_file_sha256 != manifest.shard_file_sha256:
        raise ShardFileTamperedError(
            f"{manifest.symbol}: shard file SHA-256 mismatch "
            f"(expected {manifest.shard_file_sha256}, got {actual_file_sha256})"
        )

    table = pq.read_table(path)
    if not table.schema.equals(persist.FUNDING_SCHEMA, check_metadata=False):
        raise ShardSchemaMismatchError(
            f"{manifest.symbol}: funding shard schema {table.schema} != expected "
            f"{persist.FUNDING_SCHEMA}"
        )
    rows = [FundingRow(**d) for d in table.to_pylist()]

    actual_row_hash = canonical_hash.canonical_sha256([r.__dict__ for r in rows])
    if actual_row_hash != manifest.normalized_shard_sha256:
        raise ShardContentTamperedError(
            f"{manifest.symbol}: funding normalized row-content hash mismatch"
        )

    if len(rows) != manifest.row_count:
        raise ShardRowCountMismatchError(
            f"{manifest.symbol}: funding row_count mismatch "
            f"(manifest={manifest.row_count}, loaded={len(rows)})"
        )
    if rows and (
        rows[0].calc_time != manifest.min_calc_time_ms
        or rows[-1].calc_time != manifest.max_calc_time_ms
    ):
        raise ShardTimeRangeMismatchError(
            f"{manifest.symbol}: funding min/max calc_time mismatch"
        )
    return rows


def load_corpus(manifest: CorpusManifest, artifact_root: Path) -> dict:
    """Load+verify all symbols' kline+funding shards fully offline.

    ``validate_frozen_scope()`` runs first (cheap, structural window/universe/
    eligibility/exact-symbol-coverage check) so a tampered corpus-level
    manifest fails fast before any shard I/O; every per-shard load still runs
    its own full fail-closed chain (including its raw archives). On top of
    that, every loaded row's timestamp is checked against the frozen window,
    and each symbol's kline gap_ranges is independently recomputed from the
    loaded rows (``rob941_gaps.detect_gap_ranges``, the same algorithm used at
    build time) and compared against the manifest's declared value.
    """
    manifest.validate_frozen_scope()

    klines: dict[str, list[ks.NormalizedKline]] = {}
    for k in manifest.klines:
        rows = load_kline_shard(k, artifact_root)
        if not all(
            frozen.WINDOW_START_MS <= r.open_time_ms < frozen.WINDOW_END_MS
            for r in rows
        ):
            raise ShardWindowViolationError(
                f"{k.symbol}: a loaded kline row's open_time_ms lies outside the "
                f"frozen window [{frozen.WINDOW_START_MS}, {frozen.WINDOW_END_MS})"
            )
        recomputed_gaps = tuple(
            gaps.detect_gap_ranges(
                [r.open_time_ms for r in rows],
                frozen.WINDOW_START_MS,
                frozen.WINDOW_END_MS,
            )
        )
        if recomputed_gaps != k.gap_ranges:
            raise ShardGapRangesMismatchError(
                f"{k.symbol}: recomputed gap_ranges != manifest.gap_ranges "
                f"(recomputed {len(recomputed_gaps)} ranges, manifest declared "
                f"{len(k.gap_ranges)})"
            )
        klines[k.symbol] = rows

    funding: dict[str, list[FundingRow]] = {}
    for f in manifest.funding:
        frows = load_funding_shard(f, artifact_root)
        if not all(
            frozen.WINDOW_START_MS <= r.calc_time < frozen.WINDOW_END_MS for r in frows
        ):
            raise ShardWindowViolationError(
                f"{f.symbol}: a loaded funding row's calc_time lies outside the "
                f"frozen window [{frozen.WINDOW_START_MS}, {frozen.WINDOW_END_MS})"
            )
        funding[f.symbol] = frows

    return {"klines": klines, "funding": funding}
