"""Corpus build orchestrator: budget gate -> 1Hour -> midpoint checkpoint -> 1Min.

Order of operations is fixed by the brief:

1. §3.1  measure the multi-symbol form, project the request budget, and STOP
         with BLOCKED_PRECONDITION if the projection exceeds MAX_REQUESTS.
         The cap is not raisable here.
2. §3.10 build 1Hour for the full universe, then report a midpoint checkpoint
         *before* starting 1Min, so an over-budget run can be sealed as
         BUILT_WITH_GAPS at a clean boundary.
3. §3.11 build 1Min for the pre-selected top 500.

Work is decomposed into **units** of (timeframe, fetch_year, batch) and each
completed unit drops a marker in `_staging/`. A 7-hour run that dies at hour 5
resumes from the last finished unit instead of starting over.

Measured page geometry (see the job's progress log) drives two design choices:

* the 10,000-row page cap is shared across *all* symbols in a request, so
  batching symbols does not multiply throughput. Request count tracks total
  rows. 1Hour batches 100 symbols/request purely to amortise the per-symbol
  minimum; 1Min fetches per symbol because a year of one symbol already fills
  ~11 pages, and per-symbol units keep memory bounded and resume granular.
* rows are routed to `dataset/` vs `holdout/` by **session_date**, not by the
  UTC year they were fetched under. A bar at 2024-12-31 19:00 ET arrives as
  2025-01-01 UTC but belongs to the 2024 exploration session -- routing on the
  fetch year would silently leak it into the seal.
"""

from __future__ import annotations

import argparse
import csv
import datetime as _dt
import json
import time
from collections.abc import Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from . import alpaca_data, bars, config, finalize, labels, writer

# Symbols per request. See module docstring: this amortises the per-symbol
# minimum, it does not raise throughput.
BATCH_1H = 100
BATCH_1M = 1


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
    units_done: int = 0
    units_resumed: int = 0
    symbols_with_data_any_window: int = 0
    zero_exploration_symbols: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "timeframe": self.timeframe,
            "symbols_attempted": self.symbols_attempted,
            "symbols_with_data": self.symbols_with_data,
            "symbols_with_data_definition": (
                "symbols having at least one EXPLORATION row; holdout-only "
                "symbols are excluded because they are unusable for research"
            ),
            "symbols_with_data_any_window": self.symbols_with_data_any_window,
            "symbols_empty": self.symbols_empty,
            "symbols_empty_definition": "symbols with zero exploration rows",
            "zero_exploration_symbols": self.zero_exploration_symbols,
            "rows_exploration": self.rows_exploration,
            "rows_holdout": self.rows_holdout,
            "explicit_gap_count": len(self.gaps),
            "ohlc_invariant_violations": self.ohlc_violations,
            "page_chains_recorded": len(self.page_chains),
            "page_chains_incomplete": sum(
                1 for c in self.page_chains if not c["complete"]
            ),
            "units_done": self.units_done,
            "units_resumed_from_marker": self.units_resumed,
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

    Nothing here is assumed. `bars_per_page`, the per-session bar densities and
    the fraction of the universe carrying data all come from live measurement,
    because guessing them moves the projection by an order of magnitude.
    """
    rows_per_request = float(measurement["m1_rows_per_request_measured"])
    rows_2016 = float(measurement["m1_rows_per_symbol_year_2016"])
    rows_2024 = float(measurement["m1_rows_per_symbol_year_2024"])
    already_spent = int(measurement.get("requests_already_spent", 0))

    # Rows per symbol-year, interpolated between the two measured years and
    # held flat after 2024. Scope C collects 1Min only, so there is no 1Hour
    # term: see config.HOUR_DATA_GAP.
    first, last = config.START_DATE.year, config.CUTOFF_DATE.year
    rows_total = 0.0
    for year in range(first, last + 1):
        frac = min(1.0, (year - 2016) / 8) if year >= 2016 else 0.0
        rows_total += config.TOP500_COUNT * (rows_2016 + (rows_2024 - rows_2016) * frac)

    r1m = rows_total / rows_per_request
    total = int(r1m) + already_spent

    return {
        "scope": config.SCOPE_DECISION,
        "m1_rows_per_request_measured": rows_per_request,
        "m1_rows_per_symbol_year_2016": rows_2016,
        "m1_rows_per_symbol_year_2024": rows_2024,
        "requests_1h_projected": 0,
        "requests_1h_note": (
            "1Hour collection dropped under scope C -- it needs 130k-246k "
            "requests at the measured <=416 rows/request. See data_gaps."
        ),
        "requests_1m_projected": int(r1m),
        "requests_already_spent": already_spent,
        "requests_total_projected": total,
        "max_requests": config.MAX_REQUESTS,
        "within_budget": total <= config.MAX_REQUESTS,
        "budget_margin_pct": round(
            (config.MAX_REQUESTS - total) / config.MAX_REQUESTS * 100, 1
        ),
        "projected_wall_clock_hours": round(
            int(r1m) * config.MIN_REQUEST_INTERVAL_SEC / 3600, 2
        ),
        "max_wall_clock_hours": config.MAX_WALL_CLOCK_HOURS,
        "rows_total_projected": int(rows_total),
    }


def year_windows() -> Iterator[tuple[int, str, str]]:
    """UTC fetch windows, one per calendar year, clipped to START..CUTOFF."""
    for year in range(config.START_DATE.year, config.CUTOFF_DATE.year + 1):
        start = max(_dt.date(year, 1, 1), config.START_DATE)
        end = min(_dt.date(year, 12, 31), config.CUTOFF_DATE)
        if start > end:
            continue
        yield year, f"{start}T00:00:00Z", f"{end}T23:59:59Z"


def _chunks(items: Sequence[str], size: int) -> Iterator[list[str]]:
    for i in range(0, len(items), size):
        yield list(items[i : i + size])


def _marker(label: str, year: int, batch_id: int):
    return config.STAGING_DIR / "units" / f"{label}_{year}_{batch_id}.done"


def _route_and_write(
    rows: list[dict[str, Any]], label: str, year: int, batch_id: int, stats: PhaseStats
) -> None:
    """Split rows by session_date and write exploration/holdout partitions.

    Routing is on `session_date`, never on the fetch year -- see module docstring.
    """
    import pyarrow as pa

    if not rows:
        return

    by_target: dict[tuple[Any, int], list[dict[str, Any]]] = {}
    for row in rows:
        day = row["session_date"]
        root = config.HOLDOUT_DIR if config.is_holdout_date(day) else config.DATASET_DIR
        by_target.setdefault((root, day.year), []).append(row)

    for (root, session_year), bucket in sorted(
        by_target.items(), key=lambda kv: (str(kv[0][0]), kv[0][1])
    ):
        for row in bucket:
            row["session_date"] = str(row["session_date"])
        table = pa.Table.from_pylist(bucket)
        path = (
            root
            / f"freq={label}"
            / "market=us"
            / f"year={session_year}"
            / f"part-{year}-{batch_id:05d}.parquet"
        )
        result = writer.write_parquet_atomic(table, path)
        finalize.register_digest(path, result.sha256)
        if root == config.HOLDOUT_DIR:
            stats.rows_holdout += len(bucket)
        else:
            stats.rows_exploration += len(bucket)


def run_phase(
    client: alpaca_data.AlpacaDataClient,
    symbols: list[str],
    timeframe: str,
    label: str,
    batch_size: int,
    stats: PhaseStats | None = None,
) -> PhaseStats:
    """Fetch one timeframe across all years, unit by unit, with resume."""
    stats = stats or PhaseStats(timeframe=label)
    stats.symbols_attempted = len(symbols)
    now_utc = _dt.datetime.now(_dt.UTC)
    tf_minutes = 60 if timeframe == "1Hour" else 1
    seen_exploration: set[str] = set()
    seen_any_window: set[str] = set()

    for year, start, end in year_windows():
        for batch_id, batch in enumerate(_chunks(symbols, batch_size)):
            marker = _marker(label, year, batch_id)
            if marker.exists():
                # Replay which symbols this unit found data for. Without this a
                # resumed run would see an empty set and fabricate a
                # "no_bars_in_any_year" gap for every symbol it skipped.
                try:
                    _m = json.loads(marker.read_text(encoding="utf-8"))
                    seen_any_window.update(_m.get("symbols", []))
                    # Markers written before this field existed carry no
                    # exploration list; fall back to the any-window set rather
                    # than silently reporting zero exploration coverage.
                    seen_exploration.update(
                        _m.get("exploration_symbols", _m.get("symbols", []))
                    )
                except (json.JSONDecodeError, OSError):
                    stats.gaps.append(
                        {
                            "symbol": f"batch{batch_id}",
                            "timeframe": label,
                            "reason": f"{year}: unreadable resume marker {marker.name}",
                        }
                    )
                stats.units_resumed += 1
                continue

            collected: list[dict[str, Any]] = []
            chain_dict: dict[str, Any] | None = None
            live_chain: alpaca_data.PageChain | None = None
            try:
                for payload, chain in client.iter_bars(batch, timeframe, start, end):
                    for sym, raw in (payload.get("bars") or {}).items():
                        normalized = bars.normalize_bars(sym, raw)
                        # §3.8 -- never store an unfinished bar
                        # §3.8 never store an unfinished bar; and keep the
                        # corpus inside its declared session window. A bar at
                        # 2016-01-01T00:00Z is session 2015-12-31 in New York,
                        # which is outside START..CUTOFF -- excluded here rather
                        # than silently creating an out-of-window partition.
                        normalized = [
                            r
                            for r in normalized
                            if bars.is_finished_bar(r["ts_utc"], tf_minutes, now_utc)
                            and config.START_DATE
                            <= r["session_date"]
                            <= config.CUTOFF_DATE
                        ]
                        collected.extend(normalized)
                        if normalized:
                            seen_any_window.add(sym)
                            # A symbol whose only rows fall in the sealed
                            # holdout window is NOT usable for exploration.
                            # Counting it as "has data" is what let PSKY read
                            # as ordinary coverage.
                            if any(
                                not config.is_holdout_date(r["session_date"])
                                for r in normalized
                            ):
                                seen_exploration.add(sym)
                    # Keep a reference, do NOT snapshot here: `termination` is
                    # only set after the final yield resumes, so a snapshot
                    # taken inside the loop always reads "unstarted" and marks
                    # every chain incomplete. Snapshot after the loop instead.
                    live_chain = chain
                # Loop exhausted: the generator has set `termination`, so this
                # snapshot reflects how the chain actually ended (§3.9).
                if live_chain is not None:
                    chain_dict = live_chain.as_dict()
            except Exception as exc:  # explicit gap, never a silent drop (§3.5)
                stats.gaps.append(
                    {
                        "symbol": ",".join(batch)
                        if len(batch) <= 3
                        else f"batch{batch_id}",
                        "timeframe": label,
                        "reason": f"{year}: {type(exc).__name__}: {exc}",
                    }
                )
                if live_chain is not None:
                    stats.page_chains.append(live_chain.as_dict())
                if isinstance(exc, RuntimeError) and "budget exhausted" in str(exc):
                    raise
                continue

            if chain_dict:
                stats.page_chains.append(chain_dict)

            stats.ohlc_violations += len(bars.ohlc_violations(collected))
            unit_symbols = sorted({r["symbol"] for r in collected})
            unit_expl = sorted(
                {
                    r["symbol"]
                    for r in collected
                    if not config.is_holdout_date(r["session_date"])
                }
            )
            _route_and_write(collected, label, year, batch_id, stats)

            # Marker is written only after the parquet lands, so a crash between
            # the two re-does the unit rather than skipping it.
            marker.parent.mkdir(parents=True, exist_ok=True)
            marker.write_text(
                json.dumps(
                    {
                        "rows": len(collected),
                        "symbols": unit_symbols,
                        "exploration_symbols": unit_expl,
                    }
                ),
                encoding="utf-8",
            )
            stats.units_done += 1

            if stats.units_done % 25 == 0:
                finalize.save_registry()
                print(
                    f"[{label}] {year} batch {batch_id} | units={stats.units_done} "
                    f"rows_expl={stats.rows_exploration} rows_hold={stats.rows_holdout} "
                    f"req={client.counter.count}",
                    flush=True,
                )

    stats.symbols_with_data = len(seen_exploration)
    stats.symbols_with_data_any_window = len(seen_any_window)
    stats.symbols_empty = len(symbols) - len(seen_exploration)
    stats.zero_exploration_symbols = sorted(set(symbols) - seen_exploration)
    for sym in symbols:
        if sym not in seen_any_window:
            stats.gaps.append(
                {"symbol": sym, "timeframe": label, "reason": "no_bars_in_any_year"}
            )
    return stats


def completeness_assessment(chains: list[dict[str, Any]]) -> dict[str, Any]:
    """Derive the page-completeness claim from the chains themselves.

    Emitted by every run so the shipped artifact contract is reproducible from
    source rather than hand-written after the fact. When any chain failed to
    terminate on a null token the claim is UNVERIFIED, and the substitute
    arguments are recorded WITH their limitations -- argument B in particular
    is unsound because annual chains are independent, so a chain truncated in
    one year says nothing about another year completing normally.
    """
    incomplete = [c for c in chains if not c.get("complete")]
    if chains and not incomplete:
        return {
            "PAGE_COMPLETENESS": "VERIFIED",
            "basis": "every recorded chain terminated on a null next_page_token",
            "chains_recorded": len(chains),
            "chains_incomplete": 0,
        }
    return {
        "PAGE_COMPLETENESS": "UNVERIFIED",
        "completeness_resolution": "WEAKENED_CLAIM",
        "chains_recorded": len(chains),
        "chains_incomplete": len(incomplete),
        "metric_status": (
            "MIS_INSTRUMENTED_IN_THIS_RUN"
            if chains and len(incomplete) == len(chains)
            else "PARTIAL"
        ),
        "substitute_arguments_assessed": {
            "A_row_reconciliation": "INTERNAL_CONSISTENCY_ONLY -- truncation lowers "
            "the runner counter and the parquet count together",
            "B_stop_then_resume": "WITHDRAWN_COUNTEREXAMPLE_EXISTS -- annual chains "
            "are independent, so one year truncating is compatible with another "
            "completing",
            "C_checksum_and_access_log": "NOT_INDEPENDENT -- write-time digest "
            "compared against write-time records is circular",
        },
        "what_would_actually_prove_it": (
            "re-run collection with fixed instrumentation and capture the terminal "
            "next_page_token=null per chain"
        ),
    }


def write_checkpoint(name: str, payload: dict[str, Any]) -> None:
    """Append a checkpoint to the job progress log and persist it (§3.2/§3.10)."""
    config.PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
    stamp = _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with config.PROGRESS_PATH.open("a", encoding="utf-8") as handle:
        handle.write(f"\n## checkpoint {name} ({stamp})\n")
        handle.write("```json\n" + json.dumps(payload, indent=2) + "\n```\n")
    labels.write_labelled_json(config.REPORTS_DIR / f"checkpoint_{name}.json", payload)


MEASUREMENT_CACHE = "measurement_1m.json"


def measure(client: alpaca_data.AlpacaDataClient) -> dict[str, Any]:
    """Measure what the scope-C budget depends on (§3.1). Nothing is assumed.

    Cached to `_staging/` because each measurement costs real requests against
    the same 80,000 budget it is protecting -- re-running it on every relaunch
    would spend the budget on measuring instead of collecting.

    Two lessons from the 1Hour block are baked in here:
      * page geometry is measured PER TIMEFRAME. A bars-per-page figure taken
        from 1Min does not transfer to 1Hour, and assuming it did is what made
        the first projection wrong by ~29x.
      * density is sampled across ranks AND years. A 2-symbol probe projected
        90,750 requests (would have blocked) because NVDA is an outlier at
        229k rows/year against a 12-symbol mean of 111k.
    """
    cache = config.STAGING_DIR / MEASUREMENT_CACHE
    if cache.exists():
        cached = json.loads(cache.read_text(encoding="utf-8"))
        print(f"reusing cached measurement from {cache}", flush=True)
        return cached

    m: dict[str, Any] = alpaca_data.probe_multi_symbol_form(
        client, ["AAPL", "MSFT", "NVDA"]
    )

    sample = load_top500()[::42][:12]
    m["m1_sample"] = sample
    m["m1_sample_size"] = len(sample)

    per_year: dict[str, float] = {}
    rows_per_request: list[float] = []
    for year in (2016, 2024):
        total_rows = 0
        before = client.counter.count
        for symbol in sample:
            for payload, _chain in client.iter_bars(
                [symbol], "1Min", f"{year}-01-01T00:00:00Z", f"{year}-12-31T23:59:59Z"
            ):
                total_rows += len((payload.get("bars") or {}).get(symbol) or [])
        used = max(1, client.counter.count - before)
        per_year[str(year)] = total_rows / len(sample)
        rows_per_request.append(total_rows / used)

    m["m1_rows_per_symbol_year_2016"] = round(per_year["2016"], 1)
    m["m1_rows_per_symbol_year_2024"] = round(per_year["2024"], 1)
    m["m1_rows_per_request_measured"] = round(
        sum(rows_per_request) / len(rows_per_request), 1
    )
    m["m1_year_ratio_2016_over_2024"] = round(per_year["2016"] / per_year["2024"], 3)
    m["requests_spent_measuring"] = client.counter.count
    m["pagination_model"] = (
        "1Min is ROW-capped (~9,800 rows/request). 1Hour is TIME-chunk limited "
        "(<=416 rows/request) and never reaches the row cap -- which is why the "
        "full-universe hourly corpus did not fit the budget."
    )

    cache.parent.mkdir(parents=True, exist_ok=True)
    cache.write_text(json.dumps(m, indent=2), encoding="utf-8")
    return m


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="build us-intraday-corpus-v1")
    parser.add_argument("--probe-only", action="store_true")
    parser.add_argument("--skip-1m", action="store_true")
    # Scope C (operator decision, 2026-08-03): 1Min top-500 only. 1Hour is a
    # recorded data gap, so the DEFAULT run must not silently collect it --
    # the declared scope and the default code path have to agree.
    parser.add_argument(
        "--phase",
        choices=["1h", "1m", "both"],
        default="1m",
        help="default 1m enforces SCOPE_DECISION=C_1M_TOP500_ONLY",
    )
    parser.add_argument(
        "--override-scope-c",
        action="store_true",
        help="explicitly opt out of scope C to collect 1Hour (see config.HOUR_DATA_GAP)",
    )
    args = parser.parse_args(argv)

    if args.phase in ("1h", "both") and not args.override_scope_c:
        print(
            f"BLOCKED: scope is {config.SCOPE_DECISION} and 1Hour collection is a "
            "recorded data gap (config.HOUR_DATA_GAP). Pass --override-scope-c to "
            "collect it deliberately.",
            flush=True,
        )
        return 2

    started = time.monotonic()
    finalize.load_registry()

    try:
        client = alpaca_data.AlpacaDataClient()
    except alpaca_data.AlpacaCredentialsMissing as exc:
        write_checkpoint("blocked_credentials", {"error": str(exc)})
        print(f"BLOCKED_PRECONDITION: {exc}")
        return 2

    measurement = measure(client)
    projection = project_budget(measurement)
    write_checkpoint("budget", {"measurement": measurement, "projection": projection})
    print(json.dumps(projection, indent=2), flush=True)

    if not projection["within_budget"]:
        print(
            f"BLOCKED_PRECONDITION: projected {projection['requests_total_projected']} "
            f"requests exceeds MAX_REQUESTS={config.MAX_REQUESTS}"
        )
        return 2
    if args.probe_only:
        return 0

    phases: list[dict[str, Any]] = []
    all_gaps: list[dict[str, Any]] = []
    all_chains: list[dict[str, Any]] = []

    if args.phase in ("1h", "both"):
        stats_1h = run_phase(client, load_universe(), "1Hour", "1h", BATCH_1H)
        finalize.save_registry()
        phases.append(stats_1h.as_dict())
        all_gaps += stats_1h.gaps
        all_chains += stats_1h.page_chains
        write_checkpoint(
            "midpoint_1h_complete_before_1m",
            {
                "phase": stats_1h.as_dict(),
                "requests_actual": client.counter.count,
                "rate_429_count": client.counter.rate_429,
                "wall_clock_hours": round((time.monotonic() - started) / 3600, 3),
                "note": (
                    "1Hour complete. 1Min has NOT started; this is a clean "
                    "boundary at which the corpus can be sealed BUILT_WITH_GAPS."
                ),
            },
        )

    if args.phase in ("1m", "both") and not args.skip_1m:
        stats_1m = run_phase(client, load_top500(), "1Min", "1m", BATCH_1M)
        finalize.save_registry()
        phases.append(stats_1m.as_dict())
        all_gaps += stats_1m.gaps
        all_chains += stats_1m.page_chains

    assessment = completeness_assessment(all_chains)
    labels.write_labelled_json(
        config.REPORTS_DIR / "page_chain_integrity.json",
        {
            **assessment,
            "chains_total": len(all_chains),
            "incomplete": [c for c in all_chains if not c["complete"]][:200],
            "chains": all_chains[:2000],
        },
    )
    labels.write_labelled_csv(
        config.REPORTS_DIR / "explicit_gaps.csv",
        all_gaps,
        fieldnames=["symbol", "timeframe", "reason"],
    )

    zero_expl = sorted(
        {s for p in phases for s in p.get("zero_exploration_symbols", [])}
    )
    # A symbol with no exploration rows, or an unproven page-completeness
    # claim, both mean the corpus is not unqualifiedly research-ready.
    verdict = (
        "READY_FOR_RESEARCH"
        if not all_gaps
        and not zero_expl
        and assessment["PAGE_COMPLETENESS"] == "VERIFIED"
        else "BUILT_WITH_GAPS"
    )
    finalize.seal(
        terminal_verdict=verdict,
        body={
            "phases": phases,
            "page_chain_integrity": {
                **assessment,
                "report": "reports/page_chain_integrity.json",
            },
            "coverage": {
                "symbols_selected": sum(p["symbols_attempted"] for p in phases),
                "symbols_with_exploration_data": sum(
                    p["symbols_with_data"] for p in phases
                ),
                "symbols_with_zero_exploration_data": len(zero_expl),
                "zero_exploration_symbols": zero_expl,
                "symbols_with_data_in_ANY_window_including_holdout": sum(
                    p["symbols_with_data_any_window"] for p in phases
                ),
                "reading_guide": (
                    "The any-window count INCLUDES the sealed holdout. For "
                    "exploration/backtest purposes use symbols_with_exploration_data; "
                    "a holdout-only symbol is not ordinary coverage."
                ),
            },
            "budget_measurement": measurement,
            "budget_projection": projection,
            "requests_actual": client.counter.count,
            "rate_429_count": client.counter.rate_429,
            "wall_clock_hours": round((time.monotonic() - started) / 3600, 3),
            "forward_fill_used": False,
            "unfinished_bar_stored": False,
        },
    )
    print(verdict, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
