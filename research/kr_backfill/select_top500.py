"""Deterministic top-500 KR symbol selection for the Phase 1 1m backfill.

Reads ONLY the kr-corpus-v1 exploration window (``dataset/.../year=2024``).
Never touches ``holdout/`` — asserted at runtime, not merely by convention.

RULE DEVIATION (must be approved before use)
--------------------------------------------
The signed brief specifies ranking by *2024 average trading value* (거래대금).
The corpus column ``value`` is **100% NaN** across the 2024 partition (measured:
47,437/47,437 sampled rows over 200 randomly chosen tickers). ``volume`` is
fully populated. This module therefore ranks by a documented **proxy**:

    turnover_proxy_krw(session) = close * volume

Because ``price_mode='adjusted'``, this is *adjusted* turnover, not the exact
historical 거래대금. Rank order is close but NOT identical to the literal rule.
The deviation is surfaced in the manifest so a reviewer can accept or reject it.
Nothing here silently substitutes one rule for the other.
"""

from __future__ import annotations

import argparse
import csv
import glob
import hashlib
import json
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

import pandas as pd

# --- literals -------------------------------------------------------------

CORPUS_RUN_DIR = Path(
    "/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1/runs/kr-corpus-v1-20260803-1001"
)
DATASET_DIR = CORPUS_RUN_DIR / "dataset"

#: Hard refusal target. Selection must never resolve a path under here.
HOLDOUT_DIR = Path("/Users/mgh3326/work/herdr-artifacts/kr-corpus-v1/holdout")

#: Exploration window only. 2025+ is the corpus holdout and is off-limits.
EXPLORATION_YEAR = 2024

MARKETS = ("KOSPI", "KOSDAQ")

#: 2024 had ~245 KRX sessions. Require most of them so thin/newly-listed names
#: cannot outrank continuously-traded ones on a handful of spiky sessions.
MIN_SESSIONS = 200

TOP_N = 500


class HoldoutTouched(RuntimeError):
    """Raised if any input path resolves under the corpus holdout directory."""


def _assert_not_holdout(path: Path) -> None:
    resolved = str(path.resolve()).casefold()
    root = str(HOLDOUT_DIR.resolve()).casefold()
    if resolved == root or resolved.startswith(root.rstrip("/") + "/"):
        raise HoldoutTouched(f"refusing holdout path: {path}")


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


@dataclass(frozen=True)
class Row:
    rank: int
    ticker: str
    market: str
    sessions: int
    mean_turnover_proxy_krw: float


def collect() -> tuple[list[Row], dict]:
    _assert_not_holdout(DATASET_DIR)

    files: list[Path] = []
    for market in MARKETS:
        pattern = str(
            DATASET_DIR
            / f"market={market}"
            / f"year={EXPLORATION_YEAR}"
            / "ticker=*.parquet"
        )
        files.extend(Path(p) for p in sorted(glob.glob(pattern)))

    for f in files:
        _assert_not_holdout(f)

    records: list[Row] = []
    skipped_thin = 0
    value_non_nan_rows = 0
    total_rows = 0

    for f in files:
        market = f.parent.parent.name.split("=", 1)[1]
        ticker = f.stem.split("=", 1)[1]
        df = pd.read_parquet(f, columns=["session", "close", "volume", "value"])
        total_rows += len(df)
        value_non_nan_rows += int(df["value"].notna().sum())

        if len(df) < MIN_SESSIONS:
            skipped_thin += 1
            continue

        proxy = (df["close"].astype("float64") * df["volume"].astype("float64")).mean()
        records.append(
            Row(
                rank=0,
                ticker=ticker,
                market=market,
                sessions=int(len(df)),
                mean_turnover_proxy_krw=float(proxy),
            )
        )

    # Deterministic order: turnover desc, then ticker asc as the tie-break.
    records.sort(key=lambda r: (-r.mean_turnover_proxy_krw, r.ticker))
    top = [
        Row(
            rank=i + 1,
            ticker=r.ticker,
            market=r.market,
            sessions=r.sessions,
            mean_turnover_proxy_krw=r.mean_turnover_proxy_krw,
        )
        for i, r in enumerate(records[:TOP_N])
    ]

    meta = {
        "corpus_run_dir": str(CORPUS_RUN_DIR),
        "exploration_year": EXPLORATION_YEAR,
        "markets": list(MARKETS),
        "input_files": len(files),
        "input_rows": total_rows,
        "candidates_after_min_sessions": len(records),
        "skipped_below_min_sessions": skipped_thin,
        "min_sessions": MIN_SESSIONS,
        "top_n": TOP_N,
        "ranking_rule": "mean(close * volume) over year=2024 sessions, desc; tie-break ticker asc",
        "ranking_rule_is_literal_brief_rule": False,
        "rule_deviation_reason": (
            "corpus column `value` (거래대금) is 100% NaN in the 2024 partition; "
            "close*volume used as documented proxy. price_mode=adjusted, so this is "
            "adjusted turnover and rank order is close but not identical to the literal rule."
        ),
        "value_column_non_nan_rows": value_non_nan_rows,
        "holdout_touched": False,
        "holdout_dir_asserted_against": str(HOLDOUT_DIR),
    }
    return top, meta


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", required=True, type=Path)
    args = ap.parse_args()

    out_dir: Path = args.out_dir
    out_dir.mkdir(parents=True, exist_ok=True)

    rows, meta = collect()

    csv_path = out_dir / "top500.csv"
    with csv_path.open("w", newline="") as fh:
        w = csv.DictWriter(
            fh,
            fieldnames=[
                "rank",
                "ticker",
                "market",
                "sessions",
                "mean_turnover_proxy_krw",
            ],
        )
        w.writeheader()
        for r in rows:
            w.writerow(asdict(r))

    meta["selected"] = len(rows)
    meta["output_csv"] = str(csv_path)
    meta["output_csv_sha256"] = sha256_file(csv_path)
    meta["selection_script_sha256"] = sha256_file(Path(__file__).resolve())

    manifest_path = out_dir / "selection_manifest.json"
    manifest_path.write_text(json.dumps(meta, indent=2, ensure_ascii=False) + "\n")

    print(json.dumps(meta, indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
