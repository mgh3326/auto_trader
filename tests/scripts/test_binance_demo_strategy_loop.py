"""ROB-993 — Binance Demo strategy loop CLI tests.

Covers:
  * Default-disabled clean exit (no env -> exit 0, single log line).
  * ``--readiness`` reports env state without HTTP/credentials.
  * Modes are mutually exclusive at argparse level.
  * Enabled + no flag exits with guidance text.
  * ``--paper-signal`` builds a Signal from the CLI flags.
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace

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


@pytest.mark.asyncio
async def test_data_unavailable_tick_does_not_advance_the_loop_cursor(monkeypatch):
    decision_ts = 1_700_000_000_000
    seen_cursors = []
    outcomes = (
        SimpleNamespace(
            decision_ts=decision_ts,
            blocked_reason="data_unavailable",
            symbol_decisions=(
                SimpleNamespace(
                    symbol="BTCUSDT",
                    reason="data_missing_bars",
                    missing_buckets=(decision_ts,),
                    error_class=None,
                ),
            ),
        ),
        SimpleNamespace(
            decision_ts=decision_ts,
            blocked_reason="no_signal",
            symbol_decisions=(),
        ),
    )

    async def run_tick(args, *, already_processed_decision_ts):
        seen_cursors.append(already_processed_decision_ts)
        return 0, decision_ts, outcomes[len(seen_cursors) - 1]

    sleeps = 0

    async def sleep(_seconds):
        nonlocal sleeps
        sleeps += 1
        if sleeps == 2:
            raise KeyboardInterrupt

    monkeypatch.delenv("BINANCE_TREND_ALERT_ENABLED", raising=False)
    monkeypatch.setattr(cli, "_run_tick", run_tick)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    args = SimpleNamespace(poll_interval_seconds=1)

    assert await cli._run_loop(args) == 0
    assert seen_cursors == [None, None]


@pytest.mark.asyncio
async def test_data_quality_alert_sends_once_per_episode_and_reminds_after_six_hours():
    from app.services.brokers.binance.demo_strategy_loop.alerting import (
        DataFailureEpisodes,
        DataQualityMonitor,
    )

    now = {"value": cli._now_utc()}
    sent = []

    async def send(failure):
        sent.append(failure)
        return True

    monitor = DataQualityMonitor(
        enabled=True,
        send=send,
        clock=lambda: now["value"],
    )
    episodes = DataFailureEpisodes()
    missing = SimpleNamespace(
        symbol="BTCUSDT", reason="data_missing_bars", detail="1730000000000"
    )
    assert episodes.update((missing,)) == ()
    failure = episodes.update((missing,))
    assert len(failure) == 1
    await monitor.tick_done(failure)
    await monitor.drain()
    await monitor.tick_done(failure)
    await monitor.drain()
    assert sent == list(failure)

    import datetime as dt

    now["value"] += dt.timedelta(hours=6)
    await monitor.tick_done(failure)
    await monitor.drain()
    assert sent == [failure[0], failure[0]]


@pytest.mark.asyncio
async def test_failed_data_alert_retries_after_five_minutes():
    from app.services.brokers.binance.demo_strategy_loop.alerting import (
        DataQualityMonitor,
        TickFailure,
    )

    now = {"value": cli._now_utc()}
    attempts = 0

    async def send(_failure):
        nonlocal attempts
        attempts += 1
        return attempts > 1

    monitor = DataQualityMonitor(
        enabled=True,
        send=send,
        clock=lambda: now["value"],
    )
    failure = TickFailure("BTCUSDT: data_fetch_failed", "data_quality:BTCUSDT")
    await monitor.tick_done((failure,))
    await monitor.drain()
    await monitor.tick_done((failure,))
    await monitor.drain()
    assert attempts == 1

    import datetime as dt

    now["value"] += dt.timedelta(minutes=5)
    await monitor.tick_done((failure,))
    await monitor.drain()
    assert attempts == 2


@pytest.mark.asyncio
async def test_late_delivery_from_cleared_episode_does_not_suppress_new_episode():
    from app.services.brokers.binance.demo_strategy_loop.alerting import (
        DataQualityMonitor,
        TickFailure,
    )

    started = asyncio.Event()
    release_first = asyncio.Event()
    sent = []

    async def send(failure):
        sent.append(failure)
        if len(sent) == 1:
            started.set()
            await release_first.wait()
        return True

    failure = TickFailure("BTCUSDT: data_missing_bars", "data_quality:BTCUSDT")
    monitor = DataQualityMonitor(enabled=True, send=send)
    await monitor.tick_done((failure,))
    await started.wait()

    await monitor.tick_done(())
    release_first.set()
    await monitor.drain()

    await monitor.tick_done((failure,))
    await monitor.drain()

    assert sent == [failure, failure]
