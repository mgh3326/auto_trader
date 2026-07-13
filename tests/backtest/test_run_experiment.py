from __future__ import annotations

import argparse
import importlib.util
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest


def _load_runner():
    path = Path(__file__).resolve().parents[2] / "backtest" / "run_experiment.py"
    spec = importlib.util.spec_from_file_location("run_experiment_test_module", path)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_runner()


@dataclass(frozen=True)
class Case:
    returncode: int
    stdout: str
    timed_out: bool
    best: float
    expected_status: str
    expected_exit: int
    reverts: bool


CASES = (
    Case(0, "cv_score: 2\nmean_score: 2\n", False, 1.0, "completed", 0, False),
    Case(0, "cv_score: 1\nmean_score: 1\n", False, 2.0, "rejected", 1, True),
    Case(1, "", False, 2.0, "crashed", 2, True),
    Case(-1, "", True, 2.0, "timeout", 2, True),
)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case.expected_status)
def test_registered_runner_records_each_terminal_status_once_before_revert(
    monkeypatch: pytest.MonkeyPatch, case: Case
) -> None:
    events: list[str] = []
    recorded: list[dict] = []
    monkeypatch.setattr(
        runner,
        "parse_args",
        lambda: argparse.Namespace(
            description="fixture",
            identity_json="identity.json",
            information_cutoff="2026-01-01T00:00:00+00:00",
            idempotency_key="invocation-1",
        ),
    )
    monkeypatch.setattr(runner, "next_experiment_id", lambda: "exp1")
    monkeypatch.setattr(runner, "get_best_cv_score", lambda: case.best)

    async def prepare_registered_experiment(path):
        return "experiment-id"

    async def record_terminal_trial(**kwargs):
        events.append("record")
        recorded.append(kwargs)

    monkeypatch.setattr(
        runner, "prepare_registered_experiment", prepare_registered_experiment
    )
    monkeypatch.setattr(runner, "record_terminal_trial", record_terminal_trial)
    monkeypatch.setattr(
        runner,
        "run_backtest",
        lambda: runner.BacktestInvocation(
            returncode=case.returncode,
            stdout=case.stdout,
            timed_out=case.timed_out,
        ),
    )
    monkeypatch.setattr(runner, "git_revert", lambda: events.append("revert"))
    monkeypatch.setattr(runner, "append_result", lambda *args, **kwargs: None)

    exit_code = runner.main()

    assert exit_code == case.expected_exit
    assert [item["status"] for item in recorded] == [case.expected_status]
    assert recorded[0]["idempotency_key"] == "invocation-1"
    assert events == (["record", "revert"] if case.reverts else ["record"])


def test_parse_failure_records_crashed_before_revert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        runner,
        "parse_args",
        lambda: argparse.Namespace(
            description="fixture",
            identity_json="identity.json",
            information_cutoff="2026-01-01T00:00:00+00:00",
            idempotency_key="invocation-parse",
        ),
    )
    monkeypatch.setattr(runner, "next_experiment_id", lambda: "exp1")
    monkeypatch.setattr(runner, "get_best_cv_score", lambda: 1.0)

    async def prepare_registered_experiment(path):
        return "experiment-id"

    async def record_terminal_trial(**kwargs):
        events.append(kwargs["status"])

    monkeypatch.setattr(
        runner, "prepare_registered_experiment", prepare_registered_experiment
    )
    monkeypatch.setattr(runner, "record_terminal_trial", record_terminal_trial)
    monkeypatch.setattr(
        runner,
        "run_backtest",
        lambda: runner.BacktestInvocation(
            returncode=0, stdout="garbled", timed_out=False
        ),
    )
    monkeypatch.setattr(runner, "git_revert", lambda: events.append("revert"))
    monkeypatch.setattr(runner, "append_result", lambda *args, **kwargs: None)

    assert runner.main() == 2
    assert events == ["crashed", "revert"]


def test_identityless_legacy_run_is_explicitly_non_promotable(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    monkeypatch.setattr(
        runner,
        "parse_args",
        lambda: argparse.Namespace(
            description="legacy",
            identity_json=None,
            information_cutoff=None,
            idempotency_key=None,
        ),
    )
    monkeypatch.setattr(runner, "next_experiment_id", lambda: "exp1")
    monkeypatch.setattr(runner, "get_best_cv_score", lambda: 0.0)

    async def prepare_registered_experiment(path):
        return None

    async def forbidden_record(**kwargs):
        raise AssertionError("identity-less runs cannot write canonical trials")

    monkeypatch.setattr(
        runner, "prepare_registered_experiment", prepare_registered_experiment
    )
    monkeypatch.setattr(runner, "record_terminal_trial", forbidden_record)
    monkeypatch.setattr(
        runner,
        "run_backtest",
        lambda: runner.BacktestInvocation(
            returncode=0, stdout="cv_score: 1\n", timed_out=False
        ),
    )
    monkeypatch.setattr(runner, "append_result", lambda *args, **kwargs: None)

    assert runner.main() == 0
    assert "missing_experiment_identity" in capsys.readouterr().out


def test_git_revert_uses_revert_commit_not_hard_reset(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0

    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or Result(),
    )

    runner.git_revert()

    assert calls == [["git", "revert", "--no-edit", "HEAD"]]
