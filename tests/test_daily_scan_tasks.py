from __future__ import annotations

import pytest


class _FakeDailyScanner:
    def __init__(self, result: dict[str, object]) -> None:
        self._result = result
        self.closed = False

    async def run_strategy_scan(self) -> dict[str, object]:
        return self._result

    async def run_crash_detection(self) -> dict[str, object]:
        return self._result

    async def close(self) -> None:
        self.closed = True


class _FailingDailyScanner(_FakeDailyScanner):
    async def run_strategy_scan(self) -> dict[str, object]:
        raise RuntimeError("strategy failed")

    async def run_crash_detection(self) -> dict[str, object]:
        raise RuntimeError("crash failed")


def _passthrough_lock_runner(
    calls: list[tuple[str, int]],
):
    async def _run_with_task_lock(
        *,
        lock_key: str,
        ttl_seconds: int,
        coro_factory,
    ) -> dict[str, object]:
        calls.append((lock_key, ttl_seconds))
        return await coro_factory()

    return _run_with_task_lock


@pytest.mark.asyncio
async def test_run_strategy_scan_task_uses_telegram_only_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.daily_scan_tasks import (
        STRATEGY_SCAN_LOCK_KEY,
        STRATEGY_SCAN_LOCK_TTL_SECONDS,
        run_strategy_scan_task,
    )

    scanner = _FakeDailyScanner(result={"alerts_sent": 1})
    constructor_calls: list[str] = []
    lock_calls: list[tuple[str, int]] = []

    def _build_scanner(*, alert_mode: str = "both") -> _FakeDailyScanner:
        constructor_calls.append(alert_mode)
        return scanner

    monkeypatch.setattr("app.tasks.daily_scan_tasks.DailyScanner", _build_scanner)
    monkeypatch.setattr(
        "app.tasks.daily_scan_tasks.run_with_task_lock",
        _passthrough_lock_runner(lock_calls),
    )

    result = await run_strategy_scan_task()

    assert result == {"alerts_sent": 1}
    assert constructor_calls == ["telegram_only"]
    assert lock_calls == [(STRATEGY_SCAN_LOCK_KEY, STRATEGY_SCAN_LOCK_TTL_SECONDS)]
    assert scanner.closed is True


@pytest.mark.asyncio
async def test_run_crash_detection_task_uses_telegram_only_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.daily_scan_tasks import (
        CRASH_DETECTION_LOCK_KEY,
        CRASH_DETECTION_LOCK_TTL_SECONDS,
        run_crash_detection_task,
    )

    scanner = _FakeDailyScanner(result={"alerts_sent": 1})
    constructor_calls: list[str] = []
    lock_calls: list[tuple[str, int]] = []

    def _build_scanner(*, alert_mode: str = "both") -> _FakeDailyScanner:
        constructor_calls.append(alert_mode)
        return scanner

    monkeypatch.setattr("app.tasks.daily_scan_tasks.DailyScanner", _build_scanner)
    monkeypatch.setattr(
        "app.tasks.daily_scan_tasks.run_with_task_lock",
        _passthrough_lock_runner(lock_calls),
    )

    result = await run_crash_detection_task()

    assert result == {"alerts_sent": 1}
    assert constructor_calls == ["telegram_only"]
    assert lock_calls == [(CRASH_DETECTION_LOCK_KEY, CRASH_DETECTION_LOCK_TTL_SECONDS)]
    assert scanner.closed is True


@pytest.mark.asyncio
async def test_run_strategy_scan_task_closes_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.daily_scan_tasks import run_strategy_scan_task

    scanner = _FailingDailyScanner(result={})
    lock_calls: list[tuple[str, int]] = []

    def _build_scanner(*, alert_mode: str = "both") -> _FailingDailyScanner:
        assert alert_mode == "telegram_only"
        return scanner

    monkeypatch.setattr("app.tasks.daily_scan_tasks.DailyScanner", _build_scanner)
    monkeypatch.setattr(
        "app.tasks.daily_scan_tasks.run_with_task_lock",
        _passthrough_lock_runner(lock_calls),
    )

    with pytest.raises(RuntimeError, match="strategy failed"):
        await run_strategy_scan_task()

    assert len(lock_calls) == 1
    assert scanner.closed is True


@pytest.mark.asyncio
async def test_run_crash_detection_task_closes_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.daily_scan_tasks import run_crash_detection_task

    scanner = _FailingDailyScanner(result={})
    lock_calls: list[tuple[str, int]] = []

    def _build_scanner(*, alert_mode: str = "both") -> _FailingDailyScanner:
        assert alert_mode == "telegram_only"
        return scanner

    monkeypatch.setattr("app.tasks.daily_scan_tasks.DailyScanner", _build_scanner)
    monkeypatch.setattr(
        "app.tasks.daily_scan_tasks.run_with_task_lock",
        _passthrough_lock_runner(lock_calls),
    )

    with pytest.raises(RuntimeError, match="crash failed"):
        await run_crash_detection_task()

    assert len(lock_calls) == 1
    assert scanner.closed is True


@pytest.mark.asyncio
async def test_run_strategy_scan_task_returns_skipped_on_lock_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.daily_scan_tasks import (
        STRATEGY_SCAN_LOCK_KEY,
        run_strategy_scan_task,
    )

    def _build_scanner(*, alert_mode: str = "both") -> _FakeDailyScanner:
        del alert_mode
        raise AssertionError("scanner must not be created when lock is held")

    async def _lock_held(
        *, lock_key: str, ttl_seconds: int, coro_factory
    ) -> dict[str, str]:
        del ttl_seconds, coro_factory
        return {"status": "skipped", "reason": "lock_held", "lock_key": lock_key}

    monkeypatch.setattr("app.tasks.daily_scan_tasks.DailyScanner", _build_scanner)
    monkeypatch.setattr("app.tasks.daily_scan_tasks.run_with_task_lock", _lock_held)

    result = await run_strategy_scan_task()

    assert result == {
        "status": "skipped",
        "reason": "lock_held",
        "lock_key": STRATEGY_SCAN_LOCK_KEY,
    }


@pytest.mark.asyncio
async def test_run_crash_detection_task_returns_skipped_on_lock_contention(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.daily_scan_tasks import (
        CRASH_DETECTION_LOCK_KEY,
        run_crash_detection_task,
    )

    def _build_scanner(*, alert_mode: str = "both") -> _FakeDailyScanner:
        del alert_mode
        raise AssertionError("scanner must not be created when lock is held")

    async def _lock_held(
        *, lock_key: str, ttl_seconds: int, coro_factory
    ) -> dict[str, str]:
        del ttl_seconds, coro_factory
        return {"status": "skipped", "reason": "lock_held", "lock_key": lock_key}

    monkeypatch.setattr("app.tasks.daily_scan_tasks.DailyScanner", _build_scanner)
    monkeypatch.setattr("app.tasks.daily_scan_tasks.run_with_task_lock", _lock_held)

    result = await run_crash_detection_task()

    assert result == {
        "status": "skipped",
        "reason": "lock_held",
        "lock_key": CRASH_DETECTION_LOCK_KEY,
    }
