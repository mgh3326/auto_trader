from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.brokers.binance.r4_p0_collector import (
    REQUIRED_ACTIVE_SOURCES,
    SIGNAL_SYMBOLS,
)
from app.services.brokers.binance.r4_p0_hardening import (
    STUDY_MANIFEST_SCHEMA_VERSION,
    EpochPolicy,
    canonical_json,
    sha256_text,
)
from scripts import r4_p0_collector as collector_cli
from scripts import r4_p0_watchdog as watchdog_cli


def _write_manifest(tmp_path: Path) -> tuple[Path, str]:
    policy = EpochPolicy(
        required_sources=tuple(sorted(REQUIRED_ACTIVE_SOURCES)),
        symbols=tuple(sorted(SIGNAL_SYMBOLS)),
        study_id="TEST-R4-P0-CLI",
        policy_hash="a" * 64,
        t0=dt.datetime(2026, 7, 30, 8, tzinfo=dt.UTC),
    )
    payload = {
        "schema_version": STUDY_MANIFEST_SCHEMA_VERSION,
        "effective_at": "2026-07-30T00:00:00Z",
        "study_id": policy.study_id,
        "contract_hash": policy.policy_hash,
        "t0": "2026-07-30T08:00:00Z",
        "required_sources": list(policy.required_sources),
        "symbols": list(policy.symbols),
        "source_manifest_hash": policy.source_manifest_hash,
    }
    pin = sha256_text(canonical_json(payload))
    path = (tmp_path / "study-manifest.json").resolve()
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path, pin


@pytest.mark.unit
@pytest.mark.parametrize(
    "argv",
    (
        ["r4_p0_collector.py", "--probe", "--duration", "1"],
        [
            "r4_p0_collector.py",
            "--probe",
            "--duration",
            "1",
            "--study-manifest",
            "/tmp/manifest.json",
        ],
    ),
)
def test_collector_network_modes_require_manifest_pair(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.delenv(collector_cli.STUDY_MANIFEST_ENV, raising=False)
    monkeypatch.delenv(
        collector_cli.STUDY_MANIFEST_SHA256_ENV,
        raising=False,
    )
    with pytest.raises(SystemExit, match="study-manifest"):
        collector_cli.main()


@pytest.mark.unit
def test_collector_dry_run_needs_no_manifest_or_write(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(sys, "argv", ["r4_p0_collector.py"])
    monkeypatch.delenv(collector_cli.STUDY_MANIFEST_ENV, raising=False)
    monkeypatch.delenv(
        collector_cli.STUDY_MANIFEST_SHA256_ENV,
        raising=False,
    )
    monkeypatch.setattr(collector_cli, "runtime_code_hash", lambda: "a" * 40)

    assert collector_cli.main() == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "dry_run"
    assert payload["database_write"] is False
    assert payload["study_manifest"] is None
    assert payload["study_manifest_required_for_network"] is True


@pytest.mark.unit
@pytest.mark.asyncio
@pytest.mark.parametrize("probe", (True, False))
async def test_collector_passes_exact_manifest_and_isolates_probe_webhooks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    probe: bool,
) -> None:
    manifest_path, pin = _write_manifest(tmp_path)
    manifest = collector_cli.load_study_manifest(
        manifest_path,
        expected_sha256=pin,
        expected_sources=tuple(sorted(REQUIRED_ACTIVE_SOURCES)),
        expected_symbols=tuple(sorted(SIGNAL_SYMBOLS)),
    )
    captured: dict[str, Any] = {}
    observed_at = dt.datetime(2026, 7, 30, 13, 17, tzinfo=dt.UTC)

    class FakeStore:
        def __init__(
            self,
            root: Path,
            *,
            study_manifest,
        ) -> None:
            captured["store_root"] = root
            captured["store_manifest"] = study_manifest

        def __enter__(self) -> FakeStore:
            return self

        def __exit__(self, *_exc: object) -> None:
            return None

        def audit(self) -> dict[str, bool]:
            return {"ok": True}

    class FakeCollector:
        def __init__(self, config, _store) -> None:
            captured["config"] = config
            self.stop = SimpleNamespace(set=lambda: None)

        async def run(self) -> None:
            return None

        def health(self) -> dict[str, bool]:
            return {"ok": True}

    class FakeLoop:
        def add_signal_handler(self, *_args: object) -> None:
            return None

    monkeypatch.setattr(collector_cli, "AppendOnlyPITStore", FakeStore)
    monkeypatch.setattr(collector_cli, "BinanceR4P0Collector", FakeCollector)
    monkeypatch.setattr(
        collector_cli.asyncio,
        "get_running_loop",
        lambda: FakeLoop(),
    )
    monkeypatch.setattr(collector_cli, "utc_now", lambda: observed_at)
    monkeypatch.setenv(
        collector_cli.ALERT_WEBHOOKS_ENV,
        "https://alerts.example.test/r4",
    )
    args = argparse.Namespace(
        alert_webhook_urls=None,
        artifact_root=str(tmp_path / "artifact"),
        collector_id="replica-a",
        duration=1.0,
        minimum_healthy_replicas=2,
        probe=probe,
        replica_artifact=[],
        status_seconds=30.0,
    )

    assert await collector_cli._run_collector(args, manifest) == 0

    config = captured["config"]
    assert captured["store_manifest"] == manifest
    assert config.study_manifest == manifest
    assert config.epoch_policy == manifest.epoch_policy
    assert config.epoch_observation_start == (observed_at if probe else None)
    assert config.alert_webhook_urls == (
        () if probe else ("https://alerts.example.test/r4",)
    )


@pytest.mark.unit
@pytest.mark.parametrize(
    "argv",
    (
        ["r4_p0_watchdog.py", "--run"],
        [
            "r4_p0_watchdog.py",
            "--run",
            "--study-manifest",
            "/tmp/manifest.json",
        ],
    ),
)
def test_watchdog_run_requires_same_manifest_pair(
    monkeypatch: pytest.MonkeyPatch,
    argv: list[str],
) -> None:
    monkeypatch.setattr(sys, "argv", argv)
    monkeypatch.delenv(watchdog_cli.STUDY_MANIFEST_ENV, raising=False)
    monkeypatch.delenv(
        watchdog_cli.STUDY_MANIFEST_SHA256_ENV,
        raising=False,
    )
    with pytest.raises(SystemExit, match="study-manifest"):
        watchdog_cli.main()
