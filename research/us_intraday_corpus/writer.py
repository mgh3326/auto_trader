"""Atomic, labelled, hashed-at-write parquet output.

One function is responsible for every parquet this corpus emits, so the three
§0 guarantees hold by construction rather than by reviewer vigilance:

* the survivorship label is stamped into file metadata (§0 pitfall 2)
* the digest is computed from the in-memory buffer, never by re-reading a
  sealed holdout file (§0 pitfall 1)
* the file lands via `.partial` -> fsync -> atomic rename, never a partial file
"""

from __future__ import annotations

import io
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import access_log, hashing, labels


@dataclass(frozen=True)
class WriteResult:
    path: Path
    sha256: str
    bytes_written: int
    rows: int


def write_parquet_atomic(
    table: Any, path: Path, *, compression: str = "zstd"
) -> WriteResult:
    """Serialise `table` to `path` atomically, returning its write-time digest.

    The table is serialised into memory first. That buffer is both what we hash
    and what we write, so the digest provably describes the shipped bytes
    without a read-back -- which is what lets a holdout file be checksummed
    while remaining unread.
    """
    import pyarrow.parquet as pq

    labelled = labels.attach_parquet_metadata(table)

    buffer = io.BytesIO()
    pq.write_table(labelled, buffer, compression=compression)
    payload = buffer.getvalue()

    digest = hashing.sha256_of_bytes(payload)

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".partial")

    # Record the write (holdout paths only) before touching the filesystem.
    if access_log.is_holdout_path(path):
        access_log.record("WRITE", path, note=f"sha256={digest} bytes={len(payload)}")

    with tmp.open("wb") as handle:
        handle.write(payload)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(tmp, path)

    # fsync the directory so the rename itself is durable.
    dir_fd = os.open(str(path.parent), os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)

    return WriteResult(
        path=path,
        sha256=digest,
        bytes_written=len(payload),
        rows=labelled.num_rows,
    )
