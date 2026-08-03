"""Shared plumbing for the DFC retro incidence probe.

EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC

Public, unsigned Binance USD-M perpetual REST only. This module deliberately does
NOT import anything from ``app.services.brokers.binance`` — the broker clients are
demo-host-locked and signed, and this probe must stay at AUTH=NONE with zero
signed-endpoint calls.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx

# --- Probe identity -------------------------------------------------------
LABEL = "EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC"
PROBE_ID = "dfc-retro-probe-v1"
PURPOSE = "RETRO_INCIDENCE_ONLY"
SOURCE = "Binance USD-M perp public REST (unsigned)"
SYMBOLS = ("XRPUSDT", "DOGEUSDT", "SOLUSDT")

# --- Hard budget ----------------------------------------------------------
# 2000 is set by the brief and is NOT a value this code may raise.
MAX_REQUESTS = 2000

BASE_URL = "https://fapi.binance.com"
KLINES_PATH = "/fapi/v1/klines"
PREMIUM_PATH = "/fapi/v1/premiumIndexKlines"

ARTIFACT_ROOT = Path("/Users/mgh3326/work/herdr-artifacts/dfc-retro-probe-v1")
JOB_EVENTS = Path(
    "/Users/mgh3326/work/herdr-inbox/jobs/dfc-retro-probe-v1-20260803-1705/events"
)
PROGRESS_PATH = JOB_EVENTS / "progress.jsonl"

BAR_MS = 5 * 60 * 1000
FOUR_H_MS = 4 * 60 * 60 * 1000


class BudgetExceeded(RuntimeError):
    """Raised when the hard request budget would be exceeded."""


# Transient upstream failures worth retrying. A one-off 408 from Binance killed
# an earlier run at page 121 of 211; these are retried with backoff, bounded.
TRANSIENT_STATUSES = frozenset({408, 425, 500, 502, 503, 504})
MAX_TRANSIENT_ATTEMPTS = 6


@dataclass
class ProgressLog:
    """Append-only JSONL progress log, flushed + fsynced on every record.

    Nothing is held only in memory: if the process dies, everything written up to
    that point is durable and the run can be reported as PARTIAL.
    """

    path: Path = PROGRESS_PATH

    def __post_init__(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def write(self, record: dict[str, Any]) -> None:
        record = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime()),
            "label": LABEL,
            **record,
        }
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            fh.flush()
            os.fsync(fh.fileno())


COUNTER_PATH = ARTIFACT_ROOT / "calls_used.json"


def read_calls_used() -> int:
    """Cumulative request count across every script in this probe run."""
    if not COUNTER_PATH.exists():
        return 0
    return int(json.loads(COUNTER_PATH.read_text(encoding="utf-8"))["calls_used"])


@dataclass
class Fetcher:
    """Counting, backing-off, unsigned HTTP client.

    The budget is a *run* total, not a per-script total: prior calls are loaded
    from a persisted counter so the 2000 cap spans probe + collection.
    Rate handling is one-way: on 429/418 we slow down and never speed back up.
    """

    progress: ProgressLog
    max_requests: int = MAX_REQUESTS
    calls: int = 0
    sleep_s: float = 0.25
    prior_calls: int = field(default_factory=read_calls_used)
    _client: httpx.Client = field(init=False)

    def __post_init__(self) -> None:
        COUNTER_PATH.parent.mkdir(parents=True, exist_ok=True)
        self._client = httpx.Client(
            base_url=BASE_URL,
            timeout=httpx.Timeout(20.0),
            follow_redirects=False,
            headers={"User-Agent": "dfc-retro-probe-v1/exploratory"},
        )

    def total_calls(self) -> int:
        return self.prior_calls + self.calls

    def _persist_counter(self) -> None:
        COUNTER_PATH.write_text(
            json.dumps({"calls_used": self.total_calls(), "label": LABEL}),
            encoding="utf-8",
        )

    def close(self) -> None:
        self._persist_counter()
        self._client.close()

    def __enter__(self) -> Fetcher:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def remaining(self) -> int:
        return self.max_requests - self.total_calls()

    def get(self, path: str, params: dict[str, Any]) -> Any:
        if self.total_calls() >= self.max_requests:
            raise BudgetExceeded(
                f"request budget exhausted: {self.total_calls()}/{self.max_requests}"
            )
        attempt = 0
        while True:
            self.calls += 1
            if self.calls % 25 == 0:
                self._persist_counter()
            try:
                resp = self._client.get(path, params=params)
            except (httpx.TimeoutException, httpx.TransportError) as exc:
                attempt += 1
                self.progress.write(
                    {
                        "event": "transport_error",
                        "endpoint": path,
                        "symbol": params.get("symbol"),
                        "error": f"{type(exc).__name__}: {exc}",
                        "attempt": attempt,
                        "calls_cumulative": self.total_calls(),
                    }
                )
                if attempt > MAX_TRANSIENT_ATTEMPTS:
                    raise
                time.sleep(min(2.0 * attempt, 20.0))
                continue

            if resp.status_code in TRANSIENT_STATUSES:
                attempt += 1
                self.progress.write(
                    {
                        "event": "transient_upstream",
                        "status": resp.status_code,
                        "endpoint": path,
                        "symbol": params.get("symbol"),
                        "attempt": attempt,
                        "calls_cumulative": self.total_calls(),
                    }
                )
                if attempt > MAX_TRANSIENT_ATTEMPTS:
                    resp.raise_for_status()
                time.sleep(min(2.0 * attempt, 20.0))
                if self.total_calls() >= self.max_requests:
                    raise BudgetExceeded("budget exhausted during transient retry")
                continue

            if resp.status_code in (429, 418):
                attempt += 1
                # One-way slowdown. Never restored.
                self.sleep_s = min(self.sleep_s * 2 + 1.0, 30.0)
                self.progress.write(
                    {
                        "event": "rate_limited",
                        "status": resp.status_code,
                        "endpoint": path,
                        "params": {k: v for k, v in params.items() if k != "symbol"},
                        "symbol": params.get("symbol"),
                        "new_sleep_s": self.sleep_s,
                        "attempt": attempt,
                        "calls_cumulative": self.total_calls(),
                    }
                )
                if attempt > 5:
                    resp.raise_for_status()
                time.sleep(self.sleep_s * attempt)
                if self.total_calls() >= self.max_requests:
                    raise BudgetExceeded("budget exhausted during backoff")
                continue
            resp.raise_for_status()
            time.sleep(self.sleep_s)
            return resp.json()


def label_header_lines(extra: dict[str, Any] | None = None) -> list[str]:
    """Comment-prefixed label block for text artifacts."""
    lines = [
        f"# {LABEL}",
        f"# probe_id={PROBE_ID} purpose={PURPOSE}",
        f"# source={SOURCE} auth=NONE signed_endpoint_calls=0",
    ]
    for key, value in (extra or {}).items():
        lines.append(f"# {key}={value}")
    return lines


def label_metadata(extra: dict[str, Any] | None = None) -> dict[bytes, bytes]:
    """Key/value metadata stamped into parquet file-level metadata."""
    meta = {
        "admissibility": LABEL,
        "EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC": "TRUE",
        "probe_id": PROBE_ID,
        "purpose": PURPOSE,
        "source": SOURCE,
        "auth": "NONE",
        "signed_endpoint_calls": "0",
        "aggtrades_used": "NO",
        "forward_fill_used": "NO",
        "pnl_or_performance_computed": "NO",
    }
    meta.update({k: str(v) for k, v in (extra or {}).items()})
    return {k.encode(): v.encode() for k, v in meta.items()}
