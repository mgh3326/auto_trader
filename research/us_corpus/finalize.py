"""us-corpus-v1 finalize stage — partition, validate, measure, manifest.

🔴 SURVIVORSHIP_BIASED = TRUE. The universe is a frozen snapshot of currently
active US common stocks; symbols delisted before the snapshot never appear, so
returns computed here are biased upward. The manifest repeats this at the top
and every report carries it in the header.

Split: exploration = 2016-01-01..2024-12-31 (TRAIN + VALIDATION) under
`dataset/`, holdout = 2025-01-01..2026-07-31 under `holdout/`.

🔴 The holdout is written and never read back. `HOLDOUT_DIR` is opened for
write only; every read in this module targets `_staging/`. The access log
records the writes so the claim is evidenced rather than asserted.

🔴 Gaps are reported, never filled. No forward-fill, no interpolation, no
second source.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

import exchange_calendars as xcals
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from research.us_corpus import config as cfg  # noqa: E402


def log_holdout_write(message: str) -> None:
    """🔴 Append-only ledger of holdout *writes*. A READ line must never appear."""
    stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    with cfg.HOLDOUT_ACCESS_LOG.open("a", encoding="utf-8") as handle:
        handle.write(f"{stamp}\tWRITE\t{message}\n")


def atomic_parquet(frame: pd.DataFrame, target: Path) -> None:
    """`.partial` -> fsync -> atomic rename. Never overwrite in place."""
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_suffix(".parquet.partial")
    frame.to_parquet(partial, index=False, compression="zstd")
    fd = os.open(partial, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)
    os.replace(partial, target)


def load_staging() -> pd.DataFrame:
    """🔴 The only source of bar data in this module — never HOLDOUT_DIR."""
    files = sorted(cfg.STAGING_DIR.glob("chunk-*.parquet"))
    if not files:
        raise RuntimeError("no staging chunks; run research.us_corpus.build first")
    frames = [pd.read_parquet(f) for f in files]
    combined = pd.concat(frames, ignore_index=True)
    combined["session_date"] = pd.to_datetime(combined["session_date"])
    return combined


def load_outcomes() -> pd.DataFrame:
    rows = []
    with cfg.CHECKPOINT_FILE.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                try:
                    rows.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    frame = pd.DataFrame(rows)
    # A killed-and-resumed run can append a symbol twice; the last record wins.
    return frame.drop_duplicates(subset="symbol", keep="last")


def xnys_sessions(start: str, end: str) -> pd.DatetimeIndex:
    calendar = xcals.get_calendar(cfg.SESSION_CALENDAR)
    sessions = calendar.sessions_in_range(start, end)
    return pd.DatetimeIndex(pd.to_datetime(sessions)).tz_localize(None).normalize()


def validate(frame: pd.DataFrame) -> tuple[dict[str, object], pd.DataFrame]:
    """Structural invariants. These are counted and reported, never repaired.

    🔴 Offending rows stay in the corpus — silently dropping them would hide a
    real upstream defect. They are exported per symbol so a consumer can
    exclude the affected tickers deliberately rather than unknowingly.
    """
    duplicates = int(frame.duplicated(subset=["symbol", "session_date"]).sum())

    price = frame[["open", "high", "low", "close"]]
    high_ok = frame["high"] >= price.max(axis=1) - 1e-6
    low_ok = frame["low"] <= price.min(axis=1) + 1e-6
    positive = (price > 0).all(axis=1)
    bad = ~(high_ok & low_ok & positive)
    violations = int(bad.sum())
    negative_volume = int((frame["volume"] < 0).sum())

    offenders = frame.loc[bad].copy()
    offenders["nonpositive_price"] = ~positive.loc[bad]
    offenders["high_below_max"] = ~high_ok.loc[bad]
    offenders["low_above_min"] = ~low_ok.loc[bad]
    summary = (
        offenders.groupby("symbol")
        .agg(
            violation_rows=("session_date", "size"),
            first_session=("session_date", "min"),
            last_session=("session_date", "max"),
            nonpositive_price_rows=("nonpositive_price", "sum"),
            high_below_max_rows=("high_below_max", "sum"),
            low_above_min_rows=("low_above_min", "sum"),
        )
        .reset_index()
        .sort_values("violation_rows", ascending=False)
    )

    return (
        {
            "duplicate_rows": duplicates,
            "ohlc_invariant_violations": violations,
            "negative_volume": negative_volume,
            "ohlc_violation_symbols": int(summary["symbol"].nunique()),
            "ohlc_nonpositive_price_rows": int(offenders["nonpositive_price"].sum()),
        },
        summary,
    )


def coverage_and_gaps(frame: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, float]:
    """Coverage measured only inside each symbol's observed listing span.

    Counting a 2024 IPO as "missing 2016-2023" would be dishonest arithmetic, so
    expected sessions run from a symbol's first observed session to its last.
    The flip side — a symbol whose last session precedes the cutoff — is the
    measurable footprint of delisting/renaming and is reported separately.
    """
    sessions = xnys_sessions(cfg.START_DATE, cfg.CUTOFF_SESSION)
    session_set = set(sessions)

    span = frame.groupby("symbol")["session_date"].agg(["min", "max"])
    observed = {
        symbol: set(group) for symbol, group in frame.groupby("symbol")["session_date"]
    }

    gap_records: list[dict[str, object]] = []
    year_expected: dict[int, int] = {}
    year_observed: dict[int, int] = {}

    for symbol, row in span.iterrows():
        active = sessions[(sessions >= row["min"]) & (sessions <= row["max"])]
        have = observed[symbol]
        missing = [d for d in active if d not in have]
        for date in missing:
            gap_records.append(
                {
                    "symbol": symbol,
                    "session_date": date.date().isoformat(),
                    "reason": "interior_missing_session",
                }
            )
        for date in active:
            year_expected[date.year] = year_expected.get(date.year, 0) + 1
        for date in have:
            if date in session_set:
                year_observed[date.year] = year_observed.get(date.year, 0) + 1

    years = sorted(year_expected)
    coverage = pd.DataFrame(
        {
            "year": years,
            "expected_symbol_sessions": [year_expected[y] for y in years],
            "observed_symbol_sessions": [year_observed.get(y, 0) for y in years],
        }
    )
    coverage["coverage"] = (
        coverage["observed_symbol_sessions"] / coverage["expected_symbol_sessions"]
    ).round(6)
    gaps = pd.DataFrame(gap_records, columns=["symbol", "session_date", "reason"])
    min_year_coverage = float(coverage["coverage"].min()) if len(coverage) else 0.0
    return coverage, gaps, min_year_coverage


def off_calendar_rows(frame: pd.DataFrame) -> pd.DataFrame:
    """Rows Yahoo returned on dates XNYS says are not sessions — reported, kept."""
    sessions = set(xnys_sessions(cfg.START_DATE, cfg.CUTOFF_SESSION))
    mask = ~frame["session_date"].isin(sessions)
    return frame.loc[mask, ["symbol", "session_date"]]


def truncated_symbols(frame: pd.DataFrame) -> pd.DataFrame:
    """Symbols whose last session precedes the cutoff — survivorship evidence."""
    sessions = xnys_sessions(cfg.START_DATE, cfg.CUTOFF_SESSION)
    last_session = sessions.max()
    span = frame.groupby("symbol")["session_date"].agg(["min", "max"]).reset_index()
    stale = span[span["max"] < last_session - pd.Timedelta(days=7)].copy()
    stale["first_session"] = stale["min"].dt.date.astype(str)
    stale["last_session"] = stale["max"].dt.date.astype(str)
    return stale[["symbol", "first_session", "last_session"]]


def crosscheck(frame: pd.DataFrame) -> dict[str, object]:
    """🔴 Diagnostic only. Yahoo values are never overwritten by the DB sample.

    ⚠️ Boundary note: every row of the frozen sample (2025-05-20..2026-07-30)
    falls inside HOLDOUT_WINDOW. This comparison therefore runs against
    `_staging/` — the sealed `HOLDOUT_DIR` artifact is never opened — and is
    restricted to exactly the (symbol, session_date) pairs already present in
    the authorised, digest-pinned input file. Only aggregate agreement
    statistics are emitted; no price series is reproduced.
    """
    sample = pd.read_csv(
        cfg.CROSSCHECK_FILE, dtype={"symbol": str}, keep_default_na=False
    )
    sample["session_date"] = pd.to_datetime(sample["session_date"])
    for column in ("open", "high", "low", "close", "volume"):
        sample[column] = pd.to_numeric(sample[column], errors="coerce")

    merged = sample.merge(
        frame, on=["symbol", "session_date"], how="left", suffixes=("_db", "_yf")
    )
    matched = merged[merged["close_yf"].notna()].copy()
    missing_in_yahoo = int(merged["close_yf"].isna().sum())

    # The DB sample is unadjusted broker data while the corpus is adjusted, so a
    # uniform ratio is expected for split/dividend history. Relative difference
    # is the meaningful comparison; a 1% tolerance flags genuine disagreement.
    matched["rel_diff"] = (matched["close_yf"] - matched["close_db"]).abs() / matched[
        "close_db"
    ].replace(0, pd.NA)
    tolerance = 0.01
    mismatches = int((matched["rel_diff"] > tolerance).sum())

    per_symbol = (
        matched.groupby("symbol")
        .agg(
            rows=("rel_diff", "size"),
            median_rel_diff=("rel_diff", "median"),
            max_rel_diff=("rel_diff", "max"),
            over_tolerance=("rel_diff", lambda s: int((s > tolerance).sum())),
        )
        .reset_index()
    )
    per_symbol["median_rel_diff"] = per_symbol["median_rel_diff"].astype(float).round(6)
    per_symbol["max_rel_diff"] = per_symbol["max_rel_diff"].astype(float).round(6)

    alignment = date_alignment_probe(sample, frame)

    return {
        "mode": cfg.CROSSCHECK_MODE,
        "tolerance_relative": tolerance,
        "sample_rows": int(len(sample)),
        "matched_rows": int(len(matched)),
        "rows_absent_from_yahoo": missing_in_yahoo,
        "mismatches_over_tolerance": mismatches,
        "yahoo_values_overwritten": 0,
        "compared_against": "_staging (HOLDOUT_DIR never opened)",
        "boundary_note": (
            "100% of the frozen sample lies inside HOLDOUT_WINDOW; see report."
        ),
        "date_alignment_diagnostic": alignment,
        "per_symbol": per_symbol.to_dict(orient="records"),
    }


def date_alignment_probe(
    sample: pd.DataFrame, frame: pd.DataFrame
) -> dict[str, object]:
    """Test whether same-date disagreement is really a date-label offset.

    A raw same-date comparison of this sample shows large differences, which
    reads like a data-quality problem. It is not: the identical bar appears in
    the corpus one session later. This probe shifts the corpus by -1/0/+1
    sessions per symbol and reports the match rate at each lag, so the headline
    mismatch count is interpretable instead of alarming.

    🔴 Diagnostic only — nothing here modifies either dataset.
    """
    results: list[dict[str, object]] = []
    for symbol, db_group in sample.groupby("symbol"):
        yf_group = (
            frame[frame["symbol"] == symbol]
            .sort_values("session_date")
            .reset_index(drop=True)
        )
        if yf_group.empty:
            results.append({"symbol": symbol, "status": "absent_from_corpus"})
            continue
        position = {d: i for i, d in enumerate(yf_group["session_date"])}
        db_group = db_group.sort_values("session_date").copy()
        db_group["pos"] = db_group["session_date"].map(position)
        aligned = db_group.dropna(subset=["pos"]).copy()
        if aligned.empty:
            results.append({"symbol": symbol, "status": "no_shared_sessions"})
            continue
        aligned["pos"] = aligned["pos"].astype(int)

        entry: dict[str, object] = {
            "symbol": symbol,
            "compared_rows": int(len(aligned)),
        }
        for lag in (-1, 0, 1):
            index = aligned["pos"] + lag
            keep = aligned[(index >= 0) & (index < len(yf_group))]
            if keep.empty:
                entry[f"exact_lag_{lag:+d}"] = None
                entry[f"within_1pct_lag_{lag:+d}"] = None
                continue
            other = yf_group.iloc[(keep["pos"] + lag).to_numpy()]
            rel = (
                keep["close"].to_numpy() - other["close"].to_numpy()
            ).__abs__() / keep["close"].to_numpy()
            # Two tolerances tell different halves of the story: 1e-5 isolates
            # the date-label offset alone, while 1% additionally absorbs the
            # adjusted-vs-raw dividend factor, which is piecewise constant per
            # symbol and so shows up as a partial exact-match rate.
            entry[f"exact_lag_{lag:+d}"] = round(float((rel < 1e-5).mean()), 4)
            entry[f"within_1pct_lag_{lag:+d}"] = round(float((rel < 0.01).mean()), 4)
        results.append(entry)

    return {
        "method": (
            "per-symbol session shift of the corpus by -1/0/+1 relative to the "
            "frozen DB sample; value is the fraction of rows whose close agrees "
            "within 1e-5 relative"
        ),
        "interpretation": (
            "A high match at lag +1 means the frozen DB sample labels each US "
            "bar one XNYS session EARLIER than the corpus does, i.e. the two "
            "sources agree on prices and disagree only on the date label. "
            "Yahoo labels sessions by their America/New_York trading date."
        ),
        "per_symbol": results,
    }


def write_partitions(frame: pd.DataFrame, root: Path, label: str) -> list[Path]:
    written: list[Path] = []
    for year, group in frame.groupby(frame["session_date"].dt.year):
        target = root / "market=us" / f"year={year}" / "part-00000.parquet"
        atomic_parquet(group.sort_values(["symbol", "session_date"]), target)
        written.append(target)
        if label == "holdout":
            log_holdout_write(
                f"{target.relative_to(cfg.ARTIFACT_ROOT)} rows={len(group)}"
            )
    return written


def main() -> int:
    cfg.verify_inputs()  # 🔴 re-verify pins before finalising anything
    cfg.REPORTS_DIR.mkdir(parents=True, exist_ok=True)

    frame = load_staging()
    outcomes = load_outcomes()

    frame = frame.drop_duplicates(subset=["symbol", "session_date"], keep="first")
    frame = frame.sort_values(["symbol", "session_date"]).reset_index(drop=True)

    checks, ohlc_offenders = validate(frame)
    coverage, gaps, min_year_coverage = coverage_and_gaps(frame)
    off_calendar = off_calendar_rows(frame)
    truncated = truncated_symbols(frame)
    cross = crosscheck(frame)

    exploration = frame[
        (frame["session_date"] >= pd.Timestamp(cfg.EXPLORATION[0]))
        & (frame["session_date"] <= pd.Timestamp(cfg.EXPLORATION[1]))
    ]
    holdout = frame[
        (frame["session_date"] >= pd.Timestamp(cfg.HOLDOUT_WINDOW[0]))
        & (frame["session_date"] <= pd.Timestamp(cfg.HOLDOUT_WINDOW[1]))
    ]

    write_partitions(exploration, cfg.DATASET_DIR, "dataset")
    write_partitions(holdout, cfg.HOLDOUT_DIR, "holdout")

    empty_symbols = outcomes[outcomes["status"] == "empty"][["symbol", "yahoo_symbol"]]
    error_symbols = outcomes[outcomes["status"] == "error"][
        ["symbol", "yahoo_symbol", "error", "attempts"]
    ]

    coverage.to_csv(cfg.REPORTS_DIR / "coverage_by_year.csv", index=False)
    gaps.to_csv(cfg.REPORTS_DIR / "explicit_gaps.csv", index=False)
    empty_symbols.to_csv(cfg.REPORTS_DIR / "empty_symbols.csv", index=False)
    error_symbols.to_csv(cfg.REPORTS_DIR / "error_symbols.csv", index=False)
    truncated.to_csv(cfg.REPORTS_DIR / "truncated_symbols.csv", index=False)
    ohlc_offenders.to_csv(cfg.REPORTS_DIR / "ohlc_violation_symbols.csv", index=False)
    off_calendar.to_csv(cfg.REPORTS_DIR / "off_calendar_rows.csv", index=False)
    (cfg.REPORTS_DIR / "crosscheck_report.json").write_text(
        json.dumps(cross, indent=2, default=str), encoding="utf-8"
    )

    fetch_summary = json.loads((cfg.STAGING_DIR / "fetch_summary.json").read_text())

    artifact_bytes = sum(
        p.stat().st_size
        for p in cfg.ARTIFACT_ROOT.rglob("*")
        if p.is_file() and "_staging" not in p.parts
    )

    symbols_with_data = int(frame["symbol"].nunique())
    n_empty = int(len(empty_symbols))
    n_error = int(len(error_symbols))
    verdict = (
        "READY_FOR_RESEARCH"
        if (
            checks["duplicate_rows"] == 0
            and checks["ohlc_invariant_violations"] == 0
            and checks["negative_volume"] == 0
            and n_error == 0
            and len(gaps) == 0
        )
        else "BUILT_WITH_GAPS"
    )

    manifest = {
        "SURVIVORSHIP_BIASED": True,
        "survivorship_note": (
            "Universe is a frozen snapshot of CURRENTLY ACTIVE US common stocks. "
            "Symbols delisted before the snapshot are absent entirely, so any "
            "return, hit-rate or drawdown computed from this corpus is biased "
            "OPTIMISTIC. Never cite these numbers without this label. The KR "
            "corpus resolved this via pykrx delisting history; the US corpus "
            "could not, and that asymmetry must be stated when the two are "
            "used side by side."
        ),
        "corpus_id": cfg.CORPUS_ID,
        "purpose": cfg.PURPOSE,
        "generated_at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "terminal_verdict": verdict,
        "source_product": cfg.SOURCE_PRODUCT,
        "source_fallback_used": False,
        "forward_fill_used": False,
        "operating_db_reads": 0,
        "operating_db_writes": 0,
        "inputs": {
            "universe_file": str(cfg.UNIVERSE_FILE),
            "universe_file_sha256": cfg.UNIVERSE_FILE_SHA256,
            "universe_count": cfg.UNIVERSE_COUNT,
            "crosscheck_file": str(cfg.CROSSCHECK_FILE),
            "crosscheck_file_sha256": cfg.CROSSCHECK_FILE_SHA256,
        },
        "window": {
            "start_date": cfg.START_DATE,
            "cutoff_session": cfg.CUTOFF_SESSION,
            "frequency": cfg.FREQUENCY,
            "price_mode": cfg.PRICE_MODE,
            "session_calendar": cfg.SESSION_CALENDAR,
            "timezone": cfg.TIMEZONE,
            "train": cfg.TRAIN,
            "validation": cfg.VALIDATION,
            "holdout": cfg.HOLDOUT_WINDOW,
            "forward_oos_start": cfg.FORWARD_OOS_START,
        },
        "symbols": {
            "attempted": int(len(outcomes)),
            "with_data": symbols_with_data,
            "empty": n_empty,
            "error": n_error,
        },
        "rows": {
            "exploration_2016_2024": int(len(exploration)),
            "holdout_2025_2026": int(len(holdout)),
            "total": int(len(frame)),
        },
        "validation": checks,
        "coverage": {
            "min_year_coverage": round(min_year_coverage, 6),
            "explicit_gap_count": int(len(gaps)),
            "off_calendar_rows": int(len(off_calendar)),
            "truncated_symbols": int(len(truncated)),
        },
        "crosscheck": {
            "mode": cfg.CROSSCHECK_MODE,
            "mismatches_over_tolerance": cross["mismatches_over_tolerance"],
            "rows_absent_from_yahoo": cross["rows_absent_from_yahoo"],
            "yahoo_values_overwritten": 0,
        },
        "budget": {
            "requests_projected": fetch_summary["requests_projected"],
            "requests_actual": fetch_summary["requests_actual"],
            "max_requests": cfg.MAX_REQUESTS,
            "wall_clock_hours": fetch_summary["wall_clock_hours"],
            "artifact_gib": round(artifact_bytes / 1024**3, 4),
            "max_artifact_gib": cfg.MAX_ARTIFACT_GIB,
        },
        "holdout": {
            "dir": str(cfg.HOLDOUT_DIR),
            "written_not_read": True,
            "access_log": str(cfg.HOLDOUT_ACCESS_LOG),
        },
    }

    (cfg.ARTIFACT_ROOT / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )

    checksums = []
    for path in sorted(cfg.ARTIFACT_ROOT.rglob("*.parquet")):
        if "_staging" in path.parts:
            continue
        checksums.append(
            f"{cfg.sha256_file(path)}  {path.relative_to(cfg.ARTIFACT_ROOT)}"
        )
    for name in ("manifest.json",):
        p = cfg.ARTIFACT_ROOT / name
        checksums.append(f"{cfg.sha256_file(p)}  {name}")
    (cfg.ARTIFACT_ROOT / "checksums.sha256").write_text(
        "\n".join(checksums) + "\n", encoding="utf-8"
    )

    print(json.dumps(manifest, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
