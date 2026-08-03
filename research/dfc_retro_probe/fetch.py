"""Collect 5m klines + premium-index klines for the retro incidence probe.

EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC

Order of operations is deliberate:
  1. budget gate  — projected requests computed from *measured* limits; if the
     projection exceeds MAX_REQUESTS we exit BLOCKED_PRECONDITION without
     issuing a single collection request. MAX_REQUESTS is never raised here.
  2. paginate     — every page appended to progress.jsonl and fsynced before the
     next page is requested, so a crash leaves a truthful partial record.
  3. atomic write — .partial -> verify -> os.replace, then SHA-256.

No aggTrades. No signed endpoints. No forward-fill (raw rows are stored exactly
as returned; gap handling happens downstream in signal.py and is explicit).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import sys
import time
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow as pa
import pyarrow.parquet as pq

from research.dfc_retro_probe.common import (
    ARTIFACT_ROOT,
    BAR_MS,
    KLINES_PATH,
    LABEL,
    MAX_REQUESTS,
    PREMIUM_PATH,
    SYMBOLS,
    BudgetExceeded,
    Fetcher,
    ProgressLog,
    label_metadata,
    read_calls_used,
)

RAW_DIR = ARTIFACT_ROOT / "raw"

KLINE_COLUMNS = [
    "open_time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "close_time",
    "quote_volume",
    "trades",
    "taker_buy_base",
    "taker_buy_quote",
    "ignore",
]


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def project_budget(
    days: int, limit: int, endpoints: int, symbols: int
) -> dict[str, Any]:
    bars = days * 288
    pages_per_series = math.ceil(bars / limit)
    series = endpoints * symbols
    projected = pages_per_series * series
    prior = read_calls_used()
    return {
        "lookback_days": days,
        "bars_per_series": bars,
        "measured_limit": limit,
        "pages_per_series": pages_per_series,
        "series_count": series,
        "projected_collection_requests": projected,
        "prior_requests_used": prior,
        "projected_total": projected + prior,
        "max_requests": MAX_REQUESTS,
        "within_budget": (projected + prior) <= MAX_REQUESTS,
        "headroom": MAX_REQUESTS - (projected + prior),
    }


def fetch_series(
    fetcher: Fetcher,
    symbol: str,
    path: str,
    start_ms: int,
    end_ms: int,
    limit: int,
) -> list[list[Any]]:
    """Forward pagination. Stops on empty page, non-advancing cursor, or end."""
    rows: list[list[Any]] = []
    cursor = start_ms
    page = 0
    while cursor < end_ms:
        batch = fetcher.get(
            path,
            {
                "symbol": symbol,
                "interval": "5m",
                "startTime": cursor,
                "endTime": end_ms,
                "limit": limit,
            },
        )
        page += 1
        if not batch:
            fetcher.progress.write(
                {
                    "event": "page_empty",
                    "symbol": symbol,
                    "endpoint": path,
                    "page": page,
                    "cursor": cursor,
                    "calls_cumulative": fetcher.total_calls(),
                }
            )
            break
        rows.extend(batch)
        oldest = int(batch[0][0])
        newest = int(batch[-1][0])
        fetcher.progress.write(
            {
                "event": "page",
                "symbol": symbol,
                "endpoint": path,
                "page": page,
                "oldest": oldest,
                "newest": newest,
                "oldest_iso": pd.Timestamp(oldest, unit="ms", tz="UTC").isoformat(),
                "newest_iso": pd.Timestamp(newest, unit="ms", tz="UTC").isoformat(),
                "rows": len(batch),
                "rows_cumulative": len(rows),
                "calls_cumulative": fetcher.total_calls(),
            }
        )
        next_cursor = newest + BAR_MS
        if next_cursor <= cursor:
            fetcher.progress.write(
                {
                    "event": "cursor_stalled",
                    "symbol": symbol,
                    "endpoint": path,
                    "cursor": cursor,
                    "calls_cumulative": fetcher.total_calls(),
                }
            )
            break
        cursor = next_cursor
        if len(batch) < limit:
            # Short page means we reached the head of available data.
            break
    return rows


def write_parquet(
    rows: list[list[Any]], out: Path, extra_meta: dict[str, Any]
) -> dict[str, Any]:
    frame = pd.DataFrame(rows, columns=KLINE_COLUMNS).drop(columns=["ignore"])
    numeric = [
        "open",
        "high",
        "low",
        "close",
        "volume",
        "quote_volume",
        "taker_buy_base",
        "taker_buy_quote",
    ]
    for col in numeric:
        frame[col] = pd.to_numeric(frame[col], errors="coerce")
    for col in ("open_time", "close_time", "trades"):
        frame[col] = pd.to_numeric(frame[col], errors="coerce").astype("int64")
    frame = frame.drop_duplicates(subset=["open_time"]).sort_values("open_time")
    frame = frame.reset_index(drop=True)

    table = pa.Table.from_pandas(frame, preserve_index=False)
    meta = dict(table.schema.metadata or {})
    meta.update(
        label_metadata(
            {
                "rows": len(frame),
                "open_time_min": int(frame["open_time"].iloc[0]),
                "open_time_max": int(frame["open_time"].iloc[-1]),
                **extra_meta,
            }
        )
    )
    table = table.replace_schema_metadata(meta)

    out.parent.mkdir(parents=True, exist_ok=True)
    partial = out.with_suffix(out.suffix + ".partial")
    pq.write_table(table, partial, compression="zstd")

    # Verify before publishing: reread and confirm rows + label survived.
    check = pq.read_table(partial)
    check_meta = check.schema.metadata or {}
    assert check.num_rows == len(frame), "row count mismatch on verify"
    assert check_meta.get(b"EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC") == b"TRUE", (
        "label missing from parquet metadata"
    )
    os.replace(partial, out)

    return {
        "path": str(out),
        "rows": len(frame),
        "sha256": sha256_file(out),
        "open_time_min": int(frame["open_time"].iloc[0]),
        "open_time_max": int(frame["open_time"].iloc[-1]),
        "utc_min": pd.Timestamp(
            int(frame["open_time"].iloc[0]), unit="ms", tz="UTC"
        ).isoformat(),
        "utc_max": pd.Timestamp(
            int(frame["open_time"].iloc[-1]), unit="ms", tz="UTC"
        ).isoformat(),
        "span_days": round(
            (int(frame["open_time"].iloc[-1]) - int(frame["open_time"].iloc[0]))
            / (BAR_MS * 288),
            2,
        ),
    }


def existing_series(out: Path, start_ms: int, end_ms: int) -> dict[str, Any] | None:
    """Reuse an already-collected series when it covers the requested window.

    Resume is by whole series: a run that dies mid-series refetches that series
    from the start, but completed ones cost zero requests.
    """
    if not out.exists():
        return None
    try:
        meta = pq.read_metadata(out).metadata or {}
        if meta.get(b"EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC") != b"TRUE":
            return None
        lo = int(meta[b"open_time_min"])
        hi = int(meta[b"open_time_max"])
        rows = int(meta[b"rows"])
    except (KeyError, ValueError, OSError):
        return None
    # Allow one bar of slack at each edge.
    if lo > start_ms + BAR_MS or hi < end_ms - 2 * BAR_MS:
        return None
    return {
        "path": str(out),
        "rows": rows,
        "sha256": sha256_file(out),
        "open_time_min": lo,
        "open_time_max": hi,
        "utc_min": pd.Timestamp(lo, unit="ms", tz="UTC").isoformat(),
        "utc_max": pd.Timestamp(hi, unit="ms", tz="UTC").isoformat(),
        "span_days": round((hi - lo) / (BAR_MS * 288), 2),
        "reused_from_prior_run": True,
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--days", type=int, default=1095)
    parser.add_argument("--limit", type=int, default=1500)
    args = parser.parse_args()

    progress = ProgressLog()
    limits_path = ARTIFACT_ROOT / "limits.json"
    if not limits_path.exists():
        print("BLOCKED_PRECONDITION: limits.json missing; run probe_limits first")
        return 2
    limits = json.loads(limits_path.read_text(encoding="utf-8"))
    measured = {
        "klines": limits["limits"]["klines"]["max_limit_measured"],
        "premium": limits["limits"]["premium_index_klines"]["max_limit_measured"],
    }
    effective_limit = min(args.limit, measured["klines"], measured["premium"])

    gate = project_budget(args.days, effective_limit, endpoints=2, symbols=len(SYMBOLS))
    gate["measured_limits"] = measured
    progress.write({"event": "budget_gate", **gate})
    print(json.dumps({"budget_gate": gate}, indent=2))

    if not gate["within_budget"]:
        progress.write({"event": "blocked_precondition", "reason": "budget_exceeded"})
        print("BLOCKED_PRECONDITION: projected requests exceed MAX_REQUESTS")
        return 2

    now_ms = int(time.time() * 1000)
    end_ms = (now_ms // BAR_MS) * BAR_MS  # exclude the forming 5m bar
    start_ms = end_ms - args.days * 288 * BAR_MS

    manifest: dict[str, Any] = {
        "admissibility": LABEL,
        "EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC": True,
        "probe_id": "dfc-retro-probe-v1",
        "purpose": "RETRO_INCIDENCE_ONLY",
        "auth": "NONE",
        "signed_endpoint_calls": 0,
        "aggtrades_used": "NO",
        "forward_fill_used": "NO",
        "budget_gate": gate,
        "requested_window": {
            "start_ms": start_ms,
            "end_ms": end_ms,
            "start_utc": pd.Timestamp(start_ms, unit="ms", tz="UTC").isoformat(),
            "end_utc": pd.Timestamp(end_ms, unit="ms", tz="UTC").isoformat(),
            "days": args.days,
        },
        "series": [],
    }

    status = "COMPLETE"
    try:
        with Fetcher(progress=progress) as fetcher:
            for symbol in SYMBOLS:
                for kind, path in (("klines", KLINES_PATH), ("premium", PREMIUM_PATH)):
                    out = RAW_DIR / f"{kind}_{symbol}_5m.parquet"
                    reuse = existing_series(out, start_ms, end_ms)
                    if reuse is not None:
                        reuse.update({"symbol": symbol, "kind": kind, "endpoint": path})
                        manifest["series"].append(reuse)
                        progress.write({"event": "series_reused", **reuse})
                        continue
                    progress.write(
                        {
                            "event": "series_start",
                            "symbol": symbol,
                            "endpoint": path,
                            "calls_cumulative": fetcher.total_calls(),
                        }
                    )
                    rows = fetch_series(
                        fetcher, symbol, path, start_ms, end_ms, effective_limit
                    )
                    if not rows:
                        progress.write(
                            {
                                "event": "series_empty",
                                "symbol": symbol,
                                "endpoint": path,
                            }
                        )
                        continue
                    info = write_parquet(
                        rows, out, {"symbol": symbol, "kind": kind, "interval": "5m"}
                    )
                    info.update({"symbol": symbol, "kind": kind, "endpoint": path})
                    manifest["series"].append(info)
                    progress.write({"event": "series_done", **info})
            manifest["requests_actual"] = fetcher.total_calls()
    except (BudgetExceeded, Exception) as exc:  # noqa: BLE001 - record then report
        status = "PARTIAL"
        manifest["error"] = f"{type(exc).__name__}: {exc}"
        manifest.setdefault("requests_actual", read_calls_used())
        progress.write({"event": "fetch_aborted", "error": manifest["error"]})

    manifest["status"] = status
    man_path = ARTIFACT_ROOT / "manifest_raw.json"
    man_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    progress.write(
        {
            "event": "fetch_done",
            "status": status,
            "series_written": len(manifest["series"]),
            "requests_actual": manifest.get("requests_actual"),
        }
    )
    print(json.dumps({"status": status, "series": manifest["series"]}, indent=2))
    return 0 if status == "COMPLETE" else 1


if __name__ == "__main__":
    sys.exit(main())
