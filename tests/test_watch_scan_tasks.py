from __future__ import annotations

import pytest


class _FakeScanner:
    def __init__(self, result: dict[str, object]) -> None:
        self._result = result
        self.closed = False

    async def run(self) -> dict[str, object]:
        return self._result

    async def close(self) -> None:
        self.closed = True


@pytest.mark.asyncio
async def test_run_watch_scan_task_uses_scanner_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.watch_scan_tasks import (
        WATCH_ALERTS_LOCK_KEY,
        WATCH_ALERTS_LOCK_TTL_SECONDS,
        run_watch_scan_task,
    )

    scanner = _FakeScanner(result={"crypto": {"alerts_sent": 1}})
    lock_calls: list[tuple[str, int]] = []

    async def _run_with_task_lock(
        *,
        lock_key: str,
        ttl_seconds: int,
        coro_factory,
    ) -> dict[str, object]:
        lock_calls.append((lock_key, ttl_seconds))
        return await coro_factory()

    monkeypatch.setattr("app.tasks.watch_scan_tasks.WatchScanner", lambda: scanner)
    monkeypatch.setattr(
        "app.tasks.watch_scan_tasks.run_with_task_lock", _run_with_task_lock
    )

    result = await run_watch_scan_task()

    assert result == {"crypto": {"alerts_sent": 1}}
    assert lock_calls == [(WATCH_ALERTS_LOCK_KEY, WATCH_ALERTS_LOCK_TTL_SECONDS)]
    assert scanner.closed is True


@pytest.mark.asyncio
async def test_run_watch_scan_task_returns_skipped_on_lock_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.watch_scan_tasks import WATCH_ALERTS_LOCK_KEY, run_watch_scan_task

    def _build_scanner() -> _FakeScanner:
        raise AssertionError("scanner must not be created when lock is held")

    async def _lock_held(
        *, lock_key: str, ttl_seconds: int, coro_factory
    ) -> dict[str, str]:
        del ttl_seconds, coro_factory
        return {"status": "skipped", "reason": "lock_held", "lock_key": lock_key}

    monkeypatch.setattr("app.tasks.watch_scan_tasks.WatchScanner", _build_scanner)
    monkeypatch.setattr("app.tasks.watch_scan_tasks.run_with_task_lock", _lock_held)

    result = await run_watch_scan_task()

    assert result == {
        "status": "skipped",
        "reason": "lock_held",
        "lock_key": WATCH_ALERTS_LOCK_KEY,
    }
