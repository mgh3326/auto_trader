import datetime as dt
from decimal import Decimal
from unittest.mock import AsyncMock

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
    from app.services.invest_screener_snapshots.repository import SnapshotUpsert

    # Use a sentinel symbol that won't appear in any other test fixture
    _DRY_RUN_SYMBOL = "DRYRUN_SENTINEL_999"

    monkeypatch.setattr(
        cli,
        "build_snapshots_for_market",
        AsyncMock(
            return_value=[
                SnapshotUpsert(
                    market="kr",
                    symbol=_DRY_RUN_SYMBOL,
                    snapshot_date=dt.date(2026, 5, 9),
                    latest_close=Decimal("78500"),
                    closes_window=[78500],
                    source="kis",
                )
            ]
        ),
    )
    monkeypatch.setattr(
        cli, "_resolve_symbols", AsyncMock(return_value=[_DRY_RUN_SYMBOL])
    )

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
