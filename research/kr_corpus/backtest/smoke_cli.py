"""Fixture smoke CLI for the KR backtest harness (Stage A).

Usage (from repo root, or any cwd — paths below are package-relative):

    uv run python smoke_cli.py
    uv run python smoke_cli.py --rebuild-fixture   # explicit regen only

Never reads CORPUS_ARTIFACT_ROOT or HOLDOUT_DIR.

**SHA-gate discipline (SHOULD-2):** the default smoke path loads the
**committed** fixture under ``fixtures/synthetic_v1/`` as-is. It does **not**
regenerate parquet before verifying manifest SHA-256. Regenerating first made
the SHA gate a false-green (smoke always rewrote matching digests). Rebuild is
opt-in via ``--rebuild-fixture`` / ``rebuild=True`` for local fixture authoring
only — never the default smoke path.
"""

from __future__ import annotations

import argparse
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

from baseline_smoke import (  # noqa: E402
    PIPELINE_SMOKE_LABEL,
    run_liquidity_proxy_decile_topn_d5,
)
from fixture_builder import FIXTURE_REL_ROOT, build_synthetic_fixture  # noqa: E402
from loader import load_manifest, load_shard  # noqa: E402
from membership import membership_rows_from_table  # noqa: E402
from pit import bars_from_table  # noqa: E402
from schema_contract import SCHEMA_ORIGIN, load_contract  # noqa: E402


def run_fixture_smoke(
    fixture_root: Path | None = None,
    *,
    rebuild: bool = False,
) -> dict:
    """Run pipeline smoke against a fixture tree.

    Default: use committed ``FIXTURE_REL_ROOT`` without rewriting files so
    ``load_shard`` actually exercises the manifest SHA-256 gate.
    """
    root = Path(fixture_root) if fixture_root is not None else FIXTURE_REL_ROOT
    if rebuild:
        build_synthetic_fixture(root)
    manifest_path = root / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(
            f"fixture manifest missing at {manifest_path}; "
            f"commit fixtures/synthetic_v1 or pass rebuild=True once to author"
        )

    manifest = load_manifest(manifest_path)

    bars = []
    membership = []
    for entry in manifest:
        # SHA gate exercises committed digests when rebuild=False.
        table = load_shard(root, entry)
        if entry.dataset == "ohlcv":
            bars.extend(bars_from_table(table))
        elif entry.dataset == "membership":
            membership.extend(membership_rows_from_table(table))
        else:
            raise RuntimeError(f"unknown dataset {entry.dataset!r}")

    # Fixture window (exploration subset) — hard-coded in fixture_builder.
    result = run_liquidity_proxy_decile_topn_d5(
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
    report["fixture_rebuild_before_verify"] = rebuild
    report["sha_gate_exercised_on_committed_bytes"] = not rebuild
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="KR backtest harness fixture smoke")
    parser.add_argument(
        "--rebuild-fixture",
        action="store_true",
        help="Regenerate synthetic fixture before smoke (NOT default; authors only)",
    )
    parser.add_argument(
        "--fixture-root",
        type=Path,
        default=None,
        help="Fixture root (default: committed fixtures/synthetic_v1)",
    )
    args = parser.parse_args(argv)
    report = run_fixture_smoke(args.fixture_root, rebuild=args.rebuild_fixture)
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    print(f"\n{PIPELINE_SMOKE_LABEL}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
