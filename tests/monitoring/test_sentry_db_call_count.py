"""hk 6378: Sentry transaction ``db.call_count`` measurement.

Closed-contract tests:
1. three statements inside one transaction give ``db.call_count = 3`` and a
   concurrent transaction (separate task) keeps its own count;
2. the listener raising internally does not break the statement (fail-open);
3. no transaction -> no error and no measurement.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

import app.monitoring.sentry as sentry_module
from app.monitoring import db_call_count


def _fire() -> None:
    db_call_count.before_cursor_execute(None, None, "SELECT 1", None, None, False)


def _transaction_event(name: str = "GET /test") -> dict[str, Any]:
    return {"transaction": name, "spans": []}


@pytest.mark.unit
class TestDbCallCountMeasurement:
    def test_three_statements_give_three(self) -> None:
        db_call_count.reset_db_call_count()
        try:
            for _ in range(3):
                _fire()
            event = sentry_module._before_send_transaction(_transaction_event(), {})
            assert event is not None
            assert event["measurements"]["db.call_count"] == {"value": 3}
        finally:
            db_call_count.reset_db_call_count()

    def test_concurrent_transactions_keep_own_counts(self) -> None:
        async def _run() -> tuple[int, int]:
            async def worker(n: int) -> int:
                db_call_count.reset_db_call_count()
                for _ in range(n):
                    _fire()
                return db_call_count.get_db_call_count()

            first, second = await asyncio.gather(worker(3), worker(1))
            return first, second

        db_call_count.reset_db_call_count()
        try:
            assert asyncio.run(_run()) == (3, 1)
            assert db_call_count.get_db_call_count() == 0
        finally:
            db_call_count.reset_db_call_count()

    def test_count_resets_after_transaction_finish(self) -> None:
        db_call_count.reset_db_call_count()
        try:
            _fire()
            sentry_module._before_send_transaction(_transaction_event(), {})
            assert db_call_count.get_db_call_count() == 0
        finally:
            db_call_count.reset_db_call_count()

    def test_listener_failure_does_not_break_statement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(db_call_count, "_increment", _boom)
        # Must not raise: the statement always proceeds.
        _fire()

    def test_no_transaction_means_no_measurement(self) -> None:
        db_call_count.reset_db_call_count()
        event = sentry_module._before_send_transaction(_transaction_event(), {})
        assert event is not None
        measurements = event.get("measurements", {})
        assert "db.call_count" not in measurements

    def test_attach_never_raises_on_malformed_event(self) -> None:
        db_call_count.reset_db_call_count()
        try:
            for _ in range(2):
                _fire()
            db_call_count.attach_db_call_count_measurement(
                {"measurements": "not-a-dict"}  # type: ignore[dict-item]
            )
        finally:
            db_call_count.reset_db_call_count()
