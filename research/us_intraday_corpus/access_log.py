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


class HoldoutGuardViolation(RuntimeError):
    """Raised when something attempts to reach a sealed holdout path."""


def canonical(path: Path) -> Path:
    """Fully canonical form of `path` for guard comparisons.

    `os.path.abspath` was not enough. It normalises `..` but does **not**
    resolve symlinks and does **not** account for case-insensitive filesystems,
    so two forms slipped past the guard entirely:

        HOLDOUT/…            (uppercase, same directory on macOS/APFS)
        link -> holdout/…    (symlink pointing into the seal)

    `os.path.realpath` resolves symlink components and works on paths that do
    not exist yet, which matters because the guard also runs before a write.
    """
    return Path(os.path.realpath(str(path)))


def _holdout_roots() -> tuple[Path, ...]:
    return (config.HOLDOUT_DIR, config.SISTER_HOLDOUT_DIR)


# Identity of every sealed file as (st_dev, st_ino).
#
# Path canonicalisation alone is structurally incapable of catching a HARDLINK:
# a second directory entry outside holdout/ points at the same inode, and no
# amount of realpath/casefold work on the *name* can see that. Identity is
# therefore anchored on the inode, with the path check kept as a second layer
# (it still covers paths that do not exist yet, i.e. the pre-write guard).
_HOLDOUT_INODES: set[tuple[int, int]] = set()
_INODE_CACHE_KEY: tuple[str, ...] | None = None


def refresh_holdout_inodes() -> int:
    """Rebuild the sealed-inode set. Returns how many inodes are tracked."""
    global _INODE_CACHE_KEY
    _HOLDOUT_INODES.clear()
    for root in _holdout_roots():
        resolved = canonical(root)
        if not resolved.is_dir():
            continue
        for entry in resolved.rglob("*"):
            try:
                if entry.is_file():
                    st = entry.stat()
                    _HOLDOUT_INODES.add((st.st_dev, st.st_ino))
            except OSError:
                continue
    _INODE_CACHE_KEY = tuple(str(r) for r in _holdout_roots())
    return len(_HOLDOUT_INODES)


def register_holdout_inode(path: Path) -> None:
    """Track a freshly written sealed file so the guard sees it immediately."""
    try:
        st = Path(path).stat()
    except OSError:
        return
    _HOLDOUT_INODES.add((st.st_dev, st.st_ino))


def _inode_is_sealed(path: Path) -> bool:
    """True when `path` resolves to the same inode as a sealed file."""
    try:
        st = os.stat(str(path))  # follows symlinks
    except OSError:
        return False  # does not exist yet -> path layer decides
    key = (st.st_dev, st.st_ino)

    if _INODE_CACHE_KEY != tuple(str(r) for r in _holdout_roots()):
        refresh_holdout_inodes()
    if key in _HOLDOUT_INODES:
        return True

    # st_nlink > 1 means this inode has more than one directory entry, i.e.
    # exactly the hardlink case. Rebuild once before trusting a negative.
    if st.st_nlink > 1:
        refresh_holdout_inodes()
        return key in _HOLDOUT_INODES
    return False


def is_holdout_path(path: Path) -> bool:
    """True if `path` denotes a sealed holdout file, by identity or by name.

    Two independent layers, because each catches what the other cannot:

    * inode identity  -- catches hardlink aliases living outside holdout/
    * canonical path  -- catches paths that do not exist yet (pre-write guard),
                         and is symlink-resolved and case-insensitive

    Covers our own holdout and the sister corpus holdout, because §4 forbids
    reading either.
    """
    if _inode_is_sealed(path):
        return True

    resolved = canonical(path)
    parts_cf = [p.casefold() for p in resolved.parts]

    for holdout_root in _holdout_roots():
        root_parts = [p.casefold() for p in canonical(holdout_root).parts]
        if parts_cf[: len(root_parts)] == root_parts:
            return True

    # Any component literally named "holdout", in any case, is treated as
    # sealed. Deliberately broad: a false positive costs an explicit error,
    # a false negative silently breaks the seal.
    return "holdout" in parts_cf


def assert_not_holdout(path: Path, action: str = "access") -> None:
    """Raise if `path` is sealed. Never returns a filtered/None result.

    Callers must not turn this into a silent skip: a guard that quietly drops
    the offending path looks identical to one that was never reached.
    """
    if is_holdout_path(path):
        raise HoldoutGuardViolation(
            f"refusing to {action} sealed holdout path: {path} "
            f"(canonical: {canonical(path)})"
        )


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
