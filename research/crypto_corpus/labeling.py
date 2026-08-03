"""One-time, non-holdout metadata-label publication for existing artifacts.

The original dataset tree remains immutable. This module reads only that
exploration tree, verifies value-equivalent labeled copies under staging, then
atomically publishes a new dataset-labeled tree. It never globs, opens, stats,
or otherwise touches the holdout tree.
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pyarrow.parquet as pq

from .constants import ARTIFACT_ROOT, CORPUS_ID, utc_iso
from .policy import label_table_for_venue, policy_from_parquet_metadata, venue_policy

SOURCE_DATASET_DIRECTORY = "dataset"
LABELED_DATASET_DIRECTORY = "dataset-labeled"


@dataclass(frozen=True)
class LabeledFileRecord:
    """Value-equivalence evidence for one metadata-only derived file."""

    source_relative_path: str
    labeled_relative_path: str
    venue: str
    row_count: int
    source_sha256: str
    labeled_sha256: str
    values_equivalent: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class LabelMigrationResult:
    """Published exploration-only label migration evidence."""

    target_dataset_relative_path: str
    receipt_relative_path: str
    file_count: int
    row_count: int
    records: tuple[LabeledFileRecord, ...]


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _token() -> str:
    return f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-{uuid.uuid4().hex}"


def _publish_migration_receipt(root: Path, payload: dict[str, Any]) -> str:
    """Publish a normal-control receipt without constructing a holdout path."""
    rendered = json.dumps(
        payload,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")
    json.loads(rendered)
    relative = Path("control") / "policy-labels" / f"label-migration-{_token()}.json"
    partial = root / ".staging" / f"{_token()}.json.partial"
    partial.parent.mkdir(parents=True, exist_ok=True)
    with partial.open("xb") as handle:
        handle.write(rendered)
        handle.flush()
        os.fsync(handle.fileno())
    _sha256_file(partial)
    destination = root / relative
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.rename(partial, destination)
    return relative.as_posix()


def _source_venue(source_relative_path: Path) -> str:
    if len(source_relative_path.parts) < 3:
        raise ValueError(
            "dataset file is not partitioned as venue=<venue>/year=<year>/file: "
            f"{source_relative_path.as_posix()}"
        )
    venue_partition = source_relative_path.parts[0]
    year_partition = source_relative_path.parts[1]
    if not venue_partition.startswith("venue=") or not year_partition.startswith(
        "year="
    ):
        raise ValueError(
            "dataset file is not partitioned as venue=<venue>/year=<year>/file: "
            f"{source_relative_path.as_posix()}"
        )
    venue = venue_partition.removeprefix("venue=")
    venue_policy(venue)
    return venue


def _write_labeled_partial(
    *,
    source: Path,
    destination: Path,
    venue: str,
) -> LabeledFileRecord:
    """Write, verify, hash, then atomically rename one staging Parquet file."""
    source_table = pq.ParquetFile(source).read()
    labeled_table = label_table_for_venue(source_table, venue)
    source_sha256 = _sha256_file(source)

    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_suffix(f"{destination.suffix}.partial")
    with partial.open("xb") as handle:
        pq.write_table(
            labeled_table,
            handle,
            compression="zstd",
            use_dictionary=True,
            write_statistics=True,
        )
        handle.flush()
        os.fsync(handle.fileno())

    staged_file = pq.ParquetFile(partial)
    staged_table = staged_file.read()
    if not source_table.equals(staged_table, check_metadata=False):
        raise ValueError(
            f"{source}: labeled Parquet copy changed one or more data values"
        )
    policy_from_parquet_metadata(
        staged_file.schema_arrow.metadata,
        expected_venue=venue,
    )
    labeled_sha256 = _sha256_file(partial)
    os.rename(partial, destination)
    return LabeledFileRecord(
        source_relative_path="",
        labeled_relative_path="",
        venue=venue,
        row_count=source_table.num_rows,
        source_sha256=source_sha256,
        labeled_sha256=labeled_sha256,
        values_equivalent=True,
    )


def label_existing_exploration_parquet(
    artifact_root: str | Path = ARTIFACT_ROOT,
) -> LabelMigrationResult:
    """Publish labeled copies of the exploration dataset without reading holdout.

    The operation intentionally refuses to replace an existing output tree.
    Every source file is copied to a fresh staging tree as a partial file,
    value-checked, hashed, atomically renamed inside staging, and finally
    published as one atomic dataset-tree rename.
    """
    root = Path(artifact_root)
    source_root = root / SOURCE_DATASET_DIRECTORY
    target_root = root / LABELED_DATASET_DIRECTORY
    if not source_root.is_dir():
        raise FileNotFoundError(f"exploration dataset tree is absent: {source_root}")
    if target_root.exists():
        raise FileExistsError(
            f"labeled dataset tree already exists and will not be overwritten: "
            f"{target_root}"
        )

    source_paths = tuple(
        path for path in sorted(source_root.rglob("*.parquet")) if path.is_file()
    )
    if not source_paths:
        raise ValueError("exploration dataset tree contains no Parquet files")

    staging_root = (
        root
        / ".staging"
        / f"dataset-labeled-{datetime.now(UTC).strftime('%Y%m%dT%H%M%S%fZ')}-"
        f"{uuid.uuid4().hex}"
    )
    staging_root.mkdir(parents=True, exist_ok=False)
    records: list[LabeledFileRecord] = []
    for source in source_paths:
        source_relative = source.relative_to(source_root)
        venue = _source_venue(source_relative)
        staged_destination = staging_root / source_relative
        record = _write_labeled_partial(
            source=source,
            destination=staged_destination,
            venue=venue,
        )
        records.append(
            LabeledFileRecord(
                source_relative_path=(
                    Path(SOURCE_DATASET_DIRECTORY) / source_relative
                ).as_posix(),
                labeled_relative_path=(
                    Path(LABELED_DATASET_DIRECTORY) / source_relative
                ).as_posix(),
                venue=record.venue,
                row_count=record.row_count,
                source_sha256=record.source_sha256,
                labeled_sha256=record.labeled_sha256,
                values_equivalent=record.values_equivalent,
            )
        )

    os.rename(staging_root, target_root)
    receipt_relative_path = _publish_migration_receipt(
        root,
        {
            "corpus_id": CORPUS_ID,
            "generated_at": utc_iso(datetime.now(UTC)),
            "operation": "METADATA_ONLY_EXPLORATION_LABEL_MIGRATION",
            "source_dataset_relative_path": SOURCE_DATASET_DIRECTORY,
            "target_dataset_relative_path": LABELED_DATASET_DIRECTORY,
            "holdout_read_operations": 0,
            "data_values_changed": False,
            "file_count": len(records),
            "row_count": sum(record.row_count for record in records),
            "files": [record.as_dict() for record in records],
        },
    )
    return LabelMigrationResult(
        target_dataset_relative_path=LABELED_DATASET_DIRECTORY,
        receipt_relative_path=receipt_relative_path,
        file_count=len(records),
        row_count=sum(record.row_count for record in records),
        records=tuple(records),
    )
