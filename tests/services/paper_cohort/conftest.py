"""Serialize paper_cohort test modules across xdist workers (ROB-968).

The runner test modules in this package exercise concurrent DML against the
same cohort/order-control tables. Under ``--dist=loadfile`` they are separate
files, so two xdist workers can run them simultaneously against the one shared
test Postgres — which produces nondeterministic ``DeadlockDetectedError`` /
``InFailedSQLTransactionError`` failures whenever shard composition happens to
co-schedule them (observed twice on run 29643108579 after the ROB-963
durations rebalance).

An OS-level file lock (workers share one machine) serializes these modules
against each other without touching event loops, the DB, or the dist mode.
Everything outside this package is unaffected.
"""

from __future__ import annotations

import fcntl
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

_LOCK_PATH = Path(tempfile.gettempdir()) / "auto_trader_paper_cohort_xdist.lock"


@pytest.fixture(scope="module", autouse=True)
def _serialize_paper_cohort_modules() -> Iterator[None]:
    with _LOCK_PATH.open("w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)
