"""Append-only artifact persistence with staging and atomic publication.

All completed data files are first materialized in ``.staging``.  Their
Parquet metadata and SHA-256 are verified from the exact in-memory byte stream
that is written to the ``.partial`` file, then the staging file is atomically
renamed into its final location.  Final artifact names are UUID based, so no
publication operation overwrites an earlier file.

The holdout directory is write-only for this job.  Metadata, hashes, and row
counts are carried in receipts outside that directory; no helper in this module
opens, stats, globs, or otherwise reads a completed holdout file.
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

import pyarrow as pa
import pyarrow.parquet as pq


@dataclass(frozen=True)
class FileRecord:
    """A published artifact known by its creation-time evidence only."""

    relative_path: str
    sha256: str
    byte_size: int
    row_count: int | None
    kind: str
    is_holdout: bool = False

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class StagedFile:
    """An immutable publication plan persisted before final rename."""

    staging_path: str
    final_relative_path: str
    record: FileRecord

    def as_dict(self) -> dict[str, Any]:
        return {
            "staging_path": self.staging_path,
            "final_relative_path": self.final_relative_path,
            "record": self.record.as_dict(),
        }


def _timestamp_token() -> str:
    return datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")


class ArtifactStore:
    """Owns only the job's artifact root; it never reads the holdout tree."""

    def __init__(self, root: str | Path) -> None:
        self.root = Path(root)
        self.staging = self.root / ".staging"
        self.inputs = self.root / "inputs"
        self.dataset = self.root / "dataset"
        self.holdout = self.root / "holdout"
        self.control = self.root / "control"
        self.receipts = self.control / "receipts"
        self.inflight = self.control / "inflight"
        self.preflight = self.control / "preflight"
        for directory in (
            self.root,
            self.staging,
            self.inputs,
            self.dataset,
            self.holdout,
            self.control,
            self.receipts,
            self.inflight,
            self.preflight,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    def _token(self) -> str:
        return f"{_timestamp_token()}-{uuid.uuid4().hex}"

    def _write_partial_bytes(self, payload: bytes, suffix: str) -> tuple[Path, str]:
        """Write a fresh partial file while calculating its exact SHA-256."""
        partial = self.staging / f"{self._token()}{suffix}.partial"
        digest = hashlib.sha256()
        with partial.open("xb") as handle:
            for start in range(0, len(payload), 1024 * 1024):
                chunk = payload[start : start + 1024 * 1024]
                handle.write(chunk)
                digest.update(chunk)
            handle.flush()
            os.fsync(handle.fileno())
        return partial, digest.hexdigest()

    def _relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    def stage_bytes(
        self,
        payload: bytes,
        final_relative_path: str,
        *,
        kind: str,
        row_count: int | None = None,
        is_holdout: bool = False,
    ) -> StagedFile:
        """Stage exact bytes and return their immutable publication record."""
        staging_path, digest = self._write_partial_bytes(payload, ".bin")
        record = FileRecord(
            relative_path=final_relative_path,
            sha256=digest,
            byte_size=len(payload),
            row_count=row_count,
            kind=kind,
            is_holdout=is_holdout,
        )
        return StagedFile(
            staging_path=str(staging_path),
            final_relative_path=final_relative_path,
            record=record,
        )

    def stage_json(
        self,
        payload: dict[str, Any] | list[Any],
        final_relative_path: str,
        *,
        kind: str,
    ) -> StagedFile:
        rendered = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
        # Validation happens before atomic publication.  This also proves that
        # the exact bytes used for the SHA are parseable JSON.
        json.loads(rendered)
        return self.stage_bytes(rendered, final_relative_path, kind=kind)

    def stage_parquet(
        self,
        table: pa.Table,
        final_relative_path: str,
        *,
        is_holdout: bool,
    ) -> StagedFile:
        """Validate a Parquet payload before publishing it from staging.

        The serialized buffer keeps validation and SHA calculation independent
        of the final destination.  That is essential for the holdout tree's
        write-only contract.
        """
        sink = pa.BufferOutputStream()
        pq.write_table(
            table,
            sink,
            compression="zstd",
            use_dictionary=True,
            write_statistics=True,
        )
        payload = sink.getvalue().to_pybytes()
        metadata = pq.ParquetFile(pa.BufferReader(payload)).metadata
        if metadata is None or metadata.num_rows != table.num_rows:
            raise ValueError("staged Parquet metadata row-count mismatch")
        if len(metadata.schema.names) != len(table.schema.names):
            raise ValueError("staged Parquet schema-column mismatch")
        return self.stage_bytes(
            payload,
            final_relative_path,
            kind="parquet",
            row_count=table.num_rows,
            is_holdout=is_holdout,
        )

    def publish(self, staged: StagedFile) -> FileRecord:
        """Atomically publish a staged file without replacing any destination.

        Final names carry a UUID, and every destination is generated exactly
        once.  ``os.rename`` is therefore an atomic move rather than an
        overwrite operation.  The method intentionally performs no final-path
        probe for holdout files, because probing would violate write-only
        handling; collision-free names are the no-overwrite guard.
        """
        source = Path(staged.staging_path)
        destination = self.root / staged.final_relative_path
        destination.parent.mkdir(parents=True, exist_ok=True)
        os.rename(source, destination)
        return staged.record

    def publish_inflight(self, inflight_payload: dict[str, Any]) -> dict[str, Any]:
        """Finish a recorded publication transaction after a resume.

        A missing staging file is only interpreted as an already-completed
        atomic rename: the job never deletes staging files.  The destination is
        deliberately not inspected, which keeps holdout paths unread.
        """
        for staged_item in inflight_payload["staged_files"]:
            source = Path(staged_item["staging_path"])
            if source.exists():
                destination = self.root / staged_item["final_relative_path"]
                destination.parent.mkdir(parents=True, exist_ok=True)
                os.rename(source, destination)
        return inflight_payload

    def new_relative_path(
        self,
        directory: str,
        stem: str,
        suffix: str,
    ) -> str:
        return f"{directory}/{stem}-{self._token()}{suffix}"

    def publish_json_once(
        self,
        payload: dict[str, Any] | list[Any],
        directory: str,
        stem: str,
        *,
        kind: str,
    ) -> FileRecord:
        relative = self.new_relative_path(directory, stem, ".json")
        return self.publish(self.stage_json(payload, relative, kind=kind))

    def write_inflight(self, payload: dict[str, Any]) -> FileRecord:
        return self.publish_json_once(
            payload, "control/inflight", "inflight", kind="inflight"
        )

    def write_receipt(self, payload: dict[str, Any]) -> FileRecord:
        return self.publish_json_once(
            payload, "control/receipts", "receipt", kind="receipt"
        )

    def write_preflight(self, payload: dict[str, Any]) -> FileRecord:
        return self.publish_json_once(
            payload,
            "control/preflight",
            "preflight",
            kind="preflight",
        )

    def append_jsonl(self, relative_path: str, payload: dict[str, Any]) -> None:
        """Append a normal-control record; never rewrite previous events."""
        path = self.root / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(payload, sort_keys=True, ensure_ascii=False))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())

    def load_json_records(self, directory: Path) -> list[dict[str, Any]]:
        """Read non-holdout control records in deterministic order."""
        records: list[dict[str, Any]] = []
        for path in sorted(directory.glob("*.json")):
            with path.open(encoding="utf-8") as handle:
                records.append(json.load(handle))
        return records

    def read_file_bytes(self, relative_path: str) -> bytes:
        """Read a non-holdout control/input file only.

        This defensive guard makes accidental holdout reads a hard failure.
        """
        normalized = Path(relative_path)
        if normalized.parts and normalized.parts[0] == "holdout":
            raise PermissionError("holdout files are write-only for crypto-corpus-v1")
        return (self.root / normalized).read_bytes()
