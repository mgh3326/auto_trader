#!/usr/bin/env python3
"""Validate the static, safe subset of Prefect B-class script timers.

The Prefect source lives in a separate deployment repository, so its exact
subprocess argv is captured here as a reviewable provenance contract.  The
source file and line references in ``ncp-job-timers.md`` are the authority.
Only flows whose deployment parameters make the argv static appear below.
"""

from __future__ import annotations

import argparse
import os
import re
import shlex
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
SYSTEMD_DIR = ROOT / "ops/ncp/systemd"
RUNNER = "/root/at-run/ops/ncp/bin/at-job.sh"
JOBS_ENV_FILE = "/root/at-secrets/.env.jobs"
_DIRECTIVE = re.compile(r"^(?P<key>[A-Za-z][A-Za-z0-9]*)=(?P<value>.*)$")
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
class Job:
    name: str
    module: str
    argv: tuple[str, ...]
    cron: str
    timeout_seconds: int


# These are the Docker-mode argv captured from the Prefect flow functions with
# their deployed parameters.  Keep their order byte-for-byte: no shell parser
# or argument normalizer is allowed between the source flow and the unit.
JOBS = (
    Job(
        "kr-investor-flow-snapshots",
        "scripts.build_investor_flow_snapshots",
        (
            "--market",
            "kr",
            "--days",
            "5",
            "--batch-size",
            "100",
            "--concurrency",
            "4",
            "--all",
            "--commit",
        ),
        "10 18 * * 1-5",
        1800,
    ),
    Job("toss-warnings-sync", "scripts.sync_toss_warnings", (), "30 7 * * *", 3600),
    Job(
        "us-invest-screener-snapshots",
        "scripts.build_invest_screener_snapshots",
        (
            "--market",
            "us",
            "--batch-size",
            "200",
            "--concurrency",
            "4",
            "--all",
            "--common-stocks-only",
            "--commit",
        ),
        "10 6 * * 2-6",
        7200,
    ),
)


def captured_prefect_argv(job: Job, run: object) -> None:
    """Call a fake ``_run`` exactly as the eligible flow's Docker path does."""
    callback = run
    assert callable(callback)
    callback(["uv", "run", "python", "-m", job.module, *job.argv])


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


def expected_execstart(job: Job) -> list[str]:
    return [RUNNER, job.module, *job.argv]


def check_job(job: Job, *, check_imports: bool) -> None:
    service_path = SYSTEMD_DIR / f"job-{job.name}.service"
    timer_path = SYSTEMD_DIR / f"job-{job.name}.timer"
    service = _directives(service_path)
    timer = _directives(timer_path)
    if service.get("EnvironmentFile") != [JOBS_ENV_FILE]:
        raise ValueError(
            f"{service_path}: only {JOBS_ENV_FILE} may provide job environment"
        )
    actual_argv = shlex.split(service.get("ExecStart", [""])[0])
    if actual_argv != expected_execstart(job):
        raise ValueError(f"{service_path}: argv differs from Prefect capture")
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
    captured: list[list[str]] = []
    captured_prefect_argv(job, captured.append)
    if captured != [["uv", "run", "python", "-m", job.module, *job.argv]]:
        raise ValueError(
            f"{service_path}: fake Prefect _run did not capture expected argv"
        )
    if check_imports:
        result = subprocess.run(
            ["uv", "run", "python", "-c", f"import {job.module}"],
            capture_output=True,
            text=True,
            check=False,
            env={**os.environ, **_IMPORT_ONLY_ENV},
        )
        if result.returncode:
            raise ValueError(f"{job.module}: import failed: {result.stderr.strip()}")


def check_all(*, check_imports: bool) -> None:
    expected_services = {f"job-{job.name}.service" for job in JOBS}
    expected_timers = {f"job-{job.name}.timer" for job in JOBS}
    actual_services = {path.name for path in SYSTEMD_DIR.glob("job-*.service")}
    actual_timers = {path.name for path in SYSTEMD_DIR.glob("job-*.timer")}
    if actual_services != expected_services or actual_timers != expected_timers:
        raise ValueError(
            "job unit inventory is unpaired or contains an unreviewed unit"
        )
    seen: dict[tuple[datetime, ...], str] = {}
    for job in JOBS:
        check_job(job, check_imports=check_imports)
        occurrences = tuple(next_runs(parse_cron(job.cron)))
        if occurrences in seen:
            raise ValueError(f"duplicate schedule: {job.name} and {seen[occurrences]}")
        seen[occurrences] = job.name


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--skip-imports", action="store_true", help="for structural tests only"
    )
    args = parser.parse_args(argv)
    check_all(check_imports=not args.skip_imports)
    print(f"checked {len(JOBS)} NCP job timer(s); network calls: 0")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, subprocess.SubprocessError) as exc:
        print(f"ncp job timer check failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
