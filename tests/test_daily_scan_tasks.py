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


@pytest.mark.asyncio
async def test_run_strategy_scan_task_uses_telegram_only_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.daily_scan_tasks import run_strategy_scan_task

    scanner = _FakeDailyScanner(result={"alerts_sent": 1})
    constructor_calls: list[str] = []

    def _build_scanner(*, alert_mode: str = "both") -> _FakeDailyScanner:
        constructor_calls.append(alert_mode)
        return scanner

    monkeypatch.setattr("app.tasks.daily_scan_tasks.DailyScanner", _build_scanner)

    result = await run_strategy_scan_task()

    assert result == {"alerts_sent": 1}
    assert constructor_calls == ["telegram_only"]
    assert scanner.closed is True


@pytest.mark.asyncio
async def test_run_crash_detection_task_uses_telegram_only_and_closes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.daily_scan_tasks import run_crash_detection_task

    scanner = _FakeDailyScanner(result={"alerts_sent": 1})
    constructor_calls: list[str] = []

    def _build_scanner(*, alert_mode: str = "both") -> _FakeDailyScanner:
        constructor_calls.append(alert_mode)
        return scanner

    monkeypatch.setattr("app.tasks.daily_scan_tasks.DailyScanner", _build_scanner)

    result = await run_crash_detection_task()

    assert result == {"alerts_sent": 1}
    assert constructor_calls == ["telegram_only"]
    assert scanner.closed is True


@pytest.mark.asyncio
async def test_run_strategy_scan_task_closes_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.daily_scan_tasks import run_strategy_scan_task

    scanner = _FailingDailyScanner(result={})

    def _build_scanner(*, alert_mode: str = "both") -> _FailingDailyScanner:
        assert alert_mode == "telegram_only"
        return scanner

    monkeypatch.setattr("app.tasks.daily_scan_tasks.DailyScanner", _build_scanner)

    with pytest.raises(RuntimeError, match="strategy failed"):
        await run_strategy_scan_task()

    assert scanner.closed is True


@pytest.mark.asyncio
async def test_run_crash_detection_task_closes_on_failure(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.tasks.daily_scan_tasks import run_crash_detection_task

    scanner = _FailingDailyScanner(result={})

    def _build_scanner(*, alert_mode: str = "both") -> _FailingDailyScanner:
        assert alert_mode == "telegram_only"
        return scanner

    monkeypatch.setattr("app.tasks.daily_scan_tasks.DailyScanner", _build_scanner)

    with pytest.raises(RuntimeError, match="crash failed"):
        await run_crash_detection_task()

    assert scanner.closed is True
