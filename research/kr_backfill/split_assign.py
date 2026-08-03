"""Deterministic 3-source symbol assignment. This table IS the provenance.

`kr_candles_1m` has no provider source column and adding one is forbidden, so
after Stage B there is no way to ask the database which broker a row came from.
This file is the only record. It must therefore be deterministic, reproducible
from inputs alone, and preserved permanently.

Assignment uses sequential Sainte-Lague apportionment over the target counts.
Two properties matter:

* exact counts (246/165/89) — the balanced-makespan split, and
* liquidity interleaving — consecutive ranks go to different sources, so no
  source ends up owning one end of the liquidity distribution. Without this a
  provider-specific quirk would be perfectly confounded with liquidity tier.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
from pathlib import Path

#: Balanced-makespan split. Derived, not measured — see PROJECTED_* in the
#: Stage A report. Recomputed at gate time from measured page size and pacing.
TARGET_COUNTS: dict[str, int] = {"toss": 246, "kiwoom": 165, "kis": 89}

#: Per-source fetch envelope (spec/code-derived; gate re-measures).
SOURCE_SPEC: dict[str, dict] = {
    "toss": {
        "rows_per_call": 200,
        "pace_seconds": 0.3,
        "rate_basis": "MARKET_DATA_CHART 5 TPS (code)",
    },
    "kiwoom": {
        "rows_per_call": 900,
        "pace_seconds": 2.0,
        "rate_basis": "measured 2026-08-03 (mock)",
    },
    "kis": {
        "rows_per_call": 120,
        "pace_seconds": 0.5,
        "rate_basis": "orch-mock spec estimate",
    },
}


def assign(ranks: list[str]) -> dict[str, str]:
    counts = dict.fromkeys(TARGET_COUNTS, 0)
    out: dict[str, str] = {}
    for ticker in ranks:
        best = max(
            TARGET_COUNTS,
            # Sainte-Lague divisor; ties broken by source name for determinism.
            key=lambda s: (TARGET_COUNTS[s] / (2 * counts[s] + 1), s),
        )
        out[ticker] = best
        counts[best] += 1
    if counts != TARGET_COUNTS:
        raise AssertionError(f"apportionment mismatch: {counts} != {TARGET_COUNTS}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--top500", required=True, type=Path)
    ap.add_argument("--out-dir", required=True, type=Path)
    args = ap.parse_args()

    rows = list(csv.DictReader(args.top500.open()))
    rows.sort(key=lambda r: int(r["rank"]))
    mapping = assign([r["ticker"] for r in rows])

    out_csv = args.out_dir / "split_assignment.csv"
    with out_csv.open("w", newline="") as fh:
        w = csv.DictWriter(fh, fieldnames=["rank", "ticker", "market", "source"])
        w.writeheader()
        for r in rows:
            w.writerow(
                {
                    "rank": r["rank"],
                    "ticker": r["ticker"],
                    "market": r["market"],
                    "source": mapping[r["ticker"]],
                }
            )

    counts: dict[str, int] = {}
    tier_mix: dict[str, dict[str, int]] = {}
    for r in rows:
        s = mapping[r["ticker"]]
        counts[s] = counts.get(s, 0) + 1
        tier = f"rank_{((int(r['rank']) - 1) // 100) * 100 + 1}-{((int(r['rank']) - 1) // 100 + 1) * 100}"
        tier_mix.setdefault(s, {}).setdefault(tier, 0)
        tier_mix[s][tier] += 1

    manifest = {
        "job": "kr-backfill-phase1",
        "scope": "1Y",
        "scope_decision": {
            "chosen": "1-year (2025-08-04..2026-08-03 KST)",
            "decided_by": "orch-mock 2026-08-03 ~17:48 KST",
            "alternative_proposed_by_worker": "90-day scope aligned to the live retention policy",
            "alternative_rejected": True,
            "rejection_reason_verbatim": (
                "운영자 기승인(1년×상위500)의 하향이고, 3사 분담으로 1년이 ~10h 라 축소 실익이 없다."
            ),
            "worker_note_on_rejection": (
                "The recorded rejection reason addresses wall-clock cost. The worker's 90-day "
                "proposal was not motivated by wall-clock but by an active TimescaleDB retention "
                "policy on kr_candles_1m (job 1001, drop_after=90 days, scheduled=true), under "
                "which roughly 75% of a 1-year backfill is dropped within ~24h of insertion. "
                "Recorded here per instruction; scope decision belongs to orch-mock."
            ),
        },
        "target_counts": TARGET_COUNTS,
        "assigned_counts": counts,
        "source_spec": SOURCE_SPEC,
        "assignment_rule": (
            "sequential Sainte-Lague apportionment over rank-ordered top500; "
            "ties broken by source name; interleaves liquidity tiers by construction"
        ),
        "liquidity_tier_mix": tier_mix,
        "provenance_warning": (
            "kr_candles_1m has no provider source column and adding one is forbidden. "
            "This file is the ONLY record of which broker served which symbol. "
            "Split is per-symbol, so each symbol's series is internally single-source; "
            "cross-symbol comparisons may mix providers."
        ),
        "split_assignment_csv": str(out_csv),
        "split_assignment_sha256": hashlib.sha256(out_csv.read_bytes()).hexdigest(),
        "top500_sha256": hashlib.sha256(args.top500.read_bytes()).hexdigest(),
        "assignment_script_sha256": hashlib.sha256(
            Path(__file__).resolve().read_bytes()
        ).hexdigest(),
    }
    mpath = args.out_dir / "split_manifest.json"
    mpath.write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    print(
        json.dumps(
            {
                k: manifest[k]
                for k in (
                    "assigned_counts",
                    "liquidity_tier_mix",
                    "split_assignment_sha256",
                )
            },
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
