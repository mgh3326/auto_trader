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
    from app.tasks.watch_scan_tasks import run_watch_scan_task

    scanner = _FakeScanner(result={"crypto": {"alerts_sent": 1}})
    monkeypatch.setattr("app.tasks.watch_scan_tasks.WatchScanner", lambda: scanner)

    result = await run_watch_scan_task()

    assert result == {"crypto": {"alerts_sent": 1}}
    assert scanner.closed is True
