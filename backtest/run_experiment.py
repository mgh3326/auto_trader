#!/usr/bin/env python3
"""run_experiment.py — single-iteration autoresearch experiment runner.

Usage:
    uv run backtest/run_experiment.py --description "RSI period 8"

Exit codes: 0 = improved (keep), 1 = worse (reverted), 2 = crashed
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

from app.core.db import AsyncSessionLocal
from app.schemas.research_backtest import (
    BacktestTrialRequest,
    StrategyExperimentIdentity,
)
from app.services.strategy_experiment_registry import (
    record_trial,
    register_experiment,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_TSV = REPO_ROOT / "results.tsv"
RUN_LOG = REPO_ROOT / "run.log"
BACKTEST_TIMEOUT = 120  # seconds


@dataclass(frozen=True)
class BacktestInvocation:
    returncode: int
    stdout: str
    timed_out: bool = False


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
        help="Stable invocation key; defaults to experiment_id + exp_id",
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


def run_backtest() -> BacktestInvocation:
    """Run CV backtest and distinguish timeout from process failure."""
    try:
        result = subprocess.run(
            ["uv", "run", "backtest/backtest.py", "--mode", "cv"],
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


def parse_metrics(stdout: str) -> dict[str, float] | None:
    """Parse cv_score, mean_score, std_score, min_fold_score from stdout."""
    patterns = {
        "cv_score": r"^cv_score:\s+([-\d.]+)",
        "mean_score": r"^mean_score:\s+([-\d.]+)",
        "std_score": r"^std_score:\s+([-\d.]+)",
        "min_fold_score": r"^min_fold_score:\s+([-\d.]+)",
    }
    metrics: dict[str, float] = {}
    for key, pattern in patterns.items():
        m = re.search(pattern, stdout, re.MULTILINE)
        if m:
            metrics[key] = float(m.group(1))
    if "cv_score" not in metrics:
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


async def prepare_registered_experiment(identity_json: str | None) -> str | None:
    """Register canonical identity before running, or enter explicit legacy mode."""
    if identity_json is None:
        return None
    payload = json.loads(Path(identity_json).read_text(encoding="utf-8"))
    identity = StrategyExperimentIdentity.model_validate(payload)
    async with AsyncSessionLocal() as session:
        experiment = await register_experiment(session, identity)
        await session.commit()
        return experiment.experiment_id


def _parse_information_cutoff(value: str | None) -> datetime | None:
    if value is None:
        return None
    cutoff = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if cutoff.tzinfo is None:
        raise ValueError("information_cutoff must be timezone-aware")
    return cutoff


async def record_terminal_trial(
    *,
    experiment_id: str,
    exp_id: str,
    status: str,
    description: str,
    information_cutoff: str | None,
    idempotency_key: str | None,
    metrics: dict[str, float],
) -> None:
    """Append exactly one terminal ROB-846 trial and commit it durably."""
    request = BacktestTrialRequest(
        status=status,
        strategy_name="backtest.strategy",
        timeframe="1d",
        runner="backtest/run_experiment.py",
        run_id=f"autoresearch-{experiment_id[:12]}-{exp_id}",
        information_cutoff=_parse_information_cutoff(information_cutoff),
        idempotency_key=idempotency_key or f"{experiment_id}:{exp_id}",
        ended_at=datetime.now(UTC),
        profit_factor=Decimal("0"),
        max_drawdown=Decimal("0"),
        raw_payload={"description": description, "metrics": metrics},
    )
    async with AsyncSessionLocal() as session:
        await record_trial(session, experiment_id=experiment_id, request=request)
        await session.commit()


def _record_if_registered(
    *,
    experiment_id: str | None,
    exp_id: str,
    status: str,
    args: argparse.Namespace,
    metrics: dict[str, float],
) -> None:
    if experiment_id is None:
        return
    asyncio.run(
        record_terminal_trial(
            experiment_id=experiment_id,
            exp_id=exp_id,
            status=status,
            description=args.description,
            information_cutoff=args.information_cutoff,
            idempotency_key=args.idempotency_key,
            metrics=metrics,
        )
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


def main() -> int:
    args = parse_args()
    exp_id = next_experiment_id()
    best = get_best_cv_score()
    experiment_id = asyncio.run(prepare_registered_experiment(args.identity_json))
    if experiment_id is None:
        print("NON_PROMOTABLE: missing_experiment_identity")

    print(f"=== {exp_id}: {args.description} ===")
    print(f"Current best cv_score: {best:.6f}")
    print("Running CV backtest...")

    invocation = run_backtest()

    if invocation.returncode != 0:
        status = "timeout" if invocation.timed_out else "crashed"
        print(f"{status.upper()} (exit code {invocation.returncode}). See run.log")
        _record_if_registered(
            experiment_id=experiment_id,
            exp_id=exp_id,
            status=status,
            args=args,
            metrics={},
        )
        git_revert()
        append_result(exp_id, 0.0, 0.0, 0.0, 0.0, status, args.description)
        print("Reverted.")
        return 2

    metrics = parse_metrics(invocation.stdout)
    if metrics is None:
        print("FAILED to parse cv_score from output. See run.log")
        _record_if_registered(
            experiment_id=experiment_id,
            exp_id=exp_id,
            status="crashed",
            args=args,
            metrics={},
        )
        git_revert()
        append_result(exp_id, 0.0, 0.0, 0.0, 0.0, "crash", args.description)
        print("Reverted.")
        return 2

    cv = metrics["cv_score"]
    mean = metrics.get("mean_score", 0.0)
    std = metrics.get("std_score", 0.0)
    min_fold = metrics.get("min_fold_score", 0.0)

    print(f"cv_score: {cv:.6f}  (best: {best:.6f})")

    if cv > best:
        print(f"IMPROVED by {cv - best:.6f} — keeping.")
        _record_if_registered(
            experiment_id=experiment_id,
            exp_id=exp_id,
            status="completed",
            args=args,
            metrics=metrics,
        )
        append_result(exp_id, cv, mean, std, min_fold, "keep", args.description)
        return 0
    else:
        print(f"No improvement ({cv:.6f} <= {best:.6f}) — reverting.")
        _record_if_registered(
            experiment_id=experiment_id,
            exp_id=exp_id,
            status="rejected",
            args=args,
            metrics=metrics,
        )
        git_revert()
        append_result(exp_id, cv, mean, std, min_fold, "revert", args.description)
        return 1


if __name__ == "__main__":
    sys.exit(main())
