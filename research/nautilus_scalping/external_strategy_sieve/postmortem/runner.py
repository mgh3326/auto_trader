"""ROB-384 — postmortem runner: build evidence, classify, emit CSV + JSON.

Default action is a summary print (no writes). ``--emit`` writes the counts-only
CSV (committed artifact) and a regenerable JSON to the gitignored research
artifact root. No network, no broker / order / scheduler / secret access; pure
stdlib so it runs under ``uv run --no-project``.

Re-parsed sources are read from the artifact root (env
``AUTO_TRADER_RESEARCH_ARTIFACT_ROOT`` or repo ``results/``). A missing source is
skipped with a note (the CSV still emits with whatever is available plus the
documented ROB-342 row), so the run is honest about what was actually re-parsed
versus documented.
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

from artifact_paths import research_artifact_root

from external_strategy_sieve.postmortem import evidence, residual, taxonomy
from external_strategy_sieve.postmortem import gatereport_io as io

# (relative subpath under the artifact root, adapter, human label)
SOURCE_SPECS = [
    (
        "rob320/meanrev.json",
        io.from_meanrev,
        "ROB-320 meanrev (validated_signal_gate.v1)",
    ),
    (
        "discovery/rob383/phase3_validation.json",
        io.from_phase3,
        "ROB-383 phase3 (validated_signal_gate.v1)",
    ),
    (
        "rob382/rob382_falsification.v1.json",
        io.from_falsification,
        "ROB-382 falsification (rob382_falsification.v1)",
    ),
    (
        "rob353/rob351_campaign.v1.json",
        io.from_campaign,
        "ROB-353 campaign (rob351_campaign.v1)",
    ),
]

_CSV_COLUMNS = [
    "issue",
    "candidate",
    "family",
    "source",
    "schema",
    "gross_bps",
    "net_bps_0",
    "net_bps_2",
    "net_bps_4",
    "net_bps_7.5",
    "net_bps_10",
    "net_moot_reason",
    "trade_count",
    "oos_trade_count",
    "n_folds",
    "single_fold_edge",
    "t_stat_gross",
    "t_stat_oos",
    "verdict",
    "baseline_beat",
    "failure_modes",
    "citation",
]


def build_records(artifact_root: Path) -> tuple[list, list[str]]:
    """Return (annotated records, provenance notes). Missing sources are skipped."""
    records: list = []
    notes: list[str] = []
    for subpath, adapter, label in SOURCE_SPECS:
        path = artifact_root / subpath
        if path.exists():
            recs = adapter(str(path))
            records.extend(recs)
            notes.append(
                f"reparsed: {label} -> {len(recs)} candidate(s) from {subpath}"
            )
        else:
            notes.append(
                f"SKIPPED (missing): {label} expected at {subpath} — not re-parsed this run"
            )
    doc = evidence.documented_registry()
    records.extend(doc)
    notes.append(f"documented: {len(doc)} candidate(s) (ROB-342, citation-backed)")
    taxonomy.annotate(records)
    return records, notes


def _sanitize_citation(citation: str, artifact_root: Path) -> str:
    """Replace the local artifact-root prefix with a portable token (no home paths)."""
    return citation.replace(str(artifact_root), "<artifact_root>")


def emit_csv(records: list, path: Path, artifact_root: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=_CSV_COLUMNS)
        writer.writeheader()
        for ev in records:
            row = ev.to_row()
            row["citation"] = _sanitize_citation(row["citation"], artifact_root)
            writer.writerow(row)


def build_summary(records: list, artifact_root: Path, notes: list[str]) -> dict:
    decision = residual.closure_decision(records)
    extras: dict = {}
    fals = artifact_root / "rob382/rob382_falsification.v1.json"
    if fals.exists():
        extras["rob382_overall_verdict"] = io.falsification_overall_verdict(str(fals))
    camp = artifact_root / "rob353/rob351_campaign.v1.json"
    if camp.exists():
        ctrl = io.campaign_controls(str(camp))
        extras["rob353_btc_buy_hold_bps"] = ctrl.get("btc_buy_hold_bps")
    return {
        "schema_version": "rob384_postmortem.v1",
        "issue": "ROB-384",
        "provenance_notes": notes,
        "closure_decision": decision,
        "source_extras": extras,
        "candidates": [
            {
                **ev.to_row(),
                "citation": _sanitize_citation(ev.to_row()["citation"], artifact_root),
            }
            for ev in records
        ],
        "memory_only_uncited": evidence.ROB342_MEMORY_ONLY_UNCITED,
    }


def emit_json(summary: dict, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(summary, fh, indent=2)


def _print_summary(summary: dict, notes: list[str]) -> None:
    d = summary["closure_decision"]
    print("ROB-384 crypto strategy failure-mode postmortem")
    print("-" * 60)
    for n in notes:
        print("  " + n)
    print("-" * 60)
    print(f"candidates: {d['n_candidates']}  status: {d['status_distribution']}")
    print(f"CLOSURE DECISION: {d['verdict']} — {d['verdict_label']}")
    print(f"  rationale: {d['decision_rationale']}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="ROB-384 crypto strategy failure-mode postmortem"
    )
    parser.add_argument(
        "--emit",
        action="store_true",
        help="write CSV + JSON (default: summary print only)",
    )
    parser.add_argument(
        "--artifact-root",
        default=None,
        help="override artifact root (default: env or repo results/)",
    )
    parser.add_argument(
        "--out-csv",
        default=str(
            Path(__file__).resolve().parents[4]
            / "docs/runbooks/rob-384-crypto-strategy-postmortem.csv"
        ),
        help="committed counts-only CSV path",
    )
    args = parser.parse_args(argv)

    artifact_root = (
        Path(args.artifact_root) if args.artifact_root else research_artifact_root()
    )
    records, notes = build_records(artifact_root)
    summary = build_summary(records, artifact_root, notes)
    _print_summary(summary, notes)

    if args.emit:
        csv_path = Path(args.out_csv)
        json_path = artifact_root / "postmortem/rob384_postmortem.v1.json"
        emit_csv(records, csv_path, artifact_root)
        emit_json(summary, json_path)
        print("-" * 60)
        print(f"  CSV  (committed): {csv_path}")
        print(f"  JSON (gitignored): {json_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
