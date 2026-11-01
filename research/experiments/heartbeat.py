"""Experiment heartbeat (ROB-1429) — read-only.

Lists every epoch in ``research/experiments/heartbeat.json`` with its last
advance, read from the epoch's own ledger or snapshot file, and the number of
sessions of the epoch's calendar that have closed since. An epoch idle for more
than ``idle_red_after_sessions`` sessions prints RED.

Read-only by construction: it opens files for reading only, takes no lock,
imports no app, broker or database module and opens no network connection.
Ledger paths are private and live outside the repository, so the committed
config leaves them null; pass them with ``--path ID=PATH`` (or point
``--config`` at a private copy).

Exit codes: 0 every epoch OK, 1 at least one RED, 2 no RED but at least one
UNKNOWN (missing path, unreadable source, no advance found), 3 usage or config
error (unreadable or malformed ``--config``, bad ``--path``/``--now``).

Usage::

    uv run python -m research.experiments.heartbeat \\
        --path ROB-1391="$PRIVATE/rob1391/ledger" \\
        --path H5="$PRIVATE/h5/weekly-snapshot.json"
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

DEFAULT_CONFIG = Path(__file__).with_name("heartbeat.json")
EVENTS_FILE = "events.jsonl"
SEAL_KIND = "sealed"


EXIT_USAGE = 3
CALENDARS = frozenset({"XKRX", "XNYS", "24/7"})
SOURCE_LISTS = {"hash_chained_ledger": "advance_kinds", "json_timestamps": "select"}


class ConfigError(Exception):
    """The heartbeat config cannot be read or has the wrong shape."""


class _Parser(argparse.ArgumentParser):
    def error(self, message: str):  # type: ignore[override]
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"{self.prog}: error: {message}\n")


def load_config(path: Path) -> dict[str, Any]:
    """Read and shape-check the config; every failure is a ConfigError."""

    try:
        config = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ConfigError(f"cannot read {path} ({exc.__class__.__name__})") from exc
    except json.JSONDecodeError as exc:
        raise ConfigError(f"{path} is not JSON (line {exc.lineno})") from exc
    if not isinstance(config, dict):
        raise ConfigError("config must be an object")
    red = config.get("idle_red_after_sessions")
    if type(red) is not int or red < 1:
        raise ConfigError("idle_red_after_sessions must be an integer >= 1")
    epochs = config.get("epochs")
    if not isinstance(epochs, list) or not epochs:
        raise ConfigError("epochs must be a non-empty list")
    for i, epoch in enumerate(epochs):
        where = f"epochs[{i}]"
        if not isinstance(epoch, dict) or not isinstance(epoch.get("id"), str):
            raise ConfigError(f"{where} must be an object with a string id")
        if epoch.get("calendar") not in CALENDARS:
            raise ConfigError(f"{where}.calendar must be one of {sorted(CALENDARS)}")
        source = epoch.get("source")
        kind = source.get("kind") if isinstance(source, dict) else None
        if kind not in SOURCE_LISTS:
            raise ConfigError(
                f"{where}.source.kind must be one of {sorted(SOURCE_LISTS)}"
            )
        listed = source.get(SOURCE_LISTS[kind])
        if not isinstance(listed, list) or not all(isinstance(x, str) for x in listed):
            raise ConfigError(
                f"{where}.source.{SOURCE_LISTS[kind]} must be a list of strings"
            )
        if source.get("path") is not None and not isinstance(source.get("path"), str):
            raise ConfigError(f"{where}.source.path must be a string or null")
    return config


class AdvanceUnavailable(Exception):
    """The epoch's last advance cannot be established from its source."""


@dataclass(frozen=True, slots=True)
class EpochResult:
    epoch_id: str
    status: str  # "OK" | "RED" | "UNKNOWN"
    calendar: str
    last_advance: dt.datetime | None
    idle_sessions: int | None
    detail: str


def _parse_ts(raw: Any, where: str) -> dt.datetime:
    if not isinstance(raw, str):
        raise AdvanceUnavailable(f"{where}: timestamp is not a string")
    try:
        value = dt.datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AdvanceUnavailable(f"{where}: unparseable timestamp") from exc
    if value.tzinfo is None:
        raise AdvanceUnavailable(f"{where}: timestamp has no timezone")
    return value.astimezone(dt.UTC)


def _ledger_last_advance(
    path: Path, advance_kinds: list[str]
) -> tuple[dt.datetime, str]:
    events = path / EVENTS_FILE if path.is_dir() else path
    try:
        lines = events.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise AdvanceUnavailable(
            f"cannot read {events.name} ({exc.__class__.__name__})"
        ) from exc
    last: dt.datetime | None = None
    sealed: dt.datetime | None = None
    for lineno, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError as exc:
            raise AdvanceUnavailable(f"line {lineno} is not JSON") from exc
        if not isinstance(event, dict):
            raise AdvanceUnavailable(f"line {lineno} is not an object")
        kind = event.get("kind")
        if kind in advance_kinds:
            ts = _parse_ts(event.get("recorded_at"), f"line {lineno}")
            last = ts if last is None or ts > last else last
        elif kind == SEAL_KIND and sealed is None:
            sealed = _parse_ts(event.get("recorded_at"), f"line {lineno}")
    if last is not None:
        return last, "last advance event"
    if sealed is not None:
        return sealed, "never advanced since seal"
    raise AdvanceUnavailable("no advance or seal event in the ledger")


def _select(node: Any, path: list[str]) -> list[Any]:
    if not path:
        return [node]
    head, rest = path[0], path[1:]
    if head == "*":
        if not isinstance(node, list):
            return []
        return [v for item in node for v in _select(item, rest)]
    if isinstance(node, dict) and head in node:
        return _select(node[head], rest)
    return []


def _json_last_advance(path: Path, select: list[str]) -> tuple[dt.datetime, str]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise AdvanceUnavailable(
            f"cannot read {path.name} ({exc.__class__.__name__})"
        ) from exc
    except json.JSONDecodeError as exc:
        raise AdvanceUnavailable(f"{path.name} is not JSON") from exc
    values = _select(payload, select)
    if not values:
        raise AdvanceUnavailable(f"no timestamp at {'.'.join(select)}")
    stamps = [_parse_ts(v, ".".join(select)) for v in values]
    return max(stamps), f"latest of {len(stamps)} timestamp(s)"


def last_advance(source: dict[str, Any], path: Path) -> tuple[dt.datetime, str]:
    kind = source.get("kind")
    if kind == "hash_chained_ledger":
        return _ledger_last_advance(path, list(source["advance_kinds"]))
    if kind == "json_timestamps":
        return _json_last_advance(path, list(source["select"]))
    raise AdvanceUnavailable(f"unknown source kind {kind!r}")


@lru_cache(maxsize=4)
def _exchange_calendar(name: str) -> Any:
    import exchange_calendars as xcals

    return xcals.get_calendar(name)


def idle_sessions(calendar: str, since: dt.datetime, now: dt.datetime) -> int:
    """Sessions of ``calendar`` whose close lies in ``(since, now]``."""

    if now <= since:
        return 0
    if calendar == "24/7":
        # One session per UTC day; it closes at the next UTC midnight.
        first_close = dt.datetime.combine(
            since.date(), dt.time(), dt.UTC
        ) + dt.timedelta(days=1)
        if first_close > now:
            return 0
        return (now - first_close).days + 1
    cal = _exchange_calendar(calendar)
    start = max(since.date() - dt.timedelta(days=1), cal.first_session.date())
    end = now.date()
    if end > cal.last_session.date():
        raise AdvanceUnavailable(f"{calendar} calendar ends before {end}")
    count = 0
    for session in cal.sessions_in_range(start.isoformat(), end.isoformat()):
        close = cal.session_close(session).to_pydatetime()
        if since < close <= now:
            count += 1
    return count


def evaluate(
    config: dict[str, Any],
    paths: dict[str, Path],
    now: dt.datetime,
) -> list[EpochResult]:
    threshold = int(config["idle_red_after_sessions"])
    results: list[EpochResult] = []
    for epoch in config["epochs"]:
        epoch_id = epoch["id"]
        calendar = epoch["calendar"]
        source = epoch["source"]
        raw_path = paths.get(epoch_id)
        if raw_path is None and source.get("path"):
            raw_path = Path(source["path"]).expanduser()
        if raw_path is None:
            results.append(
                EpochResult(
                    epoch_id,
                    "UNKNOWN",
                    calendar,
                    None,
                    None,
                    f"no path configured (pass --path {epoch_id}=PATH)",
                )
            )
            continue
        try:
            advanced_at, how = last_advance(source, raw_path)
            if advanced_at > now:
                raise AdvanceUnavailable("last advance is after now (clock skew?)")
            idle = idle_sessions(calendar, advanced_at, now)
        except AdvanceUnavailable as exc:
            results.append(
                EpochResult(epoch_id, "UNKNOWN", calendar, None, None, str(exc))
            )
            continue
        status = "RED" if idle > threshold else "OK"
        results.append(
            EpochResult(
                epoch_id,
                status,
                calendar,
                advanced_at,
                idle,
                f"{how}; idle {idle} > {threshold}" if status == "RED" else how,
            )
        )
    return results


def render(results: list[EpochResult]) -> str:
    lines = []
    for r in results:
        last = r.last_advance.isoformat() if r.last_advance else "-"
        idle = "-" if r.idle_sessions is None else str(r.idle_sessions)
        lines.append(
            f"{r.status:<7} {r.epoch_id:<16} last_advance={last} "
            f"idle_sessions={idle} calendar={r.calendar} ({r.detail})"
        )
    return "\n".join(lines)


def exit_code(results: list[EpochResult]) -> int:
    if any(r.status == "RED" for r in results):
        return 1
    if any(r.status == "UNKNOWN" for r in results):
        return 2
    return 0


def _path_arg(raw: str) -> tuple[str, Path]:
    epoch_id, sep, path = raw.partition("=")
    if not sep or not epoch_id or not path:
        raise argparse.ArgumentTypeError("expected ID=PATH")
    return epoch_id, Path(path).expanduser()


def main(argv: list[str] | None = None) -> int:
    parser = _Parser(description="Read-only experiment heartbeat")
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument(
        "--path",
        type=_path_arg,
        action="append",
        default=[],
        metavar="ID=PATH",
        help="ledger dir or snapshot file for an epoch",
    )
    parser.add_argument(
        "--now", default=None, help="ISO-8601 with timezone (default: now, UTC)"
    )
    args = parser.parse_args(argv)
    try:
        config = load_config(args.config)
    except ConfigError as exc:
        parser.error(f"config: {exc}")
    known = {e["id"] for e in config["epochs"]}
    paths = dict(args.path)
    unknown = sorted(set(paths) - known)
    if unknown:
        parser.error(f"--path for unknown epoch(s): {', '.join(unknown)}")
    try:
        now = (
            dt.datetime.now(dt.UTC)
            if args.now is None
            else _parse_ts(args.now, "--now")
        )
    except AdvanceUnavailable as exc:
        parser.error(str(exc))
    results = evaluate(config, paths, now)
    print(render(results))
    return exit_code(results)


if __name__ == "__main__":
    sys.exit(main())
