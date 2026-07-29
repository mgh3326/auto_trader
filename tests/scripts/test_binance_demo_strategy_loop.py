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


# ---------------------------------------------------------------------------
# ROB-1145 — --strategy selector (replaces the hardcoded NullStrategy()).
# ---------------------------------------------------------------------------


def test_strategy_defaults_to_none_arg(monkeypatch: pytest.MonkeyPatch) -> None:
    """No --strategy flag -> args.strategy is None -> registry default (null)."""
    args = cli._parse_args(["--once"])
    assert args.strategy is None


def test_readiness_reports_default_strategy(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_DEMO_STRATEGY_LOOP_ENABLED", "true")
    exit_code = cli.main(["--readiness"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert '"strategy_requested": "null"' in out
    assert '"strategy_id": "null"' in out
    assert '"strategy_error": null' in out
    assert "last-bar-direction" in out


def test_readiness_reports_selected_strategy(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_DEMO_STRATEGY_LOOP_ENABLED", "true")
    exit_code = cli.main(["--readiness", "--strategy", "last-bar-direction"])
    assert exit_code == 0
    out = capsys.readouterr().out
    assert '"strategy_id": "last-bar-direction"' in out


def test_readiness_unknown_strategy_fails_closed(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_DEMO_STRATEGY_LOOP_ENABLED", "true")
    exit_code = cli.main(["--readiness", "--strategy", "not-a-strategy"])
    assert exit_code == 1
    out = capsys.readouterr().out
    assert '"strategy_id": null' in out
    assert "not-a-strategy" in out


def test_unknown_strategy_refused_before_any_client_construction(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """An unknown --strategy key must exit 1 with zero HTTP/DB — in
    particular before ``from_env()`` reads credentials."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_DEMO_STRATEGY_LOOP_ENABLED", "true")

    from app.services.brokers.binance.futures_demo import (
        execution_client as execution_client_mod,
    )

    def _explode(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("from_env must not be reached for an unknown strategy")

    monkeypatch.setattr(
        execution_client_mod.BinanceFuturesDemoExecutionClient,
        "from_env",
        classmethod(lambda cls: _explode()),
    )
    caplog.set_level(logging.ERROR, logger="scripts.binance_demo_strategy_loop")
    exit_code = cli.main(["--once", "--strategy", "nope"])
    assert exit_code == 1
    messages = [r.getMessage() for r in caplog.records]
    assert any("unknown strategy" in m and "nope" in m for m in messages), messages


def test_selected_strategy_is_passed_to_run_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The plugin the CLI resolves is the plugin run_tick receives — the
    ROB-1145 regression: scripts:289 used to hardcode NullStrategy()."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_DEMO_STRATEGY_LOOP_ENABLED", "true")

    import asyncio

    from app.services.brokers.binance.demo_strategy_loop import (
        orchestrator as orchestrator_mod,
    )
    from app.services.brokers.binance.demo_strategy_loop.strategy import (
        LastBarDirectionStrategy,
    )
    from app.services.brokers.binance.futures_demo import (
        execution_client as execution_client_mod,
    )

    seen: list[object] = []

    class _FakeExecution:
        @classmethod
        def from_env(cls) -> _FakeExecution:
            return cls()

        async def aclose(self) -> None:
            return None

    class _FakeMarketClient:
        base_url = __import__("httpx").URL("https://demo-fapi.binance.com")

        async def aclose(self) -> None:
            return None

    async def _fake_run_tick(**kwargs: object):
        seen.append(kwargs["strategy"])
        return orchestrator_mod.TickOutcome(
            decision_ts=1,
            signal=None,
            round_trip=None,
            blocked_reason="no_signal",
        )

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(
        execution_client_mod.BinanceFuturesDemoExecutionClient,
        "from_env",
        classmethod(lambda cls: _FakeExecution()),
    )
    monkeypatch.setattr(
        "app.services.brokers.binance.demo_strategy_loop.bars.build_bars_client",
        lambda *a, **k: _FakeMarketClient(),
    )
    monkeypatch.setattr(
        "app.services.brokers.binance.demo_strategy_loop.orchestrator.run_tick",
        _fake_run_tick,
    )
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", lambda: _FakeSession())

    args = cli._parse_args(["--once", "--strategy", "last-bar-direction"])
    exit_code, _ = asyncio.run(cli._run_tick(args))
    assert exit_code == 0
    assert len(seen) == 1
    assert isinstance(seen[0], LastBarDirectionStrategy)


def test_default_strategy_is_still_null_strategy_at_run_tick(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """ROB-1145 must not change the default posture: no --strategy flag
    still means NullStrategy (never emits a signal)."""
    _clear_env(monkeypatch)
    monkeypatch.setenv("BINANCE_DEMO_STRATEGY_LOOP_ENABLED", "true")

    import asyncio

    from app.services.brokers.binance.demo_strategy_loop import (
        orchestrator as orchestrator_mod,
    )
    from app.services.brokers.binance.demo_strategy_loop.strategy import NullStrategy
    from app.services.brokers.binance.futures_demo import (
        execution_client as execution_client_mod,
    )

    seen: list[object] = []

    class _FakeExecution:
        async def aclose(self) -> None:
            return None

    class _FakeMarketClient:
        base_url = __import__("httpx").URL("https://demo-fapi.binance.com")

        async def aclose(self) -> None:
            return None

    async def _fake_run_tick(**kwargs: object):
        seen.append(kwargs["strategy"])
        return orchestrator_mod.TickOutcome(
            decision_ts=1, signal=None, round_trip=None, blocked_reason="no_signal"
        )

    class _FakeSession:
        async def __aenter__(self) -> _FakeSession:
            return self

        async def __aexit__(self, *_exc: object) -> None:
            return None

    monkeypatch.setattr(
        execution_client_mod.BinanceFuturesDemoExecutionClient,
        "from_env",
        classmethod(lambda cls: _FakeExecution()),
    )
    monkeypatch.setattr(
        "app.services.brokers.binance.demo_strategy_loop.bars.build_bars_client",
        lambda *a, **k: _FakeMarketClient(),
    )
    monkeypatch.setattr(
        "app.services.brokers.binance.demo_strategy_loop.orchestrator.run_tick",
        _fake_run_tick,
    )
    monkeypatch.setattr("app.core.db.AsyncSessionLocal", lambda: _FakeSession())

    args = cli._parse_args(["--once"])
    exit_code, _ = asyncio.run(cli._run_tick(args))
    assert exit_code == 0
    assert isinstance(seen[0], NullStrategy)
