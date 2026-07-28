"""Append-only JSONL hash-chain for the KR-B1 P0 campaign.

This module is deliberately file-only.  It does not import a database, broker,
order, fill, scheduler, or deployment surface.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

GENESIS_HASH = "0" * 64
_SEALED_INITIAL_ROW = {
    "next_stage": "P0_10_KRX_SESSIONS",
    "p0_state": "NOT_STARTED",
    "record_type": "SEAL_INITIALIZED",
    "recorded_date": "2026-07-28",
    "study_id": "KRB1-CSM60-H5-v1",
}
_ENTRY_KEYS = frozenset({"chain_hash", "index", "row", "row_hash"})
_HEX_DIGITS = frozenset("0123456789abcdef")


class JournalError(ValueError):
    """The journal is malformed or violates the append-only chain contract."""


@dataclass(frozen=True)
class JournalEntry:
    """One verified JSONL envelope."""

    index: int
    row: dict[str, Any]
    row_hash: str
    chain_hash: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "chain_hash": self.chain_hash,
            "index": self.index,
            "row": self.row,
            "row_hash": self.row_hash,
        }


@dataclass(frozen=True)
class JournalHead:
    """Verified append position."""

    row_count: int
    chain_hash: str


def canonical_json_bytes(value: Any) -> bytes:
    """Return the sealed-policy JSON encoding or fail closed."""
    try:
        encoded = json.dumps(
            value,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    except (TypeError, ValueError) as exc:
        raise JournalError(f"value is not canonical-JSON encodable: {exc}") from exc
    return encoded.encode("utf-8")


SEALED_INITIAL_ROW_HASH = hashlib.sha256(
    canonical_json_bytes(_SEALED_INITIAL_ROW)
).hexdigest()
SEALED_INITIAL_HEAD = hashlib.sha256(
    bytes.fromhex(GENESIS_HASH) + bytes.fromhex(SEALED_INITIAL_ROW_HASH)
).hexdigest()


def compute_row_hash(row: Mapping[str, Any]) -> str:
    """SHA-256 of one canonical row."""
    return hashlib.sha256(canonical_json_bytes(dict(row))).hexdigest()


def compute_chain_hash(previous_chain_hash: str, row_hash: str) -> str:
    """SHA-256(raw previous chain hash || raw row hash)."""
    _require_hash(previous_chain_hash, field="previous_chain_hash")
    _require_hash(row_hash, field="row_hash")
    material = bytes.fromhex(previous_chain_hash) + bytes.fromhex(row_hash)
    return hashlib.sha256(material).hexdigest()


def build_entry(
    *, index: int, row: Mapping[str, Any], previous_chain_hash: str
) -> JournalEntry:
    """Build the next immutable envelope without writing it."""
    if type(index) is not int or index < 1:
        raise JournalError("index must be a positive built-in int")
    normalized_row = _normalize_row(row)
    row_hash = compute_row_hash(normalized_row)
    return JournalEntry(
        index=index,
        row=normalized_row,
        row_hash=row_hash,
        chain_hash=compute_chain_hash(previous_chain_hash, row_hash),
    )


def verify_journal(path: Path) -> JournalHead:
    """Verify canonical bytes, every row hash, and the complete chain."""
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise JournalError(f"cannot read journal {path}: {exc}") from exc
    return _verify_bytes(data)


def append_journal_row(path: Path, row: Mapping[str, Any]) -> JournalEntry:
    """Atomically append one canonical line after verifying the current chain.

    The exclusive advisory lock serializes writers.  Existing bytes are never
    rewritten or truncated.
    """
    normalized_row = _normalize_row(row)
    try:
        with path.open("r+b") as handle:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
            handle.seek(0)
            existing = handle.read()
            head = _verify_bytes(existing)
            if head.row_count == 0:
                raise JournalError(
                    "journal has no sealed initial row; initialize it before append"
                )
            entry = build_entry(
                index=head.row_count + 1,
                row=normalized_row,
                previous_chain_hash=head.chain_hash,
            )
            line = canonical_json_bytes(entry.as_dict()) + b"\n"
            handle.seek(0, os.SEEK_END)
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
            return entry
    except OSError as exc:
        raise JournalError(f"cannot append journal {path}: {exc}") from exc


def create_journal(path: Path, initial_row: Mapping[str, Any]) -> JournalEntry:
    """Create a new journal exclusively; refuse to replace any existing path."""
    entry = build_entry(
        index=1,
        row=initial_row,
        previous_chain_hash=GENESIS_HASH,
    )
    if (
        entry.row_hash != SEALED_INITIAL_ROW_HASH
        or entry.chain_hash != SEALED_INITIAL_HEAD
    ):
        raise JournalError("initial row does not match the sealed KR-B1 anchor")
    line = canonical_json_bytes(entry.as_dict()) + b"\n"
    try:
        with path.open("xb") as handle:
            handle.write(line)
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        raise JournalError(f"cannot create new journal {path}: {exc}") from exc
    return entry


def _verify_bytes(data: bytes) -> JournalHead:
    if not data:
        return JournalHead(row_count=0, chain_hash=GENESIS_HASH)
    if not data.endswith(b"\n"):
        raise JournalError("journal must end with a newline before another append")

    previous = GENESIS_HASH
    row_count = 0
    for expected_index, raw_line in enumerate(data.splitlines(), start=1):
        if not raw_line:
            raise JournalError(f"line {expected_index} is blank")
        value = _strict_json_loads(raw_line, line=expected_index)
        if type(value) is not dict or set(value) != _ENTRY_KEYS:
            raise JournalError(
                f"line {expected_index} must have exactly {sorted(_ENTRY_KEYS)}"
            )
        if canonical_json_bytes(value) != raw_line:
            raise JournalError(f"line {expected_index} is not canonical JSON")

        index = value["index"]
        if type(index) is not int or index != expected_index:
            raise JournalError(
                f"line {expected_index} has non-contiguous index {index!r}"
            )
        row = value["row"]
        if type(row) is not dict:
            raise JournalError(f"line {expected_index} row must be an object")
        row_hash = value["row_hash"]
        chain_hash = value["chain_hash"]
        _require_hash(row_hash, field=f"line {expected_index} row_hash")
        _require_hash(chain_hash, field=f"line {expected_index} chain_hash")

        expected_row_hash = compute_row_hash(row)
        if row_hash != expected_row_hash:
            raise JournalError(f"line {expected_index} row_hash mismatch")
        expected_chain_hash = compute_chain_hash(previous, row_hash)
        if chain_hash != expected_chain_hash:
            raise JournalError(f"line {expected_index} chain_hash mismatch")
        if expected_index == 1 and (
            row_hash != SEALED_INITIAL_ROW_HASH or chain_hash != SEALED_INITIAL_HEAD
        ):
            raise JournalError("line 1 does not match the sealed KR-B1 anchor")

        previous = chain_hash
        row_count = expected_index
    return JournalHead(row_count=row_count, chain_hash=previous)


def _strict_json_loads(raw: bytes, *, line: int) -> Any:
    def pairs_hook(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, child in pairs:
            if key in value:
                raise JournalError(f"line {line} contains duplicate key {key!r}")
            value[key] = child
        return value

    def reject_constant(value: str) -> None:
        raise JournalError(f"line {line} contains non-finite number {value}")

    try:
        return json.loads(
            raw,
            object_pairs_hook=pairs_hook,
            parse_constant=reject_constant,
        )
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise JournalError(f"line {line} is not valid UTF-8 JSON: {exc}") from exc


def _normalize_row(row: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(row, Mapping):
        raise JournalError("row must be a mapping")
    normalized = dict(row)
    if any(type(key) is not str for key in normalized):
        raise JournalError("row keys must be built-in strings")
    canonical_json_bytes(normalized)
    return normalized


def _require_hash(value: Any, *, field: str) -> None:
    if (
        type(value) is not str
        or len(value) != 64
        or not set(value).issubset(_HEX_DIGITS)
    ):
        raise JournalError(f"{field} must be lowercase 64-hex SHA-256")


__all__ = [
    "GENESIS_HASH",
    "SEALED_INITIAL_HEAD",
    "SEALED_INITIAL_ROW_HASH",
    "JournalEntry",
    "JournalError",
    "JournalHead",
    "append_journal_row",
    "build_entry",
    "canonical_json_bytes",
    "compute_chain_hash",
    "compute_row_hash",
    "create_journal",
    "verify_journal",
]
