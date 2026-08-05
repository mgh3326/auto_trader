"""Append-only, hash-chained evidence streams for the KR-B1 P0-3 gate proof path.

Why a separate store: the sealed campaign journal in
:mod:`app.services.krb1_p0_journal` is operator-owned and anchored to the sealed
KR-B1 initial row. Gate evidence must never append there. This module provides
independent streams with their own genesis row, reusing only the sealed journal's
pure hashing primitives (canonical JSON, row hash, chain hash) so both chains are
verifiable with the same arithmetic.

Contract:

* append-only — existing bytes are never rewritten or truncated;
* hash-chained — every envelope commits to the whole prefix;
* clock-monotonic — a record whose ``recorded_at`` precedes the current head is
  refused, so a retrieval clock cannot be back-dated after the fact;
* file-only — no database, broker, order, network, or scheduler surface.

This is the only sanctioned write path for gate evidence (AGENTS.md #5:
service-layer writes only).
"""

from __future__ import annotations

import datetime as dt
import fcntl
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.services.krb1_p0_journal import (
    GENESIS_HASH,
    JournalError,
    canonical_json_bytes,
    compute_chain_hash,
    compute_row_hash,
)

SCHEMA_VERSION = "krb1.p0_3.evidence_chain.v1"
STREAM_INITIALIZED_RECORD_TYPE = "STREAM_INITIALIZED"
_ENVELOPE_KEYS = frozenset(
    {"chain_hash", "index", "record_type", "row", "row_hash", "stream_id"}
)
_HEX_DIGITS = frozenset("0123456789abcdef")
_STREAM_ID_CHARS = frozenset("abcdefghijklmnopqrstuvwxyz0123456789_-.")
_RECORD_TYPE_CHARS = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_")
RECORDED_AT_KEY = "recorded_at"


class EvidenceChainError(ValueError):
    """The stream is malformed or an append would violate the contract."""


@dataclass(frozen=True, slots=True)
class EvidenceRecord:
    """One verified envelope in an evidence stream."""

    index: int
    stream_id: str
    record_type: str
    row: dict[str, Any]
    row_hash: str
    chain_hash: str

    def as_envelope(self) -> dict[str, Any]:
        return {
            "chain_hash": self.chain_hash,
            "index": self.index,
            "record_type": self.record_type,
            "row": self.row,
            "row_hash": self.row_hash,
            "stream_id": self.stream_id,
        }


@dataclass(frozen=True, slots=True)
class EvidenceHead:
    """Verified append position for a stream."""

    stream_id: str
    record_count: int
    chain_hash: str
    last_recorded_at: dt.datetime | None


def genesis_row(stream_id: str) -> dict[str, Any]:
    """Deterministic first row of a stream. Contains no clock."""
    _require_stream_id(stream_id)
    return {
        "record_type": STREAM_INITIALIZED_RECORD_TYPE,
        "schema_version": SCHEMA_VERSION,
        "stream_id": stream_id,
    }


def open_stream(path: Path, *, stream_id: str) -> EvidenceHead:
    """Verify an existing stream, or create it with its genesis row."""
    _require_stream_id(stream_id)
    if path.exists():
        return verify_stream(path, stream_id=stream_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    record = _build_record(
        index=1,
        stream_id=stream_id,
        record_type=STREAM_INITIALIZED_RECORD_TYPE,
        row=genesis_row(stream_id),
        previous_chain_hash=GENESIS_HASH,
    )
    line = canonical_json_bytes(record.as_envelope()) + b"\n"
    try:
        with path.open("xb") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    except FileExistsError:
        return verify_stream(path, stream_id=stream_id)
    except OSError as exc:  # pragma: no cover - filesystem failure
        raise EvidenceChainError(f"cannot create stream {path}: {exc}") from exc
    return EvidenceHead(
        stream_id=stream_id,
        record_count=1,
        chain_hash=record.chain_hash,
        last_recorded_at=None,
    )


def append_record(
    path: Path,
    *,
    stream_id: str,
    record_type: str,
    row: Mapping[str, Any],
) -> EvidenceRecord:
    """Append one canonical envelope after verifying the whole existing chain."""
    _require_stream_id(stream_id)
    _require_record_type(record_type)
    normalized = _normalize_row(row)
    recorded_at = _recorded_at(normalized)
    try:
        with path.open("r+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            head = _verify_bytes(handle.read(), stream_id=stream_id)
            if head.record_count == 0:
                raise EvidenceChainError(
                    "stream has no genesis row; call open_stream first"
                )
            if (
                head.last_recorded_at is not None
                and recorded_at is not None
                and recorded_at < head.last_recorded_at
            ):
                raise EvidenceChainError(
                    "recorded_at regresses below the stream head; "
                    "an append-only clock cannot move backwards"
                )
            record = _build_record(
                index=head.record_count + 1,
                stream_id=stream_id,
                record_type=record_type,
                row=normalized,
                previous_chain_hash=head.chain_hash,
            )
            line = canonical_json_bytes(record.as_envelope()) + b"\n"
            handle.seek(0, os.SEEK_END)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
            return record
    except FileNotFoundError as exc:
        raise EvidenceChainError(f"stream {path} does not exist: {exc}") from exc
    except OSError as exc:  # pragma: no cover - filesystem failure
        raise EvidenceChainError(f"cannot append to stream {path}: {exc}") from exc


def read_records(path: Path, *, stream_id: str) -> tuple[EvidenceRecord, ...]:
    """Return every verified record, genesis first."""
    _require_stream_id(stream_id)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise EvidenceChainError(f"cannot read stream {path}: {exc}") from exc
    records: list[EvidenceRecord] = []
    _verify_bytes(data, stream_id=stream_id, sink=records)
    return tuple(records)


def verify_stream(path: Path, *, stream_id: str) -> EvidenceHead:
    """Verify canonical bytes, every row hash, and the complete chain."""
    _require_stream_id(stream_id)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise EvidenceChainError(f"cannot read stream {path}: {exc}") from exc
    return _verify_bytes(data, stream_id=stream_id)


def _build_record(
    *,
    index: int,
    stream_id: str,
    record_type: str,
    row: Mapping[str, Any],
    previous_chain_hash: str,
) -> EvidenceRecord:
    if type(index) is not int or index < 1:
        raise EvidenceChainError("index must be a positive built-in int")
    normalized = _normalize_row(row)
    row_hash = compute_row_hash(normalized)
    return EvidenceRecord(
        index=index,
        stream_id=stream_id,
        record_type=record_type,
        row=normalized,
        row_hash=row_hash,
        chain_hash=compute_chain_hash(previous_chain_hash, row_hash),
    )


def _verify_bytes(
    data: bytes,
    *,
    stream_id: str,
    sink: list[EvidenceRecord] | None = None,
) -> EvidenceHead:
    if not data:
        return EvidenceHead(
            stream_id=stream_id,
            record_count=0,
            chain_hash=GENESIS_HASH,
            last_recorded_at=None,
        )
    if not data.endswith(b"\n"):
        raise EvidenceChainError("stream must end with a newline before another append")

    previous = GENESIS_HASH
    record_count = 0
    last_recorded_at: dt.datetime | None = None
    for expected_index, raw_line in enumerate(data.splitlines(), start=1):
        if not raw_line:
            raise EvidenceChainError(f"line {expected_index} is blank")
        envelope = _strict_json_loads(raw_line, line=expected_index)
        if type(envelope) is not dict or set(envelope) != _ENVELOPE_KEYS:
            raise EvidenceChainError(
                f"line {expected_index} must have exactly {sorted(_ENVELOPE_KEYS)}"
            )
        if canonical_json_bytes(envelope) != raw_line:
            raise EvidenceChainError(f"line {expected_index} is not canonical JSON")
        if envelope["stream_id"] != stream_id:
            raise EvidenceChainError(
                f"line {expected_index} belongs to stream "
                f"{envelope['stream_id']!r}, not {stream_id!r}"
            )
        index = envelope["index"]
        if type(index) is not int or index != expected_index:
            raise EvidenceChainError(
                f"line {expected_index} has non-contiguous index {index!r}"
            )
        row = envelope["row"]
        if type(row) is not dict:
            raise EvidenceChainError(f"line {expected_index} row must be an object")
        record_type = envelope["record_type"]
        _require_record_type(record_type, line=expected_index)
        row_hash = envelope["row_hash"]
        chain_hash = envelope["chain_hash"]
        _require_hash(row_hash, field=f"line {expected_index} row_hash")
        _require_hash(chain_hash, field=f"line {expected_index} chain_hash")
        if row_hash != compute_row_hash(row):
            raise EvidenceChainError(f"line {expected_index} row_hash mismatch")
        if chain_hash != compute_chain_hash(previous, row_hash):
            raise EvidenceChainError(f"line {expected_index} chain_hash mismatch")
        if expected_index == 1:
            expected_genesis = genesis_row(stream_id)
            if row != expected_genesis or record_type != (
                STREAM_INITIALIZED_RECORD_TYPE
            ):
                raise EvidenceChainError(
                    "line 1 is not the deterministic genesis row for this stream"
                )
        recorded_at = _recorded_at(row)
        if recorded_at is not None:
            if last_recorded_at is not None and recorded_at < last_recorded_at:
                raise EvidenceChainError(f"line {expected_index} recorded_at regresses")
            last_recorded_at = recorded_at
        previous = chain_hash
        record_count = expected_index
        if sink is not None:
            sink.append(
                EvidenceRecord(
                    index=index,
                    stream_id=stream_id,
                    record_type=record_type,
                    row=row,
                    row_hash=row_hash,
                    chain_hash=chain_hash,
                )
            )
    return EvidenceHead(
        stream_id=stream_id,
        record_count=record_count,
        chain_hash=previous,
        last_recorded_at=last_recorded_at,
    )


def _strict_json_loads(raw: bytes, *, line: int) -> Any:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise EvidenceChainError(f"line {line} contains duplicate key {key!r}")
            value[key] = child
        return value

    def reject_constant(value: str) -> None:
        raise EvidenceChainError(f"line {line} contains non-finite number {value}")

    try:
        return json.loads(
            raw, object_pairs_hook=pairs_hook, parse_constant=reject_constant
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise EvidenceChainError(f"line {line} is not valid UTF-8 JSON: {exc}") from exc


def _normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise EvidenceChainError("row must be a mapping")
    normalized = dict(row)
    if any(type(key) is not str for key in normalized):
        raise EvidenceChainError("row keys must be built-in strings")
    try:
        canonical_json_bytes(normalized)
    except JournalError as exc:
        raise EvidenceChainError(f"row is not canonical-JSON encodable: {exc}") from exc
    return normalized


def _recorded_at(row: Mapping[str, Any]) -> dt.datetime | None:
    raw = row.get(RECORDED_AT_KEY)
    if raw is None:
        return None
    if type(raw) is not str:
        raise EvidenceChainError("recorded_at must be an ISO-8601 string")
    try:
        parsed = dt.datetime.fromisoformat(raw)
    except ValueError as exc:
        raise EvidenceChainError(f"recorded_at is not ISO-8601: {raw!r}") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise EvidenceChainError("recorded_at must be timezone-aware")
    return parsed


def _require_stream_id(value: object) -> None:
    if (
        type(value) is not str
        or not 3 <= len(value) <= 64
        or not set(value).issubset(_STREAM_ID_CHARS)
    ):
        raise EvidenceChainError("stream_id must be 3-64 chars of [a-z0-9_.-]")


def _require_record_type(value: object, *, line: int | None = None) -> None:
    where = "record_type" if line is None else f"line {line} record_type"
    if (
        type(value) is not str
        or not 3 <= len(value) <= 64
        or not set(value).issubset(_RECORD_TYPE_CHARS)
    ):
        raise EvidenceChainError(f"{where} must be 3-64 chars of [A-Z0-9_]")


def _require_hash(value: Any, *, field: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or not set(value).issubset(_HEX_DIGITS)
    ):
        raise EvidenceChainError(f"{field} must be lowercase 64-hex SHA-256")


__all__ = [
    "RECORDED_AT_KEY",
    "SCHEMA_VERSION",
    "STREAM_INITIALIZED_RECORD_TYPE",
    "EvidenceChainError",
    "EvidenceHead",
    "EvidenceRecord",
    "append_record",
    "genesis_row",
    "open_stream",
    "read_records",
    "verify_stream",
]
