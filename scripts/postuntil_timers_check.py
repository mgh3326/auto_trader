#!/usr/bin/env python3
"""Validate checked-in postuntil/systemd timer pairs without making network calls.

Each migrated job carries its immutable Prefect provenance in comments in its
TOML.  The comments keep postuntil's configuration surface unchanged while
giving this checker an exact byte-level request-body oracle.
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
TOML_DIR = ROOT / "ops/ncp/postuntil"
TIMER_DIR = ROOT / "ops/ncp/systemd"
_COMMENT = re.compile(r"^# (?P<key>Prefect\w+): (?P<value>.+)$", re.MULTILINE)
_ON_CALENDAR = re.compile(r"^OnCalendar=(?P<value>.+)$", re.MULTILINE)
_WEEKDAYS = {"Sun": 0, "Mon": 1, "Tue": 2, "Wed": 3, "Thu": 4, "Fri": 5, "Sat": 6}


@dataclass(frozen=True)
class Cron:
    minute: set[int]
    hour: set[int]
    weekday: set[int]


def _field(value: str, minimum: int, maximum: int) -> set[int]:
    values: set[int] = set()
    for part in value.split(","):
        base, slash, step = part.partition("/")
        stride = int(step) if slash else 1
        if stride < 1:
            raise ValueError(f"invalid cron stride: {value}")
        if base == "*":
            start, end = minimum, maximum
        elif "-" in base:
            start_text, end_text = base.split("-", 1)
            start, end = int(start_text), int(end_text)
        else:
            start = end = int(base)
        if not minimum <= start <= end <= maximum:
            raise ValueError(f"cron field out of range: {value}")
        values.update(range(start, end + 1, stride))
    return values


def parse_cron(value: str) -> Cron:
    minute, hour, _day, _month, weekday = value.split()
    return Cron(
        minute=_field(minute, 0, 59),
        hour=_field(hour, 0, 23),
        weekday={0 if day == 7 else day for day in _field(weekday, 0, 7)},
    )


def parse_oncalendar(value: str) -> Cron:
    """Parse this repository's KST timer format, not general systemd syntax."""

    weekday_text, _date, time_text, timezone = value.split()
    if timezone != "Asia/Seoul":
        raise ValueError(f"timer must remain KST: {value}")
    if weekday_text == "*":
        weekdays = set(range(7))
    elif ".." in weekday_text:
        start, end = weekday_text.split("..", 1)
        weekdays = set(range(_WEEKDAYS[start], _WEEKDAYS[end] + 1))
    else:
        weekdays = {_WEEKDAYS[weekday_text]}
    hour, minute, second = (int(piece) for piece in time_text.split(":"))
    if second != 0:
        raise ValueError(f"timer must run on an exact minute: {value}")
    return Cron(minute={minute}, hour={hour}, weekday=weekdays)


def next_runs(cron: Cron, count: int = 5) -> list[datetime]:
    current = datetime(2026, 9, 4, tzinfo=ZoneInfo("Asia/Seoul"))
    found: list[datetime] = []
    while len(found) < count:
        if (
            current.minute in cron.minute
            and current.hour in cron.hour
            # Python is Monday=0 while both cron and systemd use Sunday=0.
            and (current.weekday() + 1) % 7 in cron.weekday
        ):
            found.append(current)
        current += timedelta(minutes=1)
        if current.year > 2027:
            raise ValueError("could not find five schedule occurrences")
    return found


def _metadata(path: Path) -> dict[str, str]:
    return {
        match["key"]: match["value"] for match in _COMMENT.finditer(path.read_text())
    }


def check_pair(toml_path: Path, postuntil: str) -> None:
    metadata = _metadata(toml_path)
    expected_body = metadata.get("PrefectParameters")
    if expected_body is None:
        raise ValueError(f"{toml_path}: missing # PrefectParameters provenance")
    config = tomllib.loads(toml_path.read_text())
    body = config.get("body")
    if body != expected_body:
        raise ValueError(
            f"{toml_path}: body differs from Prefect deployment parameters"
        )
    json.loads(body)
    result = subprocess.run(
        [postuntil, "run", "-f", str(toml_path), "--dry-run"],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode:
        raise ValueError(
            f"{toml_path}: postuntil dry-run failed: {result.stderr.strip()}"
        )
    if not result.stdout.strip():
        raise ValueError(f"{toml_path}: postuntil dry-run emitted no preview")

    timer_path = TIMER_DIR / f"kick-{toml_path.stem}.timer"
    timer = timer_path.read_text()
    oncalendar = _ON_CALENDAR.search(timer)
    if oncalendar is None:
        raise ValueError(f"{timer_path}: missing OnCalendar")
    cron = metadata.get("PrefectCron")
    if cron is None:
        raise ValueError(f"{toml_path}: missing # PrefectCron provenance")
    if next_runs(parse_cron(cron)) != next_runs(parse_oncalendar(oncalendar["value"])):
        raise ValueError(f"{timer_path}: OnCalendar differs from Prefect cron {cron}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--postuntil", default="postuntil")
    args = parser.parse_args(argv)
    tomls = sorted(TOML_DIR.glob("*.toml"))
    for toml_path in tomls:
        check_pair(toml_path, args.postuntil)
    print(f"checked {len(tomls)} postuntil timer job(s); network calls: 0")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, tomllib.TOMLDecodeError) as exc:
        print(f"postuntil timer check failed: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
