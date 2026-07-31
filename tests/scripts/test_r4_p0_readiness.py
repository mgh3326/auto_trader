from __future__ import annotations

import datetime as dt
import json
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
    SOURCE_CADENCE_SECONDS,
    STUDY_MANIFEST_SCHEMA_VERSION,
    EpochLedger,
    EpochPolicy,
    StudyManifest,
    canonical_json,
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
        "finalize_at": iso_utc(epoch + dt.timedelta(days=1)),
        "finalizer_version": "r4-p0-epoch-finalizer.v2",
        "policy_hash": manifest.contract_hash,
        "source_evidence": evidence,
        "source_manifest_hash": manifest.source_manifest_hash,
        "study_id": manifest.study_id,
        "symbol": symbol,
    }
    evaluation_json = canonical_json(payload)
    return sha256_text(evaluation_json), evaluation_json


def _write_rehearsal(
    root: Path,
    manifest,
    *,
    collector_instance_id: str,
    code_hash: str = EXPECTED_CODE_HASH,
    missing_finalization: tuple[dt.datetime, str] | None = None,
) -> Path:
    with AppendOnlyPITStore(root, study_manifest=manifest) as store:
        ledger = EpochLedger(store._db, manifest.epoch_policy)
        ledger.append_process_version(
            collector_instance_id=collector_instance_id,
            run_id=f"run-{collector_instance_id}",
            started_at=manifest.t0 - dt.timedelta(hours=1),
            code_hash=code_hash,
            collector_version=COLLECTOR_VERSION,
            study_manifest_sha256=manifest.content_sha256,
        )
        for index in range(READINESS_EPOCHS):
            epoch = manifest.t0 + dt.timedelta(hours=index * 4)
            ledger.append_heartbeat(
                collector_instance_id=collector_instance_id,
                run_id=f"run-{collector_instance_id}",
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
                    recorded_at=epoch + dt.timedelta(days=1),
                    final_status=FINAL_COMPLETE,
                    missing_sources=(),
                    conflict_sources=(),
                    invalid_sources=(),
                    evaluation_hash=evaluation_hash,
                    evaluation_json=evaluation_json,
                )
        return store.path


@pytest.mark.unit
def test_readiness_auditor_passes_fixed_contract_and_is_read_only(
    tmp_path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _manifest()
    paths = [
        _write_rehearsal(tmp_path / "host-a", manifest, collector_instance_id="host-a"),
        _write_rehearsal(tmp_path / "host-b", manifest, collector_instance_id="host-b"),
    ]
    before = {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in paths}

    report = audit_readiness(
        paths,
        manifest,
        expected_code_hash=EXPECTED_CODE_HASH,
    )

    after = {path: (path.stat().st_mtime_ns, path.stat().st_size) for path in paths}
    assert report["ok"] is True
    assert report["counts"]["epochs_expected"] == READINESS_EPOCHS == 42
    assert report["counts"]["symbol_epochs_expected"] == READINESS_SYMBOL_EPOCHS == 126
    assert report["counts"]["source_cells_expected"] == READINESS_SOURCE_CELLS == 1260
    assert report["counts"]["source_cells_observed_per_replica"] == [1260, 1260]
    assert report["counts"]["missing_finalizations"] == 0
    assert report["counts"]["conflict_finalizations"] == 0
    assert after == before

    manifest_path = tmp_path / "study-manifest.json"
    manifest_path.write_text(manifest.canonical_payload_json, encoding="utf-8")
    exit_code = readiness_cli.main(
        [
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
    )
    cli_report = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert cli_report["ok"] is True
    assert cli_report["counts"]["source_cells_expected"] == 1260


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
    assert report["replicas"][1]["source_cells"] == READINESS_SOURCE_CELLS - 10


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
