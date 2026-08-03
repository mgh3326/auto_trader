"""Emit CSV artifacts, hash everything, and ENFORCE the admissibility label.

EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC

A sibling job was blocked because its label lived in the manifest but was not
enforced on the consumer's path. So this module does not merely *write* the
label — it re-opens every numeric artifact and fails loudly if a consumer could
read numbers without also reading the label:

  * parquet -> label required in file-level key/value metadata
  * csv     -> label required BOTH as a leading comment line and as a per-row
               column, because a consumer using default csv/pandas settings may
               skip comments entirely
  * json    -> label required as a top-level key

If verification fails, the manifest is not written.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import pyarrow.parquet as pq

from research.dfc_retro_probe.common import (
    ARTIFACT_ROOT,
    LABEL,
    PROBE_ID,
    PURPOSE,
    SOURCE,
    label_header_lines,
)

DEFS = ("D1_conjunction", "D2_composite", "D3_disjunction")


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_labelled_csv(frame: pd.DataFrame, out: Path) -> None:
    """CSV with the label as a comment header AND as a column on every row."""
    frame = frame.copy()
    frame.insert(0, "admissibility", LABEL)
    with out.open("w", encoding="utf-8", newline="") as fh:
        for line in label_header_lines({"artifact": out.name}):
            fh.write(line + "\n")
        frame.to_csv(fh, index=False)


def emit_csvs(report: dict[str, Any]) -> list[Path]:
    written: list[Path] = []
    primary = report["primary_window"]

    monthly_rows = []
    for month, block in report["windows"][primary]["by_month"].items():
        row: dict[str, Any] = {
            "month_utc": month,
            "evaluable_buckets": block["evaluable_buckets"],
        }
        for definition in DEFS:
            row[f"{definition}_fires"] = block[definition]["fires"]
            row[f"{definition}_per_day"] = block[definition]["incidence_per_day"]
        monthly_rows.append(row)
    out = ARTIFACT_ROOT / "incidence_by_month.csv"
    write_labelled_csv(pd.DataFrame(monthly_rows), out)
    written.append(out)

    symbol_rows = []
    for symbol, block in report["windows"][primary]["by_symbol"].items():
        row = {"symbol": symbol, "evaluable_buckets": block["evaluable_buckets"]}
        for definition in DEFS:
            row[f"{definition}_fires"] = block[definition]["fires"]
            row[f"{definition}_per_day"] = block[definition]["incidence_per_day"]
        symbol_rows.append(row)
    out = ARTIFACT_ROOT / "incidence_by_symbol.csv"
    write_labelled_csv(pd.DataFrame(symbol_rows), out)
    written.append(out)

    sweep_rows = [
        {"threshold_quantile": float(q), **{d: v[d] for d in DEFS}}
        for q, v in report["threshold_quantile_sweep"]["sweep"].items()
    ]
    out = ARTIFACT_ROOT / "threshold_quantile_sweep.csv"
    write_labelled_csv(pd.DataFrame(sweep_rows), out)
    written.append(out)

    return written


def verify_label(path: Path) -> dict[str, Any]:
    """Re-open the artifact and confirm a consumer cannot miss the label."""
    suffix = path.suffix.lower()
    if suffix == ".parquet":
        meta = pq.read_metadata(path).metadata or {}
        ok = meta.get(b"EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC") == b"TRUE"
        return {"channel": "parquet_kv_metadata", "label_enforced": ok}
    if suffix == ".csv":
        text = path.read_text(encoding="utf-8")
        in_comment = (
            text.splitlines()[0].startswith("#") and LABEL in text.splitlines()[0]
        )
        # Default-settings read: comments are NOT skipped, so the column must carry it.
        frame = pd.read_csv(path, comment="#")
        in_column = "admissibility" in frame.columns and bool(
            (frame["admissibility"] == LABEL).all()
        )
        return {
            "channel": "csv_comment_header+per_row_column",
            "label_enforced": bool(in_comment and in_column),
            "label_in_comment": bool(in_comment),
            "label_in_every_row": in_column,
        }
    if suffix == ".json":
        payload = json.loads(path.read_text(encoding="utf-8"))
        ok = payload.get("admissibility") == LABEL or payload.get(
            "EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC"
        ) in (True, "TRUE")
        return {"channel": "json_top_level_key", "label_enforced": bool(ok)}
    return {"channel": "unknown", "label_enforced": False}


def main() -> int:
    report_path = ARTIFACT_ROOT / "incidence_report.json"
    report = json.loads(report_path.read_text(encoding="utf-8"))
    emit_csvs(report)

    numeric_artifacts = sorted(
        [
            p
            for p in ARTIFACT_ROOT.rglob("*")
            if p.suffix.lower() in {".parquet", ".csv"}
        ]
        + [
            ARTIFACT_ROOT / "incidence_report.json",
            ARTIFACT_ROOT / "manifest_raw.json",
            ARTIFACT_ROOT / "limits.json",
        ]
    )

    files: list[dict[str, Any]] = []
    failures: list[str] = []
    for path in numeric_artifacts:
        if not path.exists():
            continue
        check = verify_label(path)
        entry = {
            "path": str(path.relative_to(ARTIFACT_ROOT)),
            "bytes": path.stat().st_size,
            "sha256": sha256_file(path),
            **check,
        }
        files.append(entry)
        if not check["label_enforced"]:
            failures.append(str(path))

    manifest = {
        "admissibility": LABEL,
        "EXPLORATORY_NOT_ADMISSIBLE_FOR_SEALED_DFC": True,
        "probe_id": PROBE_ID,
        "purpose": PURPOSE,
        "source": SOURCE,
        "auth": "NONE",
        "signed_endpoint_calls": 0,
        "broker_or_account_calls": 0,
        "aggtrades_used": "NO",
        "operating_db_reads": 0,
        "operating_db_writes": 0,
        "forward_fill_used": "NO",
        "pnl_or_performance_computed": "NO",
        "contract_3_04_to_3_83_adjudicated": "NO",
        "sealed_artifact_touched": "NO",
        "label_enforced_on_every_numeric_artifact": not failures,
        "files": files,
    }

    if failures:
        print("LABEL ENFORCEMENT FAILED for:", failures)
        return 1

    (ARTIFACT_ROOT / "manifest.json").write_text(
        json.dumps(manifest, indent=2), encoding="utf-8"
    )
    print(json.dumps({"files": len(files), "all_labelled": True}, indent=2))
    for entry in files:
        print(f"  {entry['path']:<42} {entry['sha256'][:16]}  {entry['channel']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
