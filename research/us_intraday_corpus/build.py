"""Corpus build orchestrator: budget gate -> 1Hour -> midpoint checkpoint -> 1Min.

Order of operations is fixed by the brief:

1. §3.1  measure the multi-symbol form, project the request budget, and STOP
         with BLOCKED_PRECONDITION if the projection exceeds MAX_REQUESTS.
         The cap is not raisable here.
2. §3.10 build 1Hour for the full universe, then report a midpoint checkpoint
         *before* starting 1Min, so an over-budget run can be sealed as
         BUILT_WITH_GAPS at a clean boundary.
3. §3.11 build 1Min for the pre-selected top 500.

Rows are routed to `dataset/` or `holdout/` purely by session_date, and the
holdout side is written and never read back.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import time
from dataclasses import dataclass, field
from typing import Any

from . import alpaca_data, bars, config, finalize, labels, writer


@dataclass
class PhaseStats:
    timeframe: str
    symbols_attempted: int = 0
    symbols_with_data: int = 0
    symbols_empty: int = 0
    rows_exploration: int = 0
    rows_holdout: int = 0
    gaps: list[dict[str, Any]] = field(default_factory=list)
    page_chains: list[dict[str, Any]] = field(default_factory=list)
    ohlc_violations: int = 0

    def as_dict(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "symbols_attempted": self.symbols_attempted,
            "symbols_with_data": self.symbols_with_data,
            "symbols_empty": self.symbols_empty,
            "rows_exploration": self.rows_exploration,
            "rows_holdout": self.rows_holdout,
            "explicit_gap_count": len(self.gaps),
            "ohlc_invariant_violations": self.ohlc_violations,
            "page_chains_recorded": len(self.page_chains),
            "page_chains_incomplete": sum(
                1 for c in self.page_chains if not c["complete"]
            ),
        }


def load_universe() -> list[str]:
    """Universe symbols, with the §1 sha pinned."""
    from . import hashing

    actual = hashing.sha256_of_file(config.UNIVERSE_FILE)
    if actual != config.UNIVERSE_FILE_SHA256:
        raise AssertionError(
            f"universe file sha mismatch: expected {config.UNIVERSE_FILE_SHA256}, "
            f"got {actual}"
        )
    with config.UNIVERSE_FILE.open(encoding="utf-8") as handle:
        symbols = [row["symbol"].strip() for row in csv.DictReader(handle)]
    if len(symbols) != config.UNIVERSE_COUNT:
        raise AssertionError(
            f"universe count mismatch: expected {config.UNIVERSE_COUNT}, got {len(symbols)}"
        )
    return symbols


def load_top500() -> list[str]:
    path = config.INPUTS_DIR / "top500_1m_universe.csv"
    if not path.exists():
        raise FileNotFoundError(
            f"{path} missing -- run `python -m research.us_intraday_corpus.selection` first"
        )
    with path.open(encoding="utf-8") as handle:
        return [row["symbol"].strip() for row in csv.DictReader(handle)]


def project_budget(measurement: dict[str, Any]) -> dict[str, Any]:
    """Project total requests from MEASURED page geometry (§3.1).

    Nothing here is assumed: `symbols_per_request` and `bars_per_page` both come
    from `probe_multi_symbol_form`, because guessing them moves the projection
    by an order of magnitude.
    """
    syms_per_req = max(1, int(measurement["symbols_per_request_measured"]))
    bars_per_page = max(1, int(measurement.get("bars_per_page_measured") or 10_000))

    years = (config.CUTOFF_DATE - config.START_DATE).days / 365.25
    sessions = years * 252

    # ~16 hourly bars/session (04:00-20:00 ET incl. extended hours)
    rows_1h_per_symbol = sessions * 16
    rows_1m_per_symbol = sessions * 960  # ~16h * 60

    def reqs(n_symbols: int, rows_per_symbol: float) -> int:
        total_rows = n_symbols * rows_per_symbol
        pages = total_rows / bars_per_page
        # multi-symbol batching only helps while a batch fits in one page
        return int(pages / max(1, syms_per_req) + n_symbols)

    r1h = reqs(config.UNIVERSE_COUNT, rows_1h_per_symbol)
    r1m = reqs(config.TOP500_COUNT, rows_1m_per_symbol)
    total = r1h + r1m

    return {
        "symbols_per_request_measured": syms_per_req,
        "bars_per_page_measured": bars_per_page,
        "requests_1h_projected": r1h,
        "requests_1m_projected": r1m,
        "requests_total_projected": total,
        "max_requests": config.MAX_REQUESTS,
        "within_budget": total <= config.MAX_REQUESTS,
        "projected_wall_clock_hours": round(
            total * config.MIN_REQUEST_INTERVAL_SEC / 3600, 3
        ),
    }


def _route_and_write(
    rows: list[dict[str, Any]], timeframe: str, stats: PhaseStats
) -> None:
    """Split rows by session_date and write exploration/holdout partitions."""
    import pyarrow as pa

    if not rows:
        return

    exploration = [r for r in rows if not config.is_holdout_date(r["session_date"])]
    holdout = [r for r in rows if config.is_holdout_date(r["session_date"])]

    for bucket, root in (
        (exploration, config.DATASET_DIR),
        (holdout, config.HOLDOUT_DIR),
    ):
        if not bucket:
            continue
        by_year: dict[int, list[dict[str, Any]]] = {}
        for row in bucket:
            by_year.setdefault(row["session_date"].year, []).append(row)
        for year, year_rows in sorted(by_year.items()):
            table = pa.Table.from_pylist(year_rows)
            symbol = year_rows[0]["symbol"]
            path = (
                root
                / f"freq={timeframe}"
                / "market=us"
                / f"year={year}"
                / f"{symbol}.parquet"
            )
            result = writer.write_parquet_atomic(table, path)
            finalize.register_digest(path, result.sha256)

    stats.rows_exploration += len(exploration)
    stats.rows_holdout += len(holdout)


def run_phase(
    client: alpaca_data.AlpacaDataClient,
    symbols: list[str],
    timeframe: str,
    label: str,
) -> PhaseStats:
    """Fetch one timeframe for `symbols`, writing partitions as we go."""
    stats = PhaseStats(timeframe=label)
    start = f"{config.START_DATE}T00:00:00Z"
    end = f"{config.CUTOFF_DATE}T23:59:59Z"
    now_utc = _dt.datetime.now(_dt.UTC)
    tf_minutes = 60 if timeframe == "1Hour" else 1

    for symbol in symbols:
        stats.symbols_attempted += 1
        collected: list[dict[str, Any]] = []
        chain_dict: dict[str, Any] | None = None
        try:
            for payload, chain in client.iter_bars([symbol], timeframe, start, end):
                raw = (payload.get("bars") or {}).get(symbol) or []
                normalized = bars.normalize_bars(symbol, raw)
                # §3.8 -- never store an unfinished bar
                normalized = [
                    r
                    for r in normalized
                    if bars.is_finished_bar(r["ts_utc"], tf_minutes, now_utc)
                ]
                collected.extend(normalized)
                chain_dict = chain.as_dict()
        except Exception as exc:  # explicit gap, never a silent drop (§3.5)
            stats.gaps.append(
                {
                    "symbol": symbol,
                    "timeframe": label,
                    "reason": f"{type(exc).__name__}: {exc}",
                }
            )
            if chain_dict:
                stats.page_chains.append(chain_dict)
            continue

        if chain_dict:
            stats.page_chains.append(chain_dict)

        if not collected:
            stats.symbols_empty += 1
            stats.gaps.append(
                {"symbol": symbol, "timeframe": label, "reason": "no_bars_returned"}
            )
            continue

        stats.symbols_with_data += 1
        stats.ohlc_violations += len(bars.ohlc_violations(collected))
        _route_and_write(collected, label, stats)

    return stats


def write_checkpoint(name: str, payload: dict[str, Any]) -> None:
    """Append a checkpoint to the job progress log and persist it (§3.2/§3.10)."""
    config.PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with config.PROGRESS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## checkpoint {name} ({stamp})\n")
        handle.write("```json\n" + json.dumps(payload, indent=2) + "\n```\n")
    labels.write_labelled_json(config.REPORTS_DIR / f"checkpoint_{name}.json", payload)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build us-intraday-corpus-v1")
    parser.add_argument(
        "--probe-only",
        action="store_true",
        help="measure the multi-symbol form and project budget, then stop",
    )
    parser.add_argument(
        "--skip-1m",
        action="store_true",
        help="build 1Hour only and seal at the midpoint",
    )
    args = parser.parse_args(argv)

    started = time.monotonic()

    try:
        client = alpaca_data.AlpacaDataClient()
    except alpaca_data.AlpacaCredentialsMissing as exc:
        write_checkpoint("blocked_credentials", {"error": str(exc)})
        print(f"BLOCKED_PRECONDITION: {exc}")
        return 2

    # --- §3.1 measure, then gate -------------------------------------------
    measurement = alpaca_data.probe_multi_symbol_form(client, ["AAPL", "MSFT", "NVDA"])
    page = client.fetch_bars_page(
        ["AAPL"], "1Min", "2024-01-02T14:30:00Z", "2024-01-03T21:00:00Z"
    )
    measurement["bars_per_page_measured"] = len(
        (page.get("bars") or {}).get("AAPL") or []
    )

    projection = project_budget(measurement)
    write_checkpoint("budget", {"measurement": measurement, "projection": projection})

    if not projection["within_budget"]:
        print(
            f"BLOCKED_PRECONDITION: projected {projection['requests_total_projected']} "
            f"requests exceeds MAX_REQUESTS={config.MAX_REQUESTS}"
        )
        return 2
    if args.probe_only:
        print(json.dumps(projection, indent=2))
        return 0

    # --- 1Hour --------------------------------------------------------------
    stats_1h = run_phase(client, load_universe(), "1Hour", "1h")
    finalize.save_registry()
    write_checkpoint(
        "midpoint_1h_complete_before_1m",
        {
            "phase": stats_1h.as_dict(),
            "requests_actual": client.counter.count,
            "rate_429_count": client.counter.rate_429,
            "wall_clock_hours": round((time.monotonic() - started) / 3600, 3),
            "note": "1Hour complete. 1Min has NOT started; safe to seal BUILT_WITH_GAPS here.",
        },
    )

    phases = [stats_1h.as_dict()]

    # --- 1Min ---------------------------------------------------------------
    if not args.skip_1m:
        stats_1m = run_phase(client, load_top500(), "1Min", "1m")
        finalize.save_registry()
        phases.append(stats_1m.as_dict())
        all_chains = stats_1h.page_chains + stats_1m.page_chains
        all_gaps = stats_1h.gaps + stats_1m.gaps
    else:
        all_chains = stats_1h.page_chains
        all_gaps = stats_1h.gaps

    labels.write_labelled_json(
        config.REPORTS_DIR / "page_chain_integrity.json",
        {
            "chains": all_chains,
            "incomplete": [c for c in all_chains if not c["complete"]],
        },
    )
    labels.write_labelled_csv(
        config.REPORTS_DIR / "explicit_gaps.csv",
        all_gaps,
        fieldnames=["symbol", "timeframe", "reason"],
    )

    verdict = "READY_FOR_RESEARCH" if not all_gaps else "BUILT_WITH_GAPS"
    finalize.seal(
        terminal_verdict=verdict,
        body={
            "phases": phases,
            "budget_measurement": measurement,
            "budget_projection": projection,
            "requests_actual": client.counter.count,
            "rate_429_count": client.counter.rate_429,
            "wall_clock_hours": round((time.monotonic() - started) / 3600, 3),
            "forward_fill_used": False,
            "unfinished_bar_stored": False,
        },
    )
    print(verdict)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
