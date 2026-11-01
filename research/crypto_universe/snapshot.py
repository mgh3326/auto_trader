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
  gets its line on the next run, on any later day, after its bytes pass the
  same validation as a fresh response.
* A manifest line torn by a crash is closed with a newline before the next
  append and skipped by readers; the file it described then gets a fresh line.
* Every missing source is fetched and validated before any is published, and
  the clock is read after the last fetch: a run that crosses UTC midnight
  writes nothing, so a day is never left partial by the clock.
* ``<day>/COMPLETE.json`` is published only after every source of that day is
  published and recorded; readers ignore a day without it. A crash between
  sources leaves the day incomplete: a same-day rerun fetches only the missing
  sources and then marks it complete, while a later run cannot fetch a past
  snapshot, so that day stays visibly incomplete instead of being used.

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
# Published in a day's folder only after every SNAPSHOT_SOURCES file of that
# day is published and recorded. Readers ignore a day without it.
COMPLETE_NAME = "COMPLETE.json"
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


def _write_all(fd: int, data: bytes) -> None:
    """``os.write`` until every byte is written (it may write fewer)."""
    view = memoryview(data)
    while view:
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("os.write wrote no bytes")
        view = view[written:]


def _append_line(manifest: Path, entry: Mapping[str, Any]) -> None:
    line = (json.dumps(entry, sort_keys=True, separators=(",", ":")) + "\n").encode()
    fd = os.open(manifest, os.O_RDWR | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":
            line = b"\n" + line  # close a line torn by an earlier crash
        _write_all(fd, line)
        os.fsync(fd)
    finally:
        os.close(fd)


def _publish(path: Path, body: bytes) -> None:
    """Write ``body`` under a temp name, then link it to ``path`` (never replaces)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o444)
    try:
        try:
            _write_all(fd, body)
            os.fsync(fd)
        finally:
            os.close(fd)
        if tmp.read_bytes() != body:  # never publish anything but the full body
            raise SnapshotError(f"{tmp} does not hold the bytes written to it")
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


def _reconcile_existing(
    root: Path,
    manifest: Path,
    recorded: set[tuple[Any, Any, Any]],
    snapshot_date: str,
    source: str,
    url: str,
    path: Path,
) -> SnapshotOutcome:
    """Record an existing snapshot file if the manifest lacks its line."""
    raw = _read_raw(path)
    _validate_payload(source, raw)
    digest = hashlib.sha256(raw).hexdigest()
    if (snapshot_date, source, digest) in recorded:
        return SnapshotOutcome(source, path, "already_present", digest)
    _append_line(
        manifest,
        {
            "schema_version": SCHEMA_VERSION,
            "snapshot_date": snapshot_date,
            "source": source,
            "url": url,
            "path": path.relative_to(root).as_posix(),
            "sha256": digest,
            "bytes": len(raw),
            "stored_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
            "fetched_at": None,
            "note": "manifest line repaired from file on disk",
        },
    )
    recorded.add((snapshot_date, source, digest))
    return SnapshotOutcome(source, path, "manifest_repaired", digest)


def _mark_complete_if_ready(day_dir: Path, recorded: set[tuple[Any, Any, Any]]) -> None:
    """Publish the day's completion marker once every source is recorded."""
    marker = day_dir / COMPLETE_NAME
    if marker.exists():
        return
    shas: dict[str, str] = {}
    for source in SNAPSHOT_SOURCES:
        path = day_dir / f"{source}.json.gz"
        if not path.exists():
            return
        digest = hashlib.sha256(_read_raw(path)).hexdigest()
        if (day_dir.name, source, digest) not in recorded:
            return
        shas[source] = digest
    body = json.dumps(
        {"snapshot_date": day_dir.name, "sources": shas}, indent=1, sort_keys=True
    )
    _publish(marker, (body + "\n").encode())


def is_complete(root: Path, snapshot_date: str) -> bool:
    return (snapshot_dir(root) / snapshot_date / COMPLETE_NAME).is_file()


def _earlier_day_dirs(base: Path, today: date) -> list[Path]:
    days = []
    for child in base.iterdir():
        try:
            child_day = date.fromisoformat(child.name)
        except ValueError:
            continue
        if child.is_dir() and child.name == child_day.isoformat() and child_day < today:
            days.append(child)
    return sorted(days)


def take_snapshot(
    root: Path,
    fetch: Fetch,
    *,
    now: Callable[[], datetime] = lambda: datetime.now(UTC),
    sources: Mapping[str, str] = SNAPSHOT_SOURCES,
) -> list[SnapshotOutcome]:
    """Write today's (UTC) snapshot of each source unless it already exists.

    Files left unrecorded by a crash on any earlier day are recorded first, so
    a crash is repaired by the next run whatever day it happens on. Only
    files whose outcome is not ``already_present`` are reported for earlier
    days.
    """
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
        for day_dir in _earlier_day_dirs(base, day):
            for source, url in sources.items():
                path = day_dir / f"{source}.json.gz"
                if not path.exists():
                    continue
                outcome = _reconcile_existing(
                    root, manifest, recorded, day_dir.name, source, url, path
                )
                if outcome.action != "already_present":
                    outcomes.append(outcome)
            # a crash after the last publish but before the marker; a day
            # that misses a source stays incomplete (it cannot be refetched)
            _mark_complete_if_ready(day_dir, recorded)
        # Fetch and validate every missing source before publishing any, and
        # check the clock after the last fetch: a run that crosses UTC
        # midnight writes nothing for the new day's sources.
        fetched: list[tuple[str, str, Path, bytes, datetime]] = []
        for source, url in sources.items():
            path = base / day.isoformat() / f"{source}.json.gz"
            if path.exists():
                outcomes.append(
                    _reconcile_existing(
                        root, manifest, recorded, day.isoformat(), source, url, path
                    )
                )
                continue
            resp = fetch(url)
            if resp.status != 200:
                raise SnapshotError(f"{source}: HTTP {resp.status}")
            _validate_payload(source, resp.body)
            fetched.append((source, url, path, resp.body, now().astimezone(UTC)))
        if fetched and now().astimezone(UTC).date() != day:
            raise SnapshotError(
                f"run started on {day} but its fetches finished on a later UTC "
                f"day; nothing was written, rerun to snapshot the new day"
            )
        for source, url, path, body, fetched_at in fetched:
            digest = hashlib.sha256(body).hexdigest()
            stored = _gzip(body)
            _publish(path, stored)
            _append_line(
                manifest,
                {
                    "schema_version": SCHEMA_VERSION,
                    "snapshot_date": day.isoformat(),
                    "source": source,
                    "url": url,
                    "path": path.relative_to(root).as_posix(),
                    "sha256": digest,
                    "bytes": len(body),
                    "stored_sha256": hashlib.sha256(stored).hexdigest(),
                    "fetched_at": _utc_iso(fetched_at),
                },
            )
            recorded.add((day.isoformat(), source, digest))
            outcomes.append(SnapshotOutcome(source, path, "written", digest))
        today_dir = base / day.isoformat()
        if today_dir.is_dir():
            _mark_complete_if_ready(today_dir, recorded)
        return outcomes


def latest_snapshot(root: Path, source: str) -> tuple[date, bytes, str]:
    """Most recent recorded snapshot of ``source`` (raw bytes, sha256).

    Only days with a ``COMPLETE.json`` marker count. The bytes on disk must
    match a manifest line for that day; otherwise the snapshot is refused.
    """
    base = snapshot_dir(root)
    entries = [
        e
        for e in _manifest_entries(base / MANIFEST_NAME)
        if e.get("source") == source
        and isinstance(e.get("snapshot_date"), str)
        and isinstance(e.get("path"), str)
        and isinstance(e.get("sha256"), str)
        and is_complete(root, e["snapshot_date"])
    ]
    if not entries:
        raise SnapshotError(
            f"no complete {source} snapshot under {base}; run the snapshot command"
        )
    newest = max(e["snapshot_date"] for e in entries)
    candidates = [e for e in entries if e["snapshot_date"] == newest]
    path = root / candidates[0]["path"]
    raw = _read_raw(path)
    digest = hashlib.sha256(raw).hexdigest()
    if not any(e["sha256"] == digest for e in candidates):
        raise SnapshotError(f"{path} does not match its manifest sha256")
    return date.fromisoformat(newest), raw, digest
