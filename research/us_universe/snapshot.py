"""Manual command: write today's US universe snapshot (append-only).

    uv run python -m research.us_universe.snapshot --root <ARTIFACT_ROOT>

Layout under ``<root>/snapshots/``::

    <YYYY-MM-DD>/nasdaqlisted.txt   raw bytes as served, write-once
    <YYYY-MM-DD>/otherlisted.txt
    manifest.jsonl                  one line per snapshot day, append-only

The day key is the run date in America/New_York. The command is idempotent per
day: when the day's directory already holds both files, it fetches nothing,
writes nothing and exits 0 (after appending the manifest line if a crash
had left it missing). It never overwrites or deletes an existing file.

There is no scheduler: an operator (or a desk session) runs this by hand.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, date, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .pit import sha256_hex
from .store import (
    AlreadyExistsError,
    Fetcher,
    append_jsonl,
    code_commit,
    exclusive_lock,
    http_fetcher,
    read_jsonl,
    write_once,
)
from .symdir import SERIES, SOURCE_URLS, decode_body, parse_symbol_directory

NEW_YORK = ZoneInfo("America/New_York")
REPO_ROOT = Path(__file__).resolve().parents[2]
MANIFEST_NAME = "manifest.jsonl"
LOCK_NAME = ".snapshot.lock"


@dataclass(frozen=True)
class SnapshotResult:
    day: date
    action: str  # written | already_present | manifest_repaired
    directory: Path


def _today_new_york(now: datetime | None = None) -> date:
    return (now or datetime.now(UTC)).astimezone(NEW_YORK).date()


def _manifest_record(day: date, files: dict[str, bytes], commit: str) -> dict:
    entries = {}
    for series in SERIES:
        raw = files[series]
        obs = parse_symbol_directory(series, decode_body(raw))
        entries[series] = {
            "source_url": SOURCE_URLS[series],
            "sha256": sha256_hex(raw),
            "bytes": len(raw),
            "file_created_at": obs.created_at,
            "rows": len(obs.rows),
        }
    return {
        "schema": "us_universe_snapshot.v1",
        "day": day.isoformat(),
        "files": entries,
        "code_commit": commit,
    }


def take_snapshot(
    root: Path,
    *,
    fetch: Fetcher,
    today: Callable[[], date] = _today_new_york,
    commit: str | None = None,
) -> SnapshotResult:
    # One process at a time per artifact root: the existence checks, the
    # publish and the "is this day already in the manifest" check-and-append
    # must not interleave, or two runs on the same day append two records.
    with exclusive_lock(root / LOCK_NAME):
        return _take_snapshot_locked(root, fetch=fetch, today=today, commit=commit)


def _take_snapshot_locked(
    root: Path,
    *,
    fetch: Fetcher,
    today: Callable[[], date],
    commit: str | None,
) -> SnapshotResult:
    snap_root = root / "snapshots"
    day = today()
    day_dir = snap_root / day.isoformat()
    manifest = snap_root / MANIFEST_NAME
    paths = {s: day_dir / f"{s}.txt" for s in SERIES}
    recorded = {rec["day"] for rec in read_jsonl(manifest)}

    if all(p.exists() for p in paths.values()):
        if day.isoformat() in recorded:
            return SnapshotResult(day, "already_present", day_dir)
        files = {s: p.read_bytes() for s, p in paths.items()}
        record = _manifest_record(day, files, commit or code_commit(REPO_ROOT))
        append_jsonl(manifest, record)
        return SnapshotResult(day, "manifest_repaired", day_dir)

    # Fetch and validate both files before publishing either.
    files = {}
    for series in SERIES:
        if paths[series].exists():
            files[series] = paths[series].read_bytes()
        else:
            files[series] = fetch(SOURCE_URLS[series])
    # Validate before publishing: an unparseable body raises here, writing nothing.
    _manifest_record(day, files, "validation-only")
    for series in SERIES:
        if not paths[series].exists():
            try:
                write_once(paths[series], files[series])
            except AlreadyExistsError:
                pass  # a concurrent run published it first; keep theirs
    # The manifest describes what is on disk, not what this process fetched.
    on_disk = {s: p.read_bytes() for s, p in paths.items()}
    record = _manifest_record(day, on_disk, commit or code_commit(REPO_ROOT))
    if day.isoformat() not in {rec["day"] for rec in read_jsonl(manifest)}:
        append_jsonl(manifest, record)
    return SnapshotResult(day, "written", day_dir)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--root", type=Path, required=True, help="artifact root")
    args = parser.parse_args(argv)
    result = take_snapshot(args.root, fetch=http_fetcher())
    print(f"{result.action} {result.day.isoformat()} {result.directory}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
