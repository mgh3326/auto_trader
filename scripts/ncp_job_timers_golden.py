#!/usr/bin/env python3
"""Regenerate the NCP timer Prefect argv golden from the real Prefect flows."""

from __future__ import annotations

import argparse
import difflib
import hashlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from types import ModuleType

ROOT = Path(__file__).resolve().parents[1]
FIXTURE = ROOT / "tests/fixtures/ncp_job_timers_prefect_argv.json"
FROZEN_INSIGHT_MODULE = ROOT / "scripts/build_crypto_insight_snapshots.py"
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
    """Supply only decorators; task bodies remain the real Prefect source."""
    try:
        import prefect  # noqa: F401
    except ModuleNotFoundError:

        class _Task:
            def __init__(self, fn: Callable[..., object]) -> None:
                self.fn = fn

        def task(
            fn: Callable[..., object] | None = None, **_: object
        ) -> _Task | Callable[[Callable[..., object]], _Task]:
            return _Task(fn) if fn is not None else _Task

        def flow(
            fn: Callable[..., object] | None = None, **_: object
        ) -> (
            Callable[..., object]
            | Callable[[Callable[..., object]], Callable[..., object]]
        ):
            return fn if fn is not None else lambda wrapped: wrapped

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


@contextmanager
def _temporary_environ(values: Mapping[str, str]) -> Iterator[None]:
    original = {key: os.environ.get(key) for key in values}
    os.environ.update(values)
    try:
        yield
    finally:
        for key, old_value in original.items():
            if old_value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = old_value


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
    capture_module: ModuleType | None = None,
    env: Mapping[str, str] | None = None,
) -> tuple[list[str], int]:
    captured: dict[str, object] = {}

    def capture(
        local_command: Sequence[str],
        *,
        docker_command: Sequence[str] | None = None,
        timeout: float | None = None,
        **_: object,
    ) -> subprocess.CompletedProcess[str]:
        helper_env = {
            "AUTO_TRADER_EXEC_MODE": "docker",
            "AUTO_TRADER_IMAGE_DIGEST_FILE": digest_path,
        }
        captured["argv"] = helper.auto_trader_command(
            docker_command or local_command, env=helper_env
        )
        captured["timeout"] = timeout
        return subprocess.CompletedProcess([], 0, "", "")

    with tempfile.TemporaryDirectory() as temp_dir:
        digest_file = Path(temp_dir) / "deployed-digest"
        digest_file.write_text(f"{IMAGE_DIGEST}\n")
        digest_path = str(digest_file)
        task_module = sys.modules[task.fn.__module__]  # type: ignore[attr-defined]
        patch_module = capture_module or task_module
        original = patch_module.run_auto_trader_command
        patch_module.run_auto_trader_command = capture
        try:
            with _temporary_environ(env or {}):
                task.fn(**parameters)  # type: ignore[attr-defined]
        finally:
            patch_module.run_auto_trader_command = original
    argv = captured.get("argv")
    timeout = captured.get("timeout")
    if not isinstance(argv, list) or not all(isinstance(value, str) for value in argv):
        raise GoldenError("flow task did not produce a string argv")
    if type(timeout) not in (int, float):
        raise GoldenError("flow task did not provide a timeout")
    return argv, int(timeout)


def _job(
    *,
    unit: str,
    flow: str,
    deployment: str,
    cron: str,
    timeout_seconds: int,
    argv: list[str],
) -> dict[str, object]:
    return {
        "unit": unit,
        "flow": flow,
        "deployment": deployment,
        "cron": cron,
        "timeout_seconds": timeout_seconds,
        "argv": argv,
    }


def generate(repo: Path) -> tuple[dict[str, object], str]:
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

    with _temporary_environ({"AUTO_TRADER_EXEC_MODE": "docker"}):
        legacy_definitions: tuple[
            tuple[str, str, str, str, str, Mapping[str, object]], ...
        ] = (
            (
                "kr-investor-flow-snapshots",
                "KR Investor Flow Snapshots",
                "daily-freshness",
                "10 18 * * 1-5",
                "investor_flow_snapshots.py",
                {
                    "task": "run_investor_flow_snapshot_build",
                    "parameters": {
                        "dry_run": False,
                        "all_symbols": True,
                        "limit": None,
                        "days": 5,
                        "batch_size": 100,
                        "concurrency": 4,
                        "timeout_seconds": 1800,
                    },
                },
            ),
            (
                "toss-warnings-sync",
                "Toss Warnings Sync",
                "daily-preopen",
                "30 7 * * *",
                "toss_warnings_sync.py",
                {
                    "task": "run_toss_warnings_sync",
                    "parameters": {"timeout_seconds": 3600},
                },
            ),
            (
                "us-invest-screener-snapshots",
                "US Invest Screener Snapshots",
                "post-us-close-freshness",
                "10 6 * * 2-6",
                "invest_screener_snapshots_us.py",
                {
                    "task": "run_us_invest_screener_snapshot_build",
                    "parameters": {
                        "dry_run": False,
                        "all_symbols": True,
                        "limit": None,
                        "batch_size": 200,
                        "concurrency": 4,
                        "common_stocks_only": True,
                        "timeout_seconds": 7200,
                    },
                },
            ),
        )
        for index, (unit, flow, deployment, cron, filename, task_spec) in enumerate(
            legacy_definitions
        ):
            module = _load_module(
                f"_ncp_job_timer_legacy_{index}", repo / FLOW_RELATIVE / filename
            )
            task = getattr(module, str(task_spec["task"]))
            parameters = task_spec["parameters"]
            if not isinstance(parameters, Mapping):
                raise GoldenError(f"{unit}: invalid task parameters")
            argv, timeout = _capture_task(task, helper, parameters=parameters)
            jobs.append(
                _job(
                    unit=unit,
                    flow=flow,
                    deployment=deployment,
                    cron=cron,
                    timeout_seconds=timeout,
                    argv=argv,
                )
            )

        gate_definitions: tuple[
            tuple[str, str, str, str, str, str, str, Mapping[str, object]], ...
        ] = (
            (
                "crypto-invest-screener-snapshots",
                "Invest Crypto Screener Snapshots",
                "daily-kst",
                "20 9 * * *",
                "invest_crypto_screener_snapshots.py",
                "run_invest_crypto_screener_snapshot_build",
                "INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED",
                {
                    "all_markets": True,
                    "limit": None,
                    "commit_with_gate": True,
                    "timeout_seconds": 3600,
                },
            ),
            (
                "kr-fundamentals-snapshots",
                "invest_kr_fundamentals_snapshots",
                "daily-kst",
                "0 18 * * *",
                "invest_kr_fundamentals_snapshots.py",
                "run_kr_fundamentals_snapshot_build",
                "INVEST_SCREENER_SNAPSHOTS_COMMIT_ENABLED",
                {
                    "all_symbols": True,
                    "limit": None,
                    "commit_with_gate": True,
                    "allow_partial": False,
                    "timeout_seconds": 7200,
                },
            ),
            (
                "us-market-valuation-snapshots",
                "US Market Valuation Snapshots",
                "daily-post-us-close",
                "30 8 * * 2-6",
                "market_valuation_snapshots_us.py",
                "run_us_market_valuation_snapshot_build",
                "MARKET_VALUATION_SNAPSHOTS_COMMIT_ENABLED",
                {
                    "all_symbols": True,
                    "limit": None,
                    "batch_size": 100,
                    "concurrency": 4,
                    "common_stocks_only": True,
                    "with_high_52w_date": False,
                    "commit_with_gate": True,
                    "timeout_seconds": 10800,
                },
            ),
            (
                "us-fundamentals-snapshots",
                "US Financial Fundamentals Snapshots",
                "weekly-sunday",
                "0 9 * * 0",
                "us_fundamentals_snapshots.py",
                "run_us_fundamentals_snapshot_build",
                "MARKET_VALUATION_SNAPSHOTS_COMMIT_ENABLED",
                {
                    "all_symbols": True,
                    "limit": None,
                    "concurrency": 4,
                    "include_dividends": True,
                    "commit_with_gate": True,
                    "timeout_seconds": 10800,
                },
            ),
        )
        for index, (
            unit,
            flow,
            deployment,
            cron,
            filename,
            task_name,
            commit_env,
            parameters,
        ) in enumerate(gate_definitions):
            module = _load_module(
                f"_ncp_job_timer_gate_{index}", repo / FLOW_RELATIVE / filename
            )
            task = getattr(module, task_name)
            argv_off, timeout_off = _capture_task(
                task, helper, parameters=parameters, env={commit_env: "false"}
            )
            argv_on, timeout_on = _capture_task(
                task, helper, parameters=parameters, env={commit_env: "true"}
            )
            if timeout_off != timeout_on:
                raise GoldenError(f"{unit}: commit gate changed timeout")
            jobs.append(
                {
                    "unit": unit,
                    "flow": flow,
                    "deployment": deployment,
                    "cron": cron,
                    "timeout_seconds": timeout_off,
                    "commit_env": commit_env,
                    "argv_commit_off": argv_off,
                    "argv_commit_on": argv_on,
                }
            )

        toss_module = _load_module(
            "_ncp_job_timer_toss", repo / FLOW_RELATIVE / "toss_symbol_master_sync.py"
        )
        toss_capture_module = sys.modules.get("robin_automation.toss_symbol_master")
        if not isinstance(toss_capture_module, ModuleType):
            raise GoldenError("Toss symbol master capture module was not imported")
        toss_steps: list[dict[str, object]] = []
        for market in ("kr", "us"):
            argv, timeout = _capture_task(
                toss_module.sync_market,
                helper,
                parameters={"market": market, "dry_run": False},
                capture_module=toss_capture_module,
            )
            toss_steps.append({"argv": argv, "timeout_seconds": timeout})
        jobs.append(
            {
                "unit": "toss-symbol-master-sync",
                "flow": "Toss Symbol Master Sync",
                "deployment": "weekday-preopen",
                "cron": "20 7 * * 1-5",
                "timeout_seconds": sum(
                    int(step["timeout_seconds"]) for step in toss_steps
                ),
                "steps": toss_steps,
            }
        )

        insight_module = _load_module(
            "_ncp_job_timer_insight",
            repo / FLOW_RELATIVE / "invest_crypto_insight_snapshots.py",
        )
        insight_argv, insight_timeout = _capture_task(
            insight_module.run_invest_crypto_insight_snapshot_build,
            helper,
            parameters={
                "providers": None,
                "symbols": None,
                "limit": None,
                "commit_with_gate": True,
                "timeout_seconds": 3600,
            },
        )
        runner_code = insight_module.RUNNER_CODE
        if not isinstance(runner_code, str):
            raise GoldenError("crypto insight RUNNER_CODE is not a string")
        jobs.append(
            {
                "unit": "crypto-invest-insight-snapshots",
                "flow": "Invest Crypto Insight Snapshots",
                "deployment": "daily-kst",
                "cron": "20 9 * * *",
                "timeout_seconds": insight_timeout,
                "argv_prefect": insight_argv,
                "runner_code_sha256": hashlib.sha256(runner_code.encode()).hexdigest(),
                "module": "scripts.build_crypto_insight_snapshots",
            }
        )

    return (
        {
            "provenance": {
                "helper": f"{helper_path}:auto_trader_command",
                "mode": "docker",
                "image_digest": IMAGE_DIGEST,
                "container_env_file": "/root/at-secrets/.env.api",
                "command": "uv run python -m scripts.ncp_job_timers_golden --write",
                "prefect_repo_head": _prefect_head(repo),
            },
            "jobs": jobs,
        },
        runner_code,
    )


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
    payload, runner_code = generate(Path(args.prefect_repo).expanduser())
    rendered = _render(payload)
    if args.write:
        FIXTURE.write_text(rendered)
        FROZEN_INSIGHT_MODULE.write_text(runner_code)
        print(f"wrote {FIXTURE}")
        print(f"wrote {FROZEN_INSIGHT_MODULE}")
        return 0
    current = FIXTURE.read_text()
    if current != rendered:
        print("NCP timer golden drift detected (token diff):", file=sys.stderr)
        print(_token_diff(current, rendered), file=sys.stderr)
        return 1
    if (
        not FROZEN_INSIGHT_MODULE.is_file()
        or FROZEN_INSIGHT_MODULE.read_text() != runner_code
    ):
        print(
            "crypto insight frozen module drifts from Prefect RUNNER_CODE",
            file=sys.stderr,
        )
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
