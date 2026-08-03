"""Atomic artifact writing with per-file SHA-256 provenance."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from .config import CorpusConfig
from .state import FileRecord, StateStore

ArtifactScope = Literal["main", "holdout"]


class ExistingSnapshotError(RuntimeError):
    """A final snapshot already exists and must never be overwritten."""


class ArtifactSizeLimitExceeded(RuntimeError):
    """The signed artifact size ceiling has been reached."""


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n"
    ).encode("utf-8")


def _safe_relative_path(relative_path: str) -> Path:
    path = Path(relative_path)
    if path.is_absolute() or ".." in path.parts:
        raise ValueError("artifact file path must be snapshot-relative")
    return path


def tree_size_bytes(root: Path) -> int:
    """Return metadata-only size accounting without reading file contents."""
    if not root.exists():
        return 0
    return sum(path.stat().st_size for path in root.rglob("*") if path.is_file())


@dataclass(frozen=True)
class SnapshotPaths:
    main_partial: Path
    main_final: Path
    holdout_partial: Path
    holdout_final: Path
    main_state: Path
    holdout_state: Path


def build_snapshot_paths(config: CorpusConfig) -> SnapshotPaths:
    main_runs = config.artifact_root_path / "runs"
    holdout_runs = config.holdout_root_path / "runs"
    return SnapshotPaths(
        main_partial=main_runs / f"{config.run_id}.partial",
        main_final=main_runs / config.run_id,
        holdout_partial=holdout_runs / f"{config.run_id}.partial",
        holdout_final=holdout_runs / config.run_id,
        main_state=config.artifact_root_path / ".work" / f"{config.run_id}-main.sqlite",
        holdout_state=config.holdout_root_path
        / ".work"
        / f"{config.run_id}-holdout.sqlite",
    )


class ArtifactWriter:
    """Writes only into the signed artifact roots and promotes atomically."""

    def __init__(
        self,
        config: CorpusConfig,
        main_state: StateStore,
        holdout_state: StateStore,
    ) -> None:
        self.config = config
        self.paths = build_snapshot_paths(config)
        self.main_state = main_state
        self.holdout_state = holdout_state
        self.holdout_write_count = 0
        self.holdout_final_data_read_count = 0
        self._forbidden_text_values: tuple[bytes, ...] = ()

    def set_forbidden_text_values(self, values: tuple[str, ...]) -> None:
        """Prevent credential values from entering textual diagnostics/metadata.

        Parquet rows are intentionally not byte-scanned: a numerical account
        identifier could coincidentally equal an unrelated ticker or value.
        Source output is discarded before it reaches this writer, while every
        unstructured diagnostic is checked exactly.
        """
        self._forbidden_text_values = tuple(
            value.encode("utf-8") for value in values if value
        )

    def initialize(self) -> None:
        """Create/resume only the job's own .partial paths."""
        for final_path in (self.paths.main_final, self.paths.holdout_final):
            if final_path.exists():
                raise ExistingSnapshotError(
                    "a final snapshot already exists for this fixed run identifier"
                )
        self.paths.main_partial.mkdir(parents=True, exist_ok=True)
        # Do not create a holdout snapshot until the job actually writes an
        # OOS file.  That avoids treating an empty custody directory as data.

    def root_for(self, scope: ArtifactScope) -> Path:
        if scope == "main":
            return self.paths.main_partial
        self.paths.holdout_partial.mkdir(parents=True, exist_ok=True)
        return self.paths.holdout_partial

    def state_for(self, scope: ArtifactScope) -> StateStore:
        return self.main_state if scope == "main" else self.holdout_state

    def _write_bytes_atomic(self, target: Path, data: bytes) -> tuple[str, int]:
        if target.suffix != ".parquet" and any(
            value in data for value in self._forbidden_text_values
        ):
            raise RuntimeError(
                "credential value was blocked from textual artifact output"
            )
        if target.exists():
            raise ExistingSnapshotError(
                f"refusing to overwrite existing snapshot file: {target.name}"
            )
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.partial")
        digest = _sha256(data)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        try:
            with os.fdopen(descriptor, "wb", closefd=True) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(stream.fileno())
            if temporary.stat().st_size != len(data):
                raise RuntimeError("atomic artifact write length verification failed")
            # The digest was calculated over the exact serialized bytes before
            # write; the completed .partial's verified length is then promoted
            # atomically without exposing a partial final file.
            os.replace(temporary, target)
        except Exception:
            # Do not remove a failed .partial automatically.  Its name makes
            # the interrupted state visible for operator inspection and avoids
            # destructive cleanup behavior.
            raise
        return digest, len(data)

    def write_bytes(
        self, scope: ArtifactScope, relative_path: str, data: bytes
    ) -> FileRecord:
        safe_path = _safe_relative_path(relative_path)
        root = self.root_for(scope)
        digest, byte_size = self._write_bytes_atomic(root / safe_path, data)
        record = FileRecord(str(safe_path), scope, digest, byte_size)
        self.state_for(scope).register_file(
            record.relative_path, record.scope, record.sha256, record.byte_size
        )
        if scope == "holdout":
            self.holdout_write_count += 1
        self.enforce_size_limit()
        return record

    def write_json(
        self, scope: ArtifactScope, relative_path: str, value: object
    ) -> FileRecord:
        return self.write_bytes(scope, relative_path, _canonical_json_bytes(value))

    def write_mutable_json(
        self, scope: ArtifactScope, relative_path: str, value: object
    ) -> FileRecord:
        """Atomically update a checkpoint inside a resumable ``.partial`` run.

        Final snapshots are immutable; this method is intentionally limited to
        the pre-promotion path and is never used for a completed artifact.
        """
        safe_path = _safe_relative_path(relative_path)
        root = self.root_for(scope)
        target = root / safe_path
        data = _canonical_json_bytes(value)
        temporary = target.with_name(f"{target.name}.{uuid.uuid4().hex}.partial")
        target.parent.mkdir(parents=True, exist_ok=True)
        descriptor = os.open(
            temporary,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL,
            0o600,
        )
        with os.fdopen(descriptor, "wb", closefd=True) as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        if temporary.stat().st_size != len(data):
            raise RuntimeError("checkpoint write length verification failed")
        os.replace(temporary, target)
        record = FileRecord(str(safe_path), scope, _sha256(data), len(data))
        self.state_for(scope).upsert_file(
            record.relative_path, record.scope, record.sha256, record.byte_size
        )
        return record

    def write_parquet(
        self,
        scope: ArtifactScope,
        relative_path: str,
        rows: list[dict[str, object]],
        schema: Any,
    ) -> FileRecord:
        """Serialize one bounded partition in memory, then atomically persist it."""
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ModuleNotFoundError as exc:  # pragma: no cover - preflight covers it
            raise RuntimeError("pyarrow parquet runtime is unavailable") from exc

        sink = pa.BufferOutputStream()
        table = pa.Table.from_pylist(rows, schema=schema)
        pq.write_table(table, sink, compression="zstd", version="2.6")
        return self.write_bytes(scope, relative_path, sink.getvalue().to_pybytes())

    def enforce_size_limit(self) -> None:
        current = tree_size_bytes(self.paths.main_partial) + tree_size_bytes(
            self.paths.holdout_partial
        )
        state_files = (
            self.paths.main_state,
            self.paths.holdout_state,
            self.paths.main_state.with_name(self.paths.main_state.name + "-wal"),
            self.paths.holdout_state.with_name(self.paths.holdout_state.name + "-wal"),
        )
        current += sum(path.stat().st_size for path in state_files if path.exists())
        limit = self.config.max_artifact_gib * 1024**3
        if current > limit:
            raise ArtifactSizeLimitExceeded(
                "MAX_ARTIFACT_GIB reached; no further source requests are permitted"
            )

    def checksum_manifest(self, scope: ArtifactScope) -> FileRecord:
        records = self.state_for(scope).files()
        lines = [
            f"{record.sha256}  {record.relative_path}\n"
            for record in records
            if record.relative_path not in {"checksums.sha256", "manifest.json"}
        ]
        return self.write_bytes(
            scope, "checksums.sha256", "".join(lines).encode("utf-8")
        )

    def promote(self, scope: ArtifactScope) -> Path | None:
        partial = (
            self.paths.main_partial if scope == "main" else self.paths.holdout_partial
        )
        final = self.paths.main_final if scope == "main" else self.paths.holdout_final
        if not partial.exists():
            return None
        if final.exists():
            raise ExistingSnapshotError(
                "refusing to overwrite final snapshot during promotion"
            )
        final.parent.mkdir(parents=True, exist_ok=True)
        os.replace(partial, final)
        return final

    def final_snapshot_size_bytes(self) -> int:
        return tree_size_bytes(self.paths.main_final) + tree_size_bytes(
            self.paths.holdout_final
        )
