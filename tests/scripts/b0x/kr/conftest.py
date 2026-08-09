"""KR-lane test guards — no test may reach the real submission ledger.

Contract v1.6 ① makes ``review.kis_mock_order_ledger`` a cap input, which puts
a DB read on the KR cycle's hot path. The crypto lane already established the
pattern for exactly this hazard (``tests/scripts/b0x/test_cycle.py`` replaces
``fetch_ohlcv`` with an ``AssertionError`` autouse fixture): a test that forgets
to inject must **fail loudly**, not quietly open a session against whatever
``DATABASE_URL`` happens to be set.

It matters more than convenience here. ``read_own_pending`` converts *any*
failure into :class:`~scripts.b0x.broker_truth.PendingUnreadable`, so an
un-injected test would not error — it would silently take the fail-closed
branch and keep passing while proving nothing about the readable path.
"""

from __future__ import annotations

import datetime as dt

import pytest

from scripts.b0x.broker_truth import PendingUnreadable
from scripts.b0x.kr import pending_ledger as kr_pending_ledger


@pytest.fixture(autouse=True)
def _forbid_real_pending_ledger_reads(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _refuse(
        *, now: dt.datetime, correlation_prefix: str
    ) -> tuple[str, ...] | PendingUnreadable:
        raise AssertionError(
            "a KR test reached the real kis_mock ledger reader "
            f"(now={now.isoformat()} prefix={correlation_prefix!r}). Pass "
            "pending_reader=... explicitly — an accidental read would be "
            "swallowed into PendingUnreadable and prove nothing."
        )

    monkeypatch.setattr(kr_pending_ledger, "read_own_pending", _refuse)
