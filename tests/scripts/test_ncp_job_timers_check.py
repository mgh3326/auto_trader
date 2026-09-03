from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from scripts import ncp_job_timers_check as check


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
