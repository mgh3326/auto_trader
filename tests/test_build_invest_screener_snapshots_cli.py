import datetime as dt

import pytest
import sqlalchemy as sa

from app.models.invest_screener_snapshot import InvestScreenerSnapshot
from scripts import build_invest_screener_snapshots as cli


def test_default_is_dry_run():
    args = cli.parse_args(["--market", "kr", "--limit", "5"])
    assert args.dry_run is True
    assert args.commit is False


def test_commit_flag_negates_dry_run():
    args = cli.parse_args(["--market", "kr", "--commit"])
    assert args.commit is True
    assert args.dry_run is False


@pytest.mark.asyncio
async def test_run_dry_run_produces_no_writes(monkeypatch, db_session):
    from app.jobs.invest_screener_snapshots import SnapshotBuildResult, SnapshotSample

    # Use a sentinel symbol that won't appear in any other test fixture
    _DRY_RUN_SYMBOL = "DRYRUN_SENTINEL_999"

    async def fake_run_snapshot_build(request):
        assert request.commit is False
        return SnapshotBuildResult(
            market="kr",
            symbols_resolved=1,
            snapshots_built=1,
            skipped=0,
            committed=False,
            batches=1,
            started_at=dt.datetime(2026, 5, 9, tzinfo=dt.UTC),
            finished_at=dt.datetime(2026, 5, 9, 0, 1, tzinfo=dt.UTC),
            snapshot_date_distribution={"2026-05-09": 1},
            samples=(
                SnapshotSample(
                    market="kr",
                    symbol=_DRY_RUN_SYMBOL,
                    snapshot_date=dt.date(2026, 5, 9),
                    latest_close="78500",
                    consecutive_up_days=None,
                    week_change_rate=None,
                ),
            ),
        )

    monkeypatch.setattr(cli.snapshot_job, "run_snapshot_build", fake_run_snapshot_build)

    code = await cli.run(cli.parse_args(["--market", "kr", "--limit", "1"]))
    assert code == 0

    rows = (
        (
            await db_session.execute(
                sa.select(InvestScreenerSnapshot).where(
                    InvestScreenerSnapshot.symbol == _DRY_RUN_SYMBOL
                )
            )
        )
        .scalars()
        .all()
    )
    assert rows == []  # no writes in dry-run


def test_no_broker_imports():
    """The CLI must not transitively import broker mutation modules."""
    import sys

    # scripts module should already be imported from import at top of this file
    cli_modules = {m for m in sys.modules if "build_invest_screener_snapshots" in m}
    assert cli_modules  # sanity check - module was imported
    forbidden = {
        "app.services.brokers.kis.orders",
        "app.services.brokers.alpaca.orders",
    }
    assert forbidden.isdisjoint(set(sys.modules))
