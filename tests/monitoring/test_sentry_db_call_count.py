"""hk 6378: Sentry transaction ``db.call_count`` measurement.

Counts are keyed by the active Sentry transaction's ``(trace_id, span_id)``,
not by task context, so MCP tool bodies (session task) attribute to the
``/mcp`` transaction finalized in the request task, and statements with no
active transaction are attributed to nothing.
"""

from __future__ import annotations

import threading
from typing import Any

import pytest
import sentry_sdk

import app.monitoring.sentry as sentry_module
from app.monitoring import db_call_count


def _fire() -> None:
    db_call_count.before_cursor_execute(None, "SELECT 1", None, None, None, False)


def _finalize(transaction: Any, name: str = "GET /test") -> dict[str, Any] | None:
    return sentry_module._before_send_transaction(
        {
            "transaction": name,
            "spans": [],
            "contexts": {
                "trace": {
                    "trace_id": transaction.trace_id,
                    "span_id": transaction.span_id,
                }
            },
        },
        {},
    )


def _clear() -> None:
    with db_call_count._lock:
        db_call_count._counts.clear()


@pytest.mark.unit
class TestDbCallCountMeasurement:
    def test_three_statements_give_three(self) -> None:
        _clear()
        try:
            with sentry_sdk.start_transaction(name="t") as transaction:
                for _ in range(3):
                    _fire()
                event = _finalize(transaction)
            assert event is not None
            assert event["measurements"]["db.call_count"] == {"value": 3}
        finally:
            _clear()

    def test_tool_task_attributes_to_request_transaction(self) -> None:
        """MCP topology: increments in a fresh thread sharing the request's
        scopes (as the SDK's MCP integration re-applies them) land on the
        request transaction. Fails on the contextvar head."""
        _clear()
        try:
            with sentry_sdk.start_transaction(name="mcp-request") as transaction:
                isolation = sentry_sdk.get_isolation_scope()
                current = sentry_sdk.get_current_scope()

                def fire_in_tool_task(n: int) -> None:
                    with sentry_sdk.scope.use_isolation_scope(isolation):
                        with sentry_sdk.scope.use_scope(current):
                            for _ in range(n):
                                _fire()

                worker = threading.Thread(target=fire_in_tool_task, args=(2,))
                worker.start()
                worker.join()
                event = _finalize(transaction)
            assert event is not None
            assert event["measurements"]["db.call_count"] == {"value": 2}
        finally:
            _clear()

    def test_statements_before_transaction_are_not_attributed(self) -> None:
        """Sweep statements with no active transaction never spill into the
        next transaction in the same task (recovery path)."""
        _clear()
        try:
            _fire()
            _fire()
            with sentry_sdk.start_transaction(name="job") as transaction:
                _fire()
                event = _finalize(transaction)
            assert event is not None
            assert event["measurements"]["db.call_count"] == {"value": 1}
        finally:
            _clear()

    def test_unsampled_transaction_does_not_leak_into_next(self) -> None:
        """A transaction whose event never reaches finalize leaves a bounded
        entry that the next transaction never reads."""
        _clear()
        try:
            with sentry_sdk.start_transaction(name="dropped") as dropped:
                for _ in range(5):
                    _fire()
                dropped_trace = (dropped.trace_id, dropped.span_id)
            with sentry_sdk.start_transaction(name="sampled") as sampled:
                for _ in range(2):
                    _fire()
                event = _finalize(sampled)
            assert event is not None
            assert event["measurements"]["db.call_count"] == {"value": 2}
            with db_call_count._lock:
                assert len(db_call_count._counts) <= 1024
                assert dropped_trace in db_call_count._counts
        finally:
            _clear()

    def test_listener_failure_does_not_break_statement(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def _boom() -> None:
            raise RuntimeError("boom")

        monkeypatch.setattr(db_call_count, "_active_transaction_key", _boom)
        # Must not raise: the statement always proceeds.
        _fire()

    def test_no_transaction_means_no_measurement(self) -> None:
        _clear()
        try:
            _fire()
            event = sentry_module._before_send_transaction(
                {"transaction": "GET /test", "spans": []}, {}
            )
            assert event is not None
            assert "db.call_count" not in event.get("measurements", {})
        finally:
            _clear()

    def test_attach_never_raises_on_malformed_event(self) -> None:
        _clear()
        try:
            with sentry_sdk.start_transaction(name="t"):
                _fire()
                db_call_count.attach_db_call_count_measurement(
                    {"measurements": "not-a-dict"}  # type: ignore[dict-item]
                )
        finally:
            _clear()

    def test_real_engine_statement_counts_through_listener(self) -> None:
        """End to end through a real async engine: the registered
        ``before_cursor_execute`` fires with the SDK signature."""
        from sqlalchemy import event as sqlalchemy_event
        from sqlalchemy import text
        from sqlalchemy.ext.asyncio import create_async_engine

        _clear()
        engine = create_async_engine("sqlite+aiosqlite://")
        sqlalchemy_event.listen(
            engine.sync_engine,
            "before_cursor_execute",
            db_call_count.before_cursor_execute,
        )
        try:
            import asyncio

            async def _run() -> None:
                async with engine.connect() as conn:
                    await conn.execute(text("SELECT 1"))

            with sentry_sdk.start_transaction(name="t") as transaction:
                asyncio.run(_run())
                event = _finalize(transaction)
            assert event is not None
            assert event["measurements"]["db.call_count"] == {"value": 1}
        finally:
            _clear()
