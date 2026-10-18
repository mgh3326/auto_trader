"""File-based append-only experiment ledger (no database, no migration).

Layout under the private ``--ledger-dir``::

    manifest.json        T0 seal (written once, never rewritten)
    manifest.sha256      sha256 of manifest.json bytes
    events.jsonl         hash-chained events, one JSON object per line
    market_data/         raw market-data responses, referenced by sha256
    .lock                advisory lock held for the whole command

Each event line carries ``seq``, ``prev`` (the previous line's ``sha256``) and
its own ``sha256`` over the canonical JSON of every other field. A broken
chain, a gap in ``seq`` or an unknown ``kind`` refuses every command: the
ledger is evidence and cannot be repaired by the tool that writes it.

There is deliberately no event kind that clears a latch. A stop is persisted as
a ``latch`` line and survives every restart.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

MANIFEST_FILE = "manifest.json"
MANIFEST_SHA_FILE = "manifest.sha256"
EVENTS_FILE = "events.jsonl"
MARKET_DATA_DIR = "market_data"
LOCK_FILE = ".lock"
HEAD_FILE = "events.head"
GENESIS = "0" * 64

EVENT_KINDS = frozenset({"sealed", "latch", "register", "step"})
LATCH_TYPES = frozenset({"operational", "loss", "policy_change"})


class LedgerIntegrityError(RuntimeError):
    """The ledger on disk is not a valid, unbroken chain."""


def _default(value: Any) -> Any:
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, datetime):
        if value.tzinfo is None:
            raise TypeError("naive datetime in ledger payload")
        return value.astimezone(UTC).isoformat()
    if isinstance(value, date):
        return value.isoformat()
    if isinstance(value, (set, frozenset)):
        return sorted(value)
    raise TypeError(f"not ledger-serialisable: {type(value).__name__}")


def canonical_json(obj: Any) -> str:
    return json.dumps(
        obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=_default
    )


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_bytes(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _fsync_write(path: Path, data: bytes, *, exclusive: bool) -> None:
    flags = os.O_WRONLY | os.O_CREAT | (os.O_EXCL if exclusive else os.O_TRUNC)
    fd = os.open(path, flags, 0o600)
    try:
        os.write(fd, data)
        os.fsync(fd)
    finally:
        os.close(fd)


@dataclass(frozen=True, slots=True)
class Event:
    seq: int
    kind: str
    recorded_at: str
    payload: dict[str, Any]
    prev: str
    sha256: str


class Ledger:
    def __init__(self, root: Path) -> None:
        self.root = Path(root)

    # ------------------------------------------------------------ paths
    @property
    def manifest_path(self) -> Path:
        return self.root / MANIFEST_FILE

    @property
    def manifest_sha_path(self) -> Path:
        return self.root / MANIFEST_SHA_FILE

    @property
    def events_path(self) -> Path:
        return self.root / EVENTS_FILE

    @property
    def market_data_dir(self) -> Path:
        return self.root / MARKET_DATA_DIR

    def exists(self) -> bool:
        return self.manifest_path.exists()

    # ------------------------------------------------------------ lock
    @contextmanager
    def locked(self) -> Iterator[None]:
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.root / LOCK_FILE, os.O_RDWR | os.O_CREAT, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError:
                raise LedgerIntegrityError(
                    "another command holds the ledger lock; refusing to interleave"
                ) from None
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)

    # ------------------------------------------------------------ manifest
    def write_manifest(self, manifest: dict[str, Any]) -> str:
        if self.exists() or self.events_path.exists():
            raise LedgerIntegrityError(
                "ledger already sealed; a manifest is written exactly once"
            )
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        raw = (canonical_json(manifest) + "\n").encode("utf-8")
        digest = sha256_bytes(raw)
        _fsync_write(self.manifest_path, raw, exclusive=True)
        _fsync_write(self.manifest_sha_path, (digest + "\n").encode(), exclusive=True)
        return digest

    def read_manifest(self) -> tuple[dict[str, Any], str]:
        try:
            raw = self.manifest_path.read_bytes()
            recorded = self.manifest_sha_path.read_text().strip()
        except OSError as exc:
            raise LedgerIntegrityError(f"ledger not sealed: {exc}") from None
        digest = sha256_bytes(raw)
        if digest != recorded:
            raise LedgerIntegrityError("manifest.json does not match manifest.sha256")
        return json.loads(raw), digest

    # ------------------------------------------------------------ events
    def load(self) -> list[Event]:
        if not self.events_path.exists():
            return []
        events: list[Event] = []
        prev = GENESIS
        with self.events_path.open(encoding="utf-8") as handle:
            for lineno, raw in enumerate(handle, start=1):
                if not raw.strip():
                    raise LedgerIntegrityError(f"blank line {lineno} in events.jsonl")
                try:
                    obj = json.loads(raw)
                except ValueError:
                    raise LedgerIntegrityError(f"line {lineno} is not JSON") from None
                if not isinstance(obj, dict):
                    raise LedgerIntegrityError(f"line {lineno} is not an object")
                claimed = obj.pop("sha256", None)
                if sha256_text(canonical_json(obj)) != claimed:
                    raise LedgerIntegrityError(f"line {lineno} hash mismatch")
                if obj.get("seq") != lineno:
                    raise LedgerIntegrityError(
                        f"line {lineno} has seq {obj.get('seq')}"
                    )
                if obj.get("prev") != prev:
                    raise LedgerIntegrityError(f"line {lineno} breaks the hash chain")
                kind = obj.get("kind")
                if kind not in EVENT_KINDS:
                    raise LedgerIntegrityError(f"line {lineno} unknown kind {kind!r}")
                if (
                    kind == "latch"
                    and obj["payload"].get("latch_type") not in LATCH_TYPES
                ):
                    raise LedgerIntegrityError(f"line {lineno} unknown latch type")
                events.append(
                    Event(
                        seq=obj["seq"],
                        kind=kind,
                        recorded_at=obj["recorded_at"],
                        payload=obj["payload"],
                        prev=obj["prev"],
                        sha256=claimed,
                    )
                )
                prev = claimed
        if events and events[0].kind != "sealed":
            raise LedgerIntegrityError("first event must be the seal")
        self._check_head(events)
        return events

    def _read_head(self) -> tuple[int, str] | None:
        try:
            seq, digest = (self.root / HEAD_FILE).read_text().split()
            return int(seq), digest
        except (OSError, ValueError):
            return None

    def _write_head(self, seq: int, digest: str) -> None:
        _fsync_write(
            self.root / HEAD_FILE, f"{seq} {digest}\n".encode(), exclusive=False
        )

    def _check_head(self, events: list[Event]) -> None:
        """Detect truncation (e.g. deleting a latch line to "resume").

        The head file names the last appended event. Fewer events than the head
        is truncation; exactly one more, with the head matching the previous
        line, is a crash between the two fsyncs and is repaired.
        """

        head = self._read_head()
        if head is None:
            if events:
                raise LedgerIntegrityError("events.head missing for a non-empty ledger")
            return
        seq, digest = head
        if len(events) == seq and events[-1].sha256 == digest:
            return
        if len(events) == seq + 1 and seq >= 1 and events[seq - 1].sha256 == digest:
            self._write_head(events[-1].seq, events[-1].sha256)
            return
        raise LedgerIntegrityError(
            "events.jsonl does not end at events.head (truncated?)"
        )

    def append(self, kind: str, payload: dict[str, Any], *, now: datetime) -> Event:
        if kind not in EVENT_KINDS:
            raise LedgerIntegrityError(f"refusing unknown event kind {kind!r}")
        if kind == "latch" and payload.get("latch_type") not in LATCH_TYPES:
            raise LedgerIntegrityError("refusing unknown latch type")
        existing = self.load()
        prev = existing[-1].sha256 if existing else GENESIS
        if not existing and kind != "sealed":
            raise LedgerIntegrityError("first event must be the seal")
        body = {
            "seq": len(existing) + 1,
            "kind": kind,
            "recorded_at": now.astimezone(UTC).isoformat(),
            "payload": json.loads(canonical_json(payload)),
            "prev": prev,
        }
        digest = sha256_text(canonical_json(body))
        line = canonical_json({**body, "sha256": digest}) + "\n"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd = os.open(self.events_path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        try:
            os.write(fd, line.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        self._write_head(body["seq"], digest)
        return Event(
            seq=body["seq"],
            kind=kind,
            recorded_at=body["recorded_at"],
            payload=body["payload"],
            prev=prev,
            sha256=digest,
        )

    # ------------------------------------------------------------ raw data
    def store_market_data(self, name: str, payload: Any) -> str:
        """Persist one raw response; returns its sha256 (content-addressed)."""

        raw = (canonical_json(payload) + "\n").encode("utf-8")
        digest = sha256_bytes(raw)
        self.market_data_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        path = self.market_data_dir / f"{digest}.json"
        if not path.exists():
            _fsync_write(path, raw, exclusive=True)
        return digest
