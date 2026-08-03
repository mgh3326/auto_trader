"""SHA-256 helpers with a hash-at-write discipline.

§0 pitfall 1 / 3 (the reason this module exists)
------------------------------------------------
`us-corpus-v1` computed its checksums by walking `rglob("*.parquet")` at
finalize time and reading every file back -- including the sealed holdout. The
manifest then claimed the holdout was never read.

The fix is to hash the bytes **while writing them**, so a holdout file's digest
is known without ever re-opening it. `sha256_of_bytes` is used on the buffer
that is about to be written; `sha256_of_file` is reserved for non-holdout paths
and refuses holdout paths outright.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import access_log

_CHUNK = 1024 * 1024


def sha256_of_bytes(payload: bytes) -> str:
    """Digest a buffer we are about to write. Never touches the filesystem."""
    return hashlib.sha256(payload).hexdigest()


def sha256_of_file(path: Path) -> str:
    """Digest a file by reading it.

    Refuses holdout paths: reading one would break the seal and falsify the
    `written_not_read` claim. Use the write-time digest instead.
    """
    if access_log.is_holdout_path(path):
        raise AssertionError(
            f"refusing to read holdout file for hashing: {path}. "
            "Holdout digests must come from sha256_of_bytes() at write time."
        )
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        while chunk := handle.read(_CHUNK):
            digest.update(chunk)
    return digest.hexdigest()
