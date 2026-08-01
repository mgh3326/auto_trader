from __future__ import annotations

import datetime as dt
import json
import os
import sqlite3
from pathlib import Path

import pytest

from app.services.brokers.binance import r4_p0_hardening as hardening_module
from app.services.brokers.binance.r4_p0_collector import (
    COLLECTOR_VERSION,
    REQUIRED_ACTIVE_SOURCES,
    SIGNAL_SYMBOLS,
    AppendOnlyPITStore,
)
from app.services.brokers.binance.r4_p0_hardening import (
    STUDY_MANIFEST_SCHEMA_VERSION,
    AlertDispatcher,
    EpochLedger,
    EpochPolicy,
    StudyManifest,
    canonical_json,
    parse_study_manifest,
    sha256_text,
)
from scripts import r4_p0_watchdog

T0 = dt.datetime(2026, 8, 2, tzinfo=dt.UTC)
VERIFIED_AT = T0 - dt.timedelta(hours=5)
EXPECTED_CODE_HASH = "a" * 40
POLICY = EpochPolicy(
    required_sources=("binance_usdm.basis",),
    symbols=("XRPUSDT",),
    study_id="study-t0-preflight",
    policy_hash="a" * 64,
    t0=T0,
)


def _watchdog_manifest() -> StudyManifest:
    payload = {
        "schema_version": STUDY_MANIFEST_SCHEMA_VERSION,
        "effective_at": "2026-08-01T00:00:00Z",
        "study_id": POLICY.study_id,
        "contract_hash": POLICY.policy_hash,
        "t0": "2026-08-02T00:00:00Z",
        "required_sources": list(POLICY.required_sources),
        "symbols": list(POLICY.symbols),
        "source_manifest_hash": POLICY.source_manifest_hash,
    }
    return parse_study_manifest(
        payload,
        expected_sha256=sha256_text(canonical_json(payload)),
        expected_sources=POLICY.required_sources,
        expected_symbols=POLICY.symbols,
    )


def _cli_manifest() -> StudyManifest:
    policy = EpochPolicy(
        required_sources=tuple(sorted(REQUIRED_ACTIVE_SOURCES)),
        symbols=tuple(sorted(SIGNAL_SYMBOLS)),
        study_id=POLICY.study_id,
        policy_hash=POLICY.policy_hash,
        t0=POLICY.t0,
    )
    payload = {
        "schema_version": STUDY_MANIFEST_SCHEMA_VERSION,
        "effective_at": "2026-08-01T00:00:00Z",
        "study_id": policy.study_id,
        "contract_hash": policy.policy_hash,
        "t0": "2026-08-02T00:00:00Z",
        "required_sources": list(policy.required_sources),
        "symbols": list(policy.symbols),
        "source_manifest_hash": policy.source_manifest_hash,
    }
    return parse_study_manifest(
        payload,
        expected_sha256=sha256_text(canonical_json(payload)),
        expected_sources=tuple(sorted(REQUIRED_ACTIVE_SOURCES)),
        expected_symbols=tuple(sorted(SIGNAL_SYMBOLS)),
    )


def _write_replica(
    root: Path,
    *,
    name: str,
    policy: EpochPolicy = POLICY,
    code_hash: str = EXPECTED_CODE_HASH,
    heartbeat_at: dt.datetime = VERIFIED_AT,
    study_manifest: StudyManifest | None = None,
) -> Path:
    with AppendOnlyPITStore(root, study_manifest=study_manifest) as store:
        ledger = EpochLedger(store._db, policy)  # noqa: SLF001
        ledger.append_process_version(
            collector_instance_id=f"replica-{name}",
            run_id=f"run-{name}",
            started_at=heartbeat_at - dt.timedelta(minutes=1),
            code_hash=code_hash,
            collector_version=COLLECTOR_VERSION,
            study_manifest_sha256=(
                study_manifest.content_sha256 if study_manifest is not None else None
            ),
        )
        ledger.append_heartbeat(
            collector_instance_id=f"replica-{name}",
            run_id=f"run-{name}",
            observed_at=heartbeat_at,
            health={"ok": True},
        )
        return store.path


def _report(
    paths: list[Path],
    *,
    verified_at: dt.datetime = VERIFIED_AT,
) -> dict:
    return r4_p0_watchdog.t0_preflight_report(
        paths,
        POLICY,
        verified_at=verified_at,
        expected_code_hash=EXPECTED_CODE_HASH,
        stale_after_seconds=120,
    )


def _process_version_count(path: Path) -> int:
    with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as connection:
        return connection.execute(
            "SELECT COUNT(*) FROM collector_process_versions"
        ).fetchone()[0]


@pytest.mark.unit
def test_t0_preflight_all_gates_pass(tmp_path) -> None:
    paths = [
        _write_replica(tmp_path / "a", name="a"),
        _write_replica(tmp_path / "b", name="b"),
    ]

    report = _report(paths)

    assert report["ok"] is True
    assert report["gates"] == {
        "v_le_t0_minus_4h": True,
        "code_hash_match_all": True,
        "healthy_replica_count_ge_2": True,
        "stamped_t0_matches_all": True,
    }
    assert report["t0_utc"] == "2026-08-02T00:00:00.000000Z"
    assert report["t0_minus_4h"] == "2026-08-01T20:00:00.000000Z"
    assert len(report["replicas"]) == 2
    assert all(replica["status"] == "HEALTHY" for replica in report["replicas"])
    assert all(
        replica["stamped_t0_utc"] == report["t0_utc"] for replica in report["replicas"]
    )


@pytest.mark.unit
def test_t0_preflight_fails_v_after_warmup_cutoff(tmp_path) -> None:
    paths = [
        _write_replica(tmp_path / "a", name="a"),
        _write_replica(tmp_path / "b", name="b"),
    ]

    report = _report(
        paths, verified_at=T0 - dt.timedelta(hours=4) + dt.timedelta(seconds=1)
    )

    assert report["gates"]["v_le_t0_minus_4h"] is False
    assert report["ok"] is False
    assert "새 커밋과 새 T0" in report["next_action"]


@pytest.mark.unit
def test_t0_preflight_allows_v_exactly_at_warmup_cutoff(tmp_path) -> None:
    boundary = T0 - dt.timedelta(hours=4)
    paths = [
        _write_replica(tmp_path / "a", name="a", heartbeat_at=boundary),
        _write_replica(tmp_path / "b", name="b", heartbeat_at=boundary),
    ]

    report = _report(paths, verified_at=boundary)

    assert report["gates"]["v_le_t0_minus_4h"] is True
    assert report["ok"] is True


@pytest.mark.unit
def test_t0_preflight_fails_code_hash_mismatch(tmp_path) -> None:
    paths = [
        _write_replica(tmp_path / "a", name="a"),
        _write_replica(tmp_path / "b", name="b", code_hash="b" * 40),
    ]

    report = _report(paths)

    assert report["gates"]["code_hash_match_all"] is False
    assert report["ok"] is False


@pytest.mark.unit
def test_t0_preflight_fails_when_fewer_than_two_replicas_are_healthy(
    tmp_path,
) -> None:
    paths = [
        _write_replica(tmp_path / "a", name="a"),
        _write_replica(
            tmp_path / "b",
            name="b",
            heartbeat_at=VERIFIED_AT - dt.timedelta(minutes=3),
        ),
    ]

    report = _report(paths)

    assert report["gates"]["healthy_replica_count_ge_2"] is False
    assert report["ok"] is False


@pytest.mark.unit
def test_t0_preflight_fails_stamped_t0_mismatch(tmp_path) -> None:
    other_t0_policy = EpochPolicy(
        required_sources=POLICY.required_sources,
        symbols=POLICY.symbols,
        study_id=POLICY.study_id,
        policy_hash=POLICY.policy_hash,
        t0=T0 + dt.timedelta(hours=4),
    )
    paths = [
        _write_replica(tmp_path / "a", name="a"),
        _write_replica(tmp_path / "b", name="b", policy=other_t0_policy),
    ]

    report = _report(paths)

    assert report["gates"]["stamped_t0_matches_all"] is False
    assert report["ok"] is False


@pytest.mark.unit
def test_t0_preflight_exit_codes_pass_and_fail(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    manifest = _cli_manifest()
    manifest_path = tmp_path / "study-manifest.json"
    manifest_path.write_text(manifest.canonical_payload_json, encoding="utf-8")
    policy = manifest.epoch_policy
    paths = [
        _write_replica(
            tmp_path / "a",
            name="a",
            policy=policy,
            study_manifest=manifest,
        ),
        _write_replica(
            tmp_path / "b",
            name="b",
            policy=policy,
            study_manifest=manifest,
        ),
    ]
    argv = [
        "--t0-preflight",
        "--artifact",
        str(paths[0]),
        "--artifact",
        str(paths[1]),
        "--study-manifest",
        str(manifest_path),
        "--study-manifest-sha256",
        manifest.content_sha256,
        "--state-root",
        str(tmp_path / "must-not-exist"),
    ]
    monkeypatch.setattr(r4_p0_watchdog, "runtime_code_hash", lambda: EXPECTED_CODE_HASH)
    monkeypatch.setattr(r4_p0_watchdog, "utc_now", lambda: VERIFIED_AT)

    assert r4_p0_watchdog.main(argv) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert not (tmp_path / "must-not-exist").exists()

    monkeypatch.setattr(
        r4_p0_watchdog,
        "utc_now",
        lambda: T0 - dt.timedelta(hours=4) + dt.timedelta(seconds=1),
    )
    assert r4_p0_watchdog.main(argv) == 1
    assert json.loads(capsys.readouterr().out)["ok"] is False


@pytest.mark.unit
def test_t0_preflight_opens_collector_artifacts_read_only(tmp_path) -> None:
    paths = [
        _write_replica(tmp_path / "a", name="a"),
        _write_replica(tmp_path / "b", name="b"),
    ]
    before = {
        path: (
            path.stat().st_mtime_ns,
            path.stat().st_size,
            _process_version_count(path),
        )
        for path in paths
    }
    for path in paths:
        os.chmod(path, 0o444)

    try:
        report = _report(paths)
    finally:
        for path in paths:
            os.chmod(path, 0o644)

    after = {
        path: (
            path.stat().st_mtime_ns,
            path.stat().st_size,
            _process_version_count(path),
        )
        for path in paths
    }
    assert report["ok"] is True
    assert after == before


@pytest.mark.unit
@pytest.mark.asyncio
async def test_watchdog_fault_injection_pages_loss_stall_and_version_drift(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    manifest = _watchdog_manifest()
    stores: list[AppendOnlyPITStore] = []
    paths: list[Path] = []
    now = T0 + dt.timedelta(hours=8, minutes=1)
    try:
        for name, code_hash, heartbeat_at in (
            ("a", EXPECTED_CODE_HASH, now - dt.timedelta(seconds=10)),
            ("b", "b" * 40, now - dt.timedelta(minutes=5)),
        ):
            store = AppendOnlyPITStore(tmp_path / name, study_manifest=manifest)
            stores.append(store)
            ledger = EpochLedger(store._db, POLICY)  # noqa: SLF001
            ledger.append_process_version(
                collector_instance_id=f"replica-{name}",
                run_id=f"run-{name}",
                started_at=heartbeat_at - dt.timedelta(minutes=1),
                code_hash=code_hash,
                collector_version=COLLECTOR_VERSION,
                study_manifest_sha256=manifest.content_sha256,
            )
            ledger.append_heartbeat(
                collector_instance_id=f"replica-{name}",
                run_id=f"run-{name}",
                observed_at=heartbeat_at,
                health={"ok": True},
            )
            paths.append(store.path)

        missing = tmp_path / "missing" / "r4_p0_collector.sqlite3"
        paths.append(missing)

        delivered: list[str] = []

        class FakeResponse:
            def raise_for_status(self) -> None:
                return None

        class FakeClient:
            def __init__(self, *_args, **_kwargs) -> None:
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *_args) -> None:
                return None

            async def post(self, url: str, **_kwargs):
                delivered.append(url)
                return FakeResponse()

        monkeypatch.setattr(hardening_module.httpx, "AsyncClient", FakeClient)
        with AppendOnlyPITStore(tmp_path / "watchdog") as state:
            ledger = EpochLedger(state._db, POLICY)  # noqa: SLF001
            dispatcher = AlertDispatcher(
                ledger,
                ("https://alerts.example.invalid/r4",),
            )
            detection = await r4_p0_watchdog._watch_once(  # noqa: SLF001
                artifact_paths=paths,
                policy=POLICY,
                manifest=manifest,
                expected_code_hash=EXPECTED_CODE_HASH,
                stale_after_seconds=120,
                finalizer_grace_seconds=30,
                minimum_healthy_replicas=2,
                dispatcher=dispatcher,
                now=now,
            )
            events = list(
                ledger.db.execute(
                    "SELECT event_type, delivery_status, payload_json "
                    "FROM collector_alert_events ORDER BY append_id"
                )
            )
        assert detection["emitted_alert_types"] == [
            "COLLECTOR_PROVENANCE_MISMATCH",
            "COLLECTOR_REDUNDANCY_LOST",
            "FINALIZER_STALLED",
        ]
        availability = detection["availability"]
        assert availability["healthy_replica_count"] == 1
        assert availability["version_stamp_match"] is False
        statuses = {
            replica["artifact"]: replica["status"]
            for replica in availability["replicas"]
        }
        assert statuses[str(paths[0].resolve())] == "HEALTHY"
        assert statuses[str(paths[1].resolve())] == "VERSION_MISMATCH"
        assert statuses[str(missing.resolve())] == "UNAVAILABLE"
        finalization = detection["finalization"]
        assert finalization["all_replicas_witnessed"] is False
        assert finalization["topology_ok"] is False
        assert finalization["missing_symbols"] == list(POLICY.symbols)
        assert delivered == ["https://alerts.example.invalid/r4"] * 3
        assert (
            sum(
                row["event_type"] == "WEBHOOK_0"
                and row["delivery_status"] == "SUCCEEDED"
                for row in events
            )
            == 3
        )
        raised_types = [
            json.loads(row["payload_json"])["alert_type"]
            for row in events
            if row["event_type"] == "ALERT_RAISED"
        ]
        assert raised_types == detection["emitted_alert_types"]
    finally:
        for store in stores:
            store.close()
