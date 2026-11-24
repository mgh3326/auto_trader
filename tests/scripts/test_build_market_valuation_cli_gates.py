"""CLI gates for the scheduled KR valuation build (hk 6150).

``--skip-non-session`` skips the build on XKRX non-session days, and
``--commit-from-settings`` lets the running environment decide the commit,
matching the retired TaskIQ task ``market_valuation_snapshots.kr_scheduled``.
Defaults without the new flags must not change.
"""

from __future__ import annotations

import datetime as dt
import json
from unittest.mock import AsyncMock

import pytest

from app.core.config import settings
from app.jobs import market_valuation_snapshots as val_job
from scripts import build_market_valuation_snapshots as val_cli


def _result(
    *, built: int, committed: bool
) -> val_job.MarketValuationSnapshotBuildResult:
    return val_job.MarketValuationSnapshotBuildResult(
        market="kr",
        symbols_resolved=built,
        snapshots_built=built,
        committed=committed,
        batches=1,
        started_at=dt.datetime(2026, 11, 24, tzinfo=dt.UTC),
        finished_at=dt.datetime(2026, 11, 24, tzinfo=dt.UTC),
    )


@pytest.fixture
def builders(monkeypatch):
    plain = AsyncMock(return_value=_result(built=20, committed=False))
    guarded = AsyncMock(return_value=_result(built=3000, committed=True))
    monkeypatch.setattr(val_job, "run_market_valuation_snapshot_build", plain)
    monkeypatch.setattr(val_job, "run_market_valuation_snapshot_build_guarded", guarded)
    return plain, guarded


def _session_gate(monkeypatch, is_session: bool) -> list[str]:
    import app.tasks.invest_screener_snapshot_tasks as tasks

    asked: list[str] = []

    def _fake(market, *, now=None):
        asked.append(market)
        return is_session

    monkeypatch.setattr(tasks, "is_market_session_today", _fake)
    return asked


def test_parse_defaults_keep_existing_behavior():
    args = val_cli.parse_args(["--market", "kr", "--all"])
    assert args.skip_non_session is False
    assert args.commit_from_settings is False
    assert args.commit is False
    assert args.dry_run is True


def test_commit_and_commit_from_settings_are_an_argparse_error(capsys):
    with pytest.raises(SystemExit) as excinfo:
        val_cli.parse_args(["--all", "--commit", "--commit-from-settings"])
    assert excinfo.value.code == 2
    assert "mutually exclusive" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_skip_non_session_on_holiday_exits_zero_without_building(
    monkeypatch, builders, capsys
):
    plain, guarded = builders
    asked = _session_gate(monkeypatch, is_session=False)

    code = await val_cli.run(
        val_cli.parse_args(["--market", "kr", "--all", "--skip-non-session"])
    )

    assert code == 0
    assert asked == ["kr"]
    assert plain.await_count == 0 and guarded.await_count == 0
    line = json.loads(capsys.readouterr().out.strip())
    assert line == {"status": "skipped_holiday", "market": "kr"}


@pytest.mark.asyncio
async def test_skip_non_session_on_session_day_still_builds(monkeypatch, builders):
    plain, guarded = builders
    _session_gate(monkeypatch, is_session=True)

    code = await val_cli.run(
        val_cli.parse_args(["--market", "kr", "--all", "--skip-non-session"])
    )

    assert code == 0
    assert plain.await_count == 1 and guarded.await_count == 0


@pytest.mark.asyncio
async def test_default_run_without_new_flags_never_consults_the_calendar(
    monkeypatch, builders
):
    plain, guarded = builders
    asked = _session_gate(monkeypatch, is_session=False)

    await val_cli.run(val_cli.parse_args(["--market", "kr", "--all"]))

    assert asked == []
    assert plain.await_count == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("setting", [True, False])
async def test_commit_from_settings_maps_setting_to_commit(
    monkeypatch, builders, setting
):
    plain, guarded = builders
    monkeypatch.setattr(settings, "market_valuation_snapshots_commit_enabled", setting)

    await val_cli.run(
        val_cli.parse_args(["--market", "kr", "--all", "--commit-from-settings"])
    )

    if setting:
        # Commit path: the default guarded wrapper, as with --commit.
        assert guarded.await_count == 1 and plain.await_count == 0
        request = guarded.await_args.args[0]
        assert request.commit is True
    else:
        # Dry-run path: no commit, and the guard is not used.
        assert plain.await_count == 1 and guarded.await_count == 0
        request = plain.await_args.args[0]
        assert request.commit is False
