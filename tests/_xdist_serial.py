"""Cross-worker module serialization for xdist-hostile test modules (ROB-968).

Some test modules cannot safely share the one CI Postgres with a concurrently
running sibling module: ``tests/infra/test_schema_barrier.py`` deliberately
hammers ``TRUNCATE`` (AccessExclusiveLock) on ``review.*`` tables, which
deadlocks against any other worker's multi-table DML on those tables
(observed: ``paper_cohort`` runner tests, runs 29643108579 / 29643559556).

Workers share one machine, so an OS file lock is enough: every module that
opts in via ``serial_module_fixture()`` runs mutually exclusively with all
other opted-in modules, while the rest of the suite is unaffected.
"""

from __future__ import annotations

import fcntl
import tempfile
from collections.abc import Iterator
from pathlib import Path

_LOCK_PATH = Path(tempfile.gettempdir()) / "auto_trader_xdist_review_tables.lock"


def hold_review_tables_lock() -> Iterator[None]:
    """Generator body for a module-scoped autouse fixture (see callers)."""
    with _LOCK_PATH.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
