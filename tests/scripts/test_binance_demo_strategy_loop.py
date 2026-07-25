"""ROB-993 — Binance Demo strategy loop CLI tests.

Covers:
  * Default-disabled clean exit (no env -> exit 0, single log line).
  * ``--readiness`` reports env state without HTTP/credentials.
  * Modes are mutually exclusive at argparse level.
  * Enabled + no flag exits with guidance text.
  * ``--paper-signal`` builds a Signal from the CLI flags.
"""

from __future__ import annotations

import logging

import pytest

import scripts.binance_demo_strategy_loop as cli

_ENV_VARS = (
    "BINANCE_DEMO_STRATEGY_LOOP_ENABLED",
    "BINANCE_FUTURES_DEMO_ENABLED",
    "BINANCE_FUTURES_DEMO_API_KEY",
    "BINANCE_FUTURES_DEMO_API_SECRET",
    "BINANCE_FUTURES_DEMO_BASE_URL",
)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _ENV_VARS:
        monkeypatch.delenv(key, raising=False)


def test_default_disabled_clean_exit(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_env(monkeypatch)
    caplog.set_level(logging.INFO, logger="scripts.binance_demo_strategy_loop")
    exit_code = cli.main([])
    assert exit_code == 0
    messages = [r.message for r in caplog.records]
    assert any("strategy loop disabled" in m for m in messages), messages


def test_default_disabled_with_once_flag_still_disabled(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """The disabled gate is checked before mode dispatch, even with --once."""
    _clear_env(monkeypatch)
    caplog.set_level(logging.INFO, logger="scripts.binance_demo_strategy_loop")
    exit_code = cli.main(["--once"])
    assert exit_code == 0
    messages = [r.message for r in caplog.records]
    assert any("strategy loop disabled" in m for m in messages), messages


def test_readiness_reports_disabled(
    capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    _clear_env(monkeypatch)
    exit_code = cli.main(["--readiness"])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert '"BINANCE_DEMO_STRATEGY_LOOP_ENABLED": false' in out


def test_readiness_reports_enabled(
    capsys: pytest.CaptureFixture[str], monkeypatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_DEMO_STRATEGY_LOOP_ENABLED", "true")
    exit_code = cli.main(["--readiness"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert '"BINANCE_DEMO_STRATEGY_LOOP_ENABLED": true' in out


def test_modes_are_mutually_exclusive() -> None:
    with pytest.raises(SystemExit):
        cli.main(["--once", "--loop"])


def test_enabled_no_flag_prints_guidance(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_DEMO_STRATEGY_LOOP_ENABLED", "true")
    caplog.set_level(logging.INFO, logger="scripts.binance_demo_strategy_loop")
    exit_code = cli.main([])
    assert exit_code == 0
    messages = [r.message for r in caplog.records]
    assert any("no action requested" in m for m in messages), messages


def test_build_paper_signal_uses_cli_flags() -> None:
    args = cli._parse_args(
        ["--paper-signal", "--paper-symbol", "dogeusdt", "--paper-side", "SELL"]
    )
    signal = cli._build_paper_signal(args, decision_ts=1_700_000_000_000)
    assert signal.symbol == "DOGEUSDT"
    assert signal.side == "SELL"
    assert signal.decision_ts == 1_700_000_000_000
    assert signal.strategy_id == "rob-993-paper-signal"
