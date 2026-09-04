#!/usr/bin/env python3
"""Regenerate the NCP timer Prefect argv golden from the real Prefect flows."""

from __future__ import annotations

import argparse
import difflib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/ncp_job_timers_prefect_argv.json"
HELPER_RELATIVE = Path("src/robin_automation/auto_trader_execution.py")
FLOW_RELATIVE = Path("flows/auto_trader")
IMAGE_DIGEST = "sha256:" + "a" * 64
TOKEN = re.compile(r'"(?:[^"\\]|\\.)*"|-?\d+(?:\.\d+)?|true|false|null|[{}\[\],:]')


class GoldenError(ValueError):
    """A malformed or unusable Prefect checkout."""


class MissingPrefectRepo(GoldenError):
    """The required read-only Prefect source checkout was not supplied."""


def _load_module(name: str, path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise GoldenError(f"could not import {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def _install_prefect_import_shim() -> None:
    """Supply only the decorator surface when this repo has no Prefect extra.

    The imported files and their task bodies remain the Prefect checkout's real
    source.  This lets the deliberately lightweight auto_trader environment
    capture task argv without installing or contacting a Prefect server.
    """
    try:
        import prefect  # noqa: F401
    except ModuleNotFoundError:

        class _Task:
            def __init__(self, fn: Callable[..., object]) -> None:
                self.fn = fn

        def task(**_: object) -> Callable[[Callable[..., object]], _Task]:
            return _Task

        def flow(
            **_: object,
        ) -> Callable[[Callable[..., object]], Callable[..., object]]:
            return lambda fn: fn

        class _Logger:
            def info(self, *_: object, **__: object) -> None:
                return None

        prefect_module = ModuleType("prefect")
        prefect_module.task = task  # type: ignore[attr-defined]
        prefect_module.flow = flow  # type: ignore[attr-defined]
        prefect_module.get_run_logger = _Logger  # type: ignore[attr-defined]
        schedules_module = ModuleType("prefect.client.schemas.schedules")
        schedules_module.CronSchedule = lambda **kwargs: kwargs  # type: ignore[attr-defined]
        sys.modules.update(
            {
                "prefect": prefect_module,
                "prefect.client": ModuleType("prefect.client"),
                "prefect.client.schemas": ModuleType("prefect.client.schemas"),
                "prefect.client.schemas.schedules": schedules_module,
            }
        )


def _prefect_head(repo: Path) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode:
        raise GoldenError(
            f"could not resolve Prefect repo HEAD: {result.stderr.strip()}"
        )
    return result.stdout.strip()


def _capture_task(
    task: object,
    helper: ModuleType,
    *,
    parameters: Mapping[str, object],
) -> tuple[list[str], int]:
    captured: dict[str, object] = {}

    def capture(
        local_command: Sequence[str],
        *,
        docker_command: Sequence[str] | None = None,
        timeout: float | None = None,
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            "AUTO_TRADER_EXEC_MODE": "docker",
            "AUTO_TRADER_IMAGE_DIGEST_FILE": digest_path,
        }
        captured["argv"] = helper.auto_trader_command(
            docker_command or local_command, env=env
        )
        captured["timeout"] = timeout
        return subprocess.CompletedProcess([], 0, "", "")

    with tempfile.TemporaryDirectory() as temp_dir:
        digest_file = Path(temp_dir) / "deployed-digest"
        digest_file.write_text(f"{IMAGE_DIGEST}\n")
        digest_path = str(digest_file)
        module = sys.modules[task.fn.__module__]  # type: ignore[attr-defined]
        original = module.run_auto_trader_command
        module.run_auto_trader_command = capture
        try:
            task.fn(**parameters)  # type: ignore[attr-defined]
        finally:
            module.run_auto_trader_command = original
    argv = captured.get("argv")
    timeout = captured.get("timeout")
    if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
        raise GoldenError("flow task did not produce a string argv")
    if type(timeout) not in (int, float):
        raise GoldenError("flow task did not provide a timeout")
    return argv, int(timeout)


def generate(repo: Path) -> dict[str, object]:
    if not repo.is_dir():
        raise MissingPrefectRepo(f"Prefect repo is unavailable: {repo}")
    helper_path = repo / HELPER_RELATIVE
    if not helper_path.is_file():
        raise GoldenError(f"Prefect helper is unavailable: {helper_path}")
    source_root = repo / "src"
    if str(source_root) not in sys.path:
        sys.path.insert(0, str(source_root))
    _install_prefect_import_shim()
    helper = _load_module("robin_automation.auto_trader_execution", helper_path)
    jobs: list[dict[str, object]] = []
    definitions: tuple[tuple[str, str, str, str, str, dict[str, object]], ...] = (
        (
            "kr-investor-flow-snapshots",
            "KR Investor Flow Snapshots",
            "daily-freshness",
            "investor_flow_snapshots.py",
            "run_investor_flow_snapshot_build",
            {
                "dry_run": False,
                "all_symbols": True,
                "limit": None,
                "days": 5,
                "batch_size": 100,
                "concurrency": 4,
                "timeout_seconds": 1800,
            },
        ),
        (
            "toss-warnings-sync",
            "Toss Warnings Sync",
            "daily-preopen",
            "toss_warnings_sync.py",
            "run_toss_warnings_sync",
            {"timeout_seconds": 3600},
        ),
        (
            "us-invest-screener-snapshots",
            "US Invest Screener Snapshots",
            "post-us-close-freshness",
            "invest_screener_snapshots_us.py",
            "run_us_invest_screener_snapshot_build",
            {
                "dry_run": False,
                "all_symbols": True,
                "limit": None,
                "batch_size": 200,
                "concurrency": 4,
                "common_stocks_only": True,
                "timeout_seconds": 7200,
            },
        ),
    )
    for index, (unit, flow, deployment, filename, task_name, parameters) in enumerate(
        definitions
    ):
        module = _load_module(
            f"_ncp_job_timer_flow_{index}", repo / FLOW_RELATIVE / filename
        )
        argv, timeout = _capture_task(
            getattr(module, task_name), helper, parameters=parameters
        )
        jobs.append(
            {
                "unit": unit,
                "flow": flow,
                "deployment": deployment,
                "cron": {
                    "kr-investor-flow-snapshots": "10 18 * * 1-5",
                    "toss-warnings-sync": "30 7 * * *",
                    "us-invest-screener-snapshots": "10 6 * * 2-6",
                }[unit],
                "timeout_seconds": timeout,
                "argv": argv,
            }
        )
    return {
        "provenance": {
            "helper": f"{helper_path}:auto_trader_command",
            "mode": "docker",
            "image_digest": IMAGE_DIGEST,
            "container_env_file": "/root/at-secrets/.env.api",
            "command": "uv run python -m scripts.ncp_job_timers_golden --write",
            "prefect_repo_head": _prefect_head(repo),
        },
        "jobs": jobs,
    }


def _render(payload: Mapping[str, object]) -> str:
    provenance = payload["provenance"]
    jobs = payload["jobs"]
    if not isinstance(provenance, Mapping) or not isinstance(jobs, list):
        raise GoldenError("generated payload has an invalid shape")
    provenance_lines = json.dumps(provenance, indent=2, ensure_ascii=False).splitlines()
    lines = ["{", '  "provenance": {']
    lines.extend(f"  {line}" for line in provenance_lines[1:-1])
    lines.extend(["  },", '  "jobs": ['])
    for index, raw_job in enumerate(jobs):
        if not isinstance(raw_job, Mapping):
            raise GoldenError("generated payload has an invalid job")
        lines.append("    {")
        entries = list(raw_job.items())
        for entry_index, (key, value) in enumerate(entries):
            rendered = json.dumps(value, ensure_ascii=False)
            suffix = "," if entry_index < len(entries) - 1 else ""
            lines.append(f"      {json.dumps(key)}: {rendered}{suffix}")
        lines.append("    }" + ("," if index < len(jobs) - 1 else ""))
    lines.extend(["  ]", "}"])
    return "\n".join(lines) + "\n"


def _token_diff(expected: str, actual: str) -> str:
    return "\n".join(
        difflib.unified_diff(
            TOKEN.findall(expected),
            TOKEN.findall(actual),
            fromfile="fixture",
            tofile="generated",
            lineterm="",
        )
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefect-repo", default=os.environ.get("ROBIN_PREFECT_REPO"))
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument(
        "--write", action="store_true", help="overwrite the committed golden"
    )
    mode.add_argument(
        "--check", action="store_true", help="fail when the committed golden drifts"
    )
    args = parser.parse_args(argv)
    if not args.prefect_repo:
        raise MissingPrefectRepo(
            "Prefect repo is required: use --prefect-repo or ROBIN_PREFECT_REPO"
        )
    rendered = _render(generate(Path(args.prefect_repo).expanduser()))
    if args.write:
        FIXTURE.write_text(rendered)
        print(f"wrote {FIXTURE}")
        return 0
    current = FIXTURE.read_text()
    if current != rendered:
        print("NCP timer golden drift detected (token diff):", file=sys.stderr)
        print(_token_diff(current, rendered), file=sys.stderr)
        return 1
    print("NCP timer golden matches Prefect generation")
    return 0


def run_cli(argv: list[str] | None = None) -> int:
    try:
        return main(argv)
    except MissingPrefectRepo as exc:
        print(f"ncp job timer golden failed: {exc}", file=sys.stderr)
        return 2
    except (GoldenError, OSError, subprocess.SubprocessError) as exc:
        print(f"ncp job timer golden failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
