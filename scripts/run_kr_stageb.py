"""Bounded KR Stage-B real-data runner; read-only and scheduleless."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

from research.kr_corpus.backtest.evidence import write_stage_b_evidence
from research.kr_corpus.backtest.real_data import load_real_main_bars
from research.kr_corpus.backtest.stage_b import build_run_contract, run_stage_b


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run explicit KR Stage-B real-data trial"
    )
    parser.add_argument("--artifact-root", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--start", type=date.fromisoformat, required=True)
    parser.add_argument("--end", type=date.fromisoformat, required=True)
    parser.add_argument("--market", action="append", required=True)
    parser.add_argument("--max-symbols", type=int, required=True)
    parser.add_argument("--cost-profile", choices=("43bp", "83bp"), required=True)
    parser.add_argument("--evidence", type=Path, required=True)
    args = parser.parse_args()
    contract = build_run_contract(
        cost_profile=args.cost_profile,
        window_start=args.start,
        window_end=args.end,
    )
    bars = load_real_main_bars(
        artifact_root=args.artifact_root,
        run_id=args.run_id,
        window_start=args.start,
        window_end=args.end,
        markets=args.market,
        max_symbols=args.max_symbols,
    )
    result = run_stage_b(bars=bars, contract=contract)
    payload = write_stage_b_evidence(args.evidence, result)
    print(
        json.dumps(
            {
                "result": result.to_dict(),
                "evidence": str(args.evidence),
                "trial_evidence_schema": payload["trial_evidence"]["schema_version"],
            },
            ensure_ascii=False,
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
