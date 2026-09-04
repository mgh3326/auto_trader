#!/usr/bin/env python3
"""Render a fail-closed 14-day Sentry performance report for ``/invest``."""

from __future__ import annotations

import argparse
import os
import statistics
import sys
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import requests

SENTRY_ORG = "mgh3326-daum"
SENTRY_EVENTS_URL = f"https://sentry.io/api/0/organizations/{SENTRY_ORG}/events/"
REQUEST_TIMEOUT_SECONDS = 30


@dataclass(frozen=True)
class Window:
    start: str
    end: str


@dataclass(frozen=True)
class Latency:
    count: int
    p50: float
    p95: float


class SentryQueryError(RuntimeError):
    """Sentry rejected a query or gave an unusable observability response."""


def _parse_moment(value: str) -> str:
    normalized = value.strip()
    if normalized == "now":
        return datetime.now(UTC).isoformat()
    try:
        return datetime.fromisoformat(normalized).astimezone(UTC).isoformat()
    except ValueError as exc:
        raise ValueError(f"invalid ISO-8601 boundary: {value!r}") from exc


def parse_window(value: str) -> Window:
    try:
        start, end = value.split("..", maxsplit=1)
    except ValueError as exc:
        raise ValueError("--window must use START..END format") from exc
    return Window(start=_parse_moment(start), end=_parse_moment(end))


def default_window() -> Window:
    return Window(
        start=(datetime.now(UTC) - timedelta(days=14)).isoformat(),
        end=datetime.now(UTC).isoformat(),
    )


def _base_params(window: Window, query: str) -> list[tuple[str, str]]:
    return [
        ("project", "-1"),
        ("start", window.start),
        ("end", window.end),
        ("query", query),
        ("per_page", "100"),
    ]


def latency_query_params(window: Window) -> list[tuple[str, str]]:
    return _base_params(
        window,
        'transaction:"GET /invest/api/*" span.op:"http.server"',
    ) + [
        ("dataset", "spans"),
        ("field", "transaction"),
        ("field", "count()"),
        ("field", "p50(span.duration)"),
        ("field", "p95(span.duration)"),
    ]


def span_share_query_params(window: Window, op: str) -> list[tuple[str, str]]:
    return _base_params(
        window,
        f'transaction:"GET /invest/api/*" span.op:"{op}"',
    ) + [
        ("dataset", "spans"),
        ("field", "transaction"),
        ("field", "sum(span.duration)"),
    ]


def rum_query_params(window: Window) -> list[tuple[str, str]]:
    return _base_params(window, 'message:"invest.rum"') + [
        ("dataset", "errors"),
        ("field", "tags[route]"),
        ("field", "tags[n_requests]"),
        ("field", "tags[wall_ms]"),
        ("field", "tags[slowest]"),
    ]


def fetch(
    *, token: str, params: list[tuple[str, str]], label: str
) -> list[dict[str, Any]]:
    try:
        response = requests.get(
            SENTRY_EVENTS_URL,
            params=params,
            headers={"Authorization": f"Bearer {token}"},
            timeout=REQUEST_TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        payload = response.json()
    except (requests.RequestException, ValueError) as exc:
        raise SentryQueryError(f"Sentry {label} query failed") from exc
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, list):
        raise SentryQueryError(f"Sentry {label} response has no data")
    return [row for row in data if isinstance(row, dict)]


def _number(value: object) -> float | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def latency_rows(rows: list[dict[str, Any]]) -> dict[str, Latency]:
    result: dict[str, Latency] = {}
    for row in rows:
        transaction = row.get("transaction")
        count = _number(row.get("count()"))
        p50 = _number(row.get("p50(span.duration)"))
        p95 = _number(row.get("p95(span.duration)"))
        if (
            isinstance(transaction, str)
            and count is not None
            and p50 is not None
            and p95 is not None
        ):
            result[transaction] = Latency(int(count), p50, p95)
    return result


def duration_rows(rows: list[dict[str, Any]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for row in rows:
        transaction = row.get("transaction")
        total = _number(row.get("sum(span.duration)"))
        if isinstance(transaction, str) and total is not None:
            result[transaction] = total
    return result


def _tag(row: dict[str, Any], name: str) -> str | None:
    value = row.get(f"tags[{name}]")
    return value if isinstance(value, str) else None


def rum_rows(rows: list[dict[str, Any]]) -> dict[str, list[int]]:
    grouped: dict[str, list[int]] = {}
    for row in rows:
        route = _tag(row, "route")
        n_requests = _number(_tag(row, "n_requests"))
        if route is not None and n_requests is not None:
            grouped.setdefault(route, []).append(int(n_requests))
    return grouped


def render_report(
    *,
    window: Window,
    latency: dict[str, Latency],
    db_ms: dict[str, float],
    ext_ms: dict[str, float],
    rum: dict[str, list[int]],
) -> str:
    if not latency or not rum:
        raise SentryQueryError(
            "refusing incomplete report: missing route or RUM samples"
        )
    lines = [
        "# /invest performance report",
        "",
        f"- Window: `{window.start}` → `{window.end}`",
        "- Source: Sentry spans (`http.server`, `db.*`, `http.client`) and `invest.rum` messages.",
        "",
        "## Browser fan-out",
        "",
        "| Route | Samples | Median API requests |",
        "|---|---:|---:|",
    ]
    for route, samples in sorted(rum.items()):
        lines.append(f"| {route} | {len(samples)} | {statistics.median(samples):.1f} |")

    lines.extend(
        [
            "",
            "## Endpoint latency and component share",
            "",
            "| Endpoint | n | p50 (ms) | p95 (ms) | DB share | External HTTP share |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for transaction, metric in sorted(latency.items()):
        db = db_ms.get(transaction, 0.0)
        ext = ext_ms.get(transaction, 0.0)
        component_total = db + ext
        db_share = db / component_total if component_total else 0.0
        ext_share = ext / component_total if component_total else 0.0
        lines.append(
            f"| {transaction} | {metric.count} | {metric.p50:.1f} | {metric.p95:.1f} | "
            f"{db_share:.1%} | {ext_share:.1%} |"
        )
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--window", help="ISO-8601 START..END; defaults to the last 14 days"
    )
    parser.add_argument(
        "--output", type=Path, help="write only a complete report to this path"
    )
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
        window = parse_window(args.window) if args.window else default_window()
        latency = latency_rows(
            fetch(token=token, params=latency_query_params(window), label="latency")
        )
        db_ms = duration_rows(
            fetch(
                token=token, params=span_share_query_params(window, "db.*"), label="db"
            )
        )
        ext_ms = duration_rows(
            fetch(
                token=token,
                params=span_share_query_params(window, "http.client"),
                label="external HTTP",
            )
        )
        rum = rum_rows(fetch(token=token, params=rum_query_params(window), label="RUM"))
        report = render_report(
            window=window, latency=latency, db_ms=db_ms, ext_ms=ext_ms, rum=rum
        )
    except (SentryQueryError, ValueError) as exc:
        print(f"invest performance report failed: {exc}", file=sys.stderr)
        return 1
    if args.output:
        args.output.write_text(report, encoding="utf-8")
    else:
        print(report, end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
