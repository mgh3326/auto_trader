#!/usr/bin/env python3
"""run_experiment.py — single-iteration autoresearch experiment runner.

Usage:
    uv run backtest/run_experiment.py --description "RSI period 8"

Exit codes: 0 = improved (keep), 1 = worse (reverted), 2 = crashed
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_TSV = REPO_ROOT / "results.tsv"
RUN_LOG = REPO_ROOT / "run.log"
BACKTEST_TIMEOUT = 120  # seconds


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run one autoresearch experiment")
    p.add_argument("--description", required=True, help="One-line experiment description")
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


def run_backtest() -> tuple[int, str]:
    """Run CV backtest, return (returncode, stdout)."""
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
        return result.returncode, result.stdout
    except subprocess.TimeoutExpired:
        RUN_LOG.write_text("TIMEOUT: backtest exceeded 120 seconds\n")
        return -1, ""


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
    """Revert last commit."""
    subprocess.run(
        ["git", "reset", "--hard", "HEAD~1"],
        cwd=REPO_ROOT,
        capture_output=True,
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

    print(f"=== {exp_id}: {args.description} ===")
    print(f"Current best cv_score: {best:.6f}")
    print("Running CV backtest...")

    returncode, stdout = run_backtest()

    if returncode != 0:
        print(f"CRASHED (exit code {returncode}). See run.log")
        git_revert()
        append_result(exp_id, 0.0, 0.0, 0.0, 0.0, "crash", args.description)
        print("Reverted.")
        return 2

    metrics = parse_metrics(stdout)
    if metrics is None:
        print("FAILED to parse cv_score from output. See run.log")
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
        append_result(exp_id, cv, mean, std, min_fold, "keep", args.description)
        return 0
    else:
        print(f"No improvement ({cv:.6f} <= {best:.6f}) — reverting.")
        git_revert()
        append_result(exp_id, cv, mean, std, min_fold, "revert", args.description)
        return 1


if __name__ == "__main__":
    sys.exit(main())
