"""Export TREND-TSMOM-v1 runner tick timestamps from ``docker logs --timestamps``.

The TREND lane has no durable tick stamp: the runner
(``scripts/binance_trend_demo.py --loop``) prints one JSON line per tick and the
only durable copy of when those ticks happened is the container log. The
experiment heartbeat reads the epoch ``TREND-TSMOM-v1`` from a timestamp
snapshot, so this exporter turns the log into that snapshot::

    docker logs --timestamps at-trend-demo 2>&1 \\
        | python -m scripts.binance_trend_tick_export --out PATH

Read-only and offline: it reads log text (stdin or ``--input``) and writes one
JSON file. No app, broker, database or network import, and no order path.

A kept line is a docker ``--timestamps`` line whose payload is one of the two
objects ``scripts/binance_trend_demo.py`` prints (closed shape, exact key sets):

* a tick result — ``{"decision_ts": <int ms>, "events": [{"symbol", "event",
  "detail"}, ...]}`` (``dataclasses.asdict(TrendTickResult)``);
* a failed tick — ``{"event": "blocked", "error_class": <str>}``.

Every other line — log noise, a cut-off line, unknown or missing keys, a
timestamp that does not parse — is ignored and counted, never guessed at.

The snapshot is ``{"schema": "trend-tick-export/v1", "lane": "TREND-TSMOM-v1",
"exported_at": <UTC ISO>, "ticks": [{"observed_at": <UTC ISO>,
"decision_ts": <int or null>}, ...]}`` sorted by ``observed_at``
and de-duplicated by it (first line wins). ``decision_ts`` is null for a blocked
tick. The write is a temp file + ``os.replace`` in the target directory, so a
reader never sees a half-written snapshot; a re-export replaces the previous
one.

Exit codes: 0 at least one tick written, 2 no tick found (nothing is written,
so a stale snapshot is never replaced by an empty-but-OK one), 3 usage or I/O
error.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

SCHEMA = "trend-tick-export/v1"
LANE = "TREND-TSMOM-v1"

EXIT_OK = 0
EXIT_NO_TICKS = 2
EXIT_USAGE = 3

# ``docker logs --timestamps`` prefixes each line with RFC 3339 UTC (``Z``) and
# a single space. Docker prints nanoseconds; Python keeps microseconds.
_PREFIX = re.compile(
    r"\A(?P<stamp>\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,9})?Z) (?P<line>.*)\Z"
)
_STAMP = re.compile(
    r"\A(?P<date>\d{4}-\d{2}-\d{2})T(?P<clock>\d{2}:\d{2}:\d{2})"
    r"(?:\.(?P<fraction>\d{1,9}))?Z\Z"
)

# Exact top-level key sets of the two objects the runner prints.
_TICK_KEYS = frozenset({"decision_ts", "events"})
_BLOCKED_KEYS = frozenset({"event", "error_class"})
# Allowed keys of one ``TrendEvent`` row; a row with any other key is not ours.
_EVENT_KEYS = frozenset({"symbol", "event", "detail"})

_NOT_TICK = object()


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def _parse_stamp(raw: str) -> dt.datetime:
    match = _STAMP.match(raw)
    if match is None:
        raise ValueError(f"not a docker timestamp: {raw!r}")
    fraction = (match.group("fraction") or "").ljust(9, "0")[:6]
    date = dt.date.fromisoformat(match.group("date"))
    clock = dt.time.fromisoformat(match.group("clock"))
    return dt.datetime.combine(
        date,
        clock.replace(microsecond=int(fraction)),
        tzinfo=dt.UTC,
    )


def tick_decision_ts(payload: Any) -> Any:
    """``decision_ts`` for a runner tick object, ``None`` for a blocked tick.

    Returns ``_NOT_TICK`` for anything else. The shapes are closed: an unknown
    or missing key, a non-``int`` ``decision_ts`` (``bool`` included) or a row
    that is not an object makes the line not ours instead of a guess.
    """

    if not isinstance(payload, dict):
        return _NOT_TICK
    keys = set(payload)
    if keys == _TICK_KEYS:
        decision_ts = payload["decision_ts"]
        if type(decision_ts) is not int:
            return _NOT_TICK
        events = payload["events"]
        if not isinstance(events, list):
            return _NOT_TICK
        for event in events:
            if not isinstance(event, dict) or set(event) - _EVENT_KEYS:
                return _NOT_TICK
        return decision_ts
    if keys == _BLOCKED_KEYS:
        if payload["event"] != "blocked":
            return _NOT_TICK
        if not isinstance(payload["error_class"], str):
            return _NOT_TICK
        return None
    return _NOT_TICK


@dataclass(frozen=True, slots=True)
class ParsedLog:
    ticks: list[dict[str, Any]]
    duplicate_lines: int
    ignored_lines: int


def parse_lines(lines: Iterable[str]) -> ParsedLog:
    """Keep only runner tick lines, ordered by and de-duplicated on the stamp."""

    by_stamp: dict[dt.datetime, dict[str, Any]] = {}
    duplicates = 0
    ignored = 0
    for raw in lines:
        line = raw.rstrip("\r\n")
        prefix = _PREFIX.match(line)
        if prefix is None:
            ignored += 1
            continue
        try:
            stamp = _parse_stamp(prefix.group("stamp"))
        except ValueError:
            ignored += 1
            continue
        try:
            payload = json.loads(prefix.group("line"))
        except json.JSONDecodeError:
            ignored += 1
            continue
        decision_ts = tick_decision_ts(payload)
        if decision_ts is _NOT_TICK:
            ignored += 1
            continue
        if stamp in by_stamp:
            duplicates += 1
            continue
        by_stamp[stamp] = {
            "observed_at": stamp.isoformat(),
            "decision_ts": decision_ts,
        }
    ticks = [by_stamp[stamp] for stamp in sorted(by_stamp)]
    return ParsedLog(ticks=ticks, duplicate_lines=duplicates, ignored_lines=ignored)


def build_snapshot(ticks: list[dict[str, Any]], *, now: dt.datetime) -> dict[str, Any]:
    return {
        "schema": SCHEMA,
        "lane": LANE,
        "exported_at": now.astimezone(dt.UTC).isoformat(),
        "ticks": ticks,
    }


def atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    """Write through a sibling temp file and ``os.replace``; never partially."""

    path.parent.mkdir(parents=True, exist_ok=True)
    staged = path.with_name(f".{path.name}.{os.getpid()}.{time.time_ns()}.partial")
    try:
        with staged.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(staged, path)
    except BaseException:
        staged.unlink(missing_ok=True)
        raise


def _parser() -> argparse.ArgumentParser:
    parser = _Parser(
        description=(
            "Export TREND-TSMOM-v1 runner tick timestamps from "
            "`docker logs --timestamps` text into the heartbeat snapshot."
        )
    )
    parser.add_argument(
        "--out",
        type=Path,
        required=True,
        help="snapshot path (private, outside the repository)",
    )
    parser.add_argument(
        "--input",
        type=Path,
        default=None,
        help="read the log from this file instead of stdin",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    out: Path = args.out
    if args.input is None:
        parsed = parse_lines(sys.stdin)
    else:
        try:
            with args.input.open("r", encoding="utf-8", errors="replace") as handle:
                parsed = parse_lines(handle)
        except OSError as exc:
            print(
                f"{Path(sys.argv[0]).name}: cannot read {args.input} "
                f"({exc.__class__.__name__})",
                file=sys.stderr,
            )
            return EXIT_USAGE

    summary: dict[str, Any] = {
        "schema": SCHEMA,
        "lane": LANE,
        "out": str(out),
        "ticks": len(parsed.ticks),
        "duplicate_lines": parsed.duplicate_lines,
        "ignored_lines": parsed.ignored_lines,
    }
    if not parsed.ticks:
        summary["written"] = False
        print(json.dumps(summary, sort_keys=True))
        print(
            f"{Path(sys.argv[0]).name}: no runner tick line found; no snapshot written",
            file=sys.stderr,
        )
        return EXIT_NO_TICKS

    snapshot = build_snapshot(parsed.ticks, now=dt.datetime.now(dt.UTC))
    try:
        atomic_write_json(out, snapshot)
    except OSError as exc:
        print(
            f"{Path(sys.argv[0]).name}: cannot write {out} ({exc.__class__.__name__})",
            file=sys.stderr,
        )
        return EXIT_USAGE
    summary["written"] = True
    summary["exported_at"] = snapshot["exported_at"]
    print(json.dumps(summary, sort_keys=True))
    return EXIT_OK


if __name__ == "__main__":
    sys.exit(main())
