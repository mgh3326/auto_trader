from __future__ import annotations

import argparse
import asyncio
import hashlib
import importlib.util
import json
import sys
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from sqlalchemy import select, text

from app.models.research_backtest import (
    ResearchBacktestRun,
    ResearchStrategyExperiment,
)


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


def _canonical_stdout(*, cv_score: float = 1.0) -> str:
    return (
        f"cv_score: {cv_score}\n"
        f"mean_score: {cv_score}\n"
        "std_score: 0.1\n"
        "min_fold_score: 0.8\n"
        "trial_sharpe: 0.5\n"
        "trial_p_value: 0.2\n"
        "trial_sample_size: 4\n"
    )


def _campaign_identity_payload(strategy_components: dict) -> dict:
    config = runner.FROZEN_CONFIG
    return {
        "strategy_key": "ROB-847",
        "strategy_version": "v1",
        **strategy_components,
        "dataset_manifest": {"bars": "fixture"},
        "universe": ["BTC", "ETH"],
        "pit": {"policy": "cutoff_required"},
        "frozen_config": config.to_dict(),
        "policy": config.policy_identity(),
        "benchmark": config.benchmark_identity(),
        "cost": config.cost_identity(),
        "mdd": config.mdd_identity(),
    }


def _registered(parameter_key: str = "params-sha256") -> SimpleNamespace:
    return SimpleNamespace(
        experiment_id="experiment-id",
        parameter_key=parameter_key,
        strategy_source_sha256="ab" * 32,
    )


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
    Case(
        0,
        _canonical_stdout(cv_score=2.0),
        False,
        1.0,
        "completed",
        0,
        False,
    ),
    Case(
        0,
        _canonical_stdout(cv_score=1.0),
        False,
        2.0,
        "rejected",
        1,
        True,
    ),
    Case(1, "", False, 2.0, "crashed", 2, True),
    Case(-1, "", True, 2.0, "timeout", 2, True),
)


def test_trial_runner_identifier_fits_persisted_varchar_contract() -> None:
    assert runner.TRIAL_RUNNER == "autoresearch"
    assert len(runner.TRIAL_RUNNER) <= 16


def test_strategy_identity_producer_hashes_and_executes_the_same_source(
    tmp_path: Path,
) -> None:
    strategy_path = tmp_path / "strategy.py"
    source = (
        "PARAMS = {'lookback': 7, 'enabled': True}\n"
        "class Strategy:\n"
        "    pass\n"
        "raise AssertionError('identity producer executed candidate code')\n"
    )
    strategy_path.write_text(source, encoding="utf-8")
    source_hash = hashlib.sha256(source.encode()).hexdigest()

    first = runner.derive_strategy_identity_components(
        strategy_path=strategy_path,
        source_label="backtest/strategy.py",
    )
    second = runner.derive_strategy_identity_components(
        strategy_path=strategy_path,
        source_label="backtest/strategy.py",
    )

    assert first == second
    assert first == {
        "strategy": {
            "schema_version": "autoresearch_strategy.v1",
            "entrypoint": "Strategy",
            "source_path": "backtest/strategy.py",
            "source_sha256": source_hash,
        },
        "code": {
            "schema_version": "python_source.v1",
            "path": "backtest/strategy.py",
            "sha256": source_hash,
        },
        "params": {"lookback": 7, "enabled": True},
    }


@pytest.mark.parametrize(
    ("component", "forged", "reason"),
    [
        ("strategy", {"caller": "description"}, "strategy_identity_mismatch"),
        ("code", {"sha256": "stale"}, "code_identity_mismatch"),
        ("params", {"lookback": 999}, "params_identity_mismatch"),
    ],
)
def test_registered_identity_rejects_forged_executable_components_before_db(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    component: str,
    forged: dict,
    reason: str,
) -> None:
    expected = {
        "strategy": {"canonical": "strategy"},
        "code": {"sha256": "actual"},
        "params": {"lookback": 7},
    }
    payload = _campaign_identity_payload(expected)
    payload[component] = forged
    identity_path = tmp_path / "identity.json"
    identity_path.write_text(json.dumps(payload), encoding="utf-8")
    monkeypatch.setattr(runner, "derive_strategy_identity_components", lambda: expected)

    async def forbidden_register(*args, **kwargs):
        raise AssertionError("forged identity must fail before DB registration")

    monkeypatch.setattr(runner, "register_experiment", forbidden_register)

    with pytest.raises(ValueError, match=reason):
        asyncio.run(runner.prepare_registered_experiment(str(identity_path)))


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
        return _registered()

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
        lambda *args, **kwargs: runner.BacktestInvocation(
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
    assert recorded[0]["parameter_key"] == "params-sha256"
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
        return _registered()

    async def record_terminal_trial(**kwargs):
        events.append(kwargs["status"])

    monkeypatch.setattr(
        runner, "prepare_registered_experiment", prepare_registered_experiment
    )
    monkeypatch.setattr(runner, "record_terminal_trial", record_terminal_trial)
    monkeypatch.setattr(
        runner,
        "run_backtest",
        lambda *args, **kwargs: runner.BacktestInvocation(
            returncode=0, stdout="garbled", timed_out=False
        ),
    )
    monkeypatch.setattr(runner, "git_revert", lambda: events.append("revert"))
    monkeypatch.setattr(runner, "append_result", lambda *args, **kwargs: None)

    assert runner.main() == 2
    assert events == ["crashed", "revert"]


def test_generated_invocation_id_is_independent_from_reused_tsv_exp_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    invocation_ids = iter(["invocation-a", "invocation-b"])
    recorded: list[dict] = []
    monkeypatch.setattr(
        runner,
        "parse_args",
        lambda: argparse.Namespace(
            description="fixture",
            identity_json="identity.json",
            information_cutoff="2026-01-01T00:00:00+00:00",
            idempotency_key=None,
        ),
    )
    monkeypatch.setattr(runner, "new_invocation_id", lambda: next(invocation_ids))
    monkeypatch.setattr(runner, "next_experiment_id", lambda: "exp1")
    monkeypatch.setattr(runner, "get_best_cv_score", lambda: 0.0)

    async def prepare_registered_experiment(path):
        return _registered()

    async def record_terminal_trial(**kwargs):
        recorded.append(kwargs)

    monkeypatch.setattr(
        runner, "prepare_registered_experiment", prepare_registered_experiment
    )
    monkeypatch.setattr(runner, "record_terminal_trial", record_terminal_trial)
    monkeypatch.setattr(
        runner,
        "run_backtest",
        lambda *args, **kwargs: runner.BacktestInvocation(
            returncode=0,
            stdout=_canonical_stdout(),
        ),
    )
    monkeypatch.setattr(runner, "append_result", lambda *args, **kwargs: None)

    assert runner.main() == 0
    assert runner.main() == 0
    assert [item["invocation_id"] for item in recorded] == [
        "invocation-a",
        "invocation-b",
    ]
    assert [item["idempotency_key"] for item in recorded] == [
        "experiment-id:invocation-a",
        "experiment-id:invocation-b",
    ]


@pytest.mark.parametrize("failing_step", ["record", "revert", "append"])
def test_failure_finalizer_records_once_before_attempting_all_local_cleanup(
    monkeypatch: pytest.MonkeyPatch,
    failing_step: str,
) -> None:
    events: list[str] = []
    monkeypatch.setattr(
        runner,
        "parse_args",
        lambda: argparse.Namespace(
            description="fixture",
            identity_json="identity.json",
            information_cutoff="2026-01-01T00:00:00+00:00",
            idempotency_key="logical-invocation",
        ),
    )
    monkeypatch.setattr(runner, "new_invocation_id", lambda: "generated-invocation")
    monkeypatch.setattr(runner, "next_experiment_id", lambda: "exp1")
    monkeypatch.setattr(runner, "get_best_cv_score", lambda: 1.0)

    async def prepare_registered_experiment(path):
        return _registered()

    async def record_terminal_trial(**kwargs):
        events.append("record")
        if failing_step == "record":
            raise RuntimeError("record failed")

    def revert():
        events.append("revert")
        if failing_step == "revert":
            raise RuntimeError("revert failed")

    def append(*args, **kwargs):
        events.append("append")
        if failing_step == "append":
            raise OSError("append failed")

    monkeypatch.setattr(
        runner, "prepare_registered_experiment", prepare_registered_experiment
    )
    monkeypatch.setattr(runner, "record_terminal_trial", record_terminal_trial)
    monkeypatch.setattr(
        runner,
        "run_backtest",
        lambda *args, **kwargs: runner.BacktestInvocation(returncode=1, stdout=""),
    )
    monkeypatch.setattr(runner, "git_revert", revert)
    monkeypatch.setattr(runner, "append_result", append)

    with pytest.raises((RuntimeError, OSError), match=f"{failing_step} failed"):
        runner.main()

    assert events == ["record", "revert", "append"]


@pytest.mark.parametrize(
    "failure_point",
    [
        "subprocess-launch",
        "run-log-write",
    ],
)
def test_post_registration_io_failure_records_crashed_once_before_revert(
    monkeypatch: pytest.MonkeyPatch,
    failure_point: str,
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
            idempotency_key="invocation-io-error",
        ),
    )
    monkeypatch.setattr(runner, "next_experiment_id", lambda: "exp1")
    monkeypatch.setattr(runner, "get_best_cv_score", lambda: 1.0)

    async def prepare_registered_experiment(path):
        events.append("registered")
        return _registered("experiment-identity")

    async def record_terminal_trial(**kwargs):
        events.append("record")
        recorded.append(kwargs)

    monkeypatch.setattr(
        runner, "prepare_registered_experiment", prepare_registered_experiment
    )
    monkeypatch.setattr(runner, "record_terminal_trial", record_terminal_trial)
    if failure_point == "subprocess-launch":

        def fail_subprocess(*args, **kwargs):
            raise FileNotFoundError("uv not found")

        monkeypatch.setattr(runner.subprocess, "run", fail_subprocess)
    else:

        class Result:
            returncode = 0
            stdout = "cv_score: 1\n"
            stderr = ""

        def fail_log_write(*args, **kwargs):
            raise OSError("run.log is not writable")

        monkeypatch.setattr(runner.subprocess, "run", lambda *a, **k: Result())
        monkeypatch.setattr(
            runner, "RUN_LOG", SimpleNamespace(write_text=fail_log_write)
        )
    monkeypatch.setattr(runner, "git_revert", lambda: events.append("revert"))
    monkeypatch.setattr(runner, "append_result", lambda *args, **kwargs: None)

    assert runner.main() == 2
    assert [item["status"] for item in recorded] == ["crashed"]
    assert recorded[0]["idempotency_key"] == "invocation-io-error"
    assert events == ["registered", "record", "revert"]


def test_post_registration_io_failure_records_before_failed_revert_propagates(
    monkeypatch: pytest.MonkeyPatch,
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
            idempotency_key="invocation-failed-revert",
        ),
    )
    monkeypatch.setattr(runner, "next_experiment_id", lambda: "exp1")
    monkeypatch.setattr(runner, "get_best_cv_score", lambda: 1.0)

    async def prepare_registered_experiment(path):
        return _registered("experiment-identity")

    async def record_terminal_trial(**kwargs):
        events.append("record")
        recorded.append(kwargs)

    def raise_invocation_error(*args, **kwargs):
        raise OSError("run.log is not writable")

    def fail_revert():
        events.append("revert")
        raise RuntimeError("git revert failed; canonical trial was recorded")

    monkeypatch.setattr(
        runner, "prepare_registered_experiment", prepare_registered_experiment
    )
    monkeypatch.setattr(runner, "record_terminal_trial", record_terminal_trial)
    monkeypatch.setattr(runner, "run_backtest", raise_invocation_error)
    monkeypatch.setattr(runner, "git_revert", fail_revert)
    monkeypatch.setattr(runner, "append_result", lambda *args, **kwargs: None)

    with pytest.raises(RuntimeError, match="git revert failed"):
        runner.main()

    assert [item["status"] for item in recorded] == ["crashed"]
    assert events == ["record", "revert"]


def test_pre_registration_error_does_not_record_or_revert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        runner,
        "parse_args",
        lambda: argparse.Namespace(
            description="fixture",
            identity_json="missing-identity.json",
            information_cutoff=None,
            idempotency_key="never-registered",
        ),
    )
    monkeypatch.setattr(runner, "next_experiment_id", lambda: "exp1")
    monkeypatch.setattr(runner, "get_best_cv_score", lambda: 1.0)

    async def prepare_registered_experiment(path):
        raise FileNotFoundError(path)

    def forbidden(*args, **kwargs):
        raise AssertionError(
            "pre-registration failures have no trial or revert boundary"
        )

    monkeypatch.setattr(
        runner, "prepare_registered_experiment", prepare_registered_experiment
    )
    monkeypatch.setattr(runner, "run_backtest", forbidden)
    monkeypatch.setattr(runner, "record_terminal_trial", forbidden)
    monkeypatch.setattr(runner, "git_revert", forbidden)

    with pytest.raises(FileNotFoundError, match="missing-identity.json"):
        runner.main()


def test_registered_runner_uses_one_event_loop_for_prepare_and_terminal_record(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    loops: list[int] = []
    monkeypatch.setattr(
        runner,
        "parse_args",
        lambda: argparse.Namespace(
            description="fixture",
            identity_json="identity.json",
            information_cutoff="2026-01-01T00:00:00+00:00",
            idempotency_key="invocation-loop",
        ),
    )
    monkeypatch.setattr(runner, "next_experiment_id", lambda: "exp1")
    monkeypatch.setattr(runner, "get_best_cv_score", lambda: 0.0)

    async def prepare_registered_experiment(path):
        loops.append(id(asyncio.get_running_loop()))
        return _registered()

    async def record_terminal_trial(**kwargs):
        loops.append(id(asyncio.get_running_loop()))

    monkeypatch.setattr(
        runner, "prepare_registered_experiment", prepare_registered_experiment
    )
    monkeypatch.setattr(runner, "record_terminal_trial", record_terminal_trial)
    monkeypatch.setattr(
        runner,
        "run_backtest",
        lambda *args, **kwargs: runner.BacktestInvocation(
            returncode=0,
            stdout=_canonical_stdout(),
        ),
    )
    monkeypatch.setattr(runner, "append_result", lambda *a, **k: None)

    assert runner.main() == 0
    assert len(loops) == 2
    assert loops[0] == loops[1]


@pytest.mark.integration
def test_registered_runner_reuses_real_pooled_engine_on_one_event_loop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.db import engine

    monkeypatch.setattr(
        runner,
        "parse_args",
        lambda: argparse.Namespace(
            description="fixture",
            identity_json="identity.json",
            information_cutoff="2026-01-01T00:00:00+00:00",
            idempotency_key="invocation-real-loop",
        ),
    )
    monkeypatch.setattr(runner, "next_experiment_id", lambda: "exp1")
    monkeypatch.setattr(runner, "get_best_cv_score", lambda: 0.0)

    async def prepare_registered_experiment(path):
        async with runner.AsyncSessionLocal() as session:
            assert await session.scalar(text("SELECT 1")) == 1
        return _registered()

    async def record_terminal_trial(**kwargs):
        async with runner.AsyncSessionLocal() as session:
            assert await session.scalar(text("SELECT 1")) == 1
        await engine.dispose()

    monkeypatch.setattr(
        runner, "prepare_registered_experiment", prepare_registered_experiment
    )
    monkeypatch.setattr(runner, "record_terminal_trial", record_terminal_trial)
    monkeypatch.setattr(
        runner,
        "run_backtest",
        lambda *args, **kwargs: runner.BacktestInvocation(
            returncode=0,
            stdout=_canonical_stdout(),
        ),
    )
    monkeypatch.setattr(runner, "append_result", lambda *a, **k: None)

    assert runner.main() == 0


def test_registered_metric_parser_requires_finite_canonical_trial_statistics() -> None:
    valid = runner.parse_metrics(
        _canonical_stdout(),
        require_trial_statistics=True,
    )
    assert valid is not None
    assert valid["trial_sharpe"] == pytest.approx(0.5)
    assert valid["trial_p_value"] == pytest.approx(0.2)
    assert valid["trial_sample_size"] == 4

    canonical = _canonical_stdout()
    for invalid in (
        "cv_score: 1\n",
        canonical.replace("trial_sharpe: 0.5", "trial_sharpe: nan"),
        canonical.replace("trial_p_value: 0.2", "trial_p_value: 2"),
        canonical.replace("trial_sample_size: 4", "trial_sample_size: 1"),
        canonical.replace("trial_sample_size: 4", "trial_sample_size: 4.9"),
    ):
        assert runner.parse_metrics(invalid, require_trial_statistics=True) is None


@pytest.mark.parametrize(
    "duplicate_line",
    [
        "cv_score: 99\n",
        "mean_score: 99\n",
        "std_score: 99\n",
        "min_fold_score: 99\n",
        "trial_sharpe: 99\n",
        "trial_p_value: 0.01\n",
        "trial_sample_size: 99\n",
    ],
)
def test_registered_metric_parser_rejects_duplicate_canonical_lines(
    duplicate_line: str,
) -> None:
    assert (
        runner.parse_metrics(
            duplicate_line + _canonical_stdout(),
            require_trial_statistics=True,
        )
        is None
    )


def test_registered_metric_parser_rejects_duplicate_full_blocks() -> None:
    assert (
        runner.parse_metrics(
            _canonical_stdout() + _canonical_stdout(cv_score=99.0),
            require_trial_statistics=True,
        )
        is None
    )


def test_frozen_backtest_subprocess_receives_cost_and_registered_strategy_hash(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[list[str]] = []

    class Result:
        returncode = 0
        stdout = ""
        stderr = ""

    monkeypatch.setattr(
        runner.subprocess,
        "run",
        lambda command, **kwargs: calls.append(command) or Result(),
    )
    monkeypatch.setattr(
        runner, "RUN_LOG", SimpleNamespace(write_text=lambda *a, **k: None)
    )

    runner.run_backtest(
        expected_strategy_sha256="ab" * 32,
        expected_params_sha256="cd" * 32,
    )

    assert calls == [
        [
            "uv",
            "run",
            "backtest/backtest.py",
            "--mode",
            "cv",
            "--fee-bps",
            "4.0",
            "--half-spread-bps",
            "0.0",
            "--slippage-bps",
            "2.0",
            "--expected-strategy-sha256",
            "ab" * 32,
            "--expected-params-sha256",
            "cd" * 32,
        ]
    ]


@pytest.mark.parametrize("status", ["completed", "rejected"])
def test_evaluated_terminal_payload_uses_canonical_trial_contract(status: str) -> None:
    payload = runner.build_terminal_payload(
        status=status,
        description="fixture",
        parameter_key="params-sha256",
        metrics={
            "cv_score": 1.0,
            "trial_sharpe": 0.5,
            "trial_p_value": 0.2,
            "trial_sample_size": 4,
        },
    )

    evidence = payload["trial_evidence"]
    assert evidence["schema_version"] == "honest_trial.v3"
    assert evidence["producer"] == "autoresearch"
    assert evidence["producer_version"] == "1"
    assert evidence["parameter_key"] == "params-sha256"
    assert evidence["config_hash"] == runner.FROZEN_CONFIG.config_hash()
    assert evidence["execution_cost"] == {
        "fee_bps": 4.0,
        "half_spread_bps": 0.0,
        "slippage_bps": 2.0,
    }
    assert evidence["sharpe"] == pytest.approx(0.5)
    assert evidence["p_value"] == pytest.approx(0.2)
    assert evidence["sample_size"] == 4
    assert evidence["validation_score"] == pytest.approx(1.0)
    assert evidence["selection_score_method"] == "canonical_cv_score"


@pytest.mark.parametrize("status", ["crashed", "timeout"])
def test_unevaluated_terminal_payload_marks_missing_statistics_explicitly(
    status: str,
) -> None:
    payload = runner.build_terminal_payload(
        status=status,
        description="fixture",
        parameter_key="params-sha256",
        metrics={},
    )

    assert payload == {
        "description": "fixture",
        "evaluation_failure": {
            "schema_version": "honest_trial_failure.v1",
            "parameter_key": "params-sha256",
            "status": status,
        },
    }


@pytest.mark.asyncio
async def test_direct_terminal_record_defaults_idempotency_to_invocation_id(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured = []

    class FakeSession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def commit(self):
            return None

    async def record_trial(session, *, experiment_id, request):
        captured.append(request)
        return SimpleNamespace(
            trial_status=request.status,
            raw_payload=request.raw_payload,
            information_cutoff=request.information_cutoff,
            run_id=request.run_id,
            runner=request.runner,
            timeframe=request.timeframe,
        )

    monkeypatch.setattr(runner, "AsyncSessionLocal", FakeSession)
    monkeypatch.setattr(runner, "record_trial", record_trial)

    await runner.record_terminal_trial(
        experiment_id="experiment-id",
        invocation_id="generated-invocation",
        status="crashed",
        description="failure",
        parameter_key="params-hash",
        information_cutoff="2026-01-01T00:00:00+00:00",
        idempotency_key=None,
        metrics={},
    )

    assert len(captured) == 1
    assert captured[0].idempotency_key == "experiment-id:generated-invocation"
    assert captured[0].run_id == "autoresearch-generated-invocation"


@pytest.mark.parametrize("field", ["run_id", "runner", "timeframe"])
def test_terminal_replay_rejects_execution_identity_mismatch(field: str) -> None:
    request = runner.BacktestTrialRequest(
        status="crashed",
        strategy_name="backtest.strategy",
        timeframe="1d",
        runner=runner.TRIAL_RUNNER,
        run_id="autoresearch-invocation",
        information_cutoff=datetime(2026, 1, 1, tzinfo=UTC),
        raw_payload={"failure": "same"},
    )
    row = SimpleNamespace(
        trial_status=request.status,
        raw_payload=request.raw_payload,
        information_cutoff=request.information_cutoff,
        run_id=request.run_id,
        runner=request.runner,
        timeframe=request.timeframe,
    )
    setattr(row, field, "different")

    with pytest.raises(runner.TerminalReplayMismatch, match=field):
        runner._assert_exact_terminal_replay(row, request)


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
        lambda *args, **kwargs: runner.BacktestInvocation(
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


def test_information_cutoff_preserves_utc_instant_for_timestamptz_column() -> None:
    assert runner._parse_information_cutoff("2026-01-01T09:00:00+09:00") == (
        runner.datetime(2026, 1, 1, tzinfo=runner.UTC)
    )


def test_information_cutoff_orm_column_is_timezone_aware() -> None:
    assert ResearchBacktestRun.__table__.c.information_cutoff.type.timezone is True
    assert ResearchBacktestRun.__table__.c.started_at.type.timezone is True
    assert ResearchBacktestRun.__table__.c.ended_at.type.timezone is True


@pytest.mark.integration
@pytest.mark.asyncio
async def test_record_terminal_trial_commits_all_statuses_with_stable_runner(
    db_session,
) -> None:
    config = runner.FROZEN_CONFIG
    unique = uuid.uuid4().hex
    identity = runner.StrategyExperimentIdentity(
        strategy_key=f"ROB-847-runner-{unique}",
        strategy_version="v1",
        strategy={"name": "fixture"},
        code={"sha": unique},
        params={"lookback": 2},
        dataset_manifest={"bars": "fixture"},
        universe=["BTC", "ETH"],
        pit={"policy": "cutoff_required"},
        frozen_config=config.to_dict(),
        policy=config.policy_identity(),
        benchmark=config.benchmark_identity(),
        cost=config.cost_identity(),
        mdd=config.mdd_identity(),
    )
    experiment = await runner.register_experiment(db_session, identity)
    await db_session.commit()
    evaluated_metrics = {
        "cv_score": 1.25,
        "trial_sharpe": 0.5,
        "trial_p_value": 0.2,
        "trial_sample_size": 4,
    }

    for index, status in enumerate(("completed", "rejected", "crashed", "timeout")):
        await runner.record_terminal_trial(
            experiment_id=experiment.experiment_id,
            invocation_id=f"{unique}-{index}",
            status=status,
            description=f"fixture-{status}",
            parameter_key=experiment.params_hash,
            information_cutoff="2026-01-01T09:00:00+09:00",
            idempotency_key=f"rob847-runner-{unique}-{status}",
            metrics=evaluated_metrics if status in {"completed", "rejected"} else {},
        )

    rows = (
        await db_session.scalars(
            select(ResearchBacktestRun)
            .where(ResearchBacktestRun.strategy_experiment_id == experiment.id)
            .order_by(ResearchBacktestRun.trial_index)
        )
    ).all()

    assert [row.trial_status for row in rows] == [
        "completed",
        "rejected",
        "crashed",
        "timeout",
    ]
    assert {row.runner for row in rows} == {"autoresearch"}
    assert all(len(row.runner) <= 16 for row in rows)
    assert all(
        row.information_cutoff == datetime(2026, 1, 1, tzinfo=UTC) for row in rows
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_terminal_retry_replays_only_exact_status_payload_and_cutoff(
    db_session,
) -> None:
    unique = uuid.uuid4().hex
    config = runner.FROZEN_CONFIG
    identity = runner.StrategyExperimentIdentity(
        strategy_key=f"ROB-847-replay-{unique}",
        strategy_version="v1",
        strategy={"name": "fixture"},
        code={"sha": unique},
        params={"lookback": 2},
        dataset_manifest={"bars": "fixture"},
        universe=["BTC", "ETH"],
        pit={"policy": "cutoff_required"},
        frozen_config=config.to_dict(),
        policy=config.policy_identity(),
        benchmark=config.benchmark_identity(),
        cost=config.cost_identity(),
        mdd=config.mdd_identity(),
    )
    experiment = await runner.register_experiment(db_session, identity)
    await db_session.commit()
    key = f"rob847-replay-{unique}"
    invocation_id = f"attempt-{unique}"
    common = {
        "experiment_id": experiment.experiment_id,
        "status": "crashed",
        "description": "same failure",
        "parameter_key": experiment.params_hash,
        "information_cutoff": "2026-01-01T00:00:00+00:00",
        "idempotency_key": key,
        "metrics": {},
    }

    await runner.record_terminal_trial(invocation_id=invocation_id, **common)
    await runner.record_terminal_trial(invocation_id=invocation_id, **common)

    mismatches = (
        ({"invocation_id": f"other-{unique}"}, "run_id"),
        ({"status": "timeout"}, "status"),
        ({"description": "different failure"}, "raw_payload"),
        ({"information_cutoff": "2026-01-02T00:00:00+00:00"}, "information_cutoff"),
    )
    for override, reason in mismatches:
        with pytest.raises(runner.TerminalReplayMismatch, match=reason):
            changed = dict(override)
            changed_invocation_id = changed.pop("invocation_id", invocation_id)
            await runner.record_terminal_trial(
                invocation_id=changed_invocation_id,
                **(common | changed),
            )

    rows = (
        await db_session.scalars(
            select(ResearchBacktestRun).where(
                ResearchBacktestRun.strategy_experiment_id == experiment.id,
                ResearchBacktestRun.trial_idempotency_key == key,
            )
        )
    ).all()
    assert len(rows) == 1
    assert rows[0].trial_status == "crashed"
    assert rows[0].raw_payload == runner.build_terminal_payload(
        status="crashed",
        description="same failure",
        parameter_key=experiment.params_hash,
        metrics={},
    )


@pytest.mark.integration
def test_post_registration_io_failure_is_durable_before_revert(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.core.db import engine

    unique = uuid.uuid4().hex
    config = runner.FROZEN_CONFIG
    strategy_key = f"ROB-847-io-failure-{unique}"
    identity_path = tmp_path / "identity.json"
    strategy_components = runner.derive_strategy_identity_components()
    identity_path.write_text(
        json.dumps(
            {
                "strategy_key": strategy_key,
                "strategy_version": "v1",
                **strategy_components,
                "dataset_manifest": {"bars": "fixture"},
                "universe": ["BTC", "ETH"],
                "pit": {"policy": "cutoff_required"},
                "frozen_config": config.to_dict(),
                "policy": config.policy_identity(),
                "benchmark": config.benchmark_identity(),
                "cost": config.cost_identity(),
                "mdd": config.mdd_identity(),
            }
        ),
        encoding="utf-8",
    )
    idempotency_key = f"rob847-io-failure-{unique}"
    events: list[str] = []

    monkeypatch.setattr(
        runner,
        "parse_args",
        lambda: argparse.Namespace(
            description="fixture",
            identity_json=str(identity_path),
            information_cutoff="2026-01-01T00:00:00+00:00",
            idempotency_key=idempotency_key,
        ),
    )
    monkeypatch.setattr(runner, "next_experiment_id", lambda: "exp1")
    monkeypatch.setattr(runner, "get_best_cv_score", lambda: 1.0)
    actual_record_terminal_trial = runner.record_terminal_trial

    async def observed_record_terminal_trial(**kwargs):
        await actual_record_terminal_trial(**kwargs)
        events.append("record")
        # runner.main() closes its event loop. Dispose connections on that same
        # loop so the verification query can open a fresh pooled connection.
        await engine.dispose()

    def fail_backtest_launch(*args, **kwargs):
        raise FileNotFoundError("uv not found")

    def observe_revert():
        events.append("revert")

    monkeypatch.setattr(runner, "record_terminal_trial", observed_record_terminal_trial)
    monkeypatch.setattr(runner.subprocess, "run", fail_backtest_launch)
    monkeypatch.setattr(runner, "git_revert", observe_revert)
    monkeypatch.setattr(runner, "append_result", lambda *args, **kwargs: None)

    assert runner.main() == 2

    async def load_persisted_rows():
        async with runner.AsyncSessionLocal() as session:
            experiment = await session.scalar(
                select(ResearchStrategyExperiment).where(
                    ResearchStrategyExperiment.strategy_key == strategy_key
                )
            )
            assert experiment is not None
            rows = (
                await session.scalars(
                    select(ResearchBacktestRun).where(
                        ResearchBacktestRun.strategy_experiment_id == experiment.id,
                        ResearchBacktestRun.trial_idempotency_key == idempotency_key,
                    )
                )
            ).all()
        await engine.dispose()
        return rows

    rows = asyncio.run(load_persisted_rows())
    assert events == ["record", "revert"]
    assert len(rows) == 1
    assert rows[0].trial_status == "crashed"


@pytest.mark.parametrize(
    ("field", "reason"),
    [
        ("policy", "policy_identity_mismatch"),
        ("mdd", "mdd_identity_mismatch"),
    ],
)
def test_registered_identity_rejects_false_policy_or_mdd_provenance(
    field: str, reason: str
) -> None:
    payload = _campaign_identity_payload(runner.derive_strategy_identity_components())
    payload[field] = {"false": "provenance"}
    identity = runner.StrategyExperimentIdentity.model_validate(payload)

    with pytest.raises(ValueError, match=reason):
        runner._validate_identity_provenance(identity)
