"""Freshness and gap check for one dataset of one market.

A partition day ``d`` is *due* once ``bar_available_at(d) + grace`` has
passed (KR/US: session close + 30 min + grace; crypto: 00:05 UTC + grace).
Within the lookback window (never before the dataset's first recorded
partition) the check fails when:

* ``gap``      — a due day is missing while a later day is present;
* ``stale``    — the newest due day is missing (the dataset stopped advancing);
* ``corrupt``  — a recorded partition's file is missing or its sha256 differs
                 from the manifest;
* ``low_coverage`` — a recorded partition's manifest coverage is below the floor;
* ``subset_partition`` — a partition was written with ``--symbols`` (a subset is
                 final for that day, so it is never healthy);
* ``repaired_unverified`` — a crash-repaired record carries no run metadata
                 (repairs now restore it from the metadata sidecar, so this only
                 flags records written before that existed);
* ``empty``    — the dataset has no recorded partition at all.

Exit code 0 = healthy, 1 = unhealthy. With ``--ping-url-env NAME`` the URL in
that environment variable is pinged (healthchecks.io convention: the URL on
success, ``<URL>/fail`` on failure). The URL is never printed or logged; a
failed ping is reported by exception class only and does not change the exit
code.
"""

from __future__ import annotations

import os
import ssl
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

from .calendars import bar_available_at, local_today, partition_days
from .contract import DATASETS, check_coverage_floor, market_spec
from .store import manifest_records, partition_path, sha256_hex

DEFAULT_LOOKBACK_DAYS = 14
DEFAULT_GRACE = timedelta(hours=3)


@dataclass
class HealthReport:
    market: str
    dataset: str
    now: str
    healthy: bool = True
    problems: list[dict[str, Any]] = field(default_factory=list)
    due: list[str] = field(default_factory=list)
    latest_partition: str | None = None

    def fail(self, kind: str, **detail: Any) -> None:
        self.healthy = False
        self.problems.append({"kind": kind, **detail})

    def as_dict(self) -> dict[str, Any]:
        return {
            "market": self.market,
            "dataset": self.dataset,
            "now": self.now,
            "healthy": self.healthy,
            "latest_partition": self.latest_partition,
            "due_partitions": len(self.due),
            "problems": self.problems,
        }


def check_health(
    root: Path,
    *,
    market: str,
    dataset: str,
    now: datetime,
    lookback_days: int = DEFAULT_LOOKBACK_DAYS,
    grace: timedelta = DEFAULT_GRACE,
    min_coverage: float = 0.95,
) -> HealthReport:
    market_spec(market)
    if dataset not in DATASETS:
        raise ValueError(f"unknown dataset {dataset!r}")
    check_coverage_floor(min_coverage)
    if lookback_days < 1:
        raise ValueError("lookback_days must be positive")
    report = HealthReport(market, dataset, now.astimezone(UTC).isoformat())
    records = manifest_records(root, dataset, market)
    recorded: dict[date, Mapping[str, Any]] = {}
    for key, rec in records.items():
        try:
            recorded[date.fromisoformat(key)] = rec
        except ValueError:
            report.fail("corrupt", partition=key, detail="unparseable partition_date")
    if not recorded:
        report.fail("empty", detail="no recorded partition")
        return report
    report.latest_partition = max(recorded).isoformat()

    today = local_today(market, now)
    start = max(min(recorded), today - timedelta(days=lookback_days))
    due = [
        d
        for d in partition_days(market, start, today)
        if bar_available_at(market, d) + grace <= now
    ]
    report.due = [d.isoformat() for d in due]
    newest_present = max(recorded)
    for d in due:
        if d in recorded:
            continue
        if d < newest_present:
            report.fail("gap", partition=d.isoformat())
        else:
            report.fail(
                "stale",
                partition=d.isoformat(),
                latest_partition=newest_present.isoformat(),
            )

    for d, rec in sorted(recorded.items()):
        if d < start:
            continue
        path = partition_path(root, dataset, market, d)
        if not path.exists():
            report.fail("corrupt", partition=d.isoformat(), detail="file missing")
            continue
        if sha256_hex(path.read_bytes()) != rec.get("sha256"):
            report.fail("corrupt", partition=d.isoformat(), detail="sha256 mismatch")
        coverage = rec.get("coverage")
        if isinstance(coverage, (int, float)) and coverage < min_coverage:
            report.fail("low_coverage", partition=d.isoformat(), coverage=coverage)
        if rec.get("instrument_selection") == "explicit_subset":
            # coverage is relative to the subset; the full universe was not collected
            report.fail("subset_partition", partition=d.isoformat())
        if rec.get("repaired") and "code_commit" not in rec:
            # a repaired record without its run metadata (pre-sidecar format)
            report.fail("repaired_unverified", partition=d.isoformat())
    return report


Ping = Callable[[str], None]


class _RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Never follow a redirect: the ping URL carries a secret token, and a
    redirect could forward it to another host or downgrade it to http."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # noqa: ANN001
        return None  # urllib then raises HTTPError for the 3xx


def _ping_opener() -> urllib.request.OpenerDirector:
    return urllib.request.build_opener(
        _RefuseRedirect(),
        urllib.request.HTTPSHandler(context=ssl.create_default_context()),
    )


def _default_ping(
    url: str, *, opener: urllib.request.OpenerDirector | None = None
) -> None:
    request = urllib.request.Request(url, method="GET")  # noqa: S310 - https enforced by caller
    with (opener or _ping_opener()).open(request, timeout=10) as resp:
        resp.read(256)


def ping_url(env_name: str | None, healthy: bool, *, ping: Ping | None = None) -> str:
    """Ping the URL held in ``env_name``. Returns a status word, never the URL."""
    if not env_name:
        return "not_configured"
    url = os.environ.get(env_name, "").strip()
    if not url:
        return "not_configured"
    parsed = urllib.parse.urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname:
        return "refused_non_https"
    target = url if healthy else url.rstrip("/") + "/fail"
    try:
        (ping or _default_ping)(target)
    except Exception as exc:  # noqa: BLE001 - a ping must never change the verdict
        return f"failed:{type(exc).__name__}"
    return "sent_ok" if healthy else "sent_fail"
