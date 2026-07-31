"""Read-only fixed-window readiness audit for the R4 P0 rehearsal."""

from __future__ import annotations

import datetime as dt
import json
import sqlite3
import urllib.parse
from collections import defaultdict
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from app.services.brokers.binance.r4_p0_hardening import (
    EPOCH_HOURS,
    FINAL_COMPLETE,
    FINALIZER_VERSION,
    SOURCE_CADENCE_SECONDS,
    ArtifactManifestMismatch,
    ArtifactSchemaMismatch,
    StudyManifest,
    _observation_slot,
    _payload_valid,
    _source_identity,
    assert_connection_manifest_compatible,
    assert_finalization_provenance_schema,
    canonical_json,
    decision_epoch_for_event,
    finalize_at,
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
    # immutable prevents SQLite's WAL reader from creating or checkpointing
    # sidecars. A stale main database can only make readiness fail closed.
    return (
        f"file:{urllib.parse.quote(str(path.expanduser().resolve()))}"
        "?mode=ro&immutable=1"
    )


def _load_json(value: Any) -> Any:
    try:
        return json.loads(value)
    except (TypeError, json.JSONDecodeError):
        return None


def _add_issue(issues: list[str], issue: str) -> None:
    if issue not in issues:
        issues.append(issue)


def _normalize_observed_at(observed_at: dt.datetime | None) -> dt.datetime:
    value = observed_at or dt.datetime.now(tz=dt.UTC)
    if value.tzinfo is None:
        raise ValueError("observed_at must be timezone-aware")
    return value.astimezone(dt.UTC)


def _validate_evidence(
    row: sqlite3.Row,
    *,
    manifest: StudyManifest,
    expected_deadline: dt.datetime,
    issues: list[str],
) -> None:
    """Validate the self-consistency of one immutable finalization witness.

    Counts and hashes in this JSON are treated as a consistency record only.
    Readiness source-cell acceptance is computed separately from ``pit_records``.
    """

    symbol = str(row["symbol"])
    epoch = str(row["decision_epoch_utc"])
    prefix = f"{symbol}@{epoch}"
    if row["source_manifest_hash"] != manifest.source_manifest_hash:
        _add_issue(issues, f"finalization_source_manifest_mismatch:{prefix}")
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
        return
    if sha256_text(canonical_json(evaluation)) != row["evaluation_hash"]:
        _add_issue(issues, f"evaluation_hash_mismatch:{prefix}")
    expected_identity = {
        "decision_epoch_utc": epoch,
        "final_status": row["final_status"],
        "policy_hash": manifest.contract_hash,
        "source_manifest_hash": manifest.source_manifest_hash,
        "study_id": manifest.study_id,
        "symbol": symbol,
        "finalize_at": iso_utc(expected_deadline),
        "finalizer_version": FINALIZER_VERSION,
    }
    for key, expected in expected_identity.items():
        if evaluation.get(key) != expected:
            _add_issue(issues, f"evaluation_identity_mismatch:{prefix}:{key}")

    evidence = evaluation.get("source_evidence")
    if not isinstance(evidence, dict):
        _add_issue(issues, f"source_evidence_missing:{prefix}")
        return
    required_sources = set(manifest.required_sources)
    if set(evidence) != required_sources:
        _add_issue(issues, f"source_evidence_source_set_mismatch:{prefix}")

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
        elif not cell.get("valid_source_identity_payload_hashes"):
            _add_issue(issues, f"source_cell_empty:{prefix}:{source}")


def _raw_rows_for_window(
    connection: sqlite3.Connection,
    *,
    start: dt.datetime,
    end: dt.datetime,
) -> list[dict[str, Any]]:
    rows = connection.execute(
        """
        SELECT record_id, partition_key, source, symbol, event_time,
               transaction_time, local_receive_time, request_started_at,
               request_completed_at, sequence_or_trade_id,
               raw_payload_sha256, collector_version, partition_sha256,
               gap_detected, reconnect_id, previous_partition_sha256,
               run_id, raw_payload
        FROM pit_records
        WHERE event_time >= ? AND event_time < ?
        ORDER BY append_id
        """,
        (
            iso_utc(start - dt.timedelta(hours=EPOCH_HOURS)),
            iso_utc(end),
        ),
    )
    return [dict(row) for row in rows]


def _validate_raw_source_cell(
    rows: Sequence[Mapping[str, Any]],
    *,
    source: str,
    symbol: str,
    epoch: dt.datetime,
    deadline: dt.datetime,
    issues: list[str],
) -> bool:
    """Recompute one source-cell verdict from immutable raw rows."""

    interval_start = epoch - dt.timedelta(hours=EPOCH_HOURS)
    source_rows = []
    for row in rows:
        if row.get("source") != source or row.get("symbol") != symbol:
            continue
        try:
            event_time = parse_utc(row["event_time"])
            received_at = parse_utc(row["local_receive_time"])
        except (TypeError, ValueError):
            _add_issue(
                issues, f"raw_timestamp_invalid:{symbol}@{iso_utc(epoch)}:{source}"
            )
            continue
        if not interval_start <= event_time < epoch or received_at > deadline:
            continue
        source_rows.append(row)

    identities: dict[str, set[str]] = defaultdict(set)
    gap_flags: dict[tuple[str, str], set[bool]] = defaultdict(set)
    valid_evidence: set[tuple[str, str]] = set()
    invalid_evidence: set[tuple[str, str]] = set()
    slots: dict[tuple[str, str], int] = {}
    for row in source_rows:
        identity = _source_identity(row)
        payload_hash = str(row.get("raw_payload_sha256"))
        evidence_key = (identity, payload_hash)
        identities[identity].add(payload_hash)
        gap_flags[evidence_key].add(bool(row.get("gap_detected")))
        try:
            payload = json.loads(row.get("raw_payload"))
        except (TypeError, json.JSONDecodeError):
            invalid_evidence.add(evidence_key)
            continue
        if sha256_text(canonical_json(payload)) != payload_hash or not _payload_valid(
            source, payload
        ):
            invalid_evidence.add(evidence_key)
            continue
        valid_evidence.add(evidence_key)
        cadence_seconds = SOURCE_CADENCE_SECONDS.get(source)
        if cadence_seconds is not None:
            slot = _observation_slot(
                row.get("event_time"),
                interval_start=interval_start,
                cadence_seconds=cadence_seconds,
            )
            if slot is not None:
                slots[evidence_key] = slot

    conflicting_ids = {
        identity for identity, hashes in identities.items() if len(hashes) > 1
    }
    uncovered_gaps = {
        evidence_key for evidence_key, flags in gap_flags.items() if flags == {True}
    }
    cadence_seconds = SOURCE_CADENCE_SECONDS.get(source)
    if cadence_seconds is None:
        missing_slots = False
    else:
        expected_count = (EPOCH_HOURS * 60 * 60) // cadence_seconds
        covered_slots = {
            slot
            for evidence_key, slot in slots.items()
            if evidence_key in valid_evidence and gap_flags[evidence_key] != {True}
        }
        missing_slots = covered_slots != set(range(expected_count))
    complete = bool(source_rows) and not (
        conflicting_ids
        or invalid_evidence
        or uncovered_gaps
        or not valid_evidence
        or missing_slots
        or (cadence_seconds is None and not valid_evidence)
    )
    if not complete:
        _add_issue(
            issues,
            f"raw_source_cell_incomplete:{symbol}@{iso_utc(epoch)}:{source}",
        )
    return complete


def _validate_event_provenance(
    row: Mapping[str, Any],
    *,
    event_name: str,
    event_at: dt.datetime | None,
    process_by_pair: Mapping[tuple[str, str], Mapping[str, Any]],
    issues: list[str],
) -> None:
    try:
        instance_id = row["collector_instance_id"]
        run_id = row["run_id"]
    except (KeyError, IndexError):
        _add_issue(issues, f"{event_name}_provenance_missing")
        return
    if not isinstance(instance_id, str) or not instance_id:
        _add_issue(issues, f"{event_name}_provenance_missing")
        return
    if not isinstance(run_id, str) or not run_id:
        _add_issue(issues, f"{event_name}_provenance_missing")
        return
    process = process_by_pair.get((instance_id, run_id))
    if process is None:
        _add_issue(issues, f"{event_name}_without_process_stamp")
        return
    if event_at is None:
        return
    try:
        started_at = parse_utc(process["started_at"])
    except (TypeError, ValueError):
        _add_issue(issues, "process_started_at_invalid")
        return
    if started_at > event_at:
        _add_issue(issues, f"{event_name}_before_process_start")


def _validate_finalization_timestamps(
    row: Mapping[str, Any],
    *,
    decision_epoch: dt.datetime,
    observed_at: dt.datetime,
    issues: list[str],
) -> dt.datetime | None:
    try:
        prefix = f"{row['symbol']}@{row['decision_epoch_utc']}"
    except (KeyError, IndexError):
        prefix = "unknown"
    deadline = finalize_at(decision_epoch)
    for field in ("finalize_at", "finalized_at"):
        try:
            value = parse_utc(row[field])
        except (KeyError, TypeError, ValueError):
            _add_issue(issues, f"finalization_{field}_invalid:{prefix}")
            continue
        if value != deadline:
            _add_issue(issues, f"finalization_{field}_mismatch:{prefix}")
    try:
        recorded_at = parse_utc(row["recorded_at"])
    except (KeyError, TypeError, ValueError):
        _add_issue(issues, f"finalization_recorded_at_invalid:{prefix}")
        return None
    if recorded_at < deadline:
        _add_issue(issues, f"finalization_recorded_before_deadline:{prefix}")
    if recorded_at > observed_at:
        _add_issue(issues, f"finalization_recorded_after_observation:{prefix}")
    return recorded_at


def _audit_replica(
    path: Path,
    *,
    manifest: StudyManifest,
    epochs: tuple[dt.datetime, ...],
    expected_code_hash: str | None,
    observed_at: dt.datetime,
) -> tuple[dict[str, Any], list[str], set[str], set[str]]:
    issues: list[str] = []
    report: dict[str, Any] = {
        "artifact": str(path),
        "status": "UNAVAILABLE",
        "collector_instance_ids": [],
        "process_instance_ids": [],
        "process_runs": 0,
        "heartbeat_rows": 0,
        "finalization_rows": 0,
        "expected_finalization_rows": 0,
        "source_cells": 0,
        "late_only_rows": 0,
        "missing_finalizations": [],
        "conflict_finalizations": [],
        "out_of_window_finalizations": [],
        "unhealthy_alerts": [],
    }
    observed_code_hashes: set[str] = set()
    observed_versions: set[str] = set()

    if not path.is_file():
        issues.append(f"artifact_unavailable:{path}")
        return report, issues, observed_code_hashes, observed_versions

    start = manifest.t0
    end = manifest.t0 + dt.timedelta(days=READINESS_DAYS)
    expected_by_key = {
        (iso_utc(epoch), symbol): epoch
        for epoch in epochs
        for symbol in manifest.symbols
    }
    try:
        with sqlite3.connect(_read_only_uri(path), uri=True) as connection:
            connection.row_factory = sqlite3.Row
            assert_connection_manifest_compatible(connection, manifest)
            assert_finalization_provenance_schema(connection)

            process_rows = list(
                connection.execute(
                    """
                    SELECT append_id, collector_instance_id, run_id, started_at,
                           code_hash, collector_version, t0_utc,
                           study_manifest_sha256
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
            process_by_pair = {
                (row["collector_instance_id"], row["run_id"]): row
                for row in process_rows
            }
            process_instance_ids = {
                row["collector_instance_id"]
                for row in process_rows
                if row["collector_instance_id"]
            }
            report["process_instance_ids"] = sorted(process_instance_ids)
            for row in process_rows:
                if row["code_hash"]:
                    observed_code_hashes.add(row["code_hash"])
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
                try:
                    parse_utc(row["started_at"])
                except (TypeError, ValueError):
                    _add_issue(issues, "process_started_at_invalid")

            heartbeat_all = list(
                connection.execute(
                    """
                    SELECT append_id, collector_instance_id, run_id, observed_at,
                           health_json
                    FROM collector_heartbeats
                    WHERE study_id = ? AND policy_hash = ?
                    ORDER BY append_id
                    """,
                    (manifest.study_id, manifest.contract_hash),
                )
            )
            heartbeat_rows: list[sqlite3.Row] = []
            heartbeat_times: list[dt.datetime] = []
            heartbeat_instance_ids: set[str] = set()
            for row in heartbeat_all:
                try:
                    heartbeat_at = parse_utc(row["observed_at"])
                except (TypeError, ValueError):
                    _add_issue(issues, "heartbeat_timestamp_invalid")
                    heartbeat_at = None
                _validate_event_provenance(
                    row,
                    event_name="heartbeat",
                    event_at=heartbeat_at,
                    process_by_pair=process_by_pair,
                    issues=issues,
                )
                if heartbeat_at is not None and heartbeat_at > observed_at:
                    _add_issue(issues, "heartbeat_after_observation")
                if heartbeat_at is not None and start <= heartbeat_at < end:
                    heartbeat_rows.append(row)
                    heartbeat_times.append(heartbeat_at)
                    if row["collector_instance_id"]:
                        heartbeat_instance_ids.add(row["collector_instance_id"])
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
            report["heartbeat_rows"] = len(heartbeat_rows)
            if not heartbeat_rows:
                _add_issue(issues, "heartbeat_evidence_missing")
            missing_heartbeat_epochs = [
                iso_utc(epoch)
                for epoch in epochs
                if not any(
                    epoch <= heartbeat_at < epoch + dt.timedelta(hours=EPOCH_HOURS)
                    for heartbeat_at in heartbeat_times
                )
            ]
            report["missing_heartbeat_epochs"] = missing_heartbeat_epochs
            if missing_heartbeat_epochs:
                _add_issue(issues, "heartbeat_continuity_gap")

            finalization_rows = list(
                connection.execute(
                    """
                    SELECT append_id, symbol, decision_epoch_utc, finalize_at,
                           finalized_at, recorded_at, collector_instance_id, run_id,
                           final_status, missing_sources_json, conflict_sources_json,
                           invalid_sources_json, evaluation_hash, evaluation_json,
                           source_manifest_hash
                    FROM symbol_epoch_finalizations
                    WHERE study_id = ? AND policy_hash = ?
                    ORDER BY append_id
                    """,
                    (manifest.study_id, manifest.contract_hash),
                )
            )
            report["finalization_rows"] = len(finalization_rows)
            expected_key_counts: dict[tuple[str, str], int] = defaultdict(int)
            finalization_instance_ids: set[str] = set()
            for row in finalization_rows:
                decision_epoch = None
                try:
                    decision_epoch = parse_utc(row["decision_epoch_utc"])
                except (TypeError, ValueError):
                    _add_issue(issues, "finalization_decision_epoch_invalid")
                key = (row["decision_epoch_utc"], row["symbol"])
                if key in expected_by_key:
                    expected_key_counts[key] += 1
                    expected_deadline = finalize_at(expected_by_key[key])
                    _validate_finalization_timestamps(
                        row,
                        decision_epoch=expected_by_key[key],
                        observed_at=observed_at,
                        issues=issues,
                    )
                    _validate_evidence(
                        row,
                        manifest=manifest,
                        expected_deadline=expected_deadline,
                        issues=issues,
                    )
                else:
                    report["out_of_window_finalizations"].append(
                        {
                            "decision_epoch_utc": row["decision_epoch_utc"],
                            "symbol": row["symbol"],
                        }
                    )
                    _add_issue(
                        issues,
                        f"finalization_outside_expected_window:{row['symbol']}@"
                        f"{row['decision_epoch_utc']}",
                    )
                    if decision_epoch is not None:
                        _validate_finalization_timestamps(
                            row,
                            decision_epoch=decision_epoch,
                            observed_at=observed_at,
                            issues=issues,
                        )
                try:
                    recorded_for_provenance = parse_utc(row["recorded_at"])
                except (TypeError, ValueError):
                    recorded_for_provenance = None
                _validate_event_provenance(
                    row,
                    event_name="finalization",
                    event_at=recorded_for_provenance,
                    process_by_pair=process_by_pair,
                    issues=issues,
                )
                if row["collector_instance_id"]:
                    finalization_instance_ids.add(row["collector_instance_id"])
                if row["source_manifest_hash"] != manifest.source_manifest_hash:
                    _add_issue(issues, "finalization_source_manifest_mismatch")
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
            report["expected_finalization_rows"] = sum(expected_key_counts.values())
            missing_keys = sorted(set(expected_by_key) - set(expected_key_counts))
            report["missing_finalizations"].extend(
                {
                    "decision_epoch_utc": epoch,
                    "symbol": symbol,
                }
                for epoch, symbol in missing_keys
            )
            for epoch, symbol in missing_keys:
                _add_issue(issues, f"finalization_missing:{symbol}@{epoch}")
            for key, count in expected_key_counts.items():
                if count > 1:
                    _add_issue(
                        issues,
                        f"finalization_duplicate:{key[1]}@{key[0]}",
                    )

            report["past_identity_finalizations"] = sum(
                1
                for row in finalization_rows
                if isinstance(row["decision_epoch_utc"], str)
                and row["decision_epoch_utc"] < iso_utc(start)
            )
            if report["past_identity_finalizations"]:
                _add_issue(issues, "past_identity_finalizations_present")

            late_rows = list(
                connection.execute(
                    """
                    SELECT correction_status
                    FROM late_only_corrections
                    WHERE study_id = ? AND policy_hash = ?
                    """,
                    (manifest.study_id, manifest.contract_hash),
                )
            )
            report["late_only_rows"] = len(late_rows)
            if any(row["correction_status"] != "LATE_ONLY" for row in late_rows):
                _add_issue(issues, "late_correction_promoted")

            raw_rows = _raw_rows_for_window(
                connection,
                start=start,
                end=end,
            )
            raw_rows_by_cell: dict[tuple[str, str, str], list[dict[str, Any]]] = (
                defaultdict(list)
            )
            expected_epoch_texts = {iso_utc(epoch) for epoch in epochs}
            for raw_row in raw_rows:
                try:
                    raw_epoch = iso_utc(
                        decision_epoch_for_event(parse_utc(raw_row["event_time"]))
                    )
                except (TypeError, ValueError):
                    _add_issue(issues, "raw_event_timestamp_invalid")
                    continue
                if (
                    raw_epoch in expected_epoch_texts
                    and raw_row["symbol"] in manifest.symbols
                    and raw_row["source"] in manifest.required_sources
                ):
                    raw_rows_by_cell[
                        (raw_epoch, raw_row["symbol"], raw_row["source"])
                    ].append(raw_row)
            for epoch in epochs:
                deadline = finalize_at(epoch)
                for symbol in manifest.symbols:
                    for source in manifest.required_sources:
                        if _validate_raw_source_cell(
                            raw_rows_by_cell.get(
                                (iso_utc(epoch), symbol, source),
                                (),
                            ),
                            source=source,
                            symbol=symbol,
                            epoch=epoch,
                            deadline=deadline,
                            issues=issues,
                        ):
                            report["source_cells"] += 1

            all_bound_instances = (
                process_instance_ids
                | heartbeat_instance_ids
                | finalization_instance_ids
            )
            report["collector_instance_ids"] = sorted(all_bound_instances)
            if len(process_instance_ids) != 1:
                _add_issue(issues, "stable_collector_instance_required")
            if process_instance_ids != heartbeat_instance_ids:
                _add_issue(issues, "topology_process_heartbeat_mismatch")
            if finalization_instance_ids - process_instance_ids:
                _add_issue(issues, "topology_finalization_process_mismatch")

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
    except (ArtifactManifestMismatch, ArtifactSchemaMismatch, sqlite3.Error) as exc:
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
    observed_at: dt.datetime | None = None,
) -> dict[str, Any]:
    """Audit both artifacts without opening either one for writing."""

    observation_time = _normalize_observed_at(observed_at)
    paths = tuple(
        sorted({path.expanduser().resolve() for path in artifact_paths}, key=str)
    )
    epochs = readiness_epochs(manifest)
    issues: list[str] = []
    final_deadline = finalize_at(epochs[-1])
    if observation_time < manifest.t0:
        _add_issue(issues, "readiness_before_t0")
    if observation_time < final_deadline:
        _add_issue(issues, "readiness_before_final_deadline")
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
            observed_at=observation_time,
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
            "final_deadline": iso_utc(final_deadline),
            "observed_at": iso_utc(observation_time),
        },
        "counts": counts,
        "gates": gates,
        "replicas": replicas,
        "issues": sorted(issues),
    }
