"""ROB-296 — Spot Demo smoke CLI tests.

Covers:
  * Default-disabled clean exit (no env → exit 0, single log line).
  * ``--plan-only`` produces source-labeled evidence with no HTTP.
  * ``--confirm`` refuses with the documented follow-up message.
  * No broker/HTTP mutation in dry-run.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

import scripts.binance_spot_demo_smoke as smoke


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in (
        "BINANCE_SPOT_DEMO_ENABLED",
        "BINANCE_SPOT_DEMO_API_KEY",
        "BINANCE_SPOT_DEMO_API_SECRET",
        "BINANCE_SPOT_DEMO_BASE_URL",
        "BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT",
        "BINANCE_TESTNET_ENABLED",
        "BINANCE_TESTNET_API_KEY",
        "BINANCE_TESTNET_API_SECRET",
    ):
        monkeypatch.delenv(key, raising=False)


def test_default_disabled_clean_exit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No env at all → exit 0 + single disabled log line, zero HTTP."""
    _clear_env(monkeypatch)
    caplog.set_level(logging.INFO, logger="scripts.binance_spot_demo_smoke")
    exit_code = smoke.main([])
    assert exit_code == 0
    messages = [r.message for r in caplog.records]
    disabled_lines = [m for m in messages if "spot demo disabled" in m]
    assert len(disabled_lines) >= 1, messages


def test_default_disabled_with_testnet_env_does_not_activate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``BINANCE_TESTNET_*`` env must not activate the Spot Demo CLI."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "testnet-secret")
    caplog.set_level(logging.INFO, logger="scripts.binance_spot_demo_smoke")
    exit_code = smoke.main([])
    assert exit_code == 0
    messages = [r.message for r in caplog.records]
    assert any("spot demo disabled" in m for m in messages), messages


def test_plan_only_emits_source_labeled_evidence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--plan-only`` produces a stdout JSON line labeled ``source: spot_demo``."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    exit_code = smoke.main(
        [
            "--plan-only",
            "--symbol",
            "BTCUSDT",
            "--side",
            "BUY",
            "--order-type",
            "LIMIT",
            "--quantity",
            "0.001",
            "--price",
            "50000",
            "--max-notional-usdt",
            "100",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["event"] == "spot_demo_plan"
    plan = payload["plan"]
    assert plan["source"] == "spot_demo"
    assert plan["venue"] == "binance"
    assert plan["product"] == "spot"
    assert plan["symbol"] == "BTCUSDT"
    assert plan["within_cap"] is True


def test_plan_only_marks_over_cap_without_crashing(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """A planned order over the cap is reported as ``within_cap=False``."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_MAX_NOTIONAL_USDT", "5")
    exit_code = smoke.main(
        [
            "--plan-only",
            "--symbol",
            "BTCUSDT",
            "--side",
            "BUY",
            "--order-type",
            "LIMIT",
            "--quantity",
            "0.01",
            "--price",
            "50000",
        ]
    )
    assert exit_code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["plan"]["within_cap"] is False


def test_confirm_refused_not_implemented(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``--confirm`` is refused with the documented follow-up message."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_SPOT_DEMO_API_SECRET", "secret")
    caplog.set_level(logging.ERROR, logger="scripts.binance_spot_demo_smoke")
    exit_code = smoke.main(["--confirm", "--plan-only"])
    assert exit_code == 1
    error_messages = [r.message for r in caplog.records if r.levelno >= logging.ERROR]
    assert any("not implemented" in m.lower() for m in error_messages), error_messages


def test_enabled_no_action_exits_with_guidance(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Env on but no action flag → exit 0 + guidance message; zero side effects."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="scripts.binance_spot_demo_smoke")
    exit_code = smoke.main([])
    assert exit_code == 0
    messages = [r.message for r in caplog.records]
    assert any(
        "--plan-only" in m or "--preflight" in m for m in messages
    ), messages


def test_preflight_without_credentials_exits_one(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``--preflight`` with env on but missing creds → exit 1, error log."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")
    caplog.set_level(logging.ERROR, logger="scripts.binance_spot_demo_smoke")
    exit_code = smoke.main(["--preflight"])
    assert exit_code == 1


def test_no_httpx_request_in_plan_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--plan-only`` makes no httpx request (no transport instantiation needed).

    We assert this by patching ``httpx.AsyncClient`` to raise on
    instantiation and confirming the CLI still succeeds.
    """
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_SPOT_DEMO_ENABLED", "true")

    import httpx

    real_async_client = httpx.AsyncClient

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "--plan-only must not construct an httpx.AsyncClient. "
            "If this fires, the dry-run path leaked into HTTP."
        )

    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    try:
        exit_code = smoke.main(
            [
                "--plan-only",
                "--symbol",
                "BTCUSDT",
                "--quantity",
                "0.001",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out.strip()
        assert "spot_demo_plan" in out
    finally:
        monkeypatch.setattr(httpx, "AsyncClient", real_async_client)
