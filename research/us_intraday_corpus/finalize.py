"""Manifest + checksum sealing.

This module is where `us-corpus-v1` failed all three §0 checks, so each fix is
called out at its implementation site:

* pitfall 1 -- the post-hoc walk **skips** `holdout/` entirely. Holdout digests
  come from the write-time registry, so they appear in `checksums.sha256`
  without any file ever being re-opened. `written_not_read` is then *derived*
  from the access log rather than typed in by hand.
* pitfall 2 -- every shipped artifact is verified to carry the survivorship
  label before the manifest is written; an unlabelled file aborts the seal.
* pitfall 3 -- `checksums.sha256` covers `reports/*` and `inputs/*`, not just
  parquet, and the manifest records the exact `git rev-parse HEAD` used to
  generate the artifacts so the seal can be checked against the shipped commit.
"""

from __future__ import annotations

import datetime as _dt
import json
import subprocess
from pathlib import Path
from typing import Any

from . import access_log, alpaca_data, config, hashing, labels

# Write-time digest registry: path -> sha256, populated by build.py as it writes.
# Holdout entries can only ever come from here.
WRITE_TIME_DIGESTS: dict[str, str] = {}

REGISTRY_PATH = config.STAGING_DIR / "write_time_digests.json"


def register_digest(path: Path, digest: str) -> None:
    WRITE_TIME_DIGESTS[str(path)] = digest


def save_registry() -> None:
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(json.dumps(WRITE_TIME_DIGESTS, indent=2), encoding="utf-8")


def load_registry() -> dict[str, str]:
    if REGISTRY_PATH.exists():
        WRITE_TIME_DIGESTS.update(json.loads(REGISTRY_PATH.read_text(encoding="utf-8")))
    return WRITE_TIME_DIGESTS


def git_head_sha(repo: Path | None = None) -> str:
    """Exact commit the artifacts were generated from (§0 pitfall 3)."""
    repo = repo or Path(__file__).resolve().parents[2]
    return subprocess.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _shippable_files() -> list[Path]:
    """All shipped artifacts EXCEPT holdout and staging.

    §0 pitfall 1: the holdout is deliberately absent from this walk. Its digests
    are supplied from the write-time registry in `build_checksums()`.
    §0 pitfall 3: this is not restricted to *.parquet -- reports and inputs are
    included, which is exactly what let a stale CSV escape the sister corpus.
    """
    out: list[Path] = []
    for path in sorted(config.ARTIFACT_ROOT.rglob("*")):
        if not path.is_file():
            continue
        parts = path.parts
        if "_staging" in parts:
            continue
        if access_log.is_holdout_path(path):
            continue  # <-- pitfall 1: never re-read the seal
        if path.name in {"checksums.sha256", "manifest.json"}:
            continue
        out.append(path)
    return out


def build_checksums() -> tuple[str, dict[str, int]]:
    """Return the checksums file body plus a small coverage summary."""
    load_registry()
    lines: list[str] = []
    stats = {"read_hashed": 0, "write_time_hashed": 0}

    for path in _shippable_files():
        digest = hashing.sha256_of_file(path)
        stats["read_hashed"] += 1
        lines.append(f"{digest}  {path.relative_to(config.ARTIFACT_ROOT)}")

    # Holdout: digest known from write time, file never re-opened.
    for raw_path, digest in sorted(WRITE_TIME_DIGESTS.items()):
        path = Path(raw_path)
        if access_log.is_holdout_path(path):
            stats["write_time_hashed"] += 1
            rel = path.relative_to(config.ARTIFACT_ROOT)
            lines.append(f"{digest}  {rel}  # hashed-at-write, not re-read")

    return "\n".join(lines) + "\n", stats


def verify_all_labelled() -> list[str]:
    """Return artifacts missing the survivorship label (§0 pitfall 2)."""
    unlabelled: list[str] = []
    for path in _shippable_files():
        if path.suffix.lower() in {".parquet", ".csv", ".json"}:
            if not labels.artifact_carries_label(path):
                unlabelled.append(str(path))
    return unlabelled


def seal(
    *,
    terminal_verdict: str,
    body: dict[str, Any],
    allow_unlabelled: bool = False,
) -> dict[str, Any]:
    """Write `checksums.sha256` and `manifest.json`, refusing on a broken invariant."""
    if terminal_verdict not in config.TERMINAL_VERDICTS:
        raise ValueError(f"{terminal_verdict!r} not in {config.TERMINAL_VERDICTS}")

    unlabelled = verify_all_labelled()
    if unlabelled and not allow_unlabelled:
        raise AssertionError(
            f"refusing to seal: artifacts without the survivorship label: {unlabelled}"
        )

    checksums, stats = build_checksums()
    config.CHECKSUMS_PATH.write_text(checksums, encoding="utf-8")

    written_not_read = access_log.verify_written_not_read()

    manifest = {
        "SURVIVORSHIP_BIASED": labels.SURVIVORSHIP_BIASED,
        "survivorship_note": labels.SURVIVORSHIP_NOTE,
        "corpus_id": config.CORPUS_ID,
        "purpose": config.PURPOSE,
        "generated_at": _dt.datetime.now(_dt.UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "generated_from_commit": git_head_sha(),  # §0 pitfall 3
        "terminal_verdict": terminal_verdict,
        "source_product": config.SOURCE_PRODUCT,
        "source_fallback_used": False,
        "data_host": config.DATA_HOST,
        "alpaca_hosts_contacted": sorted(alpaca_data.CONTACTED_HOSTS),
        "operating_db_reads": 0,
        "operating_db_writes": config.OPERATING_DB_WRITES,
        "broker_or_account_calls": config.BROKER_OR_ACCOUNT_CALLS,
        "timestamp_policy": {
            "storage": config.TIMESTAMP_STORAGE_TZ,
            "session_date_derived_from": config.SESSION_DATE_TZ,
            "kst_anchor_used": False,
            "note": (
                "ROB-1206: bars are stored with their UTC instant verbatim; "
                "session_date is derived in America/New_York. A KST anchor "
                "would shift every row by one day."
            ),
        },
        "scope": {
            "decision": config.SCOPE_DECISION,
            "hour_collection_abandoned": config.HOUR_COLLECTION_ABANDONED,
            "hour_derivable_from_1m": config.HOUR_DERIVABLE_FROM_1M,
        },
        "data_gaps": [config.HOUR_DATA_GAP],
        "partial_not_shipped": {
            "what": "1Hour partial collection, stopped when the budget projection proved wrong",
            "rows": 2_384_816,
            "symbols": 2_500,
            "of_universe": config.UNIVERSE_COUNT,
            "session_years": [2016],
            "approx_coverage_pct_of_intended_1h": 2,
            "location": str(config.STAGING_DIR / "aborted_1h_partial_20260803"),
            "status": "PARTIAL_NOT_SHIPPED",
            "promoted_to_dataset": False,
            "deleted": False,
            "note": (
                "Preserved deliberately, outside dataset/, so a future 1Hour "
                "resume can judge whether to reuse it. It is NOT part of this "
                "corpus and no statistic may be computed from it."
            ),
        },
        "window": {
            "start_date": str(config.START_DATE),
            "cutoff_session": str(config.CUTOFF_DATE),
            "train": [str(config.TRAIN[0]), str(config.TRAIN[1])],
            "validation": [str(config.VALIDATION[0]), str(config.VALIDATION[1])],
            "holdout": [str(config.HOLDOUT[0]), str(config.HOLDOUT[1])],
            "forward_oos_start": str(config.FORWARD_OOS_START),
        },
        "inputs": {
            "universe_file": str(config.UNIVERSE_FILE),
            "universe_file_sha256": config.UNIVERSE_FILE_SHA256,
            "universe_count": config.UNIVERSE_COUNT,
        },
        "holdout": {
            "dir": str(config.HOLDOUT_DIR),
            "written_not_read": written_not_read,  # derived, never asserted
            "written_not_read_derivation": (
                "computed from holdout-access.log: True iff zero READ records "
                "exist for any holdout path. The log records READ and WRITE."
            ),
            "hashed_at_write_not_reread": True,
            "excluded_from_posthoc_walk": True,
            "access_log": str(config.ACCESS_LOG_PATH),
        },
        "integrity": {
            "checksums_file": str(config.CHECKSUMS_PATH),
            "checksums_include_reports": True,
            "files_hashed_by_read": stats["read_hashed"],
            "files_hashed_at_write": stats["write_time_hashed"],
            "unlabelled_artifacts": unlabelled,
        },
        "budget": {
            "max_requests": config.MAX_REQUESTS,
            "min_request_interval_sec": config.MIN_REQUEST_INTERVAL_SEC,
            "max_wall_clock_hours": config.MAX_WALL_CLOCK_HOURS,
            "max_artifact_gib": config.MAX_ARTIFACT_GIB,
        },
        **body,
    }

    config.MANIFEST_PATH.write_text(
        json.dumps(manifest, indent=2) + "\n", encoding="utf-8"
    )
    return manifest
