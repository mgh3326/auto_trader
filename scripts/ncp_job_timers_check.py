#!/usr/bin/env python3
"""Validate static timer contracts and read-only Prefect cutover state."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import quote
from urllib.request import urlopen
from zoneinfo import ZoneInfo

from scripts.lane_event_kickoff import KICKOFF_SLOTS, KickoffSlot

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = ROOT / "ops/ncp/systemd"
RUNNER = "/root/at-run/ops/ncp/bin/at-job.sh"
PREFECT_GOLDEN = ROOT / "tests/fixtures/ncp_job_timers_prefect_argv.json"
FROZEN_INSIGHT_MODULE = ROOT / "scripts/build_crypto_insight_snapshots.py"
RUNTIME_ENV_FILE = "/root/at-secrets/.env.api"
_DIRECTIVE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9]*)=(?P<value>.*)$")
_IMAGE = re.compile(r"^ghcr\.io/mgh3326/auto_trader@sha256:[0-9a-f]{64}$")
_WEEKDAYS = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}
_IMPORT_ONLY_ENV = {
    "KIS_APP_KEY": "check-only",
    "KIS_APP_SECRET": "check-only",
    "OPENDART_API_KEY": "check-only",
    "DATABASE_URL": "postgresql+asyncpg://check:check@localhost/check",
    "UPBIT_ACCESS_KEY": "check-only",
    "UPBIT_SECRET_KEY": "check-only",
    "SECRET_KEY": "CheckOnlySecretKey12345678901234567890",
}


@dataclass(frozen=True)
class Step:
    argv: tuple[str, ...]
    timeout_seconds: int


@dataclass(frozen=True)
class Job:
    name: str
    flow: str
    deployment: str
    cron: str
    timeout_seconds: int
    argv: tuple[str, ...] | None = None
    commit_env: str | None = None
    argv_commit_on: tuple[str, ...] | None = None
    steps: tuple[Step, ...] = ()
    module: str | None = None
    runner_code_sha256: str | None = None

    @property
    def is_gate(self) -> bool:
        return self.commit_env is not None

    @property
    def is_multi_step(self) -> bool:
        return bool(self.steps)

    @property
    def is_insight(self) -> bool:
        return self.module is not None


def _string_argv(value: object, *, context: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(
        isinstance(token, str) for token in value
    ):
        raise ValueError(f"{PREFECT_GOLDEN}: {context} must be a string argv")
    return tuple(value)


def _metadata(raw: Mapping[str, object]) -> tuple[str, str, str, str, int]:
    values = ("unit", "flow", "deployment", "cron", "timeout_seconds")
    if any(key not in raw for key in values) or not all(
        isinstance(raw[key], str) for key in values[:4]
    ):
        raise ValueError(f"{PREFECT_GOLDEN}: malformed job metadata")
    timeout = raw["timeout_seconds"]
    if type(timeout) is not int:
        raise ValueError(f"{PREFECT_GOLDEN}: malformed job timeout")
    return (
        str(raw["unit"]),
        str(raw["flow"]),
        str(raw["deployment"]),
        str(raw["cron"]),
        timeout,
    )


def _load_jobs() -> tuple[Job, ...]:
    payload = json.loads(PREFECT_GOLDEN.read_text())
    if not isinstance(payload, Mapping) or not isinstance(payload.get("jobs"), list):
        raise ValueError(f"{PREFECT_GOLDEN}: malformed Prefect argv golden")
    jobs: list[Job] = []
    for raw in payload["jobs"]:
        if not isinstance(raw, Mapping):
            raise ValueError(f"{PREFECT_GOLDEN}: malformed job entry")
        name, flow, deployment, cron, timeout = _metadata(raw)
        if "argv" in raw:
            jobs.append(
                Job(
                    name,
                    flow,
                    deployment,
                    cron,
                    timeout,
                    argv=_string_argv(raw["argv"], context=name),
                )
            )
        elif "argv_commit_off" in raw:
            commit_env = raw.get("commit_env")
            if not isinstance(commit_env, str) or not re.fullmatch(
                r"[A-Z][A-Z0-9_]*", commit_env
            ):
                raise ValueError(f"{PREFECT_GOLDEN}: {name} has invalid commit_env")
            jobs.append(
                Job(
                    name,
                    flow,
                    deployment,
                    cron,
                    timeout,
                    argv=_string_argv(raw["argv_commit_off"], context=name),
                    commit_env=commit_env,
                    argv_commit_on=_string_argv(
                        raw.get("argv_commit_on"), context=name
                    ),
                )
            )
        elif "steps" in raw:
            raw_steps = raw["steps"]
            if not isinstance(raw_steps, list) or not raw_steps:
                raise ValueError(f"{PREFECT_GOLDEN}: {name} has invalid steps")
            steps: list[Step] = []
            for index, raw_step in enumerate(raw_steps):
                if (
                    not isinstance(raw_step, Mapping)
                    or type(raw_step.get("timeout_seconds")) is not int
                ):
                    raise ValueError(
                        f"{PREFECT_GOLDEN}: {name} has invalid step {index}"
                    )
                steps.append(
                    Step(
                        _string_argv(
                            raw_step.get("argv"), context=f"{name} step {index}"
                        ),
                        int(raw_step["timeout_seconds"]),
                    )
                )
            if timeout != sum(step.timeout_seconds for step in steps):
                raise ValueError(
                    f"{PREFECT_GOLDEN}: {name} timeout is not its step sum"
                )
            jobs.append(Job(name, flow, deployment, cron, timeout, steps=tuple(steps)))
        elif "argv_prefect" in raw:
            module = raw.get("module")
            runner_sha = raw.get("runner_code_sha256")
            if not isinstance(module, str) or not isinstance(runner_sha, str):
                raise ValueError(
                    f"{PREFECT_GOLDEN}: {name} has invalid insight metadata"
                )
            jobs.append(
                Job(
                    name,
                    flow,
                    deployment,
                    cron,
                    timeout,
                    argv=_string_argv(raw["argv_prefect"], context=name),
                    module=module,
                    runner_code_sha256=runner_sha,
                )
            )
        else:
            raise ValueError(f"{PREFECT_GOLDEN}: {name} has no supported argv form")
    return tuple(jobs)


JOBS = _load_jobs()


def _directives(path: Path) -> dict[str, list[str]]:
    parsed: dict[str, list[str]] = {}
    for line in path.read_text().splitlines():
        match = _DIRECTIVE.match(line)
        if match:
            parsed.setdefault(match["key"], []).append(match["value"])
    return parsed


def _field(value: str, minimum: int, maximum: int) -> set[int]:
    result: set[int] = set()
    for piece in value.split(","):
        base, separator, stride_text = piece.partition("/")
        stride = int(stride_text) if separator else 1
        if stride < 1:
            raise ValueError(f"invalid cron field {value!r}")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(base)
        if not minimum <= start <= end <= maximum:
            raise ValueError(f"cron field out of range {value!r}")
        result.update(range(start, end + 1, stride))
    return result


def parse_cron(value: str) -> tuple[set[int], set[int], set[int]]:
    minute, hour, _day, _month, weekday = value.split()
    weekdays = {0 if day == 7 else day for day in _field(weekday, 0, 7)}
    return _field(minute, 0, 59), _field(hour, 0, 23), weekdays


def parse_oncalendar(value: str) -> tuple[set[int], set[int], set[int]]:
    parts = value.split()
    if len(parts) == 3:
        weekday_text, date_text, time_text, timezone = "*", *parts
    elif len(parts) == 4:
        weekday_text, date_text, time_text, timezone = parts
    else:
        raise ValueError(f"invalid OnCalendar form: {value}")
    if date_text != "*-*-*" or timezone != "Asia/Seoul":
        raise ValueError(f"OnCalendar must use KST full-date form: {value}")
    if weekday_text == "*":
        weekdays = set(range(7))
    elif ".." in weekday_text:
        first, last = weekday_text.split("..", 1)
        weekdays = set(range(_WEEKDAYS[first], _WEEKDAYS[last] + 1))
    else:
        weekdays = {_WEEKDAYS[weekday_text]}
    hour, minute, second = (int(value) for value in time_text.split(":"))
    if second != 0:
        raise ValueError(f"OnCalendar must run on an exact minute: {value}")
    return {minute}, {hour}, weekdays


def next_runs(
    schedule: tuple[set[int], set[int], set[int]], count: int = 5
) -> list[datetime]:
    minutes, hours, weekdays = schedule
    instant = datetime(2026, 9, 4, tzinfo=ZoneInfo("Asia/Seoul"))
    result: list[datetime] = []
    while len(result) < count:
        if (
            instant.minute in minutes
            and instant.hour in hours
            and (instant.weekday() + 1) % 7 in weekdays
        ):
            result.append(instant)
        instant += timedelta(minutes=1)
        if instant.year > 2027:
            raise ValueError("could not find five timer occurrences")
    return result


def _python_args(argv: tuple[str, ...], *, module: str | None = None) -> list[str]:
    prefix = (
        "docker",
        "run",
        "--rm",
        "--network",
        "host",
        "--workdir",
        "/app",
        "--env-file",
        RUNTIME_ENV_FILE,
    )
    if argv[: len(prefix)] != prefix or len(argv) < len(prefix) + 4:
        raise ValueError(f"{PREFECT_GOLDEN}: invalid Docker prefix")
    image_index = len(prefix)
    if not _IMAGE.fullmatch(argv[image_index]):
        raise ValueError(f"{PREFECT_GOLDEN}: non-digest image")
    command = argv[image_index + 1 :]
    if command[:2] != ("/app/.venv/bin/python", "-m"):
        if command[:2] == ("/app/.venv/bin/python", "-c") and module is not None:
            return [module]
        raise ValueError(f"{PREFECT_GOLDEN}: invalid Python command")
    if len(command) < 3:
        raise ValueError(f"{PREFECT_GOLDEN}: incomplete Python module command")
    return list(command[2:])


def expected_execstart(job: Job) -> list[str]:
    if job.is_multi_step:
        result: list[str] = [RUNNER]
        for index, step in enumerate(job.steps):
            if index:
                result.append("--at-job-step")
            result.extend(_python_args(step.argv))
        return result
    if job.argv is None:
        raise ValueError(f"{PREFECT_GOLDEN}: {job.name} has no argv")
    return [RUNNER, *_python_args(job.argv, module=job.module)]


def _expected_environment(job: Job) -> set[str]:
    expected = {f"AT_RUNTIME_ENV_FILE={RUNTIME_ENV_FILE}"}
    if job.commit_env is not None:
        expected.add(f"AT_JOB_COMMIT_ENV={job.commit_env}")
    if job.is_multi_step:
        expected.add(f"AT_JOB_STEPS={len(job.steps)}")
    return expected


def _check_environment(
    service_path: Path, service: Mapping[str, list[str]], job: Job
) -> None:
    actual = service.get("Environment", [])
    expected = _expected_environment(job)
    if len(actual) != len(set(actual)) or set(actual) != expected:
        raise ValueError(f"{service_path}: environment differs from exact expected set")
    if "EnvironmentFile" in service:
        raise ValueError(
            f"{service_path}: must not inject an additional environment file"
        )


def _check_insight_hash(job: Job) -> None:
    if not job.is_insight or job.runner_code_sha256 is None:
        return
    if not FROZEN_INSIGHT_MODULE.is_file():
        raise ValueError(
            f"{FROZEN_INSIGHT_MODULE}: missing frozen crypto insight module"
        )
    actual = hashlib.sha256(FROZEN_INSIGHT_MODULE.read_bytes()).hexdigest()
    if actual != job.runner_code_sha256:
        raise ValueError(f"{FROZEN_INSIGHT_MODULE}: SHA-256 differs from golden")


def check_job(job: Job, *, check_imports: bool) -> None:
    service_path = SYSTEMD_DIR / f"job-{job.name}.service"
    timer_path = SYSTEMD_DIR / f"job-{job.name}.timer"
    service = _directives(service_path)
    timer = _directives(timer_path)
    _check_environment(service_path, service, job)
    actual_argv = shlex.split(service.get("ExecStart", [""])[0])
    expected_argv = expected_execstart(job)
    if actual_argv != expected_argv:
        raise ValueError(f"{service_path}: argv differs from Prefect capture")
    if job.is_gate and "--commit" in actual_argv:
        raise ValueError(f"{service_path}: gate ExecStart must be commit-off")
    if service.get("TimeoutStartSec") != [str(job.timeout_seconds + 30)]:
        raise ValueError(f"{service_path}: timeout does not inherit flow timeout")
    if timer.get("Persistent") != ["false"] or timer.get("RandomizedDelaySec") != ["0"]:
        raise ValueError(f"{timer_path}: timer catchup/jitter must remain disabled")
    if timer.get("Unit") != [f"job-{job.name}.service"]:
        raise ValueError(f"{timer_path}: wrong paired service")
    oncalendar = timer.get("OnCalendar", [""])[0]
    if next_runs(parse_cron(job.cron)) != next_runs(parse_oncalendar(oncalendar)):
        raise ValueError(
            f"{timer_path}: OnCalendar differs from Prefect cron {job.cron}"
        )
    _check_insight_hash(job)
    if check_imports and not job.is_insight:
        module = expected_argv[1]
        result = subprocess.run(
            ["uv", "run", "python", "-c", f"import {module}"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, **_IMPORT_ONLY_ENV},
        )
        if result.returncode:
            raise ValueError(f"{module}: import failed: {result.stderr.strip()}")


def _kickoff_lane_env(slot: str) -> str:
    if slot.startswith("crypto-"):
        return "LANE_EVENT_KICKOFF_LANE_CRYPTO"
    if slot == "us-2235":
        return "LANE_EVENT_KICKOFF_LANE_US"
    return "LANE_EVENT_KICKOFF_LANE_KR"


def _kickoff_oncalendar(slot: KickoffSlot) -> str:
    weekday = "Mon..Fri " if slot.weekdays_only else ""
    return f"{weekday}*-*-* {slot.oncalendar}:00 Asia/Seoul"


def _check_kickoff_timer(slot_name: str, slot: KickoffSlot) -> None:
    timer_path = SYSTEMD_DIR / f"job-kickoff-{slot_name}.timer"
    if not timer_path.read_text(encoding="utf-8").startswith("[Unit]\n"):
        raise ValueError(f"{timer_path}: must start with a [Unit] section")
    timer = _directives(timer_path)
    if timer.get("OnCalendar") != [_kickoff_oncalendar(slot)]:
        raise ValueError(f"{timer_path}: OnCalendar differs from kickoff slot")
    if timer.get("Persistent") != ["false"]:
        raise ValueError(f"{timer_path}: Persistent must be false to avoid catchup")
    if timer.get("RandomizedDelaySec") != ["0"]:
        raise ValueError(f"{timer_path}: RandomizedDelaySec must be 0")
    if timer.get("Unit") != [f"job-kickoff-{slot_name}.service"]:
        raise ValueError(f"{timer_path}: wrong paired service")
    if timer.get("WantedBy") != ["timers.target"]:
        raise ValueError(f"{timer_path}: must install under timers.target")


def _environment_assignments(
    service_path: Path, service: Mapping[str, list[str]]
) -> list[str]:
    assignments: list[str] = []
    for value in service.get("Environment", []):
        try:
            tokens = shlex.split(value)
        except ValueError as exc:
            raise ValueError(
                f"{service_path}: malformed Environment directive"
            ) from exc
        if not tokens or any(
            not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", token) for token in tokens
        ):
            raise ValueError(f"{service_path}: Environment has a non-assignment token")
        assignments.extend(tokens)
    return assignments


def _check_kickoff_service(slot_name: str, slot: KickoffSlot) -> None:
    service_path = SYSTEMD_DIR / f"job-kickoff-{slot_name}.service"
    if not service_path.read_text(encoding="utf-8").startswith("[Unit]\n"):
        raise ValueError(f"{service_path}: must start with a [Unit] section")
    service = _directives(service_path)
    expected_env = {
        "PANEWIRE_SOCKET=/root/Library/Application Support/panewire/panewire.sock",
        "LANE_EVENT_EMIT_BIN=/root/pw-s2pilot/bin/panewire",
        "LANE_EVENT_EMIT_INBOX_ROOT=/root/pw-s2pilot/inbox",
        "LANE_EVENT_EMIT_HOST=ncp",
    }
    actual_env = _environment_assignments(service_path, service)
    if len(actual_env) != len(expected_env) or set(actual_env) != expected_env:
        raise ValueError(f"{service_path}: kickoff environment differs from exact set")
    if service.get("EnvironmentFile") != [
        "/root/at-secrets/.env.api",
        "/root/at-secrets/.env.lane-kickoff",
    ]:
        raise ValueError(f"{service_path}: missing kickoff environment files")
    expected_directives = {
        "Type": ["oneshot"],
        "WorkingDirectory": ["/root/at-run"],
        "TimeoutStartSec": ["60"],
        "User": ["root"],
        "UMask": ["0077"],
        "NoNewPrivileges": ["true"],
        "PrivateTmp": ["true"],
    }
    for directive, expected in expected_directives.items():
        if service.get(directive) != expected:
            raise ValueError(
                f"{service_path}: {directive} differs from kickoff contract"
            )

    execstart = service.get("ExecStart", [])
    if len(execstart) != 1:
        raise ValueError(f"{service_path}: must have exactly one ExecStart")
    command = execstart[0]
    required_fragments = (
        'image="$$(cat /root/at-run/deployed-digest)"',
        "/usr/bin/docker run --rm --network host",
        "--env-file /root/at-secrets/.env.api",
        "--env-file /root/at-secrets/.env.lane-kickoff",
        "-v /root/pw-s2pilot/bin/panewire:/root/pw-s2pilot/bin/panewire:ro",
        "-v /root/pw-s2pilot/inbox:/root/pw-s2pilot/inbox",
        '-v "/root/Library/Application Support/panewire":"/root/Library/Application Support/panewire"',
        "-m scripts.lane_event_kickoff",
        f'--lane "$${_kickoff_lane_env(slot_name)}"',
        f"--slot {slot_name}",
        f"--playbook {slot.playbook}",
    )
    if any(fragment not in command for fragment in required_fragments):
        raise ValueError(f"{service_path}: ExecStart differs from kickoff contract")
    lane_match = re.search(r"--lane\s+(?P<lane>\S+)", command)
    if lane_match is None or not lane_match["lane"].lstrip("\"\\'").startswith("$"):
        raise ValueError(f"{service_path}: --lane must use an environment reference")


def check_kickoff_units() -> None:
    expected_services = {f"job-kickoff-{slot}.service" for slot in KICKOFF_SLOTS}
    expected_timers = {f"job-kickoff-{slot}.timer" for slot in KICKOFF_SLOTS}
    actual_services = {path.name for path in SYSTEMD_DIR.glob("job-kickoff-*.service")}
    actual_timers = {path.name for path in SYSTEMD_DIR.glob("job-kickoff-*.timer")}
    if actual_services != expected_services or actual_timers != expected_timers:
        raise ValueError(
            "kickoff unit inventory is unpaired or contains an unreviewed unit"
        )
    for slot_name, slot in KICKOFF_SLOTS.items():
        _check_kickoff_service(slot_name, slot)
        _check_kickoff_timer(slot_name, slot)


def check_all(*, check_imports: bool) -> None:
    expected_services = {f"job-{job.name}.service" for job in JOBS}
    expected_timers = {f"job-{job.name}.timer" for job in JOBS}
    actual_services = {
        path.name
        for path in SYSTEMD_DIR.glob("job-*.service")
        if not path.name.startswith("job-kickoff-")
    }
    actual_timers = {
        path.name
        for path in SYSTEMD_DIR.glob("job-*.timer")
        if not path.name.startswith("job-kickoff-")
    }
    if actual_services != expected_services or actual_timers != expected_timers:
        raise ValueError(
            "job unit inventory is unpaired or contains an unreviewed unit"
        )
    seen: dict[tuple[tuple[datetime, ...], tuple[str, ...]], str] = {}
    for job in JOBS:
        check_job(job, check_imports=check_imports)
        key = (tuple(next_runs(parse_cron(job.cron))), tuple(expected_execstart(job)))
        if key in seen:
            raise ValueError(f"duplicate schedule: {job.name} and {seen[key]}")
        seen[key] = job.name
    check_kickoff_units()


def deployment_paused(api_url: str, job: Job) -> bool:
    """Read one deployment through Prefect's named-deployment API endpoint."""
    endpoint = (
        f"{api_url.rstrip('/')}/deployments/name/"
        f"{quote(job.flow, safe='')}/{quote(job.deployment, safe='')}"
    )
    with urlopen(endpoint, timeout=10) as response:  # noqa: S310 - operator URL
        payload = json.load(response)
    if not isinstance(payload, Mapping) or type(payload.get("paused")) is not bool:
        raise ValueError(f"{endpoint}: deployment response lacks boolean paused")
    return payload["paused"]


def timer_is_enabled(job: Job) -> bool:
    result = subprocess.run(
        ["systemctl", "is-enabled", f"job-{job.name}.timer"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode == 0 and result.stdout.strip() == "enabled":
        return True
    if result.returncode == 0:
        raise ValueError(
            f"job-{job.name}.timer: unexpected is-enabled result {result.stdout.strip()!r}"
        )
    return False


def dual_active_units(api_url: str) -> list[str]:
    """Return unsafe timer/deployment pairs without mutating either authority."""
    active: list[str] = []
    for job in JOBS:
        paused = deployment_paused(api_url, job)
        enabled = timer_is_enabled(job)
        if enabled and not paused:
            active.append(f"job-{job.name}.timer")
    return active


def check_cutover() -> None:
    api_url = os.environ.get("PREFECT_API_URL", "").strip()
    if not api_url:
        raise ValueError("PREFECT_API_URL is required for --check-cutover")
    active = dual_active_units(api_url)
    if active:
        raise ValueError(f"dual-active NCP timer(s): {', '.join(active)}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-imports", action="store_true", help="for structural tests only"
    )
    parser.add_argument(
        "--check-cutover",
        action="store_true",
        help="read Prefect deployment pause state and local systemd timer enablement",
    )
    args = parser.parse_args(argv)
    check_all(check_imports=not args.skip_imports)
    if args.check_cutover:
        check_cutover()
    network_calls = len(JOBS) if args.check_cutover else 0
    print(
        f"checked {len(JOBS)} Prefect and {len(KICKOFF_SLOTS)} kickoff "
        f"NCP job timer(s); network calls: {network_calls}"
    )
    return 0


def run_cli(argv: list[str] | None = None) -> int:
    try:
        return main(argv)
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ncp job timer check failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(run_cli())
