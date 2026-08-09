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
from scripts.b0x.kr import cycle as kr_cycle
from scripts.b0x.kr import mock as kr_mock
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

    async def _refuse_foreign(
        *, now: dt.datetime, correlation_prefix: str
    ) -> kr_pending_ledger.ForeignLedgerTraces | PendingUnreadable:
        raise AssertionError(
            "a KR test reached the real foreign-trace ledger reader "
            f"(now={now.isoformat()} prefix={correlation_prefix!r}). Pass "
            "foreign_trace_reader=... explicitly — confirm-preflight tests "
            "must prove their contamination input."
        )

    monkeypatch.setattr(kr_pending_ledger, "read_foreign_traces", _refuse_foreign)


@pytest.fixture
def armed_confirm(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    """Arm only the confirm gates needed by an offline unit test.

    The production env/config and PostgreSQL advisory lease are deliberately
    not exercised in a unit test.  Tests request this fixture explicitly, so
    a confirm test cannot accidentally prove a route that skipped either gate.
    """

    lease_events: list[str] = []

    class _Lease:
        def __init__(self, *, writer_surface: str) -> None:
            assert writer_surface == "b0x_adapter"

        async def __aenter__(self) -> _Lease:
            lease_events.append("acquired")
            return self

        async def __aexit__(self, *exc: object) -> None:
            del exc
            lease_events.append("released")

    monkeypatch.setattr(kr_mock, "assert_kr_lane_enabled", lambda: None)
    monkeypatch.setattr(
        kr_mock,
        "account_identity_summary",
        lambda: {"fingerprint": "sha256:test-account", "product_suffix": "01"},
    )
    monkeypatch.setattr(kr_cycle, "KISMockWriterLease", _Lease)
    return lease_events
