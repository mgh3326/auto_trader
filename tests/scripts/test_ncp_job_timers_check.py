from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from scripts import ncp_job_timers_check as check


class _PrefectResponse:
    def __init__(self, paused: bool) -> None:
        self._payload = json.dumps(
            {
                "id": "00000000-0000-0000-0000-000000000001",
                "name": "stub",
                "paused": paused,
            }
        ).encode()

    def __enter__(self) -> _PrefectResponse:
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def read(self, *args: object, **kwargs: object) -> bytes:
        return self._payload


def _stub_cutover(monkeypatch: pytest.MonkeyPatch, *, paused: bool) -> list[str]:
    endpoints: list[str] = []

    def fake_urlopen(endpoint: str, *, timeout: int) -> _PrefectResponse:
        assert timeout == 10
        endpoints.append(endpoint)
        return _PrefectResponse(paused)

    def fake_run(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        assert args[:2] == ["systemctl", "is-enabled"]
        return subprocess.CompletedProcess(args, 0, "enabled\n", "")

    monkeypatch.setattr(check, "urlopen", fake_urlopen)
    monkeypatch.setattr(check.subprocess, "run", fake_run)
    monkeypatch.setenv("PREFECT_API_URL", "http://prefect.test/api")
    return endpoints


def test_all_checked_units_match_fake_prefect_run(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        check.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(args[0], 0, "", ""),
    )
    check.check_all(check_imports=True)


def test_argv_mutant_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    target = check.SYSTEMD_DIR / "job-toss-warnings-sync.service"
    original_read_text = Path.read_text

    def mutated(path: Path, *args: object, **kwargs: object) -> str:
        value = original_read_text(path, *args, **kwargs)
        return (
            value.replace("scripts.sync_toss_warnings", "scripts.wrong")
            if path == target
            else value
        )

    monkeypatch.setattr(Path, "read_text", mutated)
    with pytest.raises(ValueError, match="argv differs"):
        check.check_job(check.JOBS[1], check_imports=False)


def test_oncalendar_timezone_mutant_is_red(monkeypatch: pytest.MonkeyPatch) -> None:
    target = check.SYSTEMD_DIR / "job-toss-warnings-sync.timer"
    original_read_text = Path.read_text

    def mutated(path: Path, *args: object, **kwargs: object) -> str:
        value = original_read_text(path, *args, **kwargs)
        return value.replace("Asia/Seoul", "UTC") if path == target else value

    monkeypatch.setattr(Path, "read_text", mutated)
    with pytest.raises(ValueError, match="KST"):
        check.check_job(check.JOBS[1], check_imports=False)


def _assert_dual_active_is_rejected(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    endpoints = _stub_cutover(monkeypatch, paused=False)
    assert check.run_cli(["--skip-imports", "--check-cutover"]) == 1
    return endpoints


def test_cutover_dual_active_uses_prefect_named_deployment_api(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    endpoints = _assert_dual_active_is_rejected(monkeypatch)
    assert endpoints == [
        "http://prefect.test/api/deployments/name/KR%20Investor%20Flow%20Snapshots/daily-freshness",
        "http://prefect.test/api/deployments/name/Toss%20Warnings%20Sync/daily-preopen",
        "http://prefect.test/api/deployments/name/US%20Invest%20Screener%20Snapshots/post-us-close-freshness",
    ]
    assert "job-kr-investor-flow-snapshots.timer" in capsys.readouterr().err


def test_cutover_all_paused_is_green(monkeypatch: pytest.MonkeyPatch) -> None:
    endpoints = _stub_cutover(monkeypatch, paused=True)
    assert check.run_cli(["--skip-imports", "--check-cutover"]) == 0
    assert len(endpoints) == 3


def test_cutover_detector_removal_mutant_is_red(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _stub_cutover(monkeypatch, paused=False)
    monkeypatch.setattr(check, "dual_active_units", lambda _: [])
    with pytest.raises(AssertionError):
        assert check.run_cli(["--skip-imports", "--check-cutover"]) == 1
