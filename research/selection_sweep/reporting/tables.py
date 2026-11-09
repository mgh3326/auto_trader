"""Render ledger rows as ratio-only markdown. Usage:

python -m research.selection_sweep.reporting.tables
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .. import spec

COMMITTED = Path(__file__).resolve().parents[1] / "results"


def load(name: str, results: Path = COMMITTED) -> list[dict]:
    path = results / name
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line]


def bps(value) -> str:
    return "n/a" if value is None else f"{value * 1e4:+.0f}"


def grid_table(rows: list[dict], market: str, side: str) -> str:
    cells = {r["cell"]: r for r in rows if r.get("phase") == "exploration"}
    head = "| ranker | " + " | ".join(f"h={h}" for h in spec.HORIZONS) + " |"
    lines = [head, "|---|" + "---|" * len(spec.HORIZONS)]
    for ranker in spec.RANKERS:
        parts = []
        for h in spec.HORIZONS:
            r = cells[spec.Cell(market, ranker, h, side).key]
            m = r["metrics"]
            mark = "*" if r["carried"] else ""
            parts.append(f"{bps(m['net_primary_mean'])} / p{m['random_p']:.2f}{mark}")
        lines.append(f"| {ranker} | " + " | ".join(parts) + " |")
    return "\n".join(lines)


def detail_table(rows: list[dict], *, validation: dict[str, dict] | None = None) -> str:
    head = (
        "| cell | primary bps | long leg vs benchmark bps | random mean [p05, p95] bps | "
        "random p | NW t | years + | dates |"
    )
    lines = [head, "|---|---|---|---|---|---|---|---|"]
    for r in rows:
        if r.get("phase") == "extra":
            continue
        m = r["metrics"]
        t = m["t_newey_west"]
        lines.append(
            f"| {r['cell'].replace('|', ' / ')} | {bps(m['net_primary_mean'])} | "
            f"{bps(m['long_excess_vs_benchmark'])} | "
            f"{bps(m['random_mean'])} [{bps(m['random_p05'])}, {bps(m['random_p95'])}] | "
            f"{m['random_p']:.3f} | {'n/a' if t is None else f'{t:.2f}'} | "
            f"{m['positive_years']}/{m['years_measured']} | {m['dates']} |"
        )
    return "\n".join(lines)


def exploration_metrics(rows: list[dict]) -> dict[str, dict]:
    return {r["cell"]: r["metrics"] for r in rows if r.get("phase") == "exploration"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--results-dir", default=str(COMMITTED))
    parser.add_argument(
        "--check-against-committed",
        action="store_true",
        help="exit 1 unless every exploration cell metric equals the committed ledger",
    )
    args = parser.parse_args(argv)
    results = Path(args.results_dir)
    all_rows = load("trials_ledger.jsonl", results)
    if args.check_against_committed:
        same = exploration_metrics(all_rows) == exploration_metrics(
            load("trials_ledger.jsonl", COMMITTED)
        )
        print(f"exploration cell metrics identical to committed ledger: {same}")
        return 0 if same else 1
    ledger = [r for r in all_rows if r.get("phase") == "exploration"]
    for market in spec.MARKETS:
        for side in spec.SIDES:
            print(f"\n#### {market} {side}\n")
            print(grid_table(ledger, market, side))
    near = [
        r
        for r in ledger
        if r["metrics"]["random_p"] <= spec.CARRY_MAX_RANDOM_P
        and r["metrics"]["net_primary_mean"] > 0
    ]
    print(
        f"\n#### random p <= {spec.CARRY_MAX_RANDOM_P} and primary > 0: {len(near)} cells\n"
    )
    print(detail_table(near))
    print("\n#### every cell\n")
    print(detail_table(ledger))
    validation = load("validation_results.jsonl", results)
    if validation:
        print("\n#### validation (2024-02-01..2024-12-31)\n")
        print(detail_table(validation))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
