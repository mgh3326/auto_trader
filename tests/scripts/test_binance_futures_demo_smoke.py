"""ROB-298 PR 2 — Futures Demo smoke CLI tests.

Covers:
  * Default-disabled clean exit (no env → exit 0, single log line).
  * ``BINANCE_TESTNET_*`` env must NOT activate the Futures Demo CLI.
  * ``--plan-only`` produces source-labeled evidence (no HTTP).
  * ``--plan-only`` rejects ``BTCUSDT`` (excluded from the allowlist).
  * Excluded list wins over ``--allow-symbol`` override.
  * Modes are mutually exclusive at argparse level.
  * ``--confirm`` without credentials refuses cleanly (no HTTP).
  * Plan-only path constructs no ``httpx.AsyncClient``.
  * Enabled + no flag exits with guidance text.
"""

from __future__ import annotations

import json
import logging
from typing import Any

import pytest

import scripts.binance_futures_demo_smoke as smoke


_FUTURES_DEMO_ENV_VARS = (
    "BINANCE_FUTURES_DEMO_ENABLED",
    "BINANCE_FUTURES_DEMO_API_KEY",
    "BINANCE_FUTURES_DEMO_API_SECRET",
    "BINANCE_FUTURES_DEMO_BASE_URL",
)

_SIBLING_ENV_VARS = (
    "BINANCE_TESTNET_ENABLED",
    "BINANCE_TESTNET_API_KEY",
    "BINANCE_TESTNET_API_SECRET",
    "BINANCE_SPOT_DEMO_ENABLED",
    "BINANCE_SPOT_DEMO_API_KEY",
    "BINANCE_SPOT_DEMO_API_SECRET",
)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _FUTURES_DEMO_ENV_VARS + _SIBLING_ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def test_default_disabled_clean_exit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """No env at all → exit 0 + single disabled log line, zero HTTP."""
    _clear_env(monkeypatch)
    caplog.set_level(logging.INFO, logger="scripts.binance_futures_demo_smoke")
    exit_code = smoke.main([])
    assert exit_code == 0
    messages = [r.message for r in caplog.records]
    disabled_lines = [m for m in messages if "futures demo disabled" in m]
    assert len(disabled_lines) >= 1, messages


def test_default_disabled_with_testnet_env_does_not_activate(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """``BINANCE_TESTNET_*`` env must not activate the Futures Demo CLI."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_TESTNET_ENABLED", "true")
    monkeypatch.setenv("BINANCE_TESTNET_API_KEY", "testnet-key")
    monkeypatch.setenv("BINANCE_TESTNET_API_SECRET", "testnet-secret")
    caplog.set_level(logging.INFO, logger="scripts.binance_futures_demo_smoke")
    exit_code = smoke.main([])
    assert exit_code == 0
    messages = [r.message for r in caplog.records]
    assert any("futures demo disabled" in m for m in messages), messages


def test_plan_only_emits_source_labeled_evidence(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--plan-only`` produces a stdout JSON line labeled ``source: futures_demo``."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    exit_code = smoke.main(
        [
            "--plan-only",
            "--symbol",
            "XRPUSDT",
            "--side",
            "BUY",
            "--leverage",
            "1",
            "--cap-usdt",
            "10",
        ]
    )
    assert exit_code == 0
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["event"] == "futures_demo_plan"
    plan = payload["plan"]
    assert plan["source"] == "futures_demo"
    assert plan["venue"] == "binance"
    assert plan["product"] == "usdm_futures"
    assert plan["symbol"] == "XRPUSDT"
    assert plan["leverage"] == 1
    assert plan["cap_usdt"] == "10"


def test_plan_only_rejects_btcusdt(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--symbol BTCUSDT`` is in the excluded list → exit 1 with rejection."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    exit_code = smoke.main(["--plan-only", "--symbol", "BTCUSDT"])
    assert exit_code == 1
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["event"] == "futures_demo_plan_rejected"
    assert payload["reason"] == "BinanceFuturesDemoUnsupportedSymbol"
    assert payload["symbol"] == "BTCUSDT"


def test_plan_only_btcusdt_not_unexcluded_by_override(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--allow-symbol BTCUSDT`` cannot re-enable an excluded symbol."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    exit_code = smoke.main(
        [
            "--plan-only",
            "--symbol",
            "BTCUSDT",
            "--allow-symbol",
            "BTCUSDT",
        ]
    )
    assert exit_code == 1
    out = capsys.readouterr().out.strip()
    payload = json.loads(out)
    assert payload["event"] == "futures_demo_plan_rejected"
    assert payload["reason"] == "BinanceFuturesDemoUnsupportedSymbol"
    assert payload["symbol"] == "BTCUSDT"


def test_modes_are_mutually_exclusive(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--confirm`` and ``--plan-only`` are mutually exclusive at argparse."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_KEY", "key")
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_API_SECRET", "secret")
    with pytest.raises(SystemExit) as excinfo:
        smoke.main(["--confirm", "--plan-only"])
    # argparse uses 2 for argument errors.
    assert excinfo.value.code == 2
    err = capsys.readouterr().err
    assert (
        "not allowed with argument" in err
        or "mutually exclusive" in err.lower()
    )


def test_confirm_without_credentials_refuses_cleanly(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``--confirm`` with env on but credentials missing → exit 1, no HTTP."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")

    import httpx

    def _boom(*args: Any, **kwargs: Any) -> Any:
        raise AssertionError(
            "--confirm with missing credentials must not construct an "
            "httpx.AsyncClient. Credential check must precede transport."
        )

    monkeypatch.setattr(httpx, "AsyncClient", _boom)
    caplog.set_level(logging.ERROR, logger="scripts.binance_futures_demo_smoke")
    exit_code = smoke.main(["--confirm", "--symbol", "XRPUSDT"])
    assert exit_code == 1
    error_messages = [
        r.message for r in caplog.records if r.levelno >= logging.ERROR
    ]
    assert any(
        "credentials" in m.lower() or "refused" in m.lower()
        for m in error_messages
    ), error_messages


def test_no_httpx_request_in_plan_only(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """``--plan-only`` makes no httpx request (no transport instantiation)."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")

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
                "XRPUSDT",
            ]
        )
        assert exit_code == 0
        out = capsys.readouterr().out.strip()
        assert "futures_demo_plan" in out
    finally:
        monkeypatch.setattr(httpx, "AsyncClient", real_async_client)


def test_enabled_no_action_exits_with_guidance(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """Env on but no action flag → exit 0 + guidance message; zero side effects."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_FUTURES_DEMO_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="scripts.binance_futures_demo_smoke")
    exit_code = smoke.main([])
    assert exit_code == 0
    messages = [r.message for r in caplog.records]
    assert any(
        "--plan-only" in m or "--preflight" in m or "--confirm" in m
        for m in messages
    ), messages
