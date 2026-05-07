"""CLI tests for scripts/ingest_market_events.py (ROB-128)."""

from __future__ import annotations

from datetime import date
from unittest.mock import AsyncMock

import pytest


@pytest.mark.unit
def test_iter_partition_dates_inclusive():
    from scripts.ingest_market_events import iter_partition_dates

    dates = list(iter_partition_dates(date(2026, 5, 7), date(2026, 5, 9)))
    assert dates == [date(2026, 5, 7), date(2026, 5, 8), date(2026, 5, 9)]


@pytest.mark.unit
def test_iter_partition_dates_single_day():
    from scripts.ingest_market_events import iter_partition_dates

    dates = list(iter_partition_dates(date(2026, 5, 7), date(2026, 5, 7)))
    assert dates == [date(2026, 5, 7)]


@pytest.mark.unit
def test_parse_args_defaults():
    from scripts.ingest_market_events import parse_args

    ns = parse_args(["--from-date", "2026-05-07", "--to-date", "2026-05-09"])
    assert ns.source == "finnhub"
    assert ns.category == "earnings"
    assert ns.market == "us"
    assert ns.from_date == date(2026, 5, 7)
    assert ns.to_date == date(2026, 5, 9)
    assert ns.dry_run is False


@pytest.mark.unit
def test_parse_args_rejects_unsupported_source_category_combo():
    import argparse

    from scripts.ingest_market_events import parse_args

    with pytest.raises((SystemExit, argparse.ArgumentTypeError, ValueError)):
        parse_args(
            [
                "--source",
                "dart",
                "--category",
                "earnings",
                "--market",
                "us",
                "--from-date",
                "2026-05-07",
                "--to-date",
                "2026-05-07",
            ]
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_run_ingest_dispatches_per_day(db_session, monkeypatch):
    from scripts import ingest_market_events as cli

    fake = AsyncMock(
        return_value=type("R", (), {"status": "succeeded", "event_count": 0})()
    )
    monkeypatch.setitem(cli.SUPPORTED, ("finnhub", "earnings", "us"), fake)

    await cli.run_ingest(
        db=db_session,
        source="finnhub",
        category="earnings",
        market="us",
        from_date=date(2026, 5, 7),
        to_date=date(2026, 5, 9),
        dry_run=False,
    )
    assert fake.await_count == 3


@pytest.mark.unit
def test_parse_args_accepts_forexfactory_economic_global():
    from scripts.ingest_market_events import parse_args

    ns = parse_args(
        [
            "--source",
            "forexfactory",
            "--category",
            "economic",
            "--market",
            "global",
            "--from-date",
            "2026-05-13",
            "--to-date",
            "2026-05-13",
            "--dry-run",
        ]
    )
    assert ns.source == "forexfactory"
    assert ns.category == "economic"
    assert ns.market == "global"
    assert ns.dry_run is True


@pytest.mark.unit
def test_parse_args_rejects_forexfactory_with_us_market():
    import argparse

    from scripts.ingest_market_events import parse_args

    with pytest.raises((SystemExit, argparse.ArgumentTypeError, ValueError)):
        parse_args(
            [
                "--source",
                "forexfactory",
                "--category",
                "economic",
                "--market",
                "us",
                "--from-date",
                "2026-05-13",
                "--to-date",
                "2026-05-13",
            ]
        )


@pytest.mark.asyncio
@pytest.mark.integration
async def test_run_ingest_dry_run_does_not_call_orchestrator(db_session, monkeypatch):
    from scripts import ingest_market_events as cli

    fake = AsyncMock()
    monkeypatch.setitem(cli.SUPPORTED, ("forexfactory", "economic", "global"), fake)

    rc = await cli.run_ingest(
        db=db_session,
        source="forexfactory",
        category="economic",
        market="global",
        from_date=date(2026, 5, 13),
        to_date=date(2026, 5, 13),
        dry_run=True,
    )
    assert rc == 0
    fake.assert_not_awaited()
