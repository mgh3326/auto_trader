#!/usr/bin/env python3
"""Multi-round autoresearch orchestrator for backtest experiments."""

from __future__ import annotations

import argparse
import math
import shlex
import shutil
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
RESULTS_TSV = REPO_ROOT / "results.tsv"
PROGRAM_MD = REPO_ROOT / "backtest/program.md"
RUN_EXPERIMENT = REPO_ROOT / "backtest/run_experiment.py"
MIN_FREE_BYTES = 500 * 1024 * 1024

_shutdown_requested = False


@dataclass(slots=True)
class ExperimentResult:
    experiment: str
    cv_score: float
    status: str
    description: str


@dataclass(slots=True)
class Stats:
    initial_best_score: float
    current_best_score: float
    best_experiment: str | None = None
    kept: int = 0
    reverted: int = 0
    crashed: int = 0
    skipped: int = 0
    valid_rounds: int = 0
    consecutive_reverts: int = 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run multi-round autoresearch backtest experiments"
    )
    parser.add_argument(
        "--mode",
        choices=("manual", "auto"),
        default="manual",
        help="manual: wait for new commits, auto: invoke AI CLI each round",
    )
    parser.add_argument(
        "--rounds",
        type=int,
        required=True,
        help="Number of completed experiment rounds to run",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=3600,
        help="Maximum total wall-clock runtime in seconds",
    )
    parser.add_argument(
        "--max-consecutive-reverts",
        type=int,
        default=10,
        help="Stop after this many consecutive revert results are exceeded",
    )
    parser.add_argument(
        "--ai-cli",
        default="claude",
        help="AI CLI command used in auto mode",
    )
    parser.add_argument(
        "--description",
        help="Optional fixed description override. Defaults to HEAD commit subject.",
    )
    parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Polling interval in seconds for manual mode commit detection",
    )
    parser.add_argument(
        "--ai-timeout",
        type=int,
        default=300,
        help="Timeout for the auto-mode AI CLI subprocess in seconds",
    )
    return parser.parse_args()


def get_best_cv_score(results_path: Path = RESULTS_TSV) -> float:
    """Read the best kept cv_score from results.tsv."""
    if not results_path.exists():
        return float("-inf")

    best = float("-inf")
    for raw_line in results_path.read_text().splitlines()[1:]:
        if not raw_line.strip():
            continue
        parts = raw_line.split("\t", 7)
        if len(parts) < 7:
            continue
        status = parts[6].strip().lower()
        if status not in {"keep", "kept"}:
            continue
        try:
            best = max(best, float(parts[1]))
        except ValueError:
            continue
    return best


def get_next_experiment_id(results_path: Path = RESULTS_TSV) -> str:
    """Determine the next experiment id from results.tsv."""
    if not results_path.exists():
        return "exp1"

    max_n = 0
    for raw_line in results_path.read_text().splitlines()[1:]:
        if not raw_line.strip():
            continue
        parts = raw_line.split("\t", 1)
        if not parts:
            continue
        label = parts[0].strip()
        if label.startswith("exp") and label[3:].isdigit():
            max_n = max(max_n, int(label[3:]))
    return f"exp{max_n + 1}"


def get_recent_experiments(
    results_path: Path = RESULTS_TSV, n: int = 20
) -> list[tuple[str, str, str]]:
    """Read the latest experiment/status/description triples from results.tsv."""
    if n <= 0 or not results_path.exists():
        return []

    recent: list[tuple[str, str, str]] = []
    for raw_line in results_path.read_text().splitlines()[1:]:
        if not raw_line.strip():
            continue
        parts = raw_line.split("\t", 7)
        if len(parts) < 8:
            continue

        experiment = parts[0].strip()
        status = parts[6].strip().lower()
        description = parts[7].strip()
        if not experiment:
            continue

        recent.append((experiment, status, description))

    return list(reversed(recent[-n:]))


def read_last_result_row(results_path: Path = RESULTS_TSV) -> ExperimentResult | None:
    """Read the latest non-header row from results.tsv."""
    if not results_path.exists():
        return None

    lines = [line for line in results_path.read_text().splitlines() if line.strip()]
    if len(lines) <= 1:
        return None

    parts = lines[-1].split("\t", 7)
    if len(parts) < 8:
        return None

    try:
        cv_score = float(parts[1])
    except ValueError:
        return None

    return ExperimentResult(
        experiment=parts[0],
        cv_score=cv_score,
        status=parts[6].strip().lower(),
        description=parts[7],
    )


def read_best_result_row(results_path: Path = RESULTS_TSV) -> ExperimentResult | None:
    """Read the highest-scoring keep/kept row from results.tsv."""
    if not results_path.exists():
        return None

    best_result: ExperimentResult | None = None
    for raw_line in results_path.read_text().splitlines()[1:]:
        if not raw_line.strip():
            continue
        parts = raw_line.split("\t", 7)
        if len(parts) < 8:
            continue
        status = parts[6].strip().lower()
        if status not in {"keep", "kept"}:
            continue
        try:
            candidate = ExperimentResult(
                experiment=parts[0],
                cv_score=float(parts[1]),
                status=status,
                description=parts[7],
            )
        except ValueError:
            continue
        if best_result is None or candidate.cv_score > best_result.cv_score:
            best_result = candidate
    return best_result


def resolve_description(
    cli_description: str | None,
    commit_subject: str | None,
) -> str:
    """Resolve the experiment description from CLI override or git HEAD."""
    if cli_description and cli_description.strip():
        return cli_description.strip()
    if commit_subject and commit_subject.strip():
        return commit_subject.strip()
    raise ValueError("No description provided and HEAD commit subject is unavailable")


def format_duration(seconds: float) -> str:
    """Format elapsed seconds into a compact human-readable string."""
    remaining = max(0, int(round(seconds)))
    hours, remaining = divmod(remaining, 3600)
    minutes, secs = divmod(remaining, 60)

    parts: list[str] = []
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if secs or not parts:
        parts.append(f"{secs}s")
    return " ".join(parts)


def apply_status(
    stats: Stats,
    status: str,
    score: float | None = None,
    experiment: str | None = None,
) -> None:
    """Update cumulative stats for a completed status."""
    normalized = status.strip().lower()

    if normalized in {"keep", "kept"}:
        stats.kept += 1
        stats.valid_rounds += 1
        stats.consecutive_reverts = 0
        if score is not None and score > stats.current_best_score:
            stats.current_best_score = score
            stats.best_experiment = experiment
        elif stats.best_experiment is None and experiment:
            stats.best_experiment = experiment
        return

    if normalized in {"revert", "reverted"}:
        stats.reverted += 1
        stats.valid_rounds += 1
        stats.consecutive_reverts += 1
        return

    if normalized == "crash":
        stats.crashed += 1
        stats.valid_rounds += 1
        stats.consecutive_reverts = 0
        return

    if normalized == "skip":
        stats.skipped += 1
        return

    raise ValueError(f"Unsupported status: {status}")


def revert_limit_exceeded(stats: Stats, max_consecutive_reverts: int) -> bool:
    return stats.consecutive_reverts > max_consecutive_reverts


def handle_sigint(signum: int, frame: object) -> None:
    """Set a shutdown flag and let the current round finish."""
    del signum, frame
    global _shutdown_requested
    _shutdown_requested = True
    print("\nSIGINT received. Will stop after the current round.", flush=True)


def git_output(*args: str) -> str:
    """Run a git command and return trimmed stdout."""
    result = subprocess.run(
        ["git", *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def current_head() -> str:
    return git_output("rev-parse", "HEAD")


def head_commit_subject() -> str | None:
    try:
        subject = git_output("log", "-1", "--format=%s")
    except subprocess.CalledProcessError:
        return None
    return subject or None


def git_status_porcelain() -> str:
    try:
        return git_output("status", "--porcelain")
    except subprocess.CalledProcessError:
        return ""


def free_disk_bytes(path: Path = REPO_ROOT) -> int:
    return shutil.disk_usage(path).free


def check_disk_space() -> None:
    free_bytes = free_disk_bytes(REPO_ROOT)
    if free_bytes < MIN_FREE_BYTES:
        free_mb = free_bytes / (1024 * 1024)
        raise RuntimeError(f"Low disk space: {free_mb:.1f} MB free")


def timed_out(start_time: float, timeout_seconds: int) -> bool:
    return (time.monotonic() - start_time) > timeout_seconds


def display_score(score: float) -> str:
    if math.isfinite(score):
        return f"{score:.6f}"
    return "N/A"


def wait_for_new_commit(
    last_processed_head: str,
    poll_interval: float,
    start_time: float,
    timeout_seconds: int,
) -> str | None:
    """Poll until HEAD changes or shutdown/timeout occurs."""
    last_notice = 0.0
    while True:
        if _shutdown_requested or timed_out(start_time, timeout_seconds):
            return None

        head = current_head()
        if head != last_processed_head:
            return head

        now = time.monotonic()
        if last_notice == 0.0 or (now - last_notice) >= 30.0:
            print("Waiting for new commit... (Ctrl+C to stop)", flush=True)
            last_notice = now
        time.sleep(poll_interval)


def build_ai_prompt(round_number: int, total_rounds: int, best_score: float) -> str:
    """Build the prompt passed to the AI CLI in auto mode."""
    next_experiment = get_next_experiment_id(RESULTS_TSV)
    best_text = display_score(best_score)
    recent_experiments = get_recent_experiments(RESULTS_TSV, n=20)

    recent_section_lines = [
        "Recent experiments (DO NOT repeat these):",
        "Entries marked [REVERT] already failed; do not retry them with another small parameter tweak.",
    ]
    if recent_experiments:
        for experiment, status, description in recent_experiments:
            status_label = status.upper()
            if status in {"revert", "reverted"}:
                status_label = "REVERT"
            elif status in {"keep", "kept"}:
                status_label = "KEEP"

            recent_section_lines.append(
                f"- {experiment} [{status_label}]: {description}"
            )
    else:
        recent_section_lines.append("- No previous experiments recorded yet.")

    return "\n".join(
        [
            f"Read {PROGRAM_MD.relative_to(REPO_ROOT)}.",
            f"Current best cv_score is {best_text}.",
            f"You are on round {round_number}/{total_rounds}.",
            "Modify backtest/strategy.py with exactly ONE experimental idea to improve the score.",
            "Do not modify any other file.",
            "Use the recent experiment history below to avoid repeating past ideas.",
            *recent_section_lines,
            f"Then run: git add backtest/strategy.py && git commit -m '{next_experiment}: <description>'",
        ]
    )


def run_ai_cli(
    ai_cli: str,
    round_number: int,
    total_rounds: int,
    best_score: float,
    timeout_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Invoke the external AI CLI for one auto-mode attempt."""
    base_command = shlex.split(ai_cli)
    if not base_command:
        raise RuntimeError("AI CLI command is empty")

    command = [
        *base_command,
        "--print",
        "--permission-mode",
        "bypassPermissions",
        "-p",
        build_ai_prompt(round_number, total_rounds, best_score),
    ]
    try:
        return subprocess.run(
            command,
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=timeout_seconds,
            check=False,
        )
    except FileNotFoundError as exc:
        raise RuntimeError(f"AI CLI not found: {base_command[0]}") from exc
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"AI CLI timed out after {timeout_seconds} seconds"
        ) from exc


def run_experiment_round(description: str) -> int:
    """Run one existing experiment round."""
    result = subprocess.run(
        ["uv", "run", str(RUN_EXPERIMENT.relative_to(REPO_ROOT)), "--description", description],
        cwd=REPO_ROOT,
        check=False,
    )
    return result.returncode


def print_round_summary(
    result: ExperimentResult,
    previous_best: float,
    stats: Stats,
) -> None:
    """Print the per-round outcome."""
    delta = result.cv_score - previous_best if math.isfinite(previous_best) else 0.0
    label = result.status.upper()
    print(
        f"{result.experiment}: cv_score={result.cv_score:.6f} ({delta:+.6f}) {label}",
        flush=True,
    )
    print(
        "Cumulative: "
        f"{stats.kept} keep / {stats.reverted} revert / "
        f"{stats.crashed} crash / {stats.skipped} skip",
        flush=True,
    )


def print_skip_summary(stats: Stats, reason: str) -> None:
    """Print a skip message without advancing the round counter."""
    print(f"Skip: {reason}", flush=True)
    print(
        "Cumulative: "
        f"{stats.kept} keep / {stats.reverted} revert / "
        f"{stats.crashed} crash / {stats.skipped} skip",
        flush=True,
    )


def print_final_summary(stats: Stats, start_time: float) -> None:
    """Print the final orchestrator summary."""
    valid_rounds = stats.valid_rounds
    elapsed = time.monotonic() - start_time

    print("\n=== ORCHESTRATOR SUMMARY ===", flush=True)
    print(f"Rounds: {valid_rounds}", flush=True)

    def ratio(count: int) -> str:
        if valid_rounds == 0:
            return "0%"
        return f"{(count / valid_rounds) * 100:.0f}%"

    print(f"Kept: {stats.kept} ({ratio(stats.kept)})", flush=True)
    print(f"Reverted: {stats.reverted} ({ratio(stats.reverted)})", flush=True)
    print(f"Crashed: {stats.crashed} ({ratio(stats.crashed)})", flush=True)
    if stats.skipped:
        print(f"Skipped: {stats.skipped}", flush=True)

    if math.isfinite(stats.initial_best_score) and math.isfinite(stats.current_best_score):
        delta = stats.current_best_score - stats.initial_best_score
        pct = (
            (delta / stats.initial_best_score) * 100
            if stats.initial_best_score != 0
            else 0.0
        )
        print(
            "Score: "
            f"{stats.initial_best_score:.6f} -> {stats.current_best_score:.6f} "
            f"({delta:+.6f}, {pct:+.1f}%)",
            flush=True,
        )
    else:
        print(
            f"Score: {display_score(stats.initial_best_score)} -> "
            f"{display_score(stats.current_best_score)}",
            flush=True,
        )

    if stats.best_experiment:
        print(f"Best experiment: {stats.best_experiment}", flush=True)
    print(f"Total time: {format_duration(elapsed)}", flush=True)


def main() -> int:
    args = parse_args()
    signal.signal(signal.SIGINT, handle_sigint)

    start_time = time.monotonic()
    best_result = read_best_result_row(RESULTS_TSV)
    initial_best = best_result.cv_score if best_result is not None else float("-inf")
    stats = Stats(
        initial_best_score=initial_best,
        current_best_score=initial_best,
        best_experiment=best_result.experiment if best_result is not None else None,
    )
    exit_code = 0

    try:
        check_disk_space()
        last_processed_head = current_head()

        while stats.valid_rounds < args.rounds:
            if _shutdown_requested:
                break
            if timed_out(start_time, args.timeout):
                print("Total timeout reached. Stopping.", flush=True)
                break

            check_disk_space()
            dirty = git_status_porcelain()
            if dirty:
                print("Warning: git working tree is dirty.", flush=True)

            round_number = stats.valid_rounds + 1
            print(f"\n=== Round {round_number}/{args.rounds} ===", flush=True)

            commit_head: str | None = None
            if args.mode == "manual":
                commit_head = wait_for_new_commit(
                    last_processed_head=last_processed_head,
                    poll_interval=args.poll_interval,
                    start_time=start_time,
                    timeout_seconds=args.timeout,
                )
                if commit_head is None:
                    if timed_out(start_time, args.timeout):
                        print("Total timeout reached while waiting for a new commit.", flush=True)
                    break
            else:
                print(
                    f"Best: {display_score(stats.current_best_score)} | Running AI CLI...",
                    flush=True,
                )
                ai_result = run_ai_cli(
                    ai_cli=args.ai_cli,
                    round_number=round_number,
                    total_rounds=args.rounds,
                    best_score=stats.current_best_score,
                    timeout_seconds=args.ai_timeout,
                )
                commit_head = current_head()
                if commit_head == last_processed_head:
                    apply_status(stats, "skip")
                    print_skip_summary(
                        stats,
                        reason=(
                            f"AI CLI produced no new commit"
                            f" (exit {ai_result.returncode})"
                        ),
                    )
                    continue
                if ai_result.returncode != 0:
                    print(
                        f"Warning: AI CLI exited with status {ai_result.returncode}; "
                        "continuing because a new commit was created.",
                        flush=True,
                    )

            description = resolve_description(args.description, head_commit_subject())
            print(
                f"Best: {display_score(stats.current_best_score)} | Running experiment...",
                flush=True,
            )

            previous_result = read_last_result_row(RESULTS_TSV)
            previous_best = stats.current_best_score
            run_experiment_round(description)
            result = read_last_result_row(RESULTS_TSV)

            if result is None:
                raise RuntimeError("run_experiment.py completed without writing results.tsv")
            if (
                previous_result is not None
                and result.experiment == previous_result.experiment
                and result.status == previous_result.status
                and result.description == previous_result.description
            ):
                raise RuntimeError("run_experiment.py did not append a new result row")

            apply_status(
                stats,
                status=result.status,
                score=result.cv_score,
                experiment=result.experiment,
            )
            print_round_summary(result, previous_best, stats)
            last_processed_head = current_head()

            if revert_limit_exceeded(stats, args.max_consecutive_reverts):
                print(
                    "Consecutive revert limit exceeded. Pausing orchestrator.",
                    flush=True,
                )
                exit_code = 3
                break

        return exit_code
    except RuntimeError as exc:
        print(f"Error: {exc}", flush=True)
        return 1
    finally:
        print_final_summary(stats, start_time)


if __name__ == "__main__":
    sys.exit(main())
