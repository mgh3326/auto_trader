"""Precondition measurement: measure real API limits and real lookback reach.

EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC

Nothing here is estimated. Every number written to ``limits.json`` came from a
response. The request-budget gate in ``fetch.py`` consumes this file, so if the
measured reach makes the projected budget exceed MAX_REQUESTS, the collection is
never started.
"""

from __future__ import annotations

import json
from typing import Any

import httpx

from research.dfc_retro_probe.common import (
    ARTIFACT_ROOT,
    BAR_MS,
    KLINES_PATH,
    LABEL,
    PREMIUM_PATH,
    SYMBOLS,
    Fetcher,
    ProgressLog,
)


def _try_limit(fetcher: Fetcher, path: str, symbol: str, limit: int) -> int | None:
    """Return row count for a candidate limit, or None if the API rejects it."""
    try:
        rows = fetcher.get(path, {"symbol": symbol, "interval": "5m", "limit": limit})
    except httpx.HTTPStatusError as exc:
        if exc.response.status_code in (400, 404):
            return None
        raise
    return len(rows)


def measure_max_limit(fetcher: Fetcher, path: str, symbol: str) -> dict[str, Any]:
    """Probe documented candidates downward until one is accepted."""
    attempts: list[dict[str, Any]] = []
    accepted: int | None = None
    for candidate in (1500, 1000, 500):
        count = _try_limit(fetcher, path, symbol, candidate)
        attempts.append({"limit_requested": candidate, "rows_returned": count})
        fetcher.progress.write(
            {
                "event": "limit_probe",
                "endpoint": path,
                "symbol": symbol,
                "limit_requested": candidate,
                "rows_returned": count,
                "calls_cumulative": fetcher.calls,
            }
        )
        if count is not None:
            accepted = candidate
            break
    # Confirm nothing above the accepted value is silently allowed.
    over = None
    if accepted == 1500:
        over = _try_limit(fetcher, path, symbol, 2000)
        attempts.append({"limit_requested": 2000, "rows_returned": over})
        fetcher.progress.write(
            {
                "event": "limit_probe_over",
                "endpoint": path,
                "symbol": symbol,
                "limit_requested": 2000,
                "rows_returned": over,
                "calls_cumulative": fetcher.calls,
            }
        )
    return {
        "endpoint": path,
        "probe_symbol": symbol,
        "max_limit_measured": accepted,
        "rows_at_limit_2000": over,
        "attempts": attempts,
    }


def measure_earliest(fetcher: Fetcher, path: str, symbol: str) -> dict[str, Any]:
    """Ask for startTime=0 — Binance clamps to the true earliest available bar."""
    rows = fetcher.get(
        path, {"symbol": symbol, "interval": "5m", "startTime": 0, "limit": 1}
    )
    earliest = int(rows[0][0]) if rows else None
    latest_rows = fetcher.get(path, {"symbol": symbol, "interval": "5m", "limit": 1})
    latest = int(latest_rows[0][0]) if latest_rows else None
    span_days = None
    if earliest is not None and latest is not None:
        span_days = round((latest - earliest) / (BAR_MS * 288), 2)
    fetcher.progress.write(
        {
            "event": "earliest_probe",
            "endpoint": path,
            "symbol": symbol,
            "oldest": earliest,
            "newest": latest,
            "span_days": span_days,
            "calls_cumulative": fetcher.calls,
        }
    )
    return {
        "endpoint": path,
        "symbol": symbol,
        "earliest_open_ms": earliest,
        "latest_open_ms": latest,
        "span_days_measured": span_days,
    }


def main() -> None:
    progress = ProgressLog()
    progress.write({"event": "probe_limits_start", "label": LABEL})
    ARTIFACT_ROOT.mkdir(parents=True, exist_ok=True)

    with Fetcher(progress=progress) as fetcher:
        limits = {
            "klines": measure_max_limit(fetcher, KLINES_PATH, SYMBOLS[0]),
            "premium_index_klines": measure_max_limit(
                fetcher, PREMIUM_PATH, SYMBOLS[0]
            ),
        }
        reach = {
            "klines": [measure_earliest(fetcher, KLINES_PATH, s) for s in SYMBOLS],
            "premium_index_klines": [
                measure_earliest(fetcher, PREMIUM_PATH, s) for s in SYMBOLS
            ],
        }
        payload = {
            "admissibility": LABEL,
            "EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC": True,
            "auth": "NONE",
            "signed_endpoint_calls": 0,
            "aggtrades_used": "NO",
            "limits": limits,
            "reach": reach,
            "probe_calls_used": fetcher.calls,
        }

    out = ARTIFACT_ROOT / "limits.json"
    out.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    progress.write(
        {
            "event": "probe_limits_done",
            "calls_cumulative": payload["probe_calls_used"],
            "out": str(out),
        }
    )
    print(json.dumps(payload, indent=2))


if __name__ == "__main__":
    main()
