"""ROB-272 Phase 2 — measure /invest/api/calendar response time + payload size.

Read-only benchmark that compares fetch ranges so Phase 2 (per-day lazy
loading) can pick the initial fetch window from data and estimate
build_calendar's range-independent fixed overhead.

Auth
----
`/invest/api/calendar` is gated by `get_authenticated_user`, which requires a
valid `session` cookie verified against Redis. There is no dev-mode bypass;
the script therefore needs a real logged-in session cookie. **The cookie
value is never printed, logged, or written to disk by this script** — only
its presence is checked.

How to run (cookie stays out of shell history and terminal output)
------------------------------------------------------------------
    cd /Users/mgh3326/work/auto_trader.rob-272
    make dev              # in one terminal — starts the FastAPI server

    # In another terminal: type the cookie value when prompted; `-s`
    # suppresses echo so it doesn't appear on screen or in shell history.
    read -s CALENDAR_COOKIE
    export CALENDAR_COOKIE

    uv run python scripts/measure_calendar_endpoint.py \
        --iterations 5 \
        --output-json /tmp/rob272-measure.json

    cat /tmp/rob272-measure.json

The cookie value is what your browser sends to `/invest/...`; just the value
after `session=` is enough, but the script also accepts the full header
(`session=abc...; other=...`). Either is fine.

What this measures
------------------
Four primary scenarios plus a "repeated single_day" cache-warm pass:
    * 42d_grid (current)      — Sunday-aligned 6-week grid (today's baseline)
    * 7d (±3)                 — selectedDate ±3
    * 15d (±7)                — selectedDate ±7
    * single_day              — selectedDate only
    * single_day_repeated     — same single_day call N times back-to-back, to
                                isolate fixed overhead from per-day marginal
                                cost (server-side warm cache).

Per-sample we record: HTTP status, wall-clock ms, response body bytes, and
parsed counts from the JSON body (days, total events, total clusters). For
each scenario we report cold (first sample after warmup) and warm (rest)
separately so cold-path overhead is visible.

Output also estimates a linear cost model
    cost(N days) ≈ fixed_overhead + N * per_day_marginal
and projects W-day cold-view fanout vs a single ±k range request, which is
the actual UX trade-off Phase 2 will pick from.

Safety
------
GET requests only. No broker/order/watch/order-intent mutation. The cookie
value is read from the env var `CALENDAR_COOKIE` and passed straight to the
HTTP `Cookie` header — never echoed, formatted into log lines, or written to
the `--output-json` file.
"""

from __future__ import annotations

import argparse
import json
import os
import statistics
import sys
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from http.client import HTTPConnection, HTTPSConnection
from urllib.parse import urlencode, urlparse
from zoneinfo import ZoneInfo

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
    day_count: int = 0
    event_count: int = 0
    cluster_count: int = 0
    parse_error: str | None = None


@dataclass
class ScenarioRow:
    days: int
    from_date: str
    to_date: str
    samples: list[Sample] = field(default_factory=list)


def _grid_range(anchor: date) -> tuple[date, date]:
    """6-week Sunday-aligned grid containing the month of `anchor` (mirrors FE)."""
    first = anchor.replace(day=1)
    # Sunday-aligned start. weekday() Mon=0..Sun=6 → Sun-first offset.
    dow_sun_first = (first.weekday() + 1) % 7
    start = first - timedelta(days=dow_sun_first)
    end = start + timedelta(days=41)
    return start, end


def _parse_counts(body: bytes) -> tuple[int, int, int, str | None]:
    """Return (day_count, event_count, cluster_count, parse_error)."""
    try:
        data = json.loads(body)
    except Exception as e:  # noqa: BLE001 — script-level
        return 0, 0, 0, f"json: {type(e).__name__}"
    days = data.get("days") if isinstance(data, dict) else None
    if not isinstance(days, list):
        return 0, 0, 0, "no 'days' array"
    event_total = 0
    cluster_total = 0
    for d in days:
        if not isinstance(d, dict):
            continue
        events = d.get("events") or []
        clusters = d.get("clusters") or []
        if isinstance(events, list):
            event_total += len(events)
        if isinstance(clusters, list):
            cluster_total += len(clusters)
    return len(days), event_total, cluster_total, None


def _http_get(url: str, cookie: str, timeout: float = 30.0) -> Sample:
    parsed = urlparse(url)
    conn_cls = HTTPSConnection if parsed.scheme == "https" else HTTPConnection
    conn = conn_cls(parsed.netloc, timeout=timeout)
    path = parsed.path + ("?" + parsed.query if parsed.query else "")
    headers = {
        "Cookie": cookie,  # value never logged below
        "Accept": "application/json",
        "User-Agent": "rob-272-measure/1.0",
    }
    started = time.perf_counter()
    try:
        conn.request("GET", path, headers=headers)
        resp = conn.getresponse()
        body = resp.read()
        status = resp.status
    finally:
        conn.close()
    elapsed_ms = (time.perf_counter() - started) * 1000

    sample = Sample(elapsed_ms=elapsed_ms, body_bytes=len(body), status=status)
    if status == 200:
        d, e, c, err = _parse_counts(body)
        sample.day_count = d
        sample.event_count = e
        sample.cluster_count = c
        sample.parse_error = err
    return sample


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


def _stat_block(samples: list[Sample]) -> dict[str, float | int]:
    if not samples:
        return {"n": 0}
    times = [s.elapsed_ms for s in samples]
    sizes = [s.body_bytes for s in samples]
    return {
        "n": len(samples),
        "p50_ms": _percentile(times, 0.50),
        "p95_ms": _percentile(times, 0.95),
        "mean_ms": statistics.fmean(times),
        "min_ms": min(times),
        "max_ms": max(times),
        "bytes_median": int(statistics.median(sizes)),
    }


def _summary_dict(row: ScenarioRow) -> dict[str, object]:
    """Build the public output structure (no secrets)."""
    by_status: dict[str, int] = {}
    for s in row.samples:
        key = str(s.status)
        by_status[key] = by_status.get(key, 0) + 1

    cold = row.samples[:1]
    warm = row.samples[1:]
    successful = [s for s in row.samples if s.status == 200]
    representative = successful[0] if successful else None

    return {
        "from_date": row.from_date,
        "to_date": row.to_date,
        "days_requested": row.days,
        "status_counts": by_status,
        "cold": _stat_block(cold),
        "warm": _stat_block(warm),
        "all": _stat_block(row.samples),
        "counts": (
            {
                "day_count": representative.day_count,
                "event_count_total": representative.event_count,
                "cluster_count_total": representative.cluster_count,
                "parse_error": representative.parse_error,
            }
            if representative
            else None
        ),
    }


def _print_scenario(name: str, summary: dict[str, object]) -> None:
    print(f"[{name}] from={summary['from_date']} to={summary['to_date']} "
          f"days_requested={summary['days_requested']}")
    print(f"  status_counts={summary['status_counts']}")

    def _fmt(label: str, block: dict[str, float | int]) -> str:
        if block.get("n", 0) == 0:
            return f"  {label:<5} | n=0"
        return (
            f"  {label:<5} | n={block['n']:>2} | "
            f"p50={block['p50_ms']:>7.1f} | p95={block['p95_ms']:>7.1f} | "
            f"mean={block['mean_ms']:>7.1f} | min={block['min_ms']:>7.1f} | "
            f"max={block['max_ms']:>7.1f} | "
            f"bytes(median)={block['bytes_median']:>8}"
        )

    print(_fmt("cold", summary["cold"]))
    print(_fmt("warm", summary["warm"]))
    print(_fmt("all",  summary["all"]))

    counts = summary.get("counts")
    if counts:
        if counts.get("parse_error"):
            print(f"  counts: parse_error={counts['parse_error']}")
        else:
            print(
                f"  counts: days={counts['day_count']} "
                f"events_total={counts['event_count_total']} "
                f"clusters_total={counts['cluster_count_total']}"
            )
    print()


def _run_scenario(
    scenario: Scenario,
    *,
    host: str,
    cookie: str,
    iterations: int,
) -> ScenarioRow:
    """Run scenario `iterations+1` times — first sample is COLD, rest are WARM.

    Note: no `warmup` discard. Cold and warm are both reported.
    """
    qs = urlencode(
        {
            "from_date": scenario.from_date.isoformat(),
            "to_date": scenario.to_date.isoformat(),
            "tab": "all",
        }
    )
    url = f"{host}/invest/api/calendar?{qs}"
    row = ScenarioRow(
        days=scenario.days,
        from_date=scenario.from_date.isoformat(),
        to_date=scenario.to_date.isoformat(),
    )
    total = max(iterations, 1) + 1  # +1 for the cold sample
    for _ in range(total):
        row.samples.append(_http_get(url, cookie))
    return row


def _fixed_overhead_block(
    rows: dict[str, ScenarioRow],
) -> dict[str, object] | None:
    """Estimate cost(N) ≈ fixed + N*per_day using single_day vs 42d_grid means.

    Uses warm-sample means when available (cold samples skew fixed_overhead
    upward due to one-time module/cache load). Falls back to all-sample mean
    if warm is empty.
    """

    def _mean(row: ScenarioRow) -> float | None:
        successful = [s for s in row.samples if s.status == 200]
        if not successful:
            return None
        warm = successful[1:] or successful
        return statistics.fmean(s.elapsed_ms for s in warm)

    single_row = rows.get("single_day")
    grid_row = rows.get("42d_grid (current)")
    if not single_row or not grid_row:
        return None
    single_mean = _mean(single_row)
    grid_mean = _mean(grid_row)
    if single_mean is None or grid_mean is None:
        return None
    n1 = single_row.days
    n2 = grid_row.days
    per_day = max((grid_mean - single_mean) / max(n2 - n1, 1), 0.0)
    fixed = max(single_mean - per_day * n1, 0.0)

    fanout: list[dict[str, object]] = []
    for W in (3, 7, 12):
        fanout.append(
            {
                "visible_days": W,
                "single_day_fanout_total_ms": W * single_mean,
                "range_single_call_total_ms": fixed + W * per_day,
            }
        )

    return {
        "model": "cost(N) ≈ fixed + N * per_day_marginal",
        "anchors": {
            "single_day_warm_mean_ms": single_mean,
            "grid_warm_mean_ms": grid_mean,
            "single_day_days": n1,
            "grid_days": n2,
        },
        "per_day_marginal_ms": per_day,
        "fixed_overhead_ms": fixed,
        "fanout_projection": fanout,
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="ROB-272 calendar endpoint measurement (read-only)."
    )
    parser.add_argument("--host", default="http://localhost:8000")
    parser.add_argument(
        "--selected",
        default=None,
        help="anchor ISO date (default = today KST)",
    )
    parser.add_argument(
        "--iterations",
        type=int,
        default=5,
        help="warm samples per scenario (cold sample always added on top)",
    )
    parser.add_argument(
        "--repeat-single-day",
        type=int,
        default=10,
        help="extra back-to-back single_day calls for fixed-overhead pass",
    )
    parser.add_argument(
        "--output-json",
        default=None,
        help="optional path to dump structured results (no secrets)",
    )
    args = parser.parse_args()

    cookie = os.environ.get("CALENDAR_COOKIE")
    if not cookie:
        print(
            "ERROR: CALENDAR_COOKIE env var is required.\n"
            "Set it in your local terminal without echoing the value:\n"
            "    read -s CALENDAR_COOKIE\n"
            "    export CALENDAR_COOKIE\n"
            "(then re-run this script). The cookie value is never printed by "
            "this script.",
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
    repeated = Scenario(
        f"single_day_repeated x{args.repeat_single_day}", anchor, anchor
    )

    print(
        f"host={args.host}  anchor={anchor.isoformat()}  "
        f"iterations={args.iterations} (+1 cold per scenario)  "
        f"repeat_single_day={args.repeat_single_day}"
    )
    print()

    rows: dict[str, ScenarioRow] = {}
    summaries: dict[str, dict[str, object]] = {}
    for sc in scenarios:
        row = _run_scenario(
            sc, host=args.host, cookie=cookie, iterations=args.iterations
        )
        rows[sc.name] = row
        summaries[sc.name] = _summary_dict(row)
        _print_scenario(sc.name, summaries[sc.name])

    # Repeated single-day pass: one cold + (repeat-1) warm.
    repeated_row = _run_scenario(
        repeated,
        host=args.host,
        cookie=cookie,
        iterations=max(args.repeat_single_day - 1, 1),
    )
    rows[repeated.name] = repeated_row
    summaries[repeated.name] = _summary_dict(repeated_row)
    _print_scenario(repeated.name, summaries[repeated.name])

    overhead = _fixed_overhead_block(rows)
    if overhead:
        print("Fixed-overhead estimation (warm-mean anchors):")
        print(
            f"  per_day_marginal ≈ {overhead['per_day_marginal_ms']:.1f} ms/day"
        )
        print(
            f"  fixed_overhead   ≈ {overhead['fixed_overhead_ms']:.1f} ms"
        )
        print()
        for proj in overhead["fanout_projection"]:
            W = proj["visible_days"]
            print(
                f"  cold view of ~{W} visible days: "
                f"single-day fanout ≈ {proj['single_day_fanout_total_ms']:.0f} ms "
                f"vs ±{(W-1)//2} range single call ≈ "
                f"{proj['range_single_call_total_ms']:.0f} ms"
            )

    if args.output_json:
        # Only the structured summaries + overhead are written; cookie and raw
        # request URLs (which contain only public query params) are intentionally
        # omitted from the dump.
        payload = {
            "anchor": anchor.isoformat(),
            "host": args.host,
            "iterations": args.iterations,
            "repeat_single_day": args.repeat_single_day,
            "scenarios": summaries,
            "fixed_overhead_estimate": overhead,
        }
        with open(args.output_json, "w") as f:
            json.dump(payload, f, indent=2)
        print(f"\nStructured results dumped to {args.output_json}")

    # Non-200 sweep: fail loudly if anything wasn't a clean 200.
    bad = [
        f"{name}: status={s.status}"
        for name, row in rows.items()
        for s in row.samples
        if s.status != 200
    ]
    if bad:
        print("\nNon-200 responses (treat measurements as suspect):", file=sys.stderr)
        for line in bad[:10]:
            print(f"  {line}", file=sys.stderr)
        if len(bad) > 10:
            print(f"  ... and {len(bad) - 10} more", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
