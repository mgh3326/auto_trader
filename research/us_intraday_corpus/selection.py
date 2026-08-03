"""Deterministic 1Min universe selection: top 500 by 2024 average dollar volume.

Rule (brief §1 / §3.11), applied literally:
    from `us-corpus-v1/dataset/` (exploration partitions ONLY),
    over 2024-01-01..2024-12-31, rank symbols by mean(close * volume) and take
    the top 500.

Holdout non-contact is enforced on **both** axes and evidenced, not asserted:

* path axis -- we enumerate the concrete parquet files we are about to open and
  assert none of them is under any holdout directory.
* date axis -- after loading, we assert `max(session_date) < 2025-01-01`, so
  even a mislabelled partition cannot smuggle holdout rows into the ranking.

Both assertions are written into the selection report as evidence.
"""

from __future__ import annotations

import datetime as _dt
from dataclasses import dataclass
from pathlib import Path

from . import access_log, config, hashing, labels

SELECTION_SCRIPT = Path(__file__).resolve()


@dataclass(frozen=True)
class SelectionEvidence:
    files_read: list[str]
    holdout_paths_touched: int
    max_session_date: str
    min_session_date: str
    rows_considered: int
    symbols_ranked: int
    selection_script_sha256: str


def _exploration_2024_files() -> list[Path]:
    """Concrete parquet files for the 2024 exploration partition."""
    part = config.SISTER_DATASET_DIR / "market=us" / "year=2024"
    if not part.exists():
        raise FileNotFoundError(f"sister exploration partition missing: {part}")
    return sorted(part.glob("*.parquet"))


def select_top500() -> tuple[object, SelectionEvidence]:
    """Return (ranked_table, evidence). Reads only the 2024 exploration files."""
    import pyarrow.compute as pc
    import pyarrow.parquet as pq

    files = _exploration_2024_files()

    # --- path-axis holdout guard --------------------------------------------
    offending = [str(p) for p in files if access_log.is_holdout_path(p)]
    if offending:
        raise AssertionError(f"selection would read holdout paths: {offending}")

    tables = [
        pq.read_table(str(p), columns=["symbol", "session_date", "close", "volume"])
        for p in files
    ]
    import pyarrow as pa

    table = pa.concat_tables(tables)

    # --- date-axis holdout guard --------------------------------------------
    max_date = pc.max(table["session_date"]).as_py()
    min_date = pc.min(table["session_date"]).as_py()
    holdout_start = _dt.datetime(
        config.HOLDOUT[0].year, config.HOLDOUT[0].month, config.HOLDOUT[0].day
    )
    if max_date >= holdout_start:
        raise AssertionError(
            f"selection input reaches {max_date}, which is inside the holdout "
            f"window starting {config.HOLDOUT[0]}. Refusing to rank."
        )

    # --- ranking -------------------------------------------------------------
    dollar_volume = pc.multiply(
        pc.cast(table["close"], pa.float64()), pc.cast(table["volume"], pa.float64())
    )
    table = table.append_column("dollar_volume", dollar_volume)

    grouped = table.group_by("symbol").aggregate(
        [("dollar_volume", "mean"), ("dollar_volume", "count")]
    )
    # Deterministic order: dollar volume desc, then symbol asc to break ties.
    grouped = grouped.sort_by(
        [("dollar_volume_mean", "descending"), ("symbol", "ascending")]
    )
    top = grouped.slice(0, config.TOP500_COUNT)

    evidence = SelectionEvidence(
        files_read=[str(p) for p in files],
        holdout_paths_touched=0,
        max_session_date=str(max_date),
        min_session_date=str(min_date),
        rows_considered=table.num_rows,
        symbols_ranked=grouped.num_rows,
        selection_script_sha256=hashing.sha256_of_file(SELECTION_SCRIPT),
    )
    return top, evidence


def write_selection_snapshot() -> dict[str, object]:
    """Run the selection and persist the labelled snapshot into `inputs/`."""
    top, evidence = select_top500()

    symbols = top.column("symbol").to_pylist()
    means = top.column("dollar_volume_mean").to_pylist()
    counts = top.column("dollar_volume_count").to_pylist()

    rows = [
        {
            "rank": i + 1,
            "symbol": sym,
            "avg_dollar_volume_2024": f"{mean:.6f}",
            "session_count_2024": cnt,
        }
        for i, (sym, mean, cnt) in enumerate(zip(symbols, means, counts, strict=True))
    ]

    csv_path = config.INPUTS_DIR / "top500_1m_universe.csv"
    labels.write_labelled_csv(
        csv_path,
        rows,
        fieldnames=["rank", "symbol", "avg_dollar_volume_2024", "session_count_2024"],
    )
    snapshot_sha = hashing.sha256_of_file(csv_path)

    report_path = config.INPUTS_DIR / "top500_1m_selection_report.json"
    labels.write_labelled_json(
        report_path,
        {
            "selection_rule": (
                "top 500 by mean(close*volume) over 2024-01-01..2024-12-31 from "
                "us-corpus-v1/dataset (exploration partitions only); ties broken "
                "by symbol ascending"
            ),
            "window": [str(config.TOP500_WINDOW[0]), str(config.TOP500_WINDOW[1])],
            "count": len(rows),
            "snapshot_file": str(csv_path),
            "snapshot_sha256": snapshot_sha,
            "selection_script": str(SELECTION_SCRIPT),
            "selection_script_sha256": evidence.selection_script_sha256,
            "holdout_non_contact_evidence": {
                "files_read": evidence.files_read,
                "holdout_paths_touched": evidence.holdout_paths_touched,
                "min_session_date_in_input": evidence.min_session_date,
                "max_session_date_in_input": evidence.max_session_date,
                "holdout_window_starts": str(config.HOLDOUT[0]),
                "date_axis_guard": "max_session_date_in_input < holdout_window_starts",
            },
            "rows_considered": evidence.rows_considered,
            "symbols_ranked": evidence.symbols_ranked,
        },
    )

    return {
        "csv_path": str(csv_path),
        "snapshot_sha256": snapshot_sha,
        "report_path": str(report_path),
        "report_sha256": hashing.sha256_of_file(report_path),
        "selection_script_sha256": evidence.selection_script_sha256,
        "count": len(rows),
        "max_session_date_in_input": evidence.max_session_date,
    }


if __name__ == "__main__":
    import json

    print(json.dumps(write_selection_snapshot(), indent=2))
