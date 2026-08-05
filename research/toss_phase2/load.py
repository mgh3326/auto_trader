"""Fail-closed, resumable loader for the Toss combined-KRX/NXT staging corpus.

This is intentionally a manual CLI, never a scheduled job.  It snapshots only
old-enough, immutable Parquet pages and writes only
``research.kr_candles_1m_toss``.  The source staging directory is read-only;
all loader state lives in a separate operator-selected directory.
"""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import json
import math
import os
import sqlite3
import time
from collections.abc import Iterator, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import asyncpg
import pyarrow as pa
import pyarrow.parquet as pq
from dotenv import dotenv_values

TARGET_TABLE = "research.kr_candles_1m_toss"
EXPECTED_DB_ROLE = "auto_trader_kr_backfill"
SOURCE = "TOSS"
VALUE_SEMANTICS = "CLOSE_X_VOLUME_SYNTHETIC"
STAGING_CONTRACT = "STAGING_ONLY_NOT_BACKTEST_INPUT"
VALID_SESSION_SEGMENTS = frozenset({"NXT_PRE", "KRX_REGULAR", "NXT_POST"})

# This order is deliberately shared by Parquet reads, COPY records, the temp
# table, and the target INSERT.  It is the entire physical target schema: no
# public relation or KRX-only research table appears in this loader.
REQUIRED_COLUMNS = (
    "time_utc",
    "session_date_kst",
    "symbol",
    "session_segment",
    "source",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "value",
    "value_semantics",
    "is_padding",
    "pre_nxt",
    "retrieved_at",
    "batch_id",
)
NULLABLE_COLUMNS = frozenset({"pre_nxt"})
DEFAULT_MIN_FRAGMENT_AGE_SECONDS = 120
DEFAULT_COMMIT_ROWS = 20_000
PREFLIGHT_FRAGMENT_CHUNK = 500
STATE_VERSION = 1
KST = timezone(timedelta(hours=9))
# Staging values are emitted as IEEE-754 doubles.  This admits only the
# round-off expected from serialising ``close * volume``, not a materially
# different synthetic value.
VALUE_RELATIVE_TOLERANCE = 1e-12
VALUE_ABSOLUTE_TOLERANCE = 1e-6


class LoadStopped(RuntimeError):
    """A fail-closed stop; callers must not continue around it."""


class StagingValidationError(LoadStopped):
    """A staged fragment violates the frozen loader contract."""


class DatabaseLoadError(LoadStopped):
    """The database did not provide the expected all-or-nothing result."""


@dataclass(frozen=True)
class FrozenFragment:
    """A stable, completed Parquet file selected into one immutable snapshot."""

    relative_path: str
    symbol: str
    size_bytes: int
    mtime_ns: int
    row_count: int


@dataclass(frozen=True)
class MergeResult:
    source_rows: int
    preexisting_rows: int
    inserted_rows: int


@dataclass(frozen=True)
class VerificationResult:
    source_rows: int
    db_rows_verified: int
    target_rows: int
    batch_rows: int
    duplicate_rows: int


def _utc_now() -> str:
    return datetime.now(UTC).isoformat()


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, sort_keys=True)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(temporary, path)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise LoadStopped(f"invalid_state_file:{path.name}") from exc
    if not isinstance(payload, dict):
        raise LoadStopped(f"invalid_state_object:{path.name}")
    return payload


def _snapshot_digest(snapshot: dict[str, Any]) -> str:
    stable = {
        "batch_id": snapshot["batch_id"],
        "fragments": snapshot["fragments"],
        "source_rows": snapshot["source_rows"],
        "staging_dir": snapshot["staging_dir"],
    }
    encoded = json.dumps(stable, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _metadata_text(metadata: dict[bytes, bytes], key: str, path: Path) -> str:
    raw = metadata.get(key.encode())
    if raw is None:
        raise StagingValidationError(f"missing_parquet_metadata:{key}:{path.name}")
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise StagingValidationError(
            f"invalid_parquet_metadata:{key}:{path.name}"
        ) from exc


def _fragment_symbol(relative_path: Path) -> str:
    if len(relative_path.parts) < 3 or relative_path.parts[0] != "data":
        raise StagingValidationError(f"unexpected_fragment_path:{relative_path}")
    partition = relative_path.parts[-2]
    if not partition.startswith("symbol=") or not partition.removeprefix("symbol="):
        raise StagingValidationError(f"unexpected_fragment_partition:{relative_path}")
    return partition.removeprefix("symbol=")


def _read_fragment_header(
    path: Path,
    *,
    expected_batch_id: str,
    expected_symbol: str,
) -> int:
    parquet = pq.ParquetFile(path)
    try:
        metadata = parquet.metadata.metadata or {}
        if _metadata_text(metadata, "artifact_state", path) != STAGING_CONTRACT:
            raise StagingValidationError(f"unexpected_artifact_state:{path.name}")
        if _metadata_text(metadata, "source", path) != SOURCE:
            raise StagingValidationError(f"unexpected_fragment_source:{path.name}")
        if _metadata_text(metadata, "value_semantics", path) != VALUE_SEMANTICS:
            raise StagingValidationError(f"unexpected_value_semantics:{path.name}")
        if _metadata_text(metadata, "batch_id", path) != expected_batch_id:
            raise StagingValidationError(f"batch_id_mismatch:{path.name}")
        if _metadata_text(metadata, "symbol", path) != expected_symbol:
            raise StagingValidationError(f"symbol_metadata_mismatch:{path.name}")
        missing = set(REQUIRED_COLUMNS) - set(parquet.schema_arrow.names)
        if missing:
            raise StagingValidationError(
                "missing_parquet_columns:" + ",".join(sorted(missing)) + f":{path.name}"
            )
        row_count = parquet.metadata.num_rows
        if row_count <= 0:
            raise StagingValidationError(f"empty_completed_fragment:{path.name}")
        return row_count
    finally:
        parquet.close()


def _manifest(staging_dir: Path) -> dict[str, Any]:
    manifest_path = staging_dir / "manifest.json"
    payload = _read_json(manifest_path)
    if payload.get("artifact_state") != STAGING_CONTRACT:
        raise StagingValidationError("unexpected_staging_manifest_contract")
    batch_id = payload.get("batch_id")
    if not isinstance(batch_id, str) or not batch_id:
        raise StagingValidationError("missing_staging_batch_id")
    return payload


def _assert_state_dir_is_separate(staging_dir: Path, state_dir: Path) -> None:
    staging = staging_dir.resolve()
    state = state_dir.resolve()
    if state == Path("/") or state == staging or staging in state.parents:
        raise LoadStopped("state_dir_must_be_outside_staging_dir")


def _snapshot_path(state_dir: Path) -> Path:
    return state_dir / "snapshot.json"


def _load_snapshot(state_dir: Path) -> dict[str, Any] | None:
    path = _snapshot_path(state_dir)
    if not path.exists():
        return None
    snapshot = _read_json(path)
    if snapshot.get("version") != STATE_VERSION:
        raise LoadStopped("unsupported_snapshot_version")
    if not isinstance(snapshot.get("fragments"), list):
        raise LoadStopped("invalid_snapshot_fragments")
    if snapshot.get("digest") != _snapshot_digest(snapshot):
        raise LoadStopped("snapshot_digest_mismatch")
    return snapshot


def freeze_completed_fragments(
    *,
    staging_dir: Path,
    state_dir: Path,
    min_fragment_age_seconds: int,
    now_ns: int | None = None,
) -> dict[str, Any]:
    """Freeze only final, old-enough Parquet pages without changing staging."""

    _assert_state_dir_is_separate(staging_dir, state_dir)
    existing = _load_snapshot(state_dir)
    if existing is not None:
        if Path(str(existing["staging_dir"])).resolve() != staging_dir.resolve():
            raise LoadStopped("snapshot_staging_dir_mismatch")
        return existing

    if min_fragment_age_seconds <= 0:
        raise LoadStopped("min_fragment_age_seconds_must_be_positive")
    manifest = _manifest(staging_dir)
    batch_id = str(manifest["batch_id"])
    cutoff_ns = (now_ns if now_ns is not None else time.time_ns()) - (
        min_fragment_age_seconds * 1_000_000_000
    )
    data_dir = staging_dir / "data"
    if not data_dir.is_dir():
        raise StagingValidationError("staging_data_directory_missing")

    fragments: list[FrozenFragment] = []
    skipped_too_recent = 0
    for path in sorted(data_dir.rglob("*.parquet")):
        stat_before = path.stat()
        if stat_before.st_mtime_ns > cutoff_ns:
            skipped_too_recent += 1
            continue
        relative_path = path.relative_to(staging_dir)
        symbol = _fragment_symbol(relative_path)
        row_count = _read_fragment_header(
            path,
            expected_batch_id=batch_id,
            expected_symbol=symbol,
        )
        stat_after = path.stat()
        if (
            stat_before.st_size != stat_after.st_size
            or stat_before.st_mtime_ns != stat_after.st_mtime_ns
        ):
            raise StagingValidationError(
                f"fragment_changed_during_snapshot:{path.name}"
            )
        fragments.append(
            FrozenFragment(
                relative_path=str(relative_path),
                symbol=symbol,
                size_bytes=stat_before.st_size,
                mtime_ns=stat_before.st_mtime_ns,
                row_count=row_count,
            )
        )

    if not fragments:
        raise StagingValidationError("no_completed_fragments_available")
    partial_files = sum(1 for _ in data_dir.rglob("*.partial"))
    snapshot: dict[str, Any] = {
        "version": STATE_VERSION,
        "created_at_utc": _utc_now(),
        "staging_dir": str(staging_dir.resolve()),
        "batch_id": batch_id,
        "min_fragment_age_seconds": min_fragment_age_seconds,
        "completed_fragments_only": True,
        "partial_files_excluded": partial_files,
        "too_recent_files_excluded": skipped_too_recent,
        "fragments": [asdict(fragment) for fragment in fragments],
        "source_rows": sum(fragment.row_count for fragment in fragments),
    }
    snapshot["digest"] = _snapshot_digest(snapshot)
    _atomic_write_json(_snapshot_path(state_dir), snapshot)
    return snapshot


def _frozen_fragments(snapshot: dict[str, Any]) -> list[FrozenFragment]:
    try:
        fragments = [FrozenFragment(**fragment) for fragment in snapshot["fragments"]]
    except (TypeError, KeyError) as exc:
        raise LoadStopped("invalid_snapshot_fragment_shape") from exc
    if sum(fragment.row_count for fragment in fragments) != snapshot["source_rows"]:
        raise LoadStopped("snapshot_row_count_mismatch")
    return fragments


def _assert_fragment_unchanged(
    staging_dir: Path,
    fragment: FrozenFragment,
) -> Path:
    path = staging_dir / fragment.relative_path
    try:
        stat = path.stat()
    except OSError as exc:
        raise StagingValidationError(
            f"frozen_fragment_missing:{fragment.relative_path}"
        ) from exc
    if stat.st_size != fragment.size_bytes or stat.st_mtime_ns != fragment.mtime_ns:
        raise StagingValidationError(
            f"frozen_fragment_changed:{fragment.relative_path}"
        )
    return path


def _column(batch: pa.RecordBatch, name: str) -> pa.Array:
    index = batch.schema.get_field_index(name)
    if index < 0:
        raise StagingValidationError(f"missing_batch_column:{name}")
    return batch.column(index)


def _validate_batch(
    batch: pa.RecordBatch,
    *,
    symbol: str,
    batch_id: str,
    path: Path,
) -> None:
    for name in REQUIRED_COLUMNS:
        if name not in NULLABLE_COLUMNS and _column(batch, name).null_count:
            raise StagingValidationError(f"null_required_value:{name}:{path.name}")

    segments = set(_column(batch, "session_segment").to_pylist())
    invalid_segments = segments - VALID_SESSION_SEGMENTS
    if invalid_segments:
        raise StagingValidationError(
            "session_segment_unclassifiable:"
            + ",".join(sorted(str(value) for value in invalid_segments))
            + f":{path.name}"
        )
    if any(value != symbol for value in _column(batch, "symbol").to_pylist()):
        raise StagingValidationError(f"fragment_symbol_row_mismatch:{path.name}")
    if any(value != SOURCE for value in _column(batch, "source").to_pylist()):
        raise StagingValidationError(f"unexpected_row_source:{path.name}")
    if any(
        value != VALUE_SEMANTICS
        for value in _column(batch, "value_semantics").to_pylist()
    ):
        raise StagingValidationError(f"unexpected_row_value_semantics:{path.name}")
    if any(value != batch_id for value in _column(batch, "batch_id").to_pylist()):
        raise StagingValidationError(f"fragment_batch_id_row_mismatch:{path.name}")

    times = _column(batch, "time_utc").to_pylist()
    dates = _column(batch, "session_date_kst").to_pylist()
    for timestamp, session_date in zip(times, dates, strict=True):
        if timestamp.tzinfo is None or timestamp.astimezone(KST).date() != session_date:
            raise StagingValidationError(f"session_date_timestamp_mismatch:{path.name}")
        if timestamp.second or timestamp.microsecond:
            raise StagingValidationError(f"non_minute_timestamp:{path.name}")

    volumes = _column(batch, "volume").to_pylist()
    paddings = _column(batch, "is_padding").to_pylist()
    if any(
        bool(padding) is not (float(volume) == 0.0)
        for volume, padding in zip(volumes, paddings, strict=True)
    ):
        raise StagingValidationError(f"invalid_padding_semantics:{path.name}")

    opens = _column(batch, "open").to_pylist()
    highs = _column(batch, "high").to_pylist()
    lows = _column(batch, "low").to_pylist()
    closes = _column(batch, "close").to_pylist()
    values = _column(batch, "value").to_pylist()
    for open_, high, low, close, volume, value in zip(
        opens,
        highs,
        lows,
        closes,
        volumes,
        values,
        strict=True,
    ):
        try:
            open_float = float(open_)
            high_float = float(high)
            low_float = float(low)
            close_float = float(close)
            volume_float = float(volume)
            value_float = float(value)
        except (TypeError, ValueError) as exc:
            raise StagingValidationError(
                f"non_numeric_candle_value:{path.name}"
            ) from exc
        numeric_values = (
            open_float,
            high_float,
            low_float,
            close_float,
            volume_float,
            value_float,
        )
        if not all(math.isfinite(number) for number in numeric_values):
            raise StagingValidationError(f"non_finite_candle_value:{path.name}")
        if volume_float < 0:
            raise StagingValidationError(f"negative_volume:{path.name}")
        if min(open_float, high_float, low_float, close_float) < 0:
            raise StagingValidationError(f"negative_ohlc_value:{path.name}")
        if high_float < max(open_float, close_float) or low_float > min(
            open_float, close_float
        ):
            raise StagingValidationError(f"incoherent_ohlc_values:{path.name}")
        expected_value = close_float * volume_float
        if not math.isclose(
            value_float,
            expected_value,
            rel_tol=VALUE_RELATIVE_TOLERANCE,
            abs_tol=VALUE_ABSOLUTE_TOLERANCE,
        ):
            raise StagingValidationError(f"synthetic_value_mismatch:{path.name}")


def iter_validated_batches(
    *,
    staging_dir: Path,
    fragment: FrozenFragment,
    batch_id: str,
) -> Iterator[pa.RecordBatch]:
    """Read one frozen page and validate every row before exposing it."""

    path = _assert_fragment_unchanged(staging_dir, fragment)
    _read_fragment_header(
        path,
        expected_batch_id=batch_id,
        expected_symbol=fragment.symbol,
    )
    parquet = pq.ParquetFile(path)
    rows_read = 0
    try:
        for batch in parquet.iter_batches(
            batch_size=8192,
            columns=list(REQUIRED_COLUMNS),
        ):
            _validate_batch(
                batch,
                symbol=fragment.symbol,
                batch_id=batch_id,
                path=path,
            )
            rows_read += batch.num_rows
            yield batch
    finally:
        parquet.close()
    if rows_read != fragment.row_count:
        raise StagingValidationError(f"fragment_row_count_changed:{path.name}")


def _key_pairs(batch: pa.RecordBatch) -> list[tuple[int, str]]:
    timestamps = _column(batch, "time_utc").cast(pa.int64()).to_pylist()
    symbols = _column(batch, "symbol").to_pylist()
    return list(zip(timestamps, symbols, strict=True))


def _records(batch: pa.RecordBatch) -> list[tuple[Any, ...]]:
    values = [_column(batch, column).to_pylist() for column in REQUIRED_COLUMNS]
    return list(zip(*values, strict=True))


class SourceKeyIndex:
    """Durable local uniqueness proof for the frozen source rows."""

    def __init__(self, *, state_dir: Path, snapshot_digest: str) -> None:
        self._path = state_dir / "source_keys.sqlite3"
        self._connection = sqlite3.connect(self._path)
        self._connection.execute("PRAGMA journal_mode=DELETE")
        self._connection.execute("PRAGMA synchronous=FULL")
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS load_metadata "
            "(key TEXT PRIMARY KEY, value TEXT NOT NULL)"
        )
        self._connection.execute(
            "CREATE TABLE IF NOT EXISTS source_keys "
            "(time_utc_us INTEGER NOT NULL, symbol TEXT NOT NULL, "
            "PRIMARY KEY (time_utc_us, symbol)) WITHOUT ROWID"
        )
        existing = self._metadata("snapshot_digest")
        if existing is None:
            self._set_metadata("snapshot_digest", snapshot_digest)
            self._set_metadata("preflight_next_fragment", "0")
            self._set_metadata("preflight_complete", "0")
            self._connection.commit()
        elif existing != snapshot_digest:
            raise LoadStopped("source_key_index_snapshot_mismatch")

    def close(self) -> None:
        self._connection.close()

    def _metadata(self, key: str) -> str | None:
        row = self._connection.execute(
            "SELECT value FROM load_metadata WHERE key = ?", (key,)
        ).fetchone()
        return None if row is None else str(row[0])

    def _set_metadata(self, key: str, value: str) -> None:
        self._connection.execute(
            "INSERT INTO load_metadata (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )

    @property
    def next_fragment(self) -> int:
        value = self._metadata("preflight_next_fragment")
        if value is None or not value.isdigit():
            raise LoadStopped("invalid_preflight_checkpoint")
        return int(value)

    @property
    def complete(self) -> bool:
        return self._metadata("preflight_complete") == "1"

    def begin(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")

    def add(self, pairs: Sequence[tuple[int, str]]) -> None:
        try:
            self._connection.executemany(
                "INSERT INTO source_keys (time_utc_us, symbol) VALUES (?, ?)",
                pairs,
            )
        except sqlite3.IntegrityError as exc:
            raise StagingValidationError("duplicate_staging_key") from exc

    def commit_through(self, next_fragment: int) -> None:
        self._set_metadata("preflight_next_fragment", str(next_fragment))
        self._connection.commit()

    def mark_complete(self) -> None:
        self._connection.execute("BEGIN IMMEDIATE")
        self._set_metadata("preflight_complete", "1")
        self._connection.commit()

    def rollback(self) -> None:
        self._connection.rollback()

    def key_count(self) -> int:
        return int(
            self._connection.execute("SELECT count(*) FROM source_keys").fetchone()[0]
        )


def preflight_source(
    *,
    staging_dir: Path,
    state_dir: Path,
    snapshot: dict[str, Any],
) -> None:
    """Prove every frozen row is valid and source-key-unique before DB writes."""

    fragments = _frozen_fragments(snapshot)
    index = SourceKeyIndex(state_dir=state_dir, snapshot_digest=str(snapshot["digest"]))
    try:
        if index.complete:
            if index.key_count() != snapshot["source_rows"]:
                raise LoadStopped("completed_source_key_index_count_mismatch")
            return
        next_fragment = index.next_fragment
        if next_fragment > len(fragments):
            raise LoadStopped("preflight_checkpoint_out_of_range")
        for start in range(next_fragment, len(fragments), PREFLIGHT_FRAGMENT_CHUNK):
            stop = min(start + PREFLIGHT_FRAGMENT_CHUNK, len(fragments))
            index.begin()
            try:
                for fragment in fragments[start:stop]:
                    for batch in iter_validated_batches(
                        staging_dir=staging_dir,
                        fragment=fragment,
                        batch_id=str(snapshot["batch_id"]),
                    ):
                        index.add(_key_pairs(batch))
                index.commit_through(stop)
            except Exception:
                index.rollback()
                raise
        if index.key_count() != snapshot["source_rows"]:
            raise StagingValidationError("staging_key_count_mismatch")
        index.mark_complete()
    finally:
        index.close()


class StateDirectoryLock:
    """Refuse two local loaders from sharing one checkpoint state directory."""

    def __init__(self, state_dir: Path) -> None:
        self._path = state_dir / ".loader.lock"
        self._handle: Any | None = None

    def __enter__(self) -> StateDirectoryLock:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._handle = self._path.open("a", encoding="utf-8")
        try:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self._handle.close()
            raise LoadStopped("loader_state_directory_locked") from exc
        return self

    def __exit__(self, *_: object) -> None:
        if self._handle is not None:
            fcntl.flock(self._handle.fileno(), fcntl.LOCK_UN)
            self._handle.close()


def _load_checkpoint_path(state_dir: Path) -> Path:
    return state_dir / "load_checkpoint.json"


def _load_checkpoint(state_dir: Path, snapshot_digest: str) -> dict[str, Any]:
    path = _load_checkpoint_path(state_dir)
    if not path.exists():
        checkpoint = {
            "version": STATE_VERSION,
            "snapshot_digest": snapshot_digest,
            "next_fragment_index": 0,
            "checkpointed_source_rows": 0,
            "db_batches": 0,
            "initial_target_rows": None,
            "load_complete": False,
        }
        _atomic_write_json(path, checkpoint)
        return checkpoint
    checkpoint = _read_json(path)
    if checkpoint.get("version") != STATE_VERSION:
        raise LoadStopped("unsupported_load_checkpoint_version")
    if checkpoint.get("snapshot_digest") != snapshot_digest:
        raise LoadStopped("load_checkpoint_snapshot_mismatch")
    return checkpoint


def _save_load_checkpoint(state_dir: Path, checkpoint: dict[str, Any]) -> None:
    _atomic_write_json(_load_checkpoint_path(state_dir), checkpoint)


def _database_url(env_file: Path) -> str:
    if "prod" in env_file.name.casefold() or not env_file.is_file():
        raise LoadStopped("dedicated_non_production_named_db_env_file_required")
    configured = dotenv_values(env_file).get("DATABASE_URL")
    if not configured:
        raise LoadStopped("DATABASE_URL_missing_from_dedicated_db_env_file")
    url = str(configured).replace("postgresql+asyncpg://", "postgresql://", 1)
    parsed = urlsplit(url)
    if parsed.scheme not in {"postgresql", "postgres"} or not parsed.hostname:
        raise LoadStopped("invalid_dedicated_database_url")
    if parsed.username != EXPECTED_DB_ROLE:
        raise LoadStopped("unexpected_database_role")
    return url


TEMP_TABLE = "toss_phase2_load_stage"
TEMP_TABLE_SQL = f"""
CREATE TEMP TABLE IF NOT EXISTS {TEMP_TABLE} (
    time_utc TIMESTAMPTZ NOT NULL,
    session_date_kst DATE NOT NULL,
    symbol TEXT NOT NULL,
    session_segment TEXT NOT NULL,
    source TEXT NOT NULL,
    open DOUBLE PRECISION NOT NULL,
    high DOUBLE PRECISION NOT NULL,
    low DOUBLE PRECISION NOT NULL,
    close DOUBLE PRECISION NOT NULL,
    volume DOUBLE PRECISION NOT NULL,
    value DOUBLE PRECISION NOT NULL,
    value_semantics TEXT NOT NULL,
    is_padding BOOLEAN NOT NULL,
    pre_nxt BOOLEAN,
    retrieved_at TIMESTAMPTZ NOT NULL,
    batch_id TEXT NOT NULL
) ON COMMIT DELETE ROWS
"""
INSERT_SQL = f"""
INSERT INTO {TARGET_TABLE} ({", ".join(REQUIRED_COLUMNS)})
SELECT
    time_utc,
    session_date_kst,
    symbol,
    session_segment,
    source,
    open::numeric,
    high::numeric,
    low::numeric,
    close::numeric,
    volume::numeric,
    value::numeric,
    value_semantics,
    is_padding,
    pre_nxt,
    retrieved_at,
    batch_id
FROM {TEMP_TABLE}
ON CONFLICT (time_utc, symbol) DO NOTHING
"""
CONFLICT_COUNT_SQL = f"""
SELECT count(*)
FROM {TEMP_TABLE} AS incoming
JOIN {TARGET_TABLE} AS stored USING (time_utc, symbol)
WHERE stored.session_date_kst IS DISTINCT FROM incoming.session_date_kst
   OR stored.session_segment IS DISTINCT FROM incoming.session_segment
   OR stored.source IS DISTINCT FROM incoming.source
   OR stored.open IS DISTINCT FROM incoming.open::numeric
   OR stored.high IS DISTINCT FROM incoming.high::numeric
   OR stored.low IS DISTINCT FROM incoming.low::numeric
   OR stored.close IS DISTINCT FROM incoming.close::numeric
   OR stored.volume IS DISTINCT FROM incoming.volume::numeric
   OR stored.value IS DISTINCT FROM incoming.value::numeric
   OR stored.value_semantics IS DISTINCT FROM incoming.value_semantics
   OR stored.is_padding IS DISTINCT FROM incoming.is_padding
   OR stored.pre_nxt IS DISTINCT FROM incoming.pre_nxt
"""
STAGED_COUNT_SQL = f"SELECT count(*) FROM {TEMP_TABLE}"
STAGED_DUPLICATE_COUNT_SQL = f"""
SELECT count(*) - count(DISTINCT (time_utc, symbol)) FROM {TEMP_TABLE}
"""
PRESENT_COUNT_SQL = f"""
SELECT count(*) FROM {TEMP_TABLE} AS incoming
JOIN {TARGET_TABLE} AS stored USING (time_utc, symbol)
"""
EXISTING_COUNT_SQL = PRESENT_COUNT_SQL


async def _assert_database_ready(
    conn: asyncpg.Connection,
    *,
    expected_source_rows: int,
    checkpoint: dict[str, Any],
) -> None:
    current_role = await conn.fetchval("SELECT current_user")
    if current_role != EXPECTED_DB_ROLE:
        raise DatabaseLoadError("database_role_changed_after_connect")
    relation = await conn.fetchval("SELECT to_regclass($1)", TARGET_TABLE)
    if relation is None:
        raise DatabaseLoadError("target_table_missing")
    can_insert = await conn.fetchval(
        "SELECT has_table_privilege(current_user, $1, 'INSERT')", TARGET_TABLE
    )
    if not can_insert:
        raise DatabaseLoadError("target_insert_privilege_missing")
    target_rows = int(await conn.fetchval(f"SELECT count(*) FROM {TARGET_TABLE}"))
    initial_rows = checkpoint.get("initial_target_rows")
    if initial_rows is None:
        if target_rows != 0:
            raise DatabaseLoadError("nonempty_target_at_initial_load")
        checkpoint["initial_target_rows"] = target_rows
    elif initial_rows != 0:
        raise DatabaseLoadError("nonempty_target_at_initial_load")
    if target_rows > expected_source_rows:
        raise DatabaseLoadError("target_row_count_exceeds_frozen_source")


async def _acquire_database_lock(conn: asyncpg.Connection) -> None:
    acquired = await conn.fetchval(
        "SELECT pg_try_advisory_lock(hashtext($1))", "toss_phase2_staging_loader_v1"
    )
    if not acquired:
        raise DatabaseLoadError("another_toss_loader_holds_database_lock")


async def _connect_database(database_url: str) -> asyncpg.Connection:
    try:
        return await asyncpg.connect(database_url)
    except asyncpg.PostgresError as exc:
        raise DatabaseLoadError(f"database_error:{type(exc).__name__}") from exc


async def _merge_records(
    conn: asyncpg.Connection,
    records: Sequence[tuple[Any, ...]],
) -> MergeResult:
    if not records:
        return MergeResult(source_rows=0, preexisting_rows=0, inserted_rows=0)
    async with conn.transaction():
        await conn.copy_records_to_table(
            TEMP_TABLE,
            records=records,
            columns=list(REQUIRED_COLUMNS),
        )
        staged_rows = int(await conn.fetchval(STAGED_COUNT_SQL))
        if staged_rows != len(records):
            raise DatabaseLoadError("temporary_stage_row_count_mismatch")
        staged_duplicates = int(await conn.fetchval(STAGED_DUPLICATE_COUNT_SQL))
        if staged_duplicates:
            raise DatabaseLoadError("duplicate_key_reached_database_stage")
        conflicts = int(await conn.fetchval(CONFLICT_COUNT_SQL))
        if conflicts:
            raise DatabaseLoadError("existing_target_value_conflict")
        preexisting = int(await conn.fetchval(EXISTING_COUNT_SQL))
        await conn.execute(INSERT_SQL)
        present = int(await conn.fetchval(PRESENT_COUNT_SQL))
        if present != len(records):
            raise DatabaseLoadError("target_coverage_mismatch_after_merge")
    return MergeResult(
        source_rows=len(records),
        preexisting_rows=preexisting,
        inserted_rows=len(records) - preexisting,
    )


def _record_fragments(
    *,
    staging_dir: Path,
    fragments: Sequence[FrozenFragment],
    batch_id: str,
) -> Iterator[tuple[int, list[tuple[Any, ...]]]]:
    """Yield full-file buffers so a checkpoint never splits a source page."""

    for index, fragment in enumerate(fragments):
        records: list[tuple[Any, ...]] = []
        for batch in iter_validated_batches(
            staging_dir=staging_dir,
            fragment=fragment,
            batch_id=batch_id,
        ):
            records.extend(_records(batch))
        if len(records) != fragment.row_count:
            raise StagingValidationError(
                f"record_materialization_count_mismatch:{fragment.relative_path}"
            )
        yield index, records


async def load_snapshot(
    *,
    staging_dir: Path,
    state_dir: Path,
    snapshot: dict[str, Any],
    database_url: str,
    commit_rows: int,
) -> dict[str, Any]:
    """Load the frozen snapshot with DB transactions before checkpoint writes."""

    if commit_rows <= 0:
        raise LoadStopped("commit_rows_must_be_positive")
    fragments = _frozen_fragments(snapshot)
    checkpoint = _load_checkpoint(state_dir, str(snapshot["digest"]))
    start_index = checkpoint.get("next_fragment_index")
    if not isinstance(start_index, int) or not 0 <= start_index <= len(fragments):
        raise LoadStopped("load_checkpoint_index_out_of_range")
    if checkpoint.get("load_complete") and start_index != len(fragments):
        raise LoadStopped("inconsistent_completed_load_checkpoint")

    conn = await _connect_database(database_url)
    try:
        await _acquire_database_lock(conn)
        await _assert_database_ready(
            conn,
            expected_source_rows=int(snapshot["source_rows"]),
            checkpoint=checkpoint,
        )
        _save_load_checkpoint(state_dir, checkpoint)
        await conn.execute(TEMP_TABLE_SQL)

        pending: list[tuple[Any, ...]] = []
        pending_through = start_index
        totals = MergeResult(source_rows=0, preexisting_rows=0, inserted_rows=0)
        for index, records in _record_fragments(
            staging_dir=staging_dir,
            fragments=fragments[start_index:],
            batch_id=str(snapshot["batch_id"]),
        ):
            actual_index = start_index + index
            pending.extend(records)
            pending_through = actual_index + 1
            if len(pending) < commit_rows:
                continue
            merged = await _merge_records(conn, pending)
            totals = MergeResult(
                source_rows=totals.source_rows + merged.source_rows,
                preexisting_rows=totals.preexisting_rows + merged.preexisting_rows,
                inserted_rows=totals.inserted_rows + merged.inserted_rows,
            )
            checkpoint.update(
                {
                    "next_fragment_index": pending_through,
                    "checkpointed_source_rows": int(
                        checkpoint["checkpointed_source_rows"]
                    )
                    + merged.source_rows,
                    "db_batches": int(checkpoint["db_batches"]) + 1,
                }
            )
            _save_load_checkpoint(state_dir, checkpoint)
            pending = []
        if pending:
            merged = await _merge_records(conn, pending)
            totals = MergeResult(
                source_rows=totals.source_rows + merged.source_rows,
                preexisting_rows=totals.preexisting_rows + merged.preexisting_rows,
                inserted_rows=totals.inserted_rows + merged.inserted_rows,
            )
            checkpoint.update(
                {
                    "next_fragment_index": pending_through,
                    "checkpointed_source_rows": int(
                        checkpoint["checkpointed_source_rows"]
                    )
                    + merged.source_rows,
                    "db_batches": int(checkpoint["db_batches"]) + 1,
                }
            )
            _save_load_checkpoint(state_dir, checkpoint)
        if checkpoint["next_fragment_index"] != len(fragments):
            raise DatabaseLoadError("load_checkpoint_not_at_snapshot_end")
        checkpoint["load_complete"] = True
        _save_load_checkpoint(state_dir, checkpoint)
        return {
            "rows_merged_this_invocation": totals.source_rows,
            "rows_preexisting_this_invocation": totals.preexisting_rows,
            "rows_inserted_this_invocation": totals.inserted_rows,
            "checkpoint": checkpoint,
        }
    except asyncpg.PostgresError as exc:
        raise DatabaseLoadError(f"database_error:{type(exc).__name__}") from exc
    finally:
        # Closing the session releases its advisory lock, even after a database
        # error.  Do not run any retry path after that error.
        await conn.close()


async def _verify_records(
    conn: asyncpg.Connection,
    records: Sequence[tuple[Any, ...]],
) -> int:
    async with conn.transaction():
        await conn.copy_records_to_table(
            TEMP_TABLE,
            records=records,
            columns=list(REQUIRED_COLUMNS),
        )
        staged_rows = int(await conn.fetchval(STAGED_COUNT_SQL))
        if staged_rows != len(records):
            raise DatabaseLoadError("verification_stage_row_count_mismatch")
        conflicts = int(await conn.fetchval(CONFLICT_COUNT_SQL))
        if conflicts:
            raise DatabaseLoadError("verification_target_value_conflict")
        present = int(await conn.fetchval(PRESENT_COUNT_SQL))
        if present != len(records):
            raise DatabaseLoadError("verification_target_coverage_mismatch")
    return present


async def verify_snapshot(
    *,
    staging_dir: Path,
    snapshot: dict[str, Any],
    database_url: str,
    commit_rows: int,
) -> VerificationResult:
    """Independently re-read the frozen source and verify every target key."""

    fragments = _frozen_fragments(snapshot)
    conn = await _connect_database(database_url)
    try:
        await _acquire_database_lock(conn)
        await conn.execute(TEMP_TABLE_SQL)
        verified = 0
        pending: list[tuple[Any, ...]] = []
        for _, records in _record_fragments(
            staging_dir=staging_dir,
            fragments=fragments,
            batch_id=str(snapshot["batch_id"]),
        ):
            pending.extend(records)
            if len(pending) < commit_rows:
                continue
            verified += await _verify_records(conn, pending)
            pending = []
        if pending:
            verified += await _verify_records(conn, pending)
        target_rows = int(await conn.fetchval(f"SELECT count(*) FROM {TARGET_TABLE}"))
        batch_rows = int(
            await conn.fetchval(
                f"SELECT count(*) FROM {TARGET_TABLE} WHERE batch_id = $1",
                str(snapshot["batch_id"]),
            )
        )
        duplicate_rows = int(
            await conn.fetchval(
                f"SELECT count(*) - count(DISTINCT (time_utc, symbol)) "
                f"FROM {TARGET_TABLE}"
            )
        )
    except asyncpg.PostgresError as exc:
        raise DatabaseLoadError(f"database_error:{type(exc).__name__}") from exc
    finally:
        await conn.close()
    expected_rows = int(snapshot["source_rows"])
    if (
        verified != expected_rows
        or target_rows != expected_rows
        or batch_rows != expected_rows
    ):
        raise DatabaseLoadError("row_reconciliation_mismatch")
    if duplicate_rows != 0:
        raise DatabaseLoadError("deduplication_verification_failed")
    return VerificationResult(
        source_rows=expected_rows,
        db_rows_verified=verified,
        target_rows=target_rows,
        batch_rows=batch_rows,
        duplicate_rows=duplicate_rows,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--staging-dir", required=True, type=Path)
    parser.add_argument("--state-dir", required=True, type=Path)
    parser.add_argument("--db-env-file", required=True, type=Path)
    parser.add_argument(
        "--min-fragment-age-seconds",
        type=int,
        default=DEFAULT_MIN_FRAGMENT_AGE_SECONDS,
    )
    parser.add_argument("--commit-rows", type=int, default=DEFAULT_COMMIT_ROWS)
    parser.add_argument("--confirm-load", action="store_true")
    return parser.parse_args()


async def async_main(args: argparse.Namespace) -> int:
    staging_dir = args.staging_dir.resolve()
    state_dir = args.state_dir.resolve()
    if args.min_fragment_age_seconds <= 0:
        raise LoadStopped("min_fragment_age_seconds_must_be_positive")
    if args.commit_rows <= 0:
        raise LoadStopped("commit_rows_must_be_positive")
    _assert_state_dir_is_separate(staging_dir, state_dir)
    with StateDirectoryLock(state_dir):
        snapshot = freeze_completed_fragments(
            staging_dir=staging_dir,
            state_dir=state_dir,
            min_fragment_age_seconds=args.min_fragment_age_seconds,
        )
        preflight_source(
            staging_dir=staging_dir,
            state_dir=state_dir,
            snapshot=snapshot,
        )
        prepared = {
            "status": "PREFLIGHT_COMPLETE",
            "completed_fragments_only": snapshot["completed_fragments_only"],
            "fragment_count": len(snapshot["fragments"]),
            "partial_files_excluded": snapshot["partial_files_excluded"],
            "source_rows": snapshot["source_rows"],
            "too_recent_files_excluded": snapshot["too_recent_files_excluded"],
        }
        if not args.confirm_load:
            print(json.dumps(prepared, ensure_ascii=False, sort_keys=True))
            return 0
        database_url = _database_url(args.db_env_file)
        merged = await load_snapshot(
            staging_dir=staging_dir,
            state_dir=state_dir,
            snapshot=snapshot,
            database_url=database_url,
            commit_rows=args.commit_rows,
        )
        verification = await verify_snapshot(
            staging_dir=staging_dir,
            snapshot=snapshot,
            database_url=database_url,
            commit_rows=args.commit_rows,
        )
        result = {
            **prepared,
            **merged,
            "verification": asdict(verification),
            "status": "COMPLETED",
            "target_table": TARGET_TABLE,
        }
        _atomic_write_json(state_dir / "load_summary.json", result)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0


def main() -> int:
    try:
        return asyncio.run(async_main(parse_args()))
    except LoadStopped as exc:
        print(
            json.dumps(
                {"status": "STOPPED", "reason": str(exc)},
                ensure_ascii=False,
                sort_keys=True,
            )
        )
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
