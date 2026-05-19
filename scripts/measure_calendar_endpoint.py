"""ROB-272 Phase 2 — measure /invest/api/calendar response time + payload size.

Compares fetch ranges so we can pick the initial Phase-2 lazy-loading range
(single-day vs ±3 vs ±7) and estimate build_calendar's fixed overhead.

Usage
-----
1. Start the dev server: `make dev`
2. Log in via browser, copy the session cookie value (whatever your auth uses;
   the script accepts a raw Cookie header).
3. Export it:
       export CALENDAR_COOKIE='session=abc...; other=...'
4. Run:
       uv run python scripts/measure_calendar_endpoint.py
   Optional flags:
       --host http://localhost:8000     base URL (default localhost:8000)
       --selected 2026-05-19            anchor date (default = today KST)
       --iterations 5                   per-scenario repeats (default 5)
       --warmup 1                       discarded warmup calls (default 1)

The script prints a table per scenario:
    name            | n  | p50 ms | p95 ms | mean ms | min ms | max ms | bytes
And a summary line for fixed-overhead estimation:
    single_day mean - per_day_marginal ≈ <fixed_ms>

Safety
------
Read-only GET requests. No broker/order/watch/order-intent side effects.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from urllib.parse import urlencode
from zoneinfo import ZoneInfo
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlparse

KST = ZoneInfo("Asia/Seoul")


@dataclass
class Scenario:
    name: str
    from_date: date
    to_date: date

    @property
    def days(self) -> int:
        return (self.to_date - self.from_date).days + 1


@dataclass
class Sample:
    elapsed_ms: float
    body_bytes: int
    status: int


def _grid_range(anchor: date) -> tuple[date, date]:
    """6-week Sunday-aligned grid containing the month of `anchor` (mirrors FE)."""
    first = anchor.replace(day=1)
    # Sunday-aligned start
    dow_sun_first = (first.weekday() + 1) % 7  # Mon=0 in weekday(); Sun=0 here
    start = first - timedelta(days=dow_sun_first)
    end = start + timedelta(days=41)
    return start, end


def _http_get(url: str, cookie: str, timeout: float = 30.0) -> Sample:
    parsed = urlparse(url)
    conn_cls = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
    conn = conn_cls(parsed.netloc, timeout=timeout)
    path = parsed.path + ("?" + parsed.query if parsed.query else "")
    headers = {
        "Cookie": cookie,
        "Accept": "application/json",
        "User-Agent": "rob-272-measure/1.0",
    }
    started = time.perf_counter()
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
    finally:
        conn.close()
    elapsed_ms = (time.perf_counter() - started) * 1000
    return Sample(elapsed_ms=elapsed_ms, body_bytes=len(body), status=resp.status)


def _run_scenario(
    scenario: Scenario,
    *,
    host: str,
    cookie: str,
    iterations: int,
    warmup: int,
) -> list[Sample]:
    qs = urlencode(
        {
            "from_date": scenario.from_date.isoformat(),
            "to_date": scenario.to_date.isoformat(),
            "tab": "all",
        }
    )
    url = f"{host}/invest/api/calendar?{qs}"
    samples: list[Sample] = []
    for i in range(warmup + iterations):
        sample = _http_get(url, cookie)
        if sample.status != 200:
            print(
                f"  [!] {scenario.name} got status {sample.status} "
                f"(body preview: {sample.body_bytes} bytes)",
                file=sys.stderr,
            )
        if i >= warmup:
            samples.append(sample)
    return samples


def _percentile(values: list[float], p: float) -> float:
    if not values:
        return float("nan")
    s = sorted(values)
    k = (len(s) - 1) * p
    f = int(k)
    c = min(f + 1, len(s) - 1)
    if f == c:
        return s[f]
    return s[f] + (s[c] - s[f]) * (k - f)


def _print_row(name: str, samples: list[Sample]) -> dict[str, float]:
    times = [s.elapsed_ms for s in samples]
    sizes = [s.body_bytes for s in samples]
    row = {
        "n": len(times),
        "p50": _percentile(times, 0.50),
        "p95": _percentile(times, 0.95),
        "mean": statistics.fmean(times) if times else float("nan"),
        "min": min(times) if times else float("nan"),
        "max": max(times) if times else float("nan"),
        "bytes_median": int(statistics.median(sizes)) if sizes else 0,
    }
    print(
        f"  {name:<18} | n={row['n']:>2} | p50={row['p50']:>7.1f} | "
        f"p95={row['p95']:>7.1f} | mean={row['mean']:>7.1f} | "
        f"min={row['min']:>7.1f} | max={row['max']:>7.1f} | "
        f"bytes(median)={row['bytes_median']:>8}"
    )
    return row


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", default="http://localhost:8000")
    parser.add_argument(
        "--selected",
        default=None,
        help="anchor ISO date (default = today KST)",
    )
    parser.add_argument("--iterations", type=int, default=5)
    parser.add_argument("--warmup", type=int, default=1)
    parser.add_argument(
        "--output-json",
        default=None,
        help="optional path to dump raw rows as JSON",
    )
    args = parser.parse_args()

    cookie = os.environ.get("CALENDAR_COOKIE")
    if not cookie:
        print(
            "ERROR: set CALENDAR_COOKIE env to a logged-in session cookie header "
            "(e.g. `export CALENDAR_COOKIE='session=...'`).",
            file=sys.stderr,
        )
        return 2

    anchor = (
        date.fromisoformat(args.selected)
        if args.selected
        else datetime.now(KST).date()
    )
    grid_start, grid_end = _grid_range(anchor)

    scenarios: list[Scenario] = [
        Scenario("42d_grid (current)", grid_start, grid_end),
        Scenario("7d (±3)", anchor - timedelta(days=3), anchor + timedelta(days=3)),
        Scenario(
            "15d (±7)", anchor - timedelta(days=7), anchor + timedelta(days=7)
        ),
        Scenario("single_day", anchor, anchor),
    ]

    print(f"host={args.host}  anchor={anchor.isoformat()}  "
          f"iterations={args.iterations}  warmup={args.warmup}")
    print()
    rows: dict[str, dict[str, float]] = {}
    for sc in scenarios:
        print(f"[{sc.name}] from={sc.from_date} to={sc.to_date} days={sc.days}")
        samples = _run_scenario(
            sc,
            host=args.host,
            cookie=cookie,
            iterations=args.iterations,
            warmup=args.warmup,
        )
        rows[sc.name] = _print_row(sc.name, samples) | {
            "days": sc.days,
            "from_date": sc.from_date.isoformat(),
            "to_date": sc.to_date.isoformat(),
        }
        print()

    # Fixed-overhead estimation: assume cost(N days) ≈ fixed + N * per_day_marginal.
    # Use single_day and 42d_grid as the two anchors.
    single = rows.get("single_day")
    grid = rows.get("42d_grid (current)")
    if single and grid:
        n1 = 1
        n2 = grid["days"]
        per_day = max((grid["mean"] - single["mean"]) / max(n2 - n1, 1), 0.0)
        fixed = max(single["mean"] - per_day * n1, 0.0)
        print("Fixed-overhead estimation (linear cost model):")
        print(f"  per_day_marginal ≈ {per_day:.1f} ms/day")
        print(f"  fixed_overhead   ≈ {fixed:.1f} ms (range-independent)")
        print()
        # Fanout cost projection for cold view if we pick single-day:
        # opening calendar then scrolling viewport over W visible days = W requests.
        for W in (3, 7, 12):
            fanout_total = W * single["mean"]
            single_range_equiv = fixed + W * per_day
            print(
                f"  cold view of ~{W} visible days: "
                f"single-day fanout ≈ {fanout_total:.0f} ms total "
                f"vs ±{(W-1)//2} range single call ≈ {single_range_equiv:.0f} ms"
            )

    if args.output_json:
        with open(args.output_json, "w") as f:
            json.dump(rows, f, indent=2)
        print(f"\nRaw rows dumped to {args.output_json}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
