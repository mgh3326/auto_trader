"""CLI for the KR-B1c C_stress_cap reference reducer."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from research.krb1_c_stress_reducer.model import (
    ContractError,
    load_cost_inputs,
    load_tick_tables,
)
from research.krb1_c_stress_reducer.reducer import run_reducer, write_artifacts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the sealed KR-B1c exact-rational reference reducer."
    )
    parser.add_argument("--cost-input", type=Path, required=True)
    parser.add_argument("--tick-input", type=Path, required=True)
    parser.add_argument("--parent-canonical", type=Path, required=True)
    parser.add_argument("--amendment-canonical", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--repo-root", type=Path, default=Path.cwd())
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cost_inputs = load_cost_inputs(args.cost_input)
        tick_tables = load_tick_tables(args.tick_input)
        run = run_reducer(cost_inputs, tick_tables)
        result = write_artifacts(
            run=run,
            cost_inputs=cost_inputs,
            tick_tables=tick_tables,
            output_dir=args.output_dir,
            repo_root=args.repo_root.resolve(),
            parent_canonical_path=args.parent_canonical,
            amendment_canonical_path=args.amendment_canonical,
        )
    except ContractError as exc:
        print(f"KR-B1c reducer FAIL: {exc}")
        return 1
    summary = {
        "status": "PASS",
        "arithmetic": result["arithmetic"],
        "float_used": result["float_used"],
        "candidate_count_total": result["candidate_count_total"],
        "market_summaries": result["market_summaries"],
        "c_raw": f"{result['c_raw_num']}/{result['c_raw_den']}",
        "witness_market": result["witness_market"],
        "witness_entry_price": result["witness_entry_price"],
        "witness_exit_price": result["witness_exit_price"],
        "c_stress_cap_bp": result["c_stress_cap_bp"],
        "c_stress_cap_decimal": result["c_stress_cap_decimal"],
        "all_target_checks_passed": result["all_target_checks_passed"],
        "p0_2_completion_state": result["p0_2_completion_state"],
    }
    print(
        json.dumps(
            summary,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
            allow_nan=False,
        )
    )
    return 0
