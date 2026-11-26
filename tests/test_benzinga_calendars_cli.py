"""CLI tests for scripts/ingest_benzinga_calendars.py (hk 6513)."""

from __future__ import annotations

from datetime import date

import pytest

from scripts import ingest_benzinga_calendars as cli


@pytest.mark.unit
def test_parse_args_defaults_to_dry_run():
    ns = cli.parse_args(["--category", "earnings", "--date", "2026-11-25"])
    assert ns.dry_run is True
    assert ns.commit is False
    assert ns.from_date == date(2026, 11, 25)
    assert ns.to_date == date(2026, 11, 25)
    assert ns.categories == ["earnings"]


@pytest.mark.unit
def test_commit_flips_dry_run_off():
    ns = cli.parse_args(["--category", "earnings", "--date", "2026-11-25", "--commit"])
    assert ns.dry_run is False


@pytest.mark.unit
def test_categories_accept_commas_and_repeats_in_order():
    ns = cli.parse_args(
        [
            "--category",
            "earnings, analyst_rating",
            "--category",
            "wiim",
            "--date",
            "2026-11-25",
        ]
    )
    assert ns.categories == ["earnings", "analyst_rating", "wiim"]


@pytest.mark.unit
def test_unknown_category_is_rejected():
    with pytest.raises(SystemExit):
        cli.parse_args(["--category", "nope", "--date", "2026-11-25"])


@pytest.mark.unit
def test_short_interest_requires_symbols():
    with pytest.raises(SystemExit):
        cli.parse_args(["--category", "short_interest", "--date", "2026-11-25"])


@pytest.mark.unit
def test_short_interest_accepts_symbols():
    ns = cli.parse_args(
        [
            "--category",
            "short_interest",
            "--date",
            "2026-11-25",
            "--symbols",
            "NVDA,AMD",
        ]
    )
    assert ns.symbols == "NVDA,AMD"


@pytest.mark.unit
def test_insider_trade_accepts_optional_symbols():
    ns = cli.parse_args(["--category", "insider_trade", "--date", "2026-11-25"])
    assert ns.symbols is None


@pytest.mark.unit
def test_new_category_accepts_documented_symbols_and_dry_run():
    ns = cli.parse_args(
        [
            "--category",
            "insider_trade,analyst_insight",
            "--date",
            "2026-11-25",
            "--symbols",
            "AAPL",
        ]
    )
    assert ns.categories == ["insider_trade", "analyst_insight"]
    assert ns.symbols == "AAPL"


@pytest.mark.unit
def test_reversed_range_is_rejected():
    with pytest.raises(SystemExit):
        cli.parse_args(
            [
                "--category",
                "earnings",
                "--from-date",
                "2026-11-27",
                "--to-date",
                "2026-11-25",
            ]
        )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_off_exits_two_without_network_or_db(monkeypatch):
    monkeypatch.delenv("BENZINGA_CALENDARS_ENABLED", raising=False)

    def _explode(*_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("no database or key read may happen with the gate off")

    monkeypatch.setattr(cli, "AsyncSessionLocal", _explode)
    monkeypatch.setattr(cli, "read_api_key", _explode)

    ns = cli.parse_args(["--category", "earnings", "--date", "2026-11-25", "--commit"])
    assert await cli.run(ns) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_dry_run_with_gate_on_does_not_read_the_key(monkeypatch):
    monkeypatch.setenv("BENZINGA_CALENDARS_ENABLED", "true")

    def _explode(*_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("a dry run must not read the key or open a session")

    monkeypatch.setattr(cli, "AsyncSessionLocal", _explode)
    monkeypatch.setattr(cli, "read_api_key", _explode)

    ns = cli.parse_args(["--category", "earnings", "--date", "2026-11-25"])
    assert await cli.run(ns) == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_missing_key_exits_two_without_touching_the_network(monkeypatch):
    monkeypatch.setenv("BENZINGA_CALENDARS_ENABLED", "true")
    monkeypatch.delenv("BENZINGA_API_KEY", raising=False)

    def _explode(*_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("no session may be opened without a key")

    monkeypatch.setattr(cli, "AsyncSessionLocal", _explode)

    ns = cli.parse_args(["--category", "earnings", "--date", "2026-11-25", "--commit"])
    assert await cli.run(ns) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gate_off_covers_the_compare_mode(monkeypatch):
    """The gate covers every mode: comparison must not open a session either."""
    monkeypatch.delenv("BENZINGA_CALENDARS_ENABLED", raising=False)

    def _explode(*_args, **_kwargs):  # pragma: no cover - guard
        raise AssertionError("no session may be opened with the gate off")

    monkeypatch.setattr(cli, "AsyncSessionLocal", _explode)

    ns = cli.parse_args(
        [
            "--compare-earnings",
            "--from-date",
            "2026-10-01",
            "--to-date",
            "2026-11-25",
        ]
    )
    assert await cli.run(ns) == 2


@pytest.mark.unit
@pytest.mark.asyncio
async def test_compare_applies_every_requested_symbol(monkeypatch, capsys):
    """``--symbols AAPL,MSFT`` narrows both collections; NVDA must not leak in.

    The old guard only applied the filter for a single symbol, so a
    multi-symbol request compared every stored row.
    """
    import json
    from types import SimpleNamespace

    class _Event:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol
            self.event_date = date(2026, 10, 29)
            self.time_hint = "after_close"

    class _Service:
        def __init__(self, _db) -> None:
            pass

        async def list_for_range(self, *_a, **_kw):
            return SimpleNamespace(
                events=[_Event("AAPL"), _Event("NVDA"), _Event("MSFT")]
            )

    class _Session:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *_exc):
            return False

    monkeypatch.setattr(cli, "MarketEventsQueryService", _Service)
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: _Session())

    code = await cli._run_compare(date(2026, 10, 1), date(2026, 11, 25), "AAPL,MSFT")
    assert code == 0
    report = json.loads(capsys.readouterr().out)["compare_earnings"]
    assert report["coverage"]["both_symbols"] == ["AAPL", "MSFT"]
    assert report["finnhub"]["items"] == 2
    assert report["benzinga"]["items"] == 2
