"""Offline CLI contracts for the NHPLUG period collector."""

from __future__ import annotations

import logging

import pytest

from app.services.brokers.nhplug.live_period_collect import (
    CollectionResult,
    NHPlugPeriodSettingsLoadError,
)
from scripts import nhplug_collect_period as cli

pytestmark = pytest.mark.unit


def test_cli_rejects_prod_names_missing_period_and_subfloor_rate() -> None:
    with pytest.raises(SystemExit):
        cli.parse_args(
            [
                "--env-file",
                ".env.prod.nhplug",
                "--token-cache",
                ".tokens.json",
                "--market",
                "kr",
                "--start-date",
                "20260801",
                "--end-date",
                "20260831",
            ]
        )
    with pytest.raises(SystemExit):
        cli.parse_args(
            [
                "--env-file",
                ".env.nhplug-live",
                "--token-cache",
                ".tokens.json",
                "--market",
                "kr",
                "--end-date",
                "20260831",
            ]
        )
    with pytest.raises(SystemExit):
        cli.parse_args(
            [
                "--env-file",
                ".env.nhplug-live",
                "--token-cache",
                ".tokens.json",
                "--market",
                "kr",
                "--start-date",
                "20260801",
                "--end-date",
                "20260831",
                "--rate-seconds",
                "0.19",
            ]
        )


@pytest.mark.asyncio
async def test_cli_defaults_to_dry_run_until_commit_is_explicit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    class CapturingCollector:
        async def collect(self, **kwargs: object) -> CollectionResult:
            captured.update(kwargs)
            return CollectionResult(
                market="kr",
                total_symbols=1,
                processed_symbols=1,
                rows_received=1,
                rows_inserted=0,
                rows_conflict_skipped=0,
                invalid_rows=0,
                failures=(),
                verification=(),
                verification_failures=(),
                resumed_from=None,
                elapsed_seconds=0.0,
                commit=bool(kwargs["commit"]),
                persistence_status="READY",
            )

    monkeypatch.setattr(cli, "arm_scoped_environment", lambda **_: None)
    monkeypatch.setattr(
        cli, "build_default_collector", lambda **_: CapturingCollector()
    )

    result = await cli.main(
        [
            "--env-file",
            ".env.nhplug-live",
            "--token-cache",
            ".tokens.json",
            "--market",
            "kr",
            "--symbols",
            "005930",
            "--start-date",
            "20260801",
            "--end-date",
            "20260831",
        ]
    )

    assert result == 0
    assert captured["commit"] is False
    assert captured["symbols"] == ["005930"]


@pytest.mark.asyncio
async def test_cli_marks_scoped_configuration_failures_with_settings_load_stage(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    def fail_settings_load(**_: object) -> None:
        raise NHPlugPeriodSettingsLoadError("fixture settings failure")

    monkeypatch.setattr(cli, "arm_scoped_environment", fail_settings_load)
    caplog.set_level(logging.ERROR)

    result = await cli.main(
        [
            "--env-file",
            ".env.nhplug-live",
            "--token-cache",
            ".tokens.json",
            "--market",
            "kr",
            "--symbols",
            "005930",
            "--start-date",
            "20260801",
            "--end-date",
            "20260831",
        ]
    )

    assert result == 2
    assert "stage=settings_load error_code=NHPlugPeriodSettingsLoadError" in caplog.text
