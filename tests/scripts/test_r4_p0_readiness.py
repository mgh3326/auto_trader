from __future__ import annotations

import datetime as dt
import hashlib
import json
import sqlite3
from pathlib import Path

import pytest

from app.services.brokers.binance.r4_p0_collector import (
    COLLECTOR_VERSION,
    REQUIRED_ACTIVE_SOURCES,
    SIGNAL_SYMBOLS,
    AppendOnlyPITStore,
)
from app.services.brokers.binance.r4_p0_hardening import (
    FINAL_COMPLETE,
    FINALIZER_VERSION,
    SOURCE_CADENCE_SECONDS,
    STUDY_MANIFEST_SCHEMA_VERSION,
    EpochLedger,
    EpochPolicy,
    StudyManifest,
    canonical_json,
    finalize_at,
    iso_utc,
    parse_study_manifest,
    sha256_text,
)
from app.services.brokers.binance.r4_p0_readiness import (
    READINESS_EPOCHS,
    READINESS_SOURCE_CELLS,
    READINESS_SYMBOL_EPOCHS,
    audit_readiness,
)
from scripts import r4_p0_readiness as readiness_cli

T0 = dt.datetime(2026, 8, 8, tzinfo=dt.UTC)
EXPECTED_CODE_HASH = "a" * 40


def _manifest() -> StudyManifest:
    policy = EpochPolicy(
        required_sources=tuple(sorted(REQUIRED_ACTIVE_SOURCES)),
        symbols=tuple(sorted(SIGNAL_SYMBOLS)),
        study_id="TEST-R4-P0-READINESS",
        policy_hash="b" * 64,
        t0=T0,
    )
    payload = {
        "schema_version": STUDY_MANIFEST_SCHEMA_VERSION,
        "effective_at": iso_utc(T0 - dt.timedelta(days=1)),
        "study_id": policy.study_id,
        "contract_hash": policy.policy_hash,
        "t0": iso_utc(T0),
        "required_sources": list(policy.required_sources),
        "symbols": list(policy.symbols),
        "source_manifest_hash": policy.source_manifest_hash,
    }
    return parse_study_manifest(
        payload,
        expected_sha256=sha256_text(canonical_json(payload)),
        expected_sources=policy.required_sources,
        expected_symbols=policy.symbols,
    )


def _evaluation(manifest, symbol: str, epoch: dt.datetime) -> tuple[str, str]:
    evidence = {}
    for source in manifest.required_sources:
        if source in SOURCE_CADENCE_SECONDS:
            expected = 240 if source == "binance_usdm.premiumIndexKline1m" else 48
            covered = expected
        else:
            expected = None
            covered = None
        evidence[source] = {
            "conflicting_source_identities": [],
            "covered_observation_count": covered,
            "expected_observation_count": expected,
            "invalid_source_identity_payload_hashes": [],
            "missing_observation_slots": [],
            "source_identity_payload_hashes": [[f"{source}:identity", "c" * 64]],
            "state": "COMPLETE",
            "uncovered_gap_identity_payload_hashes": [],
            "valid_source_identity_payload_hashes": [[f"{source}:identity", "c" * 64]],
        }
    payload = {
        "decision_epoch_utc": iso_utc(epoch),
        "final_status": FINAL_COMPLETE,
        "finalize_at": iso_utc(finalize_at(epoch)),
        "finalizer_version": FINALIZER_VERSION,
        "policy_hash": manifest.contract_hash,
        "source_evidence": evidence,
        "source_manifest_hash": manifest.source_manifest_hash,
        "study_id": manifest.study_id,
        "symbol": symbol,
    }
    evaluation_json = canonical_json(payload)
    return sha256_text(evaluation_json), evaluation_json


def _readiness_payload(source: str, timestamp: int) -> object:
    if source == "binance_usdm.aggTrade":
        return {"p": "1", "q": "1"}
    if source == "binance_usdm.bookTicker":
        return {"b": "1", "B": "1", "a": "2", "A": "1"}
    if source == "binance_usdm.depth5":
        return {"b": [["1", "1"]], "a": [["2", "1"]]}
    if source == "binance_usdm.openInterest":
        return {"openInterest": "1"}
    if source == "binance_usdm.openInterestHist":
        return {"sumOpenInterest": "1"}
    if source == "binance_usdm.basis":
        return {"basis": "1"}
    if source == "binance_usdm.takerLongShortRatio":
        return {"buySellRatio": "1", "buyVol": "1", "sellVol": "1"}
    if source == "binance_usdm.premiumIndex":
        return {"markPrice": "1", "indexPrice": "1"}
    if source == "binance_usdm.predictedFunding":
        return {"lastFundingRate": "1"}
    if source == "binance_usdm.premiumIndexKline1m":
        return [timestamp - 59_999, "1", "1", "1", "1", "1", timestamp]
    raise AssertionError(source)


def _append_complete_readiness_raw(store: AppendOnlyPITStore, manifest) -> None:
    rows: list[tuple[object, ...]] = []
    previous_by_partition: dict[str, str | None] = {}
    for index in range(READINESS_EPOCHS):
        epoch = manifest.t0 + dt.timedelta(hours=index * 4)
        deadline = finalize_at(epoch)
        for symbol in manifest.symbols:
            for source in manifest.required_sources:
                cadence_seconds = SOURCE_CADENCE_SECONDS.get(source)
                observations = (
                    240
                    if source == "binance_usdm.premiumIndexKline1m"
                    else 48
                    if cadence_seconds is not None
                    else 1
                )
                step_seconds = cadence_seconds or 1
                for observation in range(observations):
                    event_time = (
                        epoch
                        - dt.timedelta(hours=4)
                        + dt.timedelta(seconds=observation * step_seconds)
                    )
                    timestamp = int(event_time.timestamp() * 1000)
                    raw_payload = _readiness_payload(source, timestamp)
                    raw_text = canonical_json(raw_payload)
                    raw_hash = sha256_text(raw_text)
                    event_text = iso_utc(event_time)
                    received_text = iso_utc(deadline - dt.timedelta(minutes=1))
                    sequence = f"{source}:{symbol}:{timestamp}:{observation}"
                    reconnect_id = f"rehearsal:{store.root.name}"
                    run_id = f"run-{store.root.name}"
                    record_identity = {
                        "source": source,
                        "symbol": symbol,
                        "event_time": event_text,
                        "transaction_time": None,
                        "sequence_or_trade_id": sequence,
                        "raw_payload_sha256": raw_hash,
                    }
                    record_id = sha256_text(canonical_json(record_identity))
                    partition_key = f"{source}/{symbol}/{received_text[:10]}"
                    previous_hash = previous_by_partition.get(partition_key)
                    chain_payload = {
                        **record_identity,
                        "local_receive_time": received_text,
                        "request_started_at": iso_utc(
                            deadline - dt.timedelta(minutes=2)
                        ),
                        "request_completed_at": received_text,
                        "collector_version": store.collector_version,
                        "gap_detected": False,
                        "reconnect_id": reconnect_id,
                        "previous_partition_sha256": previous_hash,
                        "run_id": run_id,
                    }
                    partition_hash = sha256_text(
                        f"{previous_hash or ''}\n{canonical_json(chain_payload)}"
                    )
                    previous_by_partition[partition_key] = partition_hash
                    rows.append(
                        (
                            record_id,
                            partition_key,
                            source,
                            symbol,
                            event_text,
                            None,
                            received_text,
                            iso_utc(deadline - dt.timedelta(minutes=2)),
                            received_text,
                            sequence,
                            raw_hash,
                            store.collector_version,
                            partition_hash,
                            0,
                            reconnect_id,
                            previous_hash,
                            run_id,
                            raw_text,
                        )
                    )
    store._db.executemany(  # noqa: SLF001
        """
        INSERT INTO pit_records (
            record_id, partition_key, source, symbol, event_time,
            transaction_time, local_receive_time, request_started_at,
            request_completed_at, sequence_or_trade_id, raw_payload_sha256,
            collector_version, partition_sha256, gap_detected, reconnect_id,
            previous_partition_sha256, run_id, raw_payload
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    store._db.commit()  # noqa: SLF001


def _write_rehearsal(
    root: Path,
    manifest,
    *,
    collector_instance_id: str,
    code_hash: str = EXPECTED_CODE_HASH,
    missing_finalization: tuple[dt.datetime, str] | None = None,
    include_raw: bool = False,
    include_restart: bool = False,
    process_started_at: dt.datetime | None = None,
    heartbeat_instance_id: str | None = None,
    heartbeat_run_id: str | None = None,
    heartbeat_started_at: dt.datetime | None = None,
    finalization_instance_id: str | None = None,
    finalization_run_id: str | None = None,
    extra_decision_epochs: tuple[dt.datetime, ...] = (),
) -> Path:
    with AppendOnlyPITStore(root, study_manifest=manifest) as store:
        ledger = EpochLedger(store._db, manifest.epoch_policy)
        process_run_id = f"run-{collector_instance_id}"
        ledger.append_process_version(
            collector_instance_id=collector_instance_id,
            run_id=process_run_id,
            started_at=(
                process_started_at
                if process_started_at is not None
                else manifest.t0 - dt.timedelta(hours=1)
            ),
            code_hash=code_hash,
            collector_version=COLLECTOR_VERSION,
            study_manifest_sha256=manifest.content_sha256,
        )
        heartbeat_instance_id = heartbeat_instance_id or collector_instance_id
        heartbeat_run_id = heartbeat_run_id or process_run_id
        if (
            heartbeat_run_id != process_run_id
            or heartbeat_instance_id != collector_instance_id
        ):
            ledger.append_process_version(
                collector_instance_id=heartbeat_instance_id,
                run_id=heartbeat_run_id,
                started_at=(
                    heartbeat_started_at
                    if heartbeat_started_at is not None
                    else manifest.t0 - dt.timedelta(hours=1)
                ),
                code_hash=code_hash,
                collector_version=COLLECTOR_VERSION,
                study_manifest_sha256=manifest.content_sha256,
            )
        if include_restart:
            restart_run_id = f"restart-{collector_instance_id}"
            ledger.append_process_version(
                collector_instance_id=collector_instance_id,
                run_id=restart_run_id,
                started_at=manifest.t0 - dt.timedelta(minutes=30),
                code_hash=code_hash,
                collector_version=COLLECTOR_VERSION,
                study_manifest_sha256=manifest.content_sha256,
            )
            heartbeat_run_id = restart_run_id
            finalization_run_id = restart_run_id
        if include_raw:
            _append_complete_readiness_raw(store, manifest)
        for index in range(READINESS_EPOCHS):
            epoch = manifest.t0 + dt.timedelta(hours=index * 4)
            ledger.append_heartbeat(
                collector_instance_id=heartbeat_instance_id,
                run_id=heartbeat_run_id,
                observed_at=epoch + dt.timedelta(hours=1),
                health={
                    "code_hash": code_hash,
                    "collector_version": COLLECTOR_VERSION,
                    "ok": True,
                    "study_manifest_sha256": manifest.content_sha256,
                    "t0": iso_utc(manifest.t0),
                },
            )
            for symbol in manifest.symbols:
                if missing_finalization == (epoch, symbol):
                    continue
                evaluation_hash, evaluation_json = _evaluation(
                    manifest,
                    symbol,
                    epoch,
                )
                ledger.append_finalization(
                    symbol=symbol,
                    decision_epoch=epoch,
                    recorded_at=finalize_at(epoch),
                    collector_instance_id=(
                        finalization_instance_id or collector_instance_id
                    ),
                    run_id=finalization_run_id or process_run_id,
                    final_status=FINAL_COMPLETE,
                    missing_sources=(),
                    conflict_sources=(),
                    invalid_sources=(),
                    evaluation_hash=evaluation_hash,
                    evaluation_json=evaluation_json,
                )
        for extra_epoch in extra_decision_epochs:
            for symbol in manifest.symbols:
                evaluation_hash, evaluation_json = _evaluation(
                    manifest,
                    symbol,
                    extra_epoch,
                )
                ledger.append_finalization(
                    symbol=symbol,
                    decision_epoch=extra_epoch,
                    recorded_at=finalize_at(extra_epoch),
                    collector_instance_id=(
                        finalization_instance_id or collector_instance_id
                    ),
                    run_id=finalization_run_id or process_run_id,
                    final_status=FINAL_COMPLETE,
                    missing_sources=(),
                    conflict_sources=(),
                    invalid_sources=(),
                    evaluation_hash=evaluation_hash,
                    evaluation_json=evaluation_json,
                )
        return store.path


def _cli_argv(paths: list[Path], manifest_path: Path, manifest) -> list[str]:
    return [
        "--artifact",
        str(paths[0]),
        "--artifact",
        str(paths[1]),
        "--study-manifest",
        str(manifest_path),
        "--study-manifest-sha256",
        manifest.content_sha256,
        "--expected-code-hash",
        EXPECTED_CODE_HASH,
    ]


def _file_fingerprint(path: Path) -> tuple[int, int, str] | None:
    if not path.exists():
        return None
    stat = path.stat()
    return (
        stat.st_mtime_ns,
        stat.st_size,
        hashlib.sha256(path.read_bytes()).hexdigest(),
    )


@pytest.mark.unit
def test_readiness_auditor_passes_fixed_contract_and_is_read_only(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()
    paths = [
        _write_rehearsal(
            tmp_path / "host-a",
            manifest,
            collector_instance_id="host-a",
            include_raw=True,
            include_restart=True,
        ),
        _write_rehearsal(
            tmp_path / "host-b",
            manifest,
            collector_instance_id="host-b",
            include_raw=True,
            include_restart=True,
        ),
    ]
    manifest_path = tmp_path / "study-manifest.json"
    manifest_path.write_text(manifest.canonical_payload_json, encoding="utf-8")
    files = [
        path
        for artifact in paths
        for path in (
            artifact,
            artifact.with_name(f"{artifact.name}-wal"),
            artifact.with_name(f"{artifact.name}-shm"),
            artifact.parent / ".collector.lock",
        )
    ] + [manifest_path]
    before = {path: _file_fingerprint(path) for path in files}

    report = audit_readiness(
        paths,
        manifest,
        expected_code_hash=EXPECTED_CODE_HASH,
        observed_at=manifest.t0 + dt.timedelta(days=7),
    )

    after = {path: _file_fingerprint(path) for path in files}
    assert report["ok"] is True
    assert report["counts"]["epochs_expected"] == READINESS_EPOCHS == 42
    assert report["counts"]["symbol_epochs_expected"] == READINESS_SYMBOL_EPOCHS == 126
    assert report["counts"]["source_cells_expected"] == READINESS_SOURCE_CELLS == 1260
    assert report["counts"]["source_cells_observed_per_replica"] == [1260, 1260]
    assert report["counts"]["missing_finalizations"] == 0
    assert report["counts"]["conflict_finalizations"] == 0
    assert [replica["process_runs"] for replica in report["replicas"]] == [2, 2]
    assert report["window"]["final_deadline"] == iso_utc(
        manifest.t0 + dt.timedelta(days=7)
    )
    assert after == before

    monkeypatch.setattr(
        readiness_cli,
        "utc_now",
        lambda: manifest.t0 + dt.timedelta(days=7),
    )
    exit_code = readiness_cli.main(_cli_argv(paths, manifest_path, manifest))
    cli_report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert cli_report["ok"] is True
    assert cli_report["counts"]["source_cells_expected"] == 1260


@pytest.mark.unit
def test_readiness_rejects_nonempty_wal_without_mutating_sidecar(tmp_path) -> None:
    manifest = _manifest()
    paths = [
        _write_rehearsal(tmp_path / "host-a", manifest, collector_instance_id="host-a"),
        _write_rehearsal(tmp_path / "host-b", manifest, collector_instance_id="host-b"),
    ]
    wal_path = paths[0].with_name(f"{paths[0].name}-wal")
    wal_path.write_bytes(b"uncheckpointed-test-wal")
    before = _file_fingerprint(wal_path)

    report = audit_readiness(
        paths,
        manifest,
        expected_code_hash=EXPECTED_CODE_HASH,
        observed_at=manifest.t0 + dt.timedelta(days=7),
    )

    assert report["ok"] is False
    assert any("ArtifactSchemaMismatch" in issue for issue in report["issues"])
    assert _file_fingerprint(wal_path) == before


@pytest.mark.unit
def test_readiness_api_and_cli_reject_before_t0_and_final_deadline(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()
    paths = [
        _write_rehearsal(tmp_path / "host-a", manifest, collector_instance_id="host-a"),
        _write_rehearsal(tmp_path / "host-b", manifest, collector_instance_id="host-b"),
    ]
    report = audit_readiness(
        paths,
        manifest,
        expected_code_hash=EXPECTED_CODE_HASH,
        observed_at=manifest.t0 - dt.timedelta(seconds=1),
    )
    assert report["ok"] is False
    assert "readiness_before_t0" in report["issues"]
    assert "readiness_before_final_deadline" in report["issues"]

    manifest_path = tmp_path / "study-manifest.json"
    manifest_path.write_text(manifest.canonical_payload_json, encoding="utf-8")
    monkeypatch.setattr(
        readiness_cli,
        "utc_now",
        lambda: manifest.t0 + dt.timedelta(days=7) - dt.timedelta(seconds=1),
    )
    exit_code = readiness_cli.main(_cli_argv(paths, manifest_path, manifest))
    cli_report = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert cli_report["ok"] is False
    assert any(
        "readiness_before_final_deadline" in issue for issue in cli_report["issues"]
    )


@pytest.mark.unit
def test_readiness_rejects_raw_empty_forged_evaluation_api_and_cli(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _manifest()
    paths = [
        _write_rehearsal(tmp_path / "host-a", manifest, collector_instance_id="host-a"),
        _write_rehearsal(tmp_path / "host-b", manifest, collector_instance_id="host-b"),
    ]
    final_deadline = manifest.t0 + dt.timedelta(days=7)
    report = audit_readiness(
        paths,
        manifest,
        expected_code_hash=EXPECTED_CODE_HASH,
        observed_at=final_deadline,
    )
    assert report["ok"] is False
    assert report["counts"]["source_cells_observed_per_replica"] == [0, 0]
    assert any("raw_source_cell_incomplete" in issue for issue in report["issues"])

    manifest_path = tmp_path / "study-manifest.json"
    manifest_path.write_text(manifest.canonical_payload_json, encoding="utf-8")
    monkeypatch.setattr(readiness_cli, "utc_now", lambda: final_deadline)
    exit_code = readiness_cli.main(_cli_argv(paths, manifest_path, manifest))
    cli_report = json.loads(capsys.readouterr().out)
    assert exit_code == 1
    assert cli_report["ok"] is False
    assert cli_report["counts"]["source_cells_observed_per_replica"] == [0, 0]


@pytest.mark.unit
def test_readiness_rejects_process_started_after_heartbeat_and_finalization(
    tmp_path,
) -> None:
    manifest = _manifest()
    late_start = manifest.t0 + dt.timedelta(days=7, hours=1)
    paths = [
        _write_rehearsal(
            tmp_path / "host-a",
            manifest,
            collector_instance_id="host-a",
            process_started_at=late_start,
        ),
        _write_rehearsal(tmp_path / "host-b", manifest, collector_instance_id="host-b"),
    ]
    report = audit_readiness(
        paths,
        manifest,
        expected_code_hash=EXPECTED_CODE_HASH,
        observed_at=manifest.t0 + dt.timedelta(days=7),
    )
    replica_name = Path(report["replicas"][0]["artifact"]).name
    assert report["ok"] is False
    assert any(
        f"{replica_name}:heartbeat_before_process_start" in issue
        for issue in report["issues"]
    )
    assert any(
        f"{replica_name}:finalization_before_process_start" in issue
        for issue in report["issues"]
    )


@pytest.mark.unit
def test_readiness_does_not_bind_old_finalizations_to_late_heartbeat(tmp_path) -> None:
    manifest = _manifest()
    late_start = manifest.t0 + dt.timedelta(days=7, hours=1)
    paths = [
        _write_rehearsal(
            tmp_path / "host-a",
            manifest,
            collector_instance_id="host-a",
            heartbeat_run_id="late-run",
            heartbeat_started_at=late_start,
        ),
        _write_rehearsal(tmp_path / "host-b", manifest, collector_instance_id="host-b"),
    ]
    report = audit_readiness(
        paths,
        manifest,
        expected_code_hash=EXPECTED_CODE_HASH,
        observed_at=manifest.t0 + dt.timedelta(days=7),
    )
    assert report["ok"] is False
    assert any("heartbeat_before_process_start" in issue for issue in report["issues"])
    assert not any(
        "finalization_without_process_stamp" in issue
        for issue in report["issues"]
        if Path(paths[0]).name in issue
    )


@pytest.mark.unit
def test_readiness_rejects_finalization_producer_run_or_instance_mismatch(
    tmp_path,
) -> None:
    manifest = _manifest()
    paths = [
        _write_rehearsal(
            tmp_path / "host-a",
            manifest,
            collector_instance_id="host-a",
            finalization_instance_id="forged-host",
            finalization_run_id="forged-run",
        ),
        _write_rehearsal(tmp_path / "host-b", manifest, collector_instance_id="host-b"),
    ]
    report = audit_readiness(
        paths,
        manifest,
        expected_code_hash=EXPECTED_CODE_HASH,
        observed_at=manifest.t0 + dt.timedelta(days=7),
    )
    assert report["ok"] is False
    assert any(
        "finalization_without_process_stamp" in issue for issue in report["issues"]
    )
    assert any(
        "topology_finalization_process_mismatch" in issue for issue in report["issues"]
    )


@pytest.mark.unit
def test_readiness_rejects_exclusive_end_and_later_finalizations(tmp_path) -> None:
    manifest = _manifest()
    end = manifest.t0 + dt.timedelta(days=7)
    later = end + dt.timedelta(hours=4)
    paths = [
        _write_rehearsal(
            tmp_path / "host-a",
            manifest,
            collector_instance_id="host-a",
            extra_decision_epochs=(end, later),
        ),
        _write_rehearsal(
            tmp_path / "host-b",
            manifest,
            collector_instance_id="host-b",
            extra_decision_epochs=(end, later),
        ),
    ]
    report = audit_readiness(
        paths,
        manifest,
        expected_code_hash=EXPECTED_CODE_HASH,
        observed_at=finalize_at(later),
    )
    assert report["ok"] is False
    assert report["counts"]["symbol_epochs_observed_per_replica"] == [132, 132]
    for replica in report["replicas"]:
        assert {
            item["decision_epoch_utc"]
            for item in replica["out_of_window_finalizations"]
        } == {iso_utc(end), iso_utc(later)}
    assert any(
        "finalization_outside_expected_window" in issue for issue in report["issues"]
    )


@pytest.mark.unit
def test_readiness_rejects_legacy_finalization_schema_without_backfill(
    tmp_path,
) -> None:
    manifest = _manifest()
    paths = []
    for name in ("host-a", "host-b"):
        path = tmp_path / name / "r4_p0_collector.sqlite3"
        path.parent.mkdir()
        with sqlite3.connect(path) as connection:
            connection.executescript(
                """
                CREATE TABLE collector_study_manifest_binding (
                    binding_id INTEGER PRIMARY KEY,
                    study_id TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    t0 TEXT NOT NULL,
                    source_manifest_hash TEXT NOT NULL,
                    study_manifest_sha256 TEXT NOT NULL,
                    study_manifest_json TEXT NOT NULL,
                    recorded_at TEXT NOT NULL
                );
                CREATE TABLE symbol_epoch_finalizations (
                    append_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    finalization_id TEXT NOT NULL,
                    study_id TEXT NOT NULL,
                    policy_hash TEXT NOT NULL,
                    source_manifest_hash TEXT NOT NULL,
                    symbol TEXT NOT NULL,
                    decision_epoch_utc TEXT NOT NULL,
                    finalize_at TEXT NOT NULL,
                    finalized_at TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    final_status TEXT NOT NULL,
                    missing_sources_json TEXT NOT NULL,
                    conflict_sources_json TEXT NOT NULL,
                    invalid_sources_json TEXT NOT NULL,
                    evaluation_hash TEXT NOT NULL,
                    evaluation_json TEXT NOT NULL,
                    finalizer_version TEXT NOT NULL
                );
                """
            )
            connection.execute(
                """
                INSERT INTO collector_study_manifest_binding (
                    binding_id, study_id, policy_hash, t0,
                    source_manifest_hash, study_manifest_sha256,
                    study_manifest_json, recorded_at
                ) VALUES (1, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    manifest.study_id,
                    manifest.contract_hash,
                    iso_utc(manifest.t0),
                    manifest.source_manifest_hash,
                    manifest.content_sha256,
                    manifest.canonical_payload_json,
                    iso_utc(manifest.t0),
                ),
            )
        paths.append(path)

    report = audit_readiness(
        paths,
        manifest,
        expected_code_hash=EXPECTED_CODE_HASH,
        observed_at=manifest.t0 + dt.timedelta(days=7),
    )
    assert report["ok"] is False
    assert all(replica["status"] == "READ_ERROR" for replica in report["replicas"])
    assert any("ArtifactSchemaMismatch" in issue for issue in report["issues"])


@pytest.mark.unit
def test_readiness_auditor_requires_each_host_symbol_epoch_witness(tmp_path) -> None:
    manifest = _manifest()
    missing = (
        manifest.t0 + dt.timedelta(hours=4),
        manifest.symbols[0],
    )
    paths = [
        _write_rehearsal(tmp_path / "host-a", manifest, collector_instance_id="host-a"),
        _write_rehearsal(
            tmp_path / "host-b",
            manifest,
            collector_instance_id="host-b",
            missing_finalization=missing,
        ),
    ]

    report = audit_readiness(paths, manifest, expected_code_hash=EXPECTED_CODE_HASH)

    assert report["ok"] is False
    assert any("finalization_missing" in issue for issue in report["issues"])
    assert report["replicas"][1]["finalization_rows"] == READINESS_SYMBOL_EPOCHS - 1
    assert report["replicas"][1]["source_cells"] == 0


@pytest.mark.unit
def test_readiness_auditor_fails_code_hash_and_topology_drift(tmp_path) -> None:
    manifest = _manifest()
    paths = [
        _write_rehearsal(tmp_path / "host-a", manifest, collector_instance_id="host-a"),
        _write_rehearsal(
            tmp_path / "host-b",
            manifest,
            collector_instance_id="host-a",
            code_hash="d" * 40,
        ),
    ]

    report = audit_readiness(paths, manifest, expected_code_hash=EXPECTED_CODE_HASH)

    assert report["ok"] is False
    assert "topology_collector_instance_drift" in report["issues"]
    assert any("code_hash" in issue for issue in report["issues"])


@pytest.mark.unit
def test_readiness_cli_returns_nonzero_for_missing_replica(tmp_path, capsys) -> None:
    manifest = _manifest()
    path = tmp_path / "study-manifest.json"
    payload = json.loads(manifest.canonical_payload_json)
    path.write_text(json.dumps(payload), encoding="utf-8")

    exit_code = readiness_cli.main(
        [
            "--artifact",
            str(tmp_path / "missing-a.sqlite3"),
            "--artifact",
            str(tmp_path / "missing-b.sqlite3"),
            "--study-manifest",
            str(path),
            "--study-manifest-sha256",
            manifest.content_sha256,
            "--expected-code-hash",
            EXPECTED_CODE_HASH,
        ]
    )

    assert exit_code == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False
