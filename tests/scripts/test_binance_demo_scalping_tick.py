"""ROB-307 follow-up — tests for the Demo scalping tick operator CLI.

The CLI is a thin, env-driven wrapper over ``run_demo_scalping_tick``: it
runs one gated tick, prints a single-line JSON summary to stdout (for an
external scheduler to parse), and maps the summary to an exit code. No real
orders, no network — the tick function is faked.

Exit codes: 0 for a disabled (gate-off) or clean tick; 1 when the tick ran
with per-symbol errors or the runner raised.
"""

from __future__ import annotations

import json

import pytest

from scripts import binance_demo_scalping_tick as cli


def test_exit_code_disabled_is_zero() -> None:
    assert cli.exit_code_for({"status": "disabled"}) == 0


def test_exit_code_ran_clean_is_zero() -> None:
    assert cli.exit_code_for({"status": "ran", "error_count": 0}) == 0


def test_exit_code_ran_with_errors_is_one() -> None:
    assert cli.exit_code_for({"status": "ran", "error_count": 2}) == 1


def test_main_prints_summary_and_returns_zero_when_disabled(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _fake_tick() -> dict:
        return {"status": "disabled", "base_enabled": False, "scheduler_enabled": False}

    monkeypatch.setattr(cli, "run_demo_scalping_tick", _fake_tick)
    rc = cli.main([])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "disabled"


def test_main_returns_one_when_tick_has_errors(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _fake_tick() -> dict:
        return {
            "status": "ran",
            "error_count": 1,
            "errors": ["enter spot/XRPUSDT: boom"],
        }

    monkeypatch.setattr(cli, "run_demo_scalping_tick", _fake_tick)
    rc = cli.main([])
    assert rc == 1
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "ran"
    assert out["error_count"] == 1


def test_main_prints_entered_reason_codes_when_present(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """ROB-907: the CLI is a thin JSON passthrough — a blocked entry's
    reason_codes (produced upstream by run_scalping_tick's TickSummary) must
    reach stdout unmodified so Prefect logs carry the diagnosis."""

    async def _fake_tick() -> dict:
        return {
            "status": "ran",
            "error_count": 0,
            "entered_count": 1,
            "entered": [["usdm_futures", "XRPUSDT", "blocked", ["spread_too_wide"]]],
            "errors": [],
        }

    monkeypatch.setattr(cli, "run_demo_scalping_tick", _fake_tick)
    rc = cli.main([])
    assert rc == 0
    out = json.loads(capsys.readouterr().out.strip())
    assert out["entered"] == [
        ["usdm_futures", "XRPUSDT", "blocked", ["spread_too_wide"]]
    ]


def test_main_returns_one_and_emits_error_json_on_exception(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    async def _boom() -> dict:
        raise RuntimeError("explode")

    monkeypatch.setattr(cli, "run_demo_scalping_tick", _boom)
    rc = cli.main([])
    assert rc == 1
    out = json.loads(capsys.readouterr().out.strip())
    assert out["status"] == "error"
    assert "RuntimeError" in out["error"]
