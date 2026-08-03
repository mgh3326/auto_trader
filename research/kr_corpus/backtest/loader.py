"""Manifest-gated parquet loader for the KR backtest harness.

Pattern reused from ``research/alpaca_track/persistence.py::load_symbol_shard``:

* resolve path within artifact root (path-escape refuse)
* holdout **dual** gate (path case-fold + date/partition year) on every entrypoint
* read file bytes **once**
* SHA-256 vs manifest **before** parquet parse
* exact schema vs declared contract
* row-level session_date holdout refusal

Mismatch is always an exception (hard refusal), never a warning.

Loader public entrypoints (complete enumeration — keep this list honest):

1. ``load_manifest`` — path gate on manifest path + each entry relative_path;
   date/partition-year gate on every entry ``year`` before return.
2. ``load_shard`` — path gate (root + relative + resolved); partition-year
   gate **before** byte read; window date gate; row session_date gate after parse.

Internal helpers (not public, but always dual-gated when used):

* ``_resolve_within_root`` — path only (called only after year gate in load_shard,
  or for absolute relative_path path checks).
* ``_assert_no_holdout_rows`` — date only (post-parse row scan).
* ``_gate_manifest_entry`` — path + year for one manifest row.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Literal

import pyarrow as pa
import pyarrow.parquet as pq
from holdout_guard import (
    DEFAULT_HOLDOUT_POLICY,
    HoldoutAccessError,
    HoldoutPolicy,
    assert_date_not_holdout,
    assert_partition_year_not_holdout,
    assert_path_not_holdout,
    assert_range_not_holdout,
)
from schema_contract import CONTRACT_PATH, SchemaMismatchError, validate_table_schema
from windows import EXPLORATION_WINDOW, parse_iso_date

__all__ = [
    "LOADER_ENTRYPOINTS",
    "LoaderError",
    "ManifestEntry",
    "ManifestShaMismatchError",
    "PathEscapesArtifactRootError",
    "RowCountMismatchError",
    "ShardFileMissingError",
    "load_manifest",
    "load_shard",
    "sha256_bytes",
]

# Complete public loader surface for dual holdout gating. Update this list
# in the same PR if a new public load function is added.
LOADER_ENTRYPOINTS: tuple[str, ...] = (
    "load_manifest",
    "load_shard",
)

DatasetName = Literal["ohlcv", "membership"]


class LoaderError(RuntimeError):
    """Base for loader hard-refusals."""


class PathEscapesArtifactRootError(LoaderError):
    """Relative path resolves outside the artifact root."""


class ShardFileMissingError(LoaderError):
    """Named shard is absent on disk."""


class ManifestShaMismatchError(LoaderError):
    """On-disk SHA-256 does not match the manifest — read refused."""


class RowCountMismatchError(LoaderError):
    """Decoded row count does not match the manifest."""


@dataclass(frozen=True)
class ManifestEntry:
    relative_path: str
    file_sha256: str
    row_count: int
    dataset: str
    market: str
    year: int

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> ManifestEntry:
        required = {
            "relative_path",
            "file_sha256",
            "row_count",
            "dataset",
            "market",
            "year",
        }
        missing = required - set(raw)
        if missing:
            raise LoaderError(f"manifest entry missing fields: {sorted(missing)}")
        return cls(
            relative_path=str(raw["relative_path"]),
            file_sha256=str(raw["file_sha256"]).lower(),
            row_count=int(raw["row_count"]),
            dataset=str(raw["dataset"]),
            market=str(raw["market"]),
            year=int(raw["year"]),
        )


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _resolve_within_root(
    artifact_root: Path,
    relative_path: str,
    *,
    holdout_policy: HoldoutPolicy,
) -> Path:
    # Holdout path gate first — even before root-escape logic — so a request
    # that points at HOLDOUT_DIR is refused as HoldoutPathBlocked specifically.
    if Path(relative_path).is_absolute():
        assert_path_not_holdout(relative_path, policy=holdout_policy)

    root = artifact_root.expanduser().resolve()
    # Refuse if the artifact_root itself is under holdout.
    assert_path_not_holdout(root, policy=holdout_policy)

    candidate = (root / relative_path).resolve()
    assert_path_not_holdout(candidate, policy=holdout_policy)

    if candidate != root and root not in candidate.parents:
        # Case-fold escape check already done via assert_path_not_holdout on
        # candidate; this is pure artifact-root containment.
        raise PathEscapesArtifactRootError(
            f"path {relative_path!r} escapes artifact root {root}"
        )
    return candidate


def _gate_manifest_entry(
    entry: ManifestEntry,
    *,
    manifest_parent: Path,
    holdout_policy: HoldoutPolicy,
) -> None:
    """Dual path+date gate for one manifest row (before any shard open)."""
    # Date / partition-year gate (NICE-1: refuse before parquet open).
    assert_partition_year_not_holdout(entry.year, policy=holdout_policy)

    rel = entry.relative_path
    if Path(rel).is_absolute():
        assert_path_not_holdout(rel, policy=holdout_policy)
    else:
        assert_path_not_holdout(manifest_parent / rel, policy=holdout_policy)


def load_manifest(
    manifest_path: Path | str,
    *,
    holdout_policy: HoldoutPolicy = DEFAULT_HOLDOUT_POLICY,
) -> list[ManifestEntry]:
    """Load a manifest JSON list under dual holdout gates.

    Gates (both required — brief §4-3):
    * **path**: manifest path itself + each entry's relative_path
    * **date**: each entry's partition ``year`` must not intersect HOLDOUT_WINDOW
    """
    path = assert_path_not_holdout(manifest_path, policy=holdout_policy)
    if not path.is_file():
        raise ShardFileMissingError(f"manifest missing at {path}")
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise LoaderError("manifest root must be a JSON list of entries")
    parent = path.parent
    entries: list[ManifestEntry] = []
    for item in raw:
        entry = ManifestEntry.from_dict(item)
        _gate_manifest_entry(
            entry,
            manifest_parent=parent,
            holdout_policy=holdout_policy,
        )
        entries.append(entry)
    return entries


def load_shard(
    artifact_root: Path | str,
    entry: ManifestEntry,
    *,
    allowed_window_start: date | str | None = None,
    allowed_window_end: date | str | None = None,
    contract_path: Path | str = CONTRACT_PATH,
    holdout_policy: HoldoutPolicy = DEFAULT_HOLDOUT_POLICY,
) -> pa.Table:
    """Load one parquet shard under manifest SHA-256 gate + holdout dual gate.

    Reuses the single-buffer hash-then-parse discipline from
    ``research/alpaca_track/persistence.py`` lines 87–100.

    Order:
    1. partition year date-gate (before any byte read — NICE-1)
    2. path resolve + holdout path gate
    3. SHA-256 vs manifest (refusal on mismatch)
    4. parquet parse + schema + row-count
    5. row session_date holdout gate
    6. caller / default exploration window date gate
    """
    # Date gate first — refuse holdout partition years without opening files.
    assert_partition_year_not_holdout(entry.year, policy=holdout_policy)

    root = Path(artifact_root)
    path = _resolve_within_root(
        root,
        entry.relative_path,
        holdout_policy=holdout_policy,
    )
    if not path.is_file():
        raise ShardFileMissingError(f"shard file missing at {path}")

    # Single in-memory buffer: hash and parse the SAME bytes (no TOCTOU).
    data = path.read_bytes()
    actual_sha = sha256_bytes(data)
    if actual_sha != entry.file_sha256:
        raise ManifestShaMismatchError(
            f"SHA-256 mismatch for {entry.relative_path!r}: "
            f"manifest={entry.file_sha256} actual={actual_sha} — read refused"
        )

    table = pq.read_table(pa.BufferReader(data))
    try:
        validate_table_schema(table, entry.dataset, contract_path=contract_path)
    except SchemaMismatchError as exc:
        raise LoaderError(str(exc)) from exc

    if table.num_rows != entry.row_count:
        raise RowCountMismatchError(
            f"row_count mismatch for {entry.relative_path!r}: "
            f"manifest={entry.row_count} actual={table.num_rows}"
        )

    _assert_no_holdout_rows(table, holdout_policy=holdout_policy)

    # Optional caller window: still dual-gated against holdout.
    if allowed_window_start is not None and allowed_window_end is not None:
        assert_range_not_holdout(
            allowed_window_start,
            allowed_window_end,
            policy=holdout_policy,
        )
    else:
        # Default exploration window — never opens holdout by accident.
        assert_range_not_holdout(
            EXPLORATION_WINDOW.start,
            EXPLORATION_WINDOW.end,
            policy=holdout_policy,
        )

    return table


def _assert_no_holdout_rows(
    table: pa.Table,
    *,
    holdout_policy: HoldoutPolicy,
) -> None:
    """Date-level holdout gate over every session_date in the table."""
    if "session_date" not in table.column_names:
        raise LoaderError("table missing session_date column")
    col = table.column("session_date")
    for i in range(len(col)):
        raw = col[i].as_py()
        try:
            assert_date_not_holdout(raw, policy=holdout_policy)
        except HoldoutAccessError:
            raise
        # Bound check also rejects non-ISO via parse_iso_date inside guard.
        _ = parse_iso_date(raw) if isinstance(raw, str) else raw
