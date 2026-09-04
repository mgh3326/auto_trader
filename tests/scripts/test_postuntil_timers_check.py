from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts/postuntil_timers_check.py"
SPEC = importlib.util.spec_from_file_location("postuntil_timers_check", SCRIPT)
assert SPEC and SPEC.loader
checker = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = checker
SPEC.loader.exec_module(checker)


def _job(tmp_path: Path, *, body: str = '{"market":"us"}') -> Path:
    toml_dir = tmp_path / "ops/ncp/postuntil"
    timer_dir = tmp_path / "ops/ncp/systemd"
    toml_dir.mkdir(parents=True)
    timer_dir.mkdir(parents=True)
    job = toml_dir / "build_invest_screener_snapshots.toml"
    job.write_text(
        f"# PrefectCron: 10 6 * * 2-6\n# PrefectParameters: {body}\nbody = '{body}'\n"
    )
    (timer_dir / "kick-build_invest_screener_snapshots.timer").write_text(
        "[Timer]\nOnCalendar=Tue..Sat *-*-* 06:10:00 Asia/Seoul\n"
    )
    return job


def test_check_pair_runs_dry_run_and_compares_next_five_occurrences(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job = _job(tmp_path)
    monkeypatch.setattr(checker, "TIMER_DIR", tmp_path / "ops/ncp/systemd")
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = "dry-run request preview\n"
        stderr = ""

    def fake_run(command: list[str], **_kwargs: object) -> Result:
        calls.append(command)
        return Result()

    monkeypatch.setattr(checker.subprocess, "run", fake_run)
    checker.check_pair(job, "postuntil")

    assert calls == [["postuntil", "run", "-f", str(job), "--dry-run"]]


def test_parameter_mutation_is_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job = _job(tmp_path, body='{"market":"us"}')
    job.write_text(
        job.read_text().replace(
            'body = \'{"market":"us"}\'', 'body = \'{"market":"kr"}\''
        )
    )
    monkeypatch.setattr(checker, "TIMER_DIR", tmp_path / "ops/ncp/systemd")

    with pytest.raises(ValueError, match="body differs"):
        checker.check_pair(job, "postuntil")


def test_timezone_offset_mutation_is_red(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    job = _job(tmp_path)
    timer = tmp_path / "ops/ncp/systemd/kick-build_invest_screener_snapshots.timer"
    timer.write_text("[Timer]\nOnCalendar=Tue..Sat *-*-* 15:10:00 Asia/Seoul\n")
    monkeypatch.setattr(checker, "TIMER_DIR", tmp_path / "ops/ncp/systemd")
    monkeypatch.setattr(
        checker.subprocess,
        "run",
        lambda *_args, **_kwargs: type(
            "Result", (), {"returncode": 0, "stdout": "preview", "stderr": ""}
        )(),
    )

    with pytest.raises(ValueError, match="OnCalendar differs"):
        checker.check_pair(job, "postuntil")
