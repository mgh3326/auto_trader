"""Append-only access log for holdout artifacts.

§0 pitfall 1 (the reason this module exists)
--------------------------------------------
The sister corpus `us-corpus-v1` wrote `"written_not_read": true` into its
manifest while its own finalize step was reading every holdout parquet back to
checksum it. The claim was false, and the access log could not catch it because
it only ever recorded WRITE.

This module records **every** open of a holdout path, READ included. The
`written_not_read` claim is then not an assertion a human types -- it is
*derived* from this log by `verify_written_not_read()`. If anything reads a
holdout file, the claim mechanically becomes False.
"""

from __future__ import annotations

import datetime as _dt
import os
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Literal

from . import config

Mode = Literal["READ", "WRITE"]


def _utc_now_iso() -> str:
    return _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def record(mode: Mode, path: Path, note: str = "") -> None:
    """Append one access record. Never raises on a missing parent directory."""
    config.ACCESS_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    line = f"{_utc_now_iso()}\t{mode}\t{path}"
    if note:
        line += f"\t{note}"
    with config.ACCESS_LOG_PATH.open("a", encoding="utf-8") as handle:
        handle.write(line + "\n")


def is_holdout_path(path: Path) -> bool:
    """True if `path` lives under *any* holdout directory we must not read.

    Covers both our own holdout and the sister corpus holdout, because §4
    forbids reading either one.
    """
    resolved = Path(os.path.abspath(str(path)))
    for holdout_root in (config.HOLDOUT_DIR, config.SISTER_HOLDOUT_DIR):
        root = Path(os.path.abspath(str(holdout_root)))
        if resolved == root or root in resolved.parents:
            return True
    return "holdout" in resolved.parts


@contextmanager
def guarded_open(path: Path, mode: str = "rb", **kwargs) -> Iterator[IO]:
    """Open `path`, recording the access when it is a holdout path.

    This is the ONLY sanctioned way to open a holdout file. Reads are recorded
    exactly like writes -- that is the whole point.
    """
    access: Mode = "WRITE" if any(c in mode for c in ("w", "a", "x", "+")) else "READ"
    if is_holdout_path(path):
        record(access, path)
    handle = path.open(mode, **kwargs)
    try:
        yield handle
    finally:
        handle.close()


def read_records() -> list[tuple[str, str, str]]:
    """Return (timestamp, mode, path) tuples from the access log."""
    if not config.ACCESS_LOG_PATH.exists():
        return []
    out: list[tuple[str, str, str]] = []
    with config.ACCESS_LOG_PATH.open("r", encoding="utf-8") as handle:
        for raw in handle:
            parts = raw.rstrip("\n").split("\t")
            if len(parts) >= 3:
                out.append((parts[0], parts[1], parts[2]))
    return out


def verify_written_not_read() -> bool:
    """Derive the `written_not_read` claim from the log instead of asserting it.

    Returns True only when the log contains zero READ records for holdout paths.
    `finalize` writes this derived value into the manifest verbatim, so the
    manifest cannot claim something the log contradicts.
    """
    return not any(
        mode == "READ" and is_holdout_path(Path(path))
        for _ts, mode, path in read_records()
    )
