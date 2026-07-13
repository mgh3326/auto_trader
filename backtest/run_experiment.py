#!/usr/bin/env python3
"""run_experiment.py — single-iteration autoresearch experiment runner.

Usage:
    uv run backtest/run_experiment.py --description "RSI period 8"

Exit codes: 0 = improved (keep), 1 = worse (reverted), 2 = crashed
"""

from __future__ import annotations

import argparse
import ast
import asyncio
import hashlib
import json
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any
from uuid import uuid4

from app.core.db import AsyncSessionLocal
from app.schemas.research_backtest import (
    BacktestTrialRequest,
    StrategyExperimentIdentity,
)
from app.services.strategy_experiment_registry import (
    record_trial,
    register_experiment,
)
from research_contracts.canonical_hash import canonical_sha256
from research_contracts.frozen_config import FROZEN_CONFIG
from research_contracts.trial_evidence import build_trial_evidence

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_TSV = REPO_ROOT / "results.tsv"
RUN_LOG = REPO_ROOT / "run.log"
STRATEGY_PATH = REPO_ROOT / "backtest/strategy.py"
STRATEGY_SOURCE_LABEL = "backtest/strategy.py"
BACKTEST_TIMEOUT = 120  # seconds
TRIAL_RUNNER = "autoresearch"


@dataclass(frozen=True)
class BacktestInvocation:
    returncode: int
    stdout: str
    timed_out: bool = False


@dataclass(frozen=True)
class RegisteredExperiment:
    experiment_id: str
    parameter_key: str
    strategy_source_sha256: str


class TerminalReplayMismatch(RuntimeError):
    """An idempotency key resolved to different immutable terminal evidence."""


def new_invocation_id() -> str:
    """Generate a run identity independent of the mutable results.tsv counter."""
    return uuid4().hex


def derive_strategy_identity_components(
    *,
    strategy_path: Path = STRATEGY_PATH,
    source_label: str = STRATEGY_SOURCE_LABEL,
) -> dict[str, Any]:
    """Derive canonical identity from the exact source bytes that define PARAMS.

    Registration parses but never imports candidate strategy code. The sole
    top-level ``PARAMS`` assignment must be a side-effect-free Python literal;
    the child backtest process separately verifies and executes these same
    hashed bytes.
    """
    source = strategy_path.read_bytes()
    source_hash = hashlib.sha256(source).hexdigest()
    tree = ast.parse(source, filename=str(strategy_path))
    if not any(
        isinstance(node, ast.ClassDef) and node.name == "Strategy" for node in tree.body
    ):
        raise ValueError("strategy.py must define top-level class Strategy")

    params_values: list[ast.expr] = []
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == "PARAMS"
        ):
            params_values.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "PARAMS"
            and node.value is not None
        ):
            params_values.append(node.value)
    if len(params_values) != 1:
        raise ValueError("strategy.py must define exactly one top-level PARAMS literal")
    try:
        params = ast.literal_eval(params_values[0])
    except (ValueError, TypeError) as exc:
        raise ValueError(
            "strategy.py PARAMS must be a side-effect-free literal"
        ) from exc
    if not isinstance(params, dict):
        raise ValueError("strategy.py PARAMS literal must be a dict")
    # Validate the parsed value at this producer boundary without running any
    # caller-controlled candidate code in the registration process.
    canonical_sha256(params)
    return {
        "strategy": {
            "schema_version": "autoresearch_strategy.v1",
            "entrypoint": "Strategy",
            "source_path": source_label,
            "source_sha256": source_hash,
        },
        "code": {
            "schema_version": "python_source.v1",
            "path": source_label,
            "sha256": source_hash,
        },
        "params": params,
    }


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run one autoresearch experiment")
    p.add_argument(
        "--description", required=True, help="One-line experiment description"
    )
    p.add_argument(
        "--identity-json",
        help="ROB-846 StrategyExperimentIdentity JSON; omitted means non-promotable legacy mode",
    )
    p.add_argument(
        "--information-cutoff",
        help="Timezone-aware ISO-8601 cutoff recorded on the canonical trial",
    )
    p.add_argument(
        "--idempotency-key",
        help="Stable logical invocation key; defaults to experiment_id + generated UUID",
    )
    return p.parse_args()


def next_experiment_id() -> str:
    """Determine next exp number from results.tsv."""
    if not RESULTS_TSV.exists():
        return "exp1"
    max_n = 0
    for line in RESULTS_TSV.read_text().splitlines()[1:]:  # skip header
        parts = line.split("\t")
        if not parts:
            continue
        m = re.match(r"exp(\d+)", parts[0])
        if m:
            max_n = max(max_n, int(m.group(1)))
    return f"exp{max_n + 1}"


def get_best_cv_score() -> float:
    """Read best cv_score from results.tsv (only 'keep'/'kept' rows)."""
    if not RESULTS_TSV.exists():
        return float("-inf")
    best = float("-inf")
    for line in RESULTS_TSV.read_text().splitlines()[1:]:
        parts = line.split("\t")
        if len(parts) < 7:
            continue
        status = parts[6]  # index 6 = status column
        if status not in ("keep", "kept"):
            continue
        try:
            score = float(parts[1])
            best = max(best, score)
        except (ValueError, IndexError):
            continue
    return best


def run_backtest(
    expected_strategy_sha256: str | None = None,
    expected_params_sha256: str | None = None,
) -> BacktestInvocation:
    """Run CV backtest and distinguish timeout from process failure."""
    command = [
        "uv",
        "run",
        "backtest/backtest.py",
        "--mode",
        "cv",
        "--fee-bps",
        str(FROZEN_CONFIG.taker_bps),
        "--half-spread-bps",
        str(FROZEN_CONFIG.half_spread_bps),
        "--slippage-bps",
        str(FROZEN_CONFIG.slippage_bps),
    ]
    if expected_strategy_sha256 is not None:
        command.extend(["--expected-strategy-sha256", expected_strategy_sha256])
    if expected_params_sha256 is not None:
        command.extend(["--expected-params-sha256", expected_params_sha256])
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=BACKTEST_TIMEOUT,
            cwd=REPO_ROOT,
        )
        # Write combined output to run.log
        log_content = result.stdout + "\n" + result.stderr
        RUN_LOG.write_text(log_content)
        return BacktestInvocation(result.returncode, result.stdout)
    except subprocess.TimeoutExpired:
        RUN_LOG.write_text("TIMEOUT: backtest exceeded 120 seconds\n")
        return BacktestInvocation(-1, "", timed_out=True)


def parse_metrics(
    stdout: str, *, require_trial_statistics: bool = False
) -> dict[str, float | int] | None:
    """Parse finite CV metrics and, for registered runs, canonical trial stats."""
    patterns = {
        "cv_score": r"^cv_score:\s+(\S+)",
        "mean_score": r"^mean_score:\s+(\S+)",
        "std_score": r"^std_score:\s+(\S+)",
        "min_fold_score": r"^min_fold_score:\s+(\S+)",
        "trial_sharpe": r"^trial_sharpe:\s+(\S+)",
        "trial_p_value": r"^trial_p_value:\s+(\S+)",
        "trial_sample_size": r"^trial_sample_size:\s+(\S+)",
    }
    metrics: dict[str, float | int] = {}
    for key, pattern in patterns.items():
        matches = re.findall(pattern, stdout, re.MULTILINE)
        if len(matches) > 1:
            return None
        if matches:
            try:
                value = float(matches[0])
            except ValueError:
                return None
            if not math.isfinite(value):
                return None
            if key == "trial_sample_size" and not value.is_integer():
                return None
            metrics[key] = int(value) if key == "trial_sample_size" else value
    if "cv_score" not in metrics:
        return None
    if require_trial_statistics:
        required = set(patterns)
        if not required <= set(metrics):
            return None
        p_value = float(metrics["trial_p_value"])
        sample_size = int(metrics["trial_sample_size"])
        if not 0 <= p_value <= 1 or sample_size < FROZEN_CONFIG.trial_min_folds:
            return None
    return metrics


def git_revert() -> None:
    """Revert the experiment commit without discarding uncommitted work."""
    result = subprocess.run(
        ["git", "revert", "--no-edit", "HEAD"],
        cwd=REPO_ROOT,
        capture_output=True,
    )
    if result.returncode != 0:
        raise RuntimeError("git revert failed; canonical trial was recorded")


async def prepare_registered_experiment(
    identity_json: str | None,
) -> RegisteredExperiment | None:
    """Register canonical identity before running, or enter explicit legacy mode."""
    if identity_json is None:
        return None
    payload = json.loads(Path(identity_json).read_text(encoding="utf-8"))
    identity = StrategyExperimentIdentity.model_validate(payload)
    executable = _validate_identity_provenance(identity)
    async with AsyncSessionLocal() as session:
        experiment = await register_experiment(session, identity)
        await session.commit()
        return RegisteredExperiment(
            experiment_id=experiment.experiment_id,
            parameter_key=experiment.params_hash,
            strategy_source_sha256=executable["code"]["sha256"],
        )


def _validate_identity_provenance(
    identity: StrategyExperimentIdentity,
) -> dict[str, Any]:
    """Bind every campaign-owned ROB-846 identity component before registration."""
    executable = derive_strategy_identity_components()
    for component in ("strategy", "code", "params"):
        if canonical_sha256(getattr(identity, component)) != canonical_sha256(
            executable[component]
        ):
            raise ValueError(f"{component}_identity_mismatch")
    if canonical_sha256(identity.frozen_config) != FROZEN_CONFIG.config_hash():
        raise ValueError("frozen_config_hash_mismatch")
    if canonical_sha256(identity.policy) != canonical_sha256(
        FROZEN_CONFIG.policy_identity()
    ):
        raise ValueError("policy_identity_mismatch")
    if canonical_sha256(identity.benchmark) != canonical_sha256(
        FROZEN_CONFIG.benchmark_identity()
    ):
        raise ValueError("benchmark_identity_mismatch")
    if canonical_sha256(identity.cost) != canonical_sha256(
        FROZEN_CONFIG.cost_identity()
    ):
        raise ValueError("cost_identity_mismatch")
    if canonical_sha256(identity.mdd) != canonical_sha256(FROZEN_CONFIG.mdd_identity()):
        raise ValueError("mdd_identity_mismatch")
    return executable


def _parse_information_cutoff(value: str | None) -> datetime | None:
    if value is None:
        return None
    cutoff = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        raise ValueError("information_cutoff must be timezone-aware")
    # The ROB-846 column is TIMESTAMP WITH TIME ZONE. Normalise the presentation
    # to UTC while preserving the aware instant expected by asyncpg.
    return cutoff.astimezone(UTC)


def _assert_exact_terminal_replay(row: Any, request: BacktestTrialRequest) -> None:
    """Fail closed when an idempotent replay changes immutable terminal truth."""
    mismatches: list[str] = []
    if row.trial_status != request.status:
        mismatches.append("status")
    if canonical_sha256(row.raw_payload) != canonical_sha256(request.raw_payload):
        mismatches.append("raw_payload")
    if row.information_cutoff != request.information_cutoff:
        mismatches.append("information_cutoff")
    for field in ("run_id", "runner", "timeframe"):
        if getattr(row, field) != getattr(request, field):
            mismatches.append(field)
    if mismatches:
        raise TerminalReplayMismatch(
            "idempotent terminal replay mismatch: " + ", ".join(mismatches)
        )


async def record_terminal_trial(
    *,
    experiment_id: str,
    invocation_id: str,
    status: str,
    description: str,
    parameter_key: str,
    information_cutoff: str | None,
    idempotency_key: str | None,
    metrics: dict[str, float | int],
) -> None:
    """Append exactly one terminal ROB-846 trial and commit it durably."""
    raw_payload = build_terminal_payload(
        status=status,
        description=description,
        parameter_key=parameter_key,
        metrics=metrics,
    )
    request = BacktestTrialRequest(
        status=status,
        strategy_name="backtest.strategy",
        timeframe="1d",
        runner=TRIAL_RUNNER,
        run_id=f"autoresearch-{invocation_id}",
        information_cutoff=_parse_information_cutoff(information_cutoff),
        idempotency_key=idempotency_key or f"{experiment_id}:{invocation_id}",
        ended_at=datetime.now(UTC),
        profit_factor=Decimal("0"),
        max_drawdown=Decimal("0"),
        raw_payload=raw_payload,
    )
    async with AsyncSessionLocal() as session:
        row = await record_trial(session, experiment_id=experiment_id, request=request)
        _assert_exact_terminal_replay(row, request)
        await session.commit()


def build_terminal_payload(
    *,
    status: str,
    description: str,
    parameter_key: str,
    metrics: dict[str, float | int],
) -> dict:
    """Build evaluated evidence, or an explicit non-statistical failure record."""
    if status in {"completed", "rejected"}:
        return {
            "description": description,
            "trial_evidence": build_trial_evidence(
                parameter_key=parameter_key,
                config_hash=FROZEN_CONFIG.config_hash(),
                execution_cost={
                    "fee_bps": FROZEN_CONFIG.taker_bps,
                    "half_spread_bps": FROZEN_CONFIG.half_spread_bps,
                    "slippage_bps": FROZEN_CONFIG.slippage_bps,
                },
                sharpe=float(metrics["trial_sharpe"]),
                p_value=float(metrics["trial_p_value"]),
                sample_size=int(metrics["trial_sample_size"]),
                validation_score=float(metrics["cv_score"]),
            ),
        }
    return {
        "description": description,
        "evaluation_failure": {
            "schema_version": "honest_trial_failure.v1",
            "parameter_key": parameter_key,
            "status": status,
        },
    }


async def _record_if_registered(
    *,
    registered: RegisteredExperiment | None,
    invocation_id: str,
    status: str,
    args: argparse.Namespace,
    metrics: dict[str, float | int],
) -> None:
    if registered is None:
        return
    await record_terminal_trial(
        experiment_id=registered.experiment_id,
        invocation_id=invocation_id,
        status=status,
        description=args.description,
        parameter_key=registered.parameter_key,
        information_cutoff=args.information_cutoff,
        idempotency_key=(
            args.idempotency_key or f"{registered.experiment_id}:{invocation_id}"
        ),
        metrics=metrics,
    )


def append_result(
    exp_id: str,
    cv_score: float,
    mean: float,
    std: float,
    min_fold: float,
    status: str,
    description: str,
) -> None:
    """Append a row to results.tsv (8-column format, test_score=NA)."""
    row = f"{exp_id}\t{cv_score:.6f}\t{mean:.6f}\t{std:.6f}\t{min_fold:.6f}\tNA\t{status}\t{description}\n"
    with open(RESULTS_TSV, "a") as f:
        f.write(row)


async def _finalize_terminal_with_revert(
    *,
    registered: RegisteredExperiment | None,
    invocation_id: str,
    exp_id: str,
    status: str,
    args: argparse.Namespace,
    metrics: dict[str, float | int],
    cv_score: float,
    mean: float,
    std: float,
    min_fold: float,
    result_status: str,
    exit_code: int,
) -> int:
    """Commit terminal truth, then independently attempt revert and TSV audit."""
    finalization_errors: list[Exception] = []
    try:
        await _record_if_registered(
            registered=registered,
            invocation_id=invocation_id,
            status=status,
            args=args,
            metrics=metrics,
        )
    except Exception as exc:
        finalization_errors.append(exc)
    try:
        git_revert()
    except Exception as exc:
        finalization_errors.append(exc)
    try:
        append_result(
            exp_id,
            cv_score,
            mean,
            std,
            min_fold,
            result_status,
            args.description,
        )
    except Exception as exc:
        finalization_errors.append(exc)

    if len(finalization_errors) == 1:
        raise finalization_errors[0]
    if finalization_errors:
        raise ExceptionGroup("terminal finalization failures", finalization_errors)
    print("Reverted.")
    return exit_code


async def _main() -> int:
    args = parse_args()
    invocation_id = new_invocation_id()
    exp_id = next_experiment_id()
    best = get_best_cv_score()
    registered = await prepare_registered_experiment(args.identity_json)
    if registered is None:
        print("NON_PROMOTABLE: missing_experiment_identity")

    print(f"=== {exp_id}: {args.description} ===")
    print(f"Current best cv_score: {best:.6f}")
    print("Running CV backtest...")

    try:
        expected_strategy_sha256 = (
            registered.strategy_source_sha256 if registered is not None else None
        )
        invocation = await asyncio.to_thread(
            run_backtest,
            expected_strategy_sha256,
            registered.parameter_key if registered is not None else None,
        )
    except OSError as exc:
        print(f"CRASHED ({type(exc).__name__}). Backtest did not complete safely")
        return await _finalize_terminal_with_revert(
            registered=registered,
            invocation_id=invocation_id,
            exp_id=exp_id,
            status="crashed",
            args=args,
            metrics={},
            cv_score=0.0,
            mean=0.0,
            std=0.0,
            min_fold=0.0,
            result_status="crashed",
            exit_code=2,
        )

    if invocation.returncode != 0:
        status = "timeout" if invocation.timed_out else "crashed"
        print(f"{status.upper()} (exit code {invocation.returncode}). See run.log")
        return await _finalize_terminal_with_revert(
            registered=registered,
            invocation_id=invocation_id,
            exp_id=exp_id,
            status=status,
            args=args,
            metrics={},
            cv_score=0.0,
            mean=0.0,
            std=0.0,
            min_fold=0.0,
            result_status=status,
            exit_code=2,
        )

    metrics = parse_metrics(
        invocation.stdout,
        require_trial_statistics=registered is not None,
    )
    if metrics is None:
        print("FAILED to parse cv_score from output. See run.log")
        return await _finalize_terminal_with_revert(
            registered=registered,
            invocation_id=invocation_id,
            exp_id=exp_id,
            status="crashed",
            args=args,
            metrics={},
            cv_score=0.0,
            mean=0.0,
            std=0.0,
            min_fold=0.0,
            result_status="crashed",
            exit_code=2,
        )

    cv = metrics["cv_score"]
    mean = metrics.get("mean_score", 0.0)
    std = metrics.get("std_score", 0.0)
    min_fold = metrics.get("min_fold_score", 0.0)

    print(f"cv_score: {cv:.6f}  (best: {best:.6f})")

    if cv > best:
        print(f"IMPROVED by {cv - best:.6f} — keeping.")
        await _record_if_registered(
            registered=registered,
            invocation_id=invocation_id,
            status="completed",
            args=args,
            metrics=metrics,
        )
        append_result(exp_id, cv, mean, std, min_fold, "keep", args.description)
        return 0
    else:
        print(f"No improvement ({cv:.6f} <= {best:.6f}) — reverting.")
        return await _finalize_terminal_with_revert(
            registered=registered,
            invocation_id=invocation_id,
            exp_id=exp_id,
            status="rejected",
            args=args,
            metrics=metrics,
            cv_score=float(cv),
            mean=float(mean),
            std=float(std),
            min_fold=float(min_fold),
            result_status="revert",
            exit_code=1,
        )


def main() -> int:
    """Run one invocation on a single event loop for all pooled DB work."""
    return asyncio.run(_main())


if __name__ == "__main__":
    sys.exit(main())
