"""Serialize paper_cohort test modules against xdist-hostile siblings (ROB-968).

The runner modules here issue multi-table DML on ``review.*`` /
cohort tables. ``tests/infra/test_schema_barrier.py`` concurrently hammers
``TRUNCATE`` (AccessExclusiveLock) on ``review.*`` from another worker, which
produced nondeterministic ``DeadlockDetectedError`` /
``InFailedSQLTransactionError`` in this package whenever shard composition
co-scheduled them (runs 29643108579 / 29643559556 after the ROB-963 durations
rebalance). Both sides take the same OS file lock; see ``tests/_xdist_serial``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from tests._xdist_serial import hold_review_tables_lock


@pytest.fixture(scope="module", autouse=True)
def _serialize_paper_cohort_modules() -> Iterator[None]:
    yield from hold_review_tables_lock()
