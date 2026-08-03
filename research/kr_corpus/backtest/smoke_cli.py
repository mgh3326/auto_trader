"""Fixture smoke CLI for the KR backtest harness (Stage A).

Usage (from repo root, or any cwd — paths below are package-relative):

    uv run python -m research.kr_corpus.backtest.smoke_cli
    # or:
    cd research/kr_corpus/backtest && uv run python smoke_cli.py

Never reads CORPUS_ARTIFACT_ROOT or HOLDOUT_DIR.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# Allow flat imports when executed as a script.
_HERE = Path(__file__).resolve().parent
_REPO_ROOT = _HERE.parent.parent.parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from baseline_smoke import PIPELINE_SMOKE_LABEL, run_value_rank_topn_d5  # noqa: E402
from fixture_builder import FIXTURE_REL_ROOT, build_synthetic_fixture  # noqa: E402
from loader import load_manifest, load_shard  # noqa: E402
from membership import membership_rows_from_table  # noqa: E402
from pit import bars_from_table  # noqa: E402
from schema_contract import SCHEMA_ORIGIN, load_contract  # noqa: E402


def run_fixture_smoke(fixture_root: Path | None = None) -> dict:
    root = build_synthetic_fixture(fixture_root or FIXTURE_REL_ROOT)
    manifest = load_manifest(root / "manifest.json")

    bars = []
    membership = []
    for entry in manifest:
        table = load_shard(root, entry)
        if entry.dataset == "ohlcv":
            bars.extend(bars_from_table(table))
        elif entry.dataset == "membership":
            membership.extend(membership_rows_from_table(table))
        else:
            raise RuntimeError(f"unknown dataset {entry.dataset!r}")

    # Fixture window (exploration subset) — hard-coded in fixture_builder.
    result = run_value_rank_topn_d5(
        bars=bars,
        membership=membership,
        top_n=3,
        holding_days=5,
        window_start="2023-01-02",
        window_end="2023-02-15",
    )
    contract = load_contract()
    report = result.to_report_dict()
    report["fixture_root"] = str(root)
    report["manifest_entries"] = len(manifest)
    report["schema_origin"] = SCHEMA_ORIGIN
    report["schema_contract_id"] = contract["contract_id"]
    report["label"] = PIPELINE_SMOKE_LABEL
    report["CORPUS_ARTIFACT_ROOT_READS"] = 0
    report["HOLDOUT_READS"] = 0
    report["REAL_DATA_SMOKE_RAN"] = False
    return report


def main(argv: list[str] | None = None) -> int:
    _ = argv
    report = run_fixture_smoke()
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    print(f"\n{PIPELINE_SMOKE_LABEL}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
