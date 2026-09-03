#!/usr/bin/env python3
"""Compare Sentry span latency between two explicit deployment windows.

This is an operator-run, read-only report generator.  It never falls back to
an unauthenticated request: ``SENTRY_AUTH_TOKEN`` is required.  The only
network client is ``requests`` in this file, and the query is restricted to
the spans dataset.
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, date, datetime, time
from typing import Any

import requests

SENTRY_ORG = "mgh3326-daum"
SENTRY_EVENTS_URL = f"https://sentry.io/api/0/organizations/{SENTRY_ORG}/events/"
DEFAULT_BEFORE = "2026-08-20..2026-09-02T23:48+09"
DEFAULT_AFTER = "2026-09-02T23:48+09..now"
MAC_SERVER_NAME = "mbp-server"
NCP_SERVER_NAME = "vm-naver-20260820095006"
MIN_SAMPLE_SIZE = 5
REQUEST_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Window:
    name: str
    start: str
    end: str


@dataclass(frozen=True)
class Summary:
    count: int
    p50: float | None
    p95: float | None


class SentryQueryError(RuntimeError):
    """Sentry rejected or returned an unusable aggregate response."""


def _parse_moment(value: str) -> str:
    normalized = value.strip()
    if normalized == "now":
        return datetime.now(UTC).isoformat()
    try:
        if len(normalized) == 10:
            return datetime.combine(
                date.fromisoformat(normalized), time.min, tzinfo=UTC
            ).isoformat()
        return datetime.fromisoformat(normalized).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 range boundary: {value!r}") from exc


def parse_window(name: str, value: str) -> Window:
    try:
        start, end = value.split("..", maxsplit=1)
    except ValueError as exc:
        raise ValueError(f"{name} must use START..END format") from exc
    return Window(name=name, start=_parse_moment(start), end=_parse_moment(end))


def _group_field(group: str) -> str:
    if group == "server_name":
        return "server_name"
    if group == "order.path":
        return "tag[order.path,string]"
    raise ValueError(f"unsupported group: {group}")


def build_query_params(window: Window, group: str) -> list[tuple[str, str]]:
    """Build the bounded Explore-table query for one comparison window."""
    group_field = _group_field(group)
    return [
        ("dataset", "spans"),
        ("project", "-1"),
        ("start", window.start),
        ("end", window.end),
        ("query", 'transaction:"tools/call *"'),
        ("per_page", "100"),
        ("field", "transaction"),
        ("field", group_field),
        ("field", "count()"),
        ("field", "p50(span.duration)"),
        ("field", "p95(span.duration)"),
    ]


def fetch_window(*, token: str, window: Window, group: str) -> dict[str, Any]:
    """Perform one authenticated, read-only Sentry Explore aggregate query."""
    response = requests.get(
        SENTRY_EVENTS_URL,
        params=build_query_params(window, group),
        headers={"Authorization": f"Bearer {token}"},
        timeout=REQUEST_TIMEOUT_SECONDS,
    )
    try:
        response.raise_for_status()
    except requests.RequestException as exc:
        raise SentryQueryError(
            f"Sentry {window.name} query failed: {type(exc).__name__}"
        ) from exc
    try:
        payload = response.json()
    except ValueError as exc:
        raise SentryQueryError(f"Sentry {window.name} returned invalid JSON") from exc
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        raise SentryQueryError(f"Sentry {window.name} response has no aggregate data")
    return payload


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def summaries_from_payload(
    payload: dict[str, Any], group: str
) -> dict[tuple[str, str], Summary]:
    group_field = _group_field(group)
    summaries: dict[tuple[str, str], Summary] = {}
    for row in payload["data"]:
        if not isinstance(row, dict):
            continue
        transaction = row.get("transaction")
        group_value = row.get(group_field, row.get(group))
        count = _number(row.get("count()"))
        if (
            not isinstance(transaction, str)
            or not isinstance(group_value, str)
            or count is None
        ):
            continue
        summaries[(transaction, group_value)] = Summary(
            count=int(count),
            p50=_number(row.get("p50(span.duration)")),
            p95=_number(row.get("p95(span.duration)")),
        )
    return summaries


def _format_summary(summary: Summary | None) -> str:
    if summary is None:
        return "부족 (n=0)"
    if summary.count < MIN_SAMPLE_SIZE or summary.p50 is None or summary.p95 is None:
        return f"부족 (n={summary.count})"
    return f"n={summary.count}; p50={summary.p50:.1f}ms; p95={summary.p95:.1f}ms"


def _improvement(before: Summary | None, after: Summary | None, percentile: str) -> str:
    if (
        before is None
        or after is None
        or before.count < MIN_SAMPLE_SIZE
        or after.count < MIN_SAMPLE_SIZE
    ):
        return "부족"
    baseline = before.p50 if percentile == "p50" else before.p95
    current = after.p50 if percentile == "p50" else after.p95
    if baseline is None or current is None or baseline <= 0:
        return "부족"
    return f"{((baseline - current) / baseline) * 100:+.1f}%"


def render_report(
    *,
    before: Window,
    after: Window,
    before_rows: dict[tuple[str, str], Summary],
    after_rows: dict[tuple[str, str], Summary],
    group: str,
) -> str:
    if group == "server_name":
        tools = sorted({tool for tool, _ in before_rows | after_rows})
        lines = [
            "# Sentry performance comparison",
            "",
            '- Dataset: `spans`; filter: `transaction:"tools/call *"`',
            f"- Before: `{before.start}` → `{before.end}` ({MAC_SERVER_NAME})",
            f"- After: `{after.start}` → `{after.end}` ({NCP_SERVER_NAME})",
            f"- `n < {MIN_SAMPLE_SIZE}` is reported as 부족; latency values are Sentry milliseconds.",
            "",
            "| Tool | Mac | NCP | p50 improvement | p95 improvement |",
            "|---|---|---|---:|---:|",
        ]
        for tool in tools:
            mac = before_rows.get((tool, MAC_SERVER_NAME))
            ncp = after_rows.get((tool, NCP_SERVER_NAME))
            lines.append(
                f"| {tool} | {_format_summary(mac)} | {_format_summary(ncp)} | "
                f"{_improvement(mac, ncp, 'p50')} | {_improvement(mac, ncp, 'p95')} |"
            )
        return "\n".join(lines) + "\n"

    keys = sorted(before_rows.keys() | after_rows.keys())
    lines = [
        "# Sentry performance comparison",
        "",
        f'- Dataset: `spans`; filter: `transaction:"tools/call *"`; group: `{group}`',
        f"- Before: `{before.start}` → `{before.end}`",
        f"- After: `{after.start}` → `{after.end}`",
        f"- `n < {MIN_SAMPLE_SIZE}` is reported as 부족; latency values are Sentry milliseconds.",
        "",
        "| Tool | Group | Before | After | p50 improvement | p95 improvement |",
        "|---|---|---|---|---:|---:|",
    ]
    for tool, group_value in keys:
        before_summary = before_rows.get((tool, group_value))
        after_summary = after_rows.get((tool, group_value))
        lines.append(
            f"| {tool} | {group_value} | {_format_summary(before_summary)} | "
            f"{_format_summary(after_summary)} | "
            f"{_improvement(before_summary, after_summary, 'p50')} | "
            f"{_improvement(before_summary, after_summary, 'p95')} |"
        )
    return "\n".join(lines) + "\n"


def _store_handoffkeep(report: str, report_date: str) -> None:
    try:
        result = subprocess.run(
            [
                "handoffkeep",
                "doc",
                "put",
                "--key",
                f"perf/sentry-compare-{report_date}.md",
                "--kind",
                "report",
            ],
            input=report,
            text=True,
            capture_output=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeError("handoffkeep report storage failed") from exc
    if result.returncode != 0:
        raise RuntimeError("handoffkeep report storage failed")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--before", default=DEFAULT_BEFORE)
    parser.add_argument("--after", default=DEFAULT_AFTER)
    parser.add_argument(
        "--group", choices=("server_name", "order.path"), default="server_name"
    )
    parser.add_argument("--handoffkeep", action="store_true")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    token = os.getenv("SENTRY_AUTH_TOKEN", "").strip()
    if not token:
        print(
            "SENTRY_AUTH_TOKEN is required; refusing unauthenticated query",
            file=sys.stderr,
        )
        return 2
    try:
        before = parse_window("--before", args.before)
        after = parse_window("--after", args.after)
        before_rows = summaries_from_payload(
            fetch_window(token=token, window=before, group=args.group), args.group
        )
        after_rows = summaries_from_payload(
            fetch_window(token=token, window=after, group=args.group), args.group
        )
        report = render_report(
            before=before,
            after=after,
            before_rows=before_rows,
            after_rows=after_rows,
            group=args.group,
        )
        if args.handoffkeep:
            _store_handoffkeep(report, datetime.now(UTC).strftime("%Y%m%d"))
    except (SentryQueryError, ValueError, RuntimeError) as exc:
        print(f"performance comparison failed: {exc}", file=sys.stderr)
        return 1
    print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
