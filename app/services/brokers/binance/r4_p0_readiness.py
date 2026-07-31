"""Read-only fixed-window readiness audit for the R4 P0 rehearsal."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import urllib.parse
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from app.services.brokers.binance.r4_p0_hardening import (
    FINAL_COMPLETE,
    SOURCE_CADENCE_SECONDS,
    ArtifactManifestMismatch,
    StudyManifest,
    assert_connection_manifest_compatible,
    canonical_json,
    iso_utc,
    parse_utc,
    sha256_text,
)

READINESS_DAYS = 7
READINESS_EPOCHS = READINESS_DAYS * 24 // 4
READINESS_SYMBOL_EPOCHS = READINESS_EPOCHS * 3
READINESS_SOURCE_CELLS = READINESS_SYMBOL_EPOCHS * 10
_PIK_SOURCE = "binance_usdm.premiumIndexKline1m"
_ALERT_TYPES_THAT_FAIL_READINESS = frozenset(
    {
        "COLLECTOR_PROVENANCE_MISMATCH",
        "COLLECTOR_REDUNDANCY_LOST",
        "DATA_INTEGRITY_FAIL",
        "FINALIZER_STALLED",
    }
)


def readiness_epochs(manifest: StudyManifest) -> tuple[dt.datetime, ...]:
    """Return the fixed 42 decision epochs beginning at the pinned T0."""

    return tuple(
        manifest.t0 + dt.timedelta(hours=4 * index) for index in range(READINESS_EPOCHS)
    )


def _read_only_uri(path: Path) -> str:
    return f"file:{urllib.parse.quote(str(path.expanduser().resolve()))}?mode=ro"


def _load_json(value: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _add_issue(issues: list[str], issue: str) -> None:
    if issue not in issues:
        issues.append(issue)


def _validate_evidence(
    row: sqlite3.Row,
    *,
    manifest: StudyManifest,
    issues: list[str],
) -> int:
    """Validate one immutable finalization and return its valid source-cell count."""

    symbol = str(row["symbol"])
    epoch = str(row["decision_epoch_utc"])
    prefix = f"{symbol}@{epoch}"
    if row["final_status"] != FINAL_COMPLETE:
        _add_issue(
            issues,
            f"final_status_not_complete:{prefix}:{row['final_status']}",
        )
    for field in (
        "missing_sources_json",
        "conflict_sources_json",
        "invalid_sources_json",
    ):
        value = _load_json(row[field])
        if value:
            _add_issue(issues, f"finalization_{field}:{prefix}")

    evaluation = _load_json(row["evaluation_json"])
    if not isinstance(evaluation, dict):
        _add_issue(issues, f"evaluation_json_invalid:{prefix}")
        return 0
    if sha256_text(canonical_json(evaluation)) != row["evaluation_hash"]:
        _add_issue(issues, f"evaluation_hash_mismatch:{prefix}")
    expected_identity = {
        "decision_epoch_utc": epoch,
        "final_status": row["final_status"],
        "policy_hash": manifest.contract_hash,
        "source_manifest_hash": manifest.source_manifest_hash,
        "study_id": manifest.study_id,
        "symbol": symbol,
    }
    for key, expected in expected_identity.items():
        if evaluation.get(key) != expected:
            _add_issue(issues, f"evaluation_identity_mismatch:{prefix}:{key}")

    evidence = evaluation.get("source_evidence")
    if not isinstance(evidence, dict):
        _add_issue(issues, f"source_evidence_missing:{prefix}")
        return 0
    required_sources = set(manifest.required_sources)
    if set(evidence) != required_sources:
        _add_issue(issues, f"source_evidence_source_set_mismatch:{prefix}")

    valid_cells = 0
    for source in manifest.required_sources:
        cell = evidence.get(source)
        if not isinstance(cell, dict):
            _add_issue(issues, f"source_cell_missing:{prefix}:{source}")
            continue
        if cell.get("state") != "COMPLETE":
            _add_issue(issues, f"source_cell_not_complete:{prefix}:{source}")
            continue
        if any(
            cell.get(key)
            for key in (
                "conflicting_source_identities",
                "invalid_source_identity_payload_hashes",
                "missing_observation_slots",
                "uncovered_gap_identity_payload_hashes",
            )
        ):
            _add_issue(issues, f"source_cell_integrity_flags:{prefix}:{source}")
            continue
        expected_count = cell.get("expected_observation_count")
        covered_count = cell.get("covered_observation_count")
        if source in SOURCE_CADENCE_SECONDS:
            contract_count = 240 if source == _PIK_SOURCE else 48
            if expected_count != contract_count or covered_count != contract_count:
                _add_issue(issues, f"source_slot_count_mismatch:{prefix}:{source}")
                continue
        elif not cell.get("valid_source_identity_payload_hashes"):
            _add_issue(issues, f"source_cell_empty:{prefix}:{source}")
            continue
        valid_cells += 1
    return valid_cells


def _audit_replica(
    path: Path,
    *,
    manifest: StudyManifest,
    epochs: tuple[dt.datetime, ...],
    expected_code_hash: str | None,
) -> tuple[dict[str, Any], list[str], set[str], set[str]]:
    issues: list[str] = []
    report: dict[str, Any] = {
        "artifact": str(path),
        "status": "UNAVAILABLE",
        "collector_instance_ids": [],
        "process_runs": 0,
        "heartbeat_rows": 0,
        "finalization_rows": 0,
        "source_cells": 0,
        "late_only_rows": 0,
        "missing_finalizations": [],
        "conflict_finalizations": [],
        "unhealthy_alerts": [],
    }
    observed_code_hashes: set[str] = set()
    observed_versions: set[str] = set()

    if not path.is_file():
        issues.append(f"artifact_unavailable:{path}")
        return report, issues, observed_code_hashes, observed_versions

    start = manifest.t0
    end = manifest.t0 + dt.timedelta(days=READINESS_DAYS)
    try:
        with sqlite3.connect(_read_only_uri(path), uri=True) as connection:
            connection.row_factory = sqlite3.Row
            assert_connection_manifest_compatible(connection, manifest)
            finalization_rows = list(
                connection.execute(
                    """
                    SELECT symbol, decision_epoch_utc, final_status,
                           missing_sources_json, conflict_sources_json,
                           invalid_sources_json, evaluation_hash, evaluation_json,
                           source_manifest_hash
                    FROM symbol_epoch_finalizations
                    WHERE study_id = ? AND policy_hash = ?
                      AND source_manifest_hash = ?
                      AND decision_epoch_utc >= ? AND decision_epoch_utc < ?
                    ORDER BY decision_epoch_utc, symbol
                    """,
                    (
                        manifest.study_id,
                        manifest.contract_hash,
                        manifest.source_manifest_hash,
                        iso_utc(start),
                        iso_utc(end),
                    ),
                )
            )
            expected_keys = {
                (iso_utc(epoch), symbol)
                for epoch in epochs
                for symbol in manifest.symbols
            }
            observed_keys = {
                (row["decision_epoch_utc"], row["symbol"]) for row in finalization_rows
            }
            missing_keys = sorted(expected_keys - observed_keys)
            report["missing_finalizations"] = [
                {"decision_epoch_utc": epoch, "symbol": symbol}
                for epoch, symbol in missing_keys
            ]
            for epoch, symbol in missing_keys:
                _add_issue(issues, f"finalization_missing:{symbol}@{epoch}")
            for row in finalization_rows:
                if row["final_status"] == "FINAL_MISSING":
                    report["missing_finalizations"].append(
                        {
                            "decision_epoch_utc": row["decision_epoch_utc"],
                            "symbol": row["symbol"],
                        }
                    )
                if row["final_status"] == "FINAL_CONFLICT":
                    report["conflict_finalizations"].append(
                        {
                            "decision_epoch_utc": row["decision_epoch_utc"],
                            "symbol": row["symbol"],
                        }
                    )
                report["source_cells"] += _validate_evidence(
                    row,
                    manifest=manifest,
                    issues=issues,
                )
            report["finalization_rows"] = len(finalization_rows)

            extra_before = connection.execute(
                """
                SELECT COUNT(*) AS count
                FROM symbol_epoch_finalizations
                WHERE study_id = ? AND policy_hash = ?
                  AND decision_epoch_utc < ?
                """,
                (manifest.study_id, manifest.contract_hash, iso_utc(start)),
            ).fetchone()["count"]
            if extra_before:
                _add_issue(issues, "past_identity_finalizations_present")
            report["past_identity_finalizations"] = extra_before

            late_rows = list(
                connection.execute(
                    """
                    SELECT correction_status
                    FROM late_only_corrections
                    WHERE study_id = ? AND policy_hash = ?
                      AND decision_epoch_utc >= ? AND decision_epoch_utc < ?
                    """,
                    (
                        manifest.study_id,
                        manifest.contract_hash,
                        iso_utc(start),
                        iso_utc(end),
                    ),
                )
            )
            report["late_only_rows"] = len(late_rows)
            if any(row["correction_status"] != "LATE_ONLY" for row in late_rows):
                _add_issue(issues, "late_correction_promoted")

            process_rows = list(
                connection.execute(
                    """
                    SELECT collector_instance_id, run_id, started_at, code_hash,
                           collector_version, t0_utc, study_manifest_sha256
                    FROM collector_process_versions
                    WHERE study_id = ? AND policy_hash = ?
                    ORDER BY append_id
                    """,
                    (manifest.study_id, manifest.contract_hash),
                )
            )
            report["process_runs"] = len(process_rows)
            if not process_rows:
                _add_issue(issues, "process_version_stamp_missing")
            process_run_ids = {row["run_id"] for row in process_rows}
            process_instance_ids = {
                row["collector_instance_id"] for row in process_rows
            }
            report["process_instance_ids"] = sorted(process_instance_ids)
            for row in process_rows:
                observed_code_hash = row["code_hash"]
                if observed_code_hash:
                    observed_code_hashes.add(observed_code_hash)
                if row["collector_version"]:
                    observed_versions.add(row["collector_version"])
                if row["t0_utc"] != iso_utc(manifest.t0):
                    _add_issue(issues, "process_t0_drift")
                if row["study_manifest_sha256"] != manifest.content_sha256:
                    _add_issue(issues, "process_manifest_hash_drift")
                if (
                    expected_code_hash is not None
                    and row["code_hash"] != expected_code_hash
                ):
                    _add_issue(issues, "process_code_hash_drift")

            heartbeat_rows = list(
                connection.execute(
                    """
                    SELECT collector_instance_id, run_id, observed_at, health_json
                    FROM collector_heartbeats
                    WHERE study_id = ? AND policy_hash = ?
                      AND observed_at >= ? AND observed_at < ?
                    ORDER BY observed_at
                    """,
                    (
                        manifest.study_id,
                        manifest.contract_hash,
                        iso_utc(start),
                        iso_utc(end),
                    ),
                )
            )
            report["heartbeat_rows"] = len(heartbeat_rows)
            if not heartbeat_rows:
                _add_issue(issues, "heartbeat_evidence_missing")
            heartbeat_instance_ids = {
                row["collector_instance_id"] for row in heartbeat_rows
            }
            report["collector_instance_ids"] = sorted(heartbeat_instance_ids)
            for row in heartbeat_rows:
                if row["run_id"] not in process_run_ids:
                    _add_issue(issues, "heartbeat_without_process_stamp")
                health = _load_json(row["health_json"])
                if not isinstance(health, dict) or health.get("ok") is not True:
                    _add_issue(issues, "unhealthy_heartbeat")
                if isinstance(health, dict):
                    health_code_hash = health.get("code_hash")
                    health_version = health.get("collector_version")
                    health_manifest = health.get("study_manifest_sha256")
                    health_t0 = health.get("t0")
                    if health_code_hash:
                        observed_code_hashes.add(str(health_code_hash))
                    if health_version:
                        observed_versions.add(str(health_version))
                    if (
                        expected_code_hash is not None
                        and health_code_hash != expected_code_hash
                    ):
                        _add_issue(issues, "heartbeat_code_hash_drift")
                    if health_manifest != manifest.content_sha256:
                        _add_issue(issues, "heartbeat_manifest_hash_drift")
                    if health_t0 != iso_utc(manifest.t0):
                        _add_issue(issues, "heartbeat_t0_drift")
                try:
                    observed_at = parse_utc(row["observed_at"])
                except (TypeError, ValueError):
                    _add_issue(issues, "heartbeat_timestamp_invalid")
                    continue
                if not start <= observed_at < end:
                    _add_issue(issues, "heartbeat_outside_target_window")

            missing_heartbeat_epochs = []
            heartbeat_times: list[dt.datetime] = []
            for heartbeat in heartbeat_rows:
                try:
                    heartbeat_times.append(parse_utc(heartbeat["observed_at"]))
                except (TypeError, ValueError):
                    continue
            for epoch in epochs:
                if not any(
                    epoch <= observed_at < epoch + dt.timedelta(hours=4)
                    for observed_at in heartbeat_times
                ):
                    missing_heartbeat_epochs.append(iso_utc(epoch))
            report["missing_heartbeat_epochs"] = missing_heartbeat_epochs
            if missing_heartbeat_epochs:
                _add_issue(issues, "heartbeat_continuity_gap")

            alert_rows = connection.execute(
                """
                SELECT payload_json
                FROM collector_alert_events
                WHERE alert_key LIKE ?
                """,
                (f"%{manifest.study_id}:{manifest.contract_hash}%",),
            )
            for alert_row in alert_rows:
                payload = _load_json(alert_row["payload_json"])
                if (
                    isinstance(payload, dict)
                    and payload.get("alert_type") in _ALERT_TYPES_THAT_FAIL_READINESS
                ):
                    alert_type = str(payload["alert_type"])
                    report["unhealthy_alerts"].append(alert_type)
                    _add_issue(issues, f"unhealthy_alert:{alert_type}")
            if set(report["process_instance_ids"]) != heartbeat_instance_ids:
                _add_issue(issues, "topology_process_heartbeat_mismatch")
    except (ArtifactManifestMismatch, sqlite3.Error) as exc:
        report["status"] = "READ_ERROR"
        report["error_type"] = type(exc).__name__
        _add_issue(issues, f"artifact_read_error:{type(exc).__name__}")
        return report, issues, observed_code_hashes, observed_versions

    if report["finalization_rows"] != READINESS_SYMBOL_EPOCHS:
        _add_issue(issues, "symbol_epoch_count_mismatch")
    if report["source_cells"] != READINESS_SOURCE_CELLS:
        _add_issue(issues, "source_cell_count_mismatch")
    report["status"] = "OK" if not issues else "FAIL"
    return report, issues, observed_code_hashes, observed_versions


def audit_readiness(
    artifact_paths: Sequence[Path],
    manifest: StudyManifest,
    *,
    expected_code_hash: str | None = None,
) -> dict[str, Any]:
    """Audit both artifacts without opening either one for writing."""

    paths = tuple(
        sorted({path.expanduser().resolve() for path in artifact_paths}, key=str)
    )
    epochs = readiness_epochs(manifest)
    issues: list[str] = []
    if len(paths) != 2:
        _add_issue(issues, "topology_requires_exactly_two_artifacts")

    replicas: list[dict[str, Any]] = []
    all_code_hashes: set[str] = set()
    all_versions: set[str] = set()
    for path in paths:
        report, replica_issues, code_hashes, versions = _audit_replica(
            path,
            manifest=manifest,
            epochs=epochs,
            expected_code_hash=expected_code_hash,
        )
        replicas.append(report)
        for issue in replica_issues:
            _add_issue(issues, f"{path.name}:{issue}")
        all_code_hashes.update(code_hashes)
        all_versions.update(versions)

    all_instances = [
        instance_id
        for report in replicas
        for instance_id in report.get("collector_instance_ids", [])
    ]
    if len(all_instances) != len(set(all_instances)) or len(set(all_instances)) != 2:
        _add_issue(issues, "topology_collector_instance_drift")
    if len(all_code_hashes) != 1:
        _add_issue(issues, "code_hash_drift")
    if expected_code_hash is not None and all_code_hashes != {expected_code_hash}:
        _add_issue(issues, "code_hash_does_not_match_expected")
    if len(all_versions) != 1:
        _add_issue(issues, "collector_version_drift")

    missing_count = sum(len(report["missing_finalizations"]) for report in replicas)
    conflict_count = sum(len(report["conflict_finalizations"]) for report in replicas)
    late_only_count = sum(report["late_only_rows"] for report in replicas)
    per_replica_cells = [report["source_cells"] for report in replicas]
    counts = {
        "days_expected": READINESS_DAYS,
        "epochs_expected": READINESS_EPOCHS,
        "epochs_observed": len(epochs)
        if all(
            report.get("finalization_rows") == READINESS_SYMBOL_EPOCHS
            for report in replicas
        )
        else 0,
        "symbol_epochs_expected": READINESS_SYMBOL_EPOCHS,
        "symbol_epochs_observed_per_replica": [
            report.get("finalization_rows", 0) for report in replicas
        ],
        "source_cells_expected": READINESS_SOURCE_CELLS,
        "source_cells_observed_per_replica": per_replica_cells,
        "missing_finalizations": missing_count,
        "conflict_finalizations": conflict_count,
        "late_only_rows": late_only_count,
    }
    gates = {
        "exact_two_replicas": len(paths) == 2,
        "both_replicas_complete": bool(replicas)
        and all(report.get("status") == "OK" for report in replicas),
        "final_complete_only": missing_count == 0 and conflict_count == 0,
        "source_cells_1260_per_replica": bool(replicas)
        and all(value == READINESS_SOURCE_CELLS for value in per_replica_cells),
        "no_late_promotion": all(
            "late_correction_promoted" not in issue for issue in issues
        ),
        "no_process_version_hash_or_topology_drift": not any(
            token in issue
            for issue in issues
            for token in (
                "code_hash",
                "collector_version",
                "process_",
                "heartbeat_",
                "topology_",
            )
        ),
    }
    return {
        "ok": not issues,
        "summary": (
            "PASS: fixed seven-day R4 P0 readiness contract is satisfied"
            if not issues
            else "FAIL: fixed seven-day R4 P0 readiness contract is not satisfied"
        ),
        "manifest": {
            "content_sha256": manifest.content_sha256,
            "policy_hash": manifest.contract_hash,
            "source_manifest_hash": manifest.source_manifest_hash,
            "study_id": manifest.study_id,
            "t0": iso_utc(manifest.t0),
        },
        "window": {
            "start_decision_epoch": iso_utc(epochs[0]),
            "end_decision_epoch_exclusive": iso_utc(epochs[-1] + dt.timedelta(hours=4)),
            "final_deadline": iso_utc(epochs[-1] + dt.timedelta(days=1)),
        },
        "counts": counts,
        "gates": gates,
        "replicas": replicas,
        "issues": sorted(issues),
    }
