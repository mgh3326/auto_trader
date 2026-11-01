"""Manual daily exchange-info snapshot: append-only files plus a JSONL manifest.

Layout under ``<root>``::

    snapshots/<YYYY-MM-DD>/<source>.json.gz  raw public response bytes, gzip
                                             (mtime 0), read-only, never rewritten
    snapshots/manifest.jsonl                 one line per snapshot file, append-only
    snapshots/.lock                          serialises concurrent runs

``sha256`` in the manifest is over the raw (decompressed) response body;
``stored_sha256`` is over the ``.gz`` file on disk.

Idempotent per UTC day: a second run on the same day fetches nothing for a
source whose file already exists and appends nothing.

Crash safety:

* A snapshot file is written to a temporary name, fsynced, then published
  with ``os.link`` (fails if the name exists), so the final name only ever
  holds a complete file and an existing snapshot is never overwritten.
* A published file without a manifest line (a crash between the two writes)
  gets its line on the next run, after its bytes pass the same validation as
  a fresh response.
* A manifest line torn by a crash is closed with a newline before the next
  append and skipped by readers; the file it described then gets a fresh line.
* A run whose fetch finishes on a later UTC day than it started refuses to
  write, so a file's date always matches its fetch.

There is no scheduler; an operator runs the command. Responses come only from
the public endpoints allowlisted in :mod:`public_http`.
"""

from __future__ import annotations

import fcntl
import gzip
import hashlib
import json
import os
import zlib
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

from .public_http import Fetch

SNAPSHOT_SOURCES: Mapping[str, str] = {
    "binance_spot_exchange_info": "https://data-api.binance.vision/api/v3/exchangeInfo",
    "upbit_market_all": "https://api.upbit.com/v1/market/all?isDetails=true",
}
MANIFEST_NAME = "manifest.jsonl"
LOCK_NAME = ".lock"
SCHEMA_VERSION = 1


class SnapshotError(RuntimeError):
    pass


@dataclass(frozen=True)
class SnapshotOutcome:
    source: str
    path: Path
    action: str  # "written" | "already_present" | "manifest_repaired"
    sha256: str


def snapshot_dir(root: Path) -> Path:
    return root / "snapshots"


def _manifest_entries(manifest: Path) -> list[dict[str, Any]]:
    """Parsed manifest lines; a line torn by a crash is skipped."""
    if not manifest.exists():
        return []
    entries = []
    for line in manifest.read_bytes().splitlines():
        try:
            entry = json.loads(line)
        except ValueError:
            continue  # torn line; its file is re-recorded on the next run
        if isinstance(entry, dict):
            entries.append(entry)
    return entries


def _append_line(manifest: Path, entry: Mapping[str, Any]) -> None:
    line = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(manifest, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = b"\n" + line  # close a line torn by an earlier crash
        os.write(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(path: Path, body: bytes) -> None:
    """Write ``body`` under a temp name, then link it to ``path`` (never replaces)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o444)
    try:
        os.write(fd, body)
        os.fsync(fd)
    finally:
        os.close(fd)
    try:
        os.link(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)
    dir_fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def _gzip(body: bytes) -> bytes:
    return gzip.compress(body, compresslevel=9, mtime=0)


def _read_raw(path: Path) -> bytes:
    try:
        return gzip.decompress(path.read_bytes())
    except (OSError, EOFError, zlib.error) as exc:
        raise SnapshotError(f"{path} is not a readable gzip file") from exc


def _validate_payload(source: str, body: bytes) -> None:
    try:
        payload = json.loads(body)
    except ValueError as exc:
        raise SnapshotError(f"{source}: response is not JSON") from exc
    if source == "binance_spot_exchange_info":
        if not isinstance(payload, dict) or not isinstance(
            payload.get("symbols"), list
        ):
            raise SnapshotError(f"{source}: no symbols list")
        if not payload["symbols"]:
            raise SnapshotError(f"{source}: empty symbols list")
    elif source == "upbit_market_all":
        if not isinstance(payload, list) or not payload:
            raise SnapshotError(f"{source}: not a non-empty list")
    else:
        raise SnapshotError(f"unknown source {source}")


@contextmanager
def _run_lock(base: Path) -> Iterator[None]:
    fd = os.open(base / LOCK_NAME, os.O_RDWR | os.O_CREAT, 0o644)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(fd, fcntl.LOCK_UN)
        os.close(fd)


def _utc_iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def take_snapshot(
    root: Path,
    fetch: Fetch,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sources: Mapping[str, str] = SNAPSHOT_SOURCES,
) -> list[SnapshotOutcome]:
    """Write today's (UTC) snapshot of each source unless it already exists."""
    base = snapshot_dir(root)
    base.mkdir(parents=True, exist_ok=True)
    with _run_lock(base):
        day = now().astimezone(UTC).date()
        manifest = base / MANIFEST_NAME
        recorded = {
            (e.get("snapshot_date"), e.get("source"), e.get("sha256"))
            for e in _manifest_entries(manifest)
        }
        outcomes: list[SnapshotOutcome] = []
        for source, url in sources.items():
            path = base / day.isoformat() / f"{source}.json.gz"
            relative = path.relative_to(root).as_posix()
            if path.exists():
                raw = _read_raw(path)
                _validate_payload(source, raw)
                digest = hashlib.sha256(raw).hexdigest()
                if (day.isoformat(), source, digest) in recorded:
                    outcomes.append(
                        SnapshotOutcome(source, path, "already_present", digest)
                    )
                    continue
                _append_line(
                    manifest,
                    {
                        "schema_version": SCHEMA_VERSION,
                        "snapshot_date": day.isoformat(),
                        "source": source,
                        "url": url,
                        "path": relative,
                        "sha256": digest,
                        "bytes": len(raw),
                        "stored_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
                        "fetched_at": None,
                        "note": "manifest line repaired from file on disk",
                    },
                )
                outcomes.append(
                    SnapshotOutcome(source, path, "manifest_repaired", digest)
                )
                continue
            resp = fetch(url)
            fetched_at = now().astimezone(UTC)
            if fetched_at.date() != day:
                raise SnapshotError(
                    f"{source}: run started on {day} but fetched on "
                    f"{fetched_at.date()}; rerun to snapshot the new day"
                )
            if resp.status != 200:
                raise SnapshotError(f"{source}: HTTP {resp.status}")
            _validate_payload(source, resp.body)
            digest = hashlib.sha256(resp.body).hexdigest()
            stored = _gzip(resp.body)
            _publish(path, stored)
            _append_line(
                manifest,
                {
                    "schema_version": SCHEMA_VERSION,
                    "snapshot_date": day.isoformat(),
                    "source": source,
                    "url": url,
                    "path": relative,
                    "sha256": digest,
                    "bytes": len(resp.body),
                    "stored_sha256": hashlib.sha256(stored).hexdigest(),
                    "fetched_at": _utc_iso(fetched_at),
                },
            )
            outcomes.append(SnapshotOutcome(source, path, "written", digest))
        return outcomes


def latest_snapshot(root: Path, source: str) -> tuple[date, bytes, str]:
    """Most recent recorded snapshot of ``source`` (raw bytes, sha256).

    The bytes on disk must match a manifest line for that day; otherwise the
    snapshot is refused.
    """
    base = snapshot_dir(root)
    entries = [
        e for e in _manifest_entries(base / MANIFEST_NAME) if e.get("source") == source
    ]
    if not entries:
        raise SnapshotError(
            f"no {source} snapshot under {base}; run the snapshot command"
        )
    newest = max(e["snapshot_date"] for e in entries)
    candidates = [e for e in entries if e["snapshot_date"] == newest]
    path = root / candidates[0]["path"]
    raw = _read_raw(path)
    digest = hashlib.sha256(raw).hexdigest()
    if not any(e["sha256"] == digest for e in candidates):
        raise SnapshotError(f"{path} does not match its manifest sha256")
    return date.fromisoformat(newest), raw, digest
