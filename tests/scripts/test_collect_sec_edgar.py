"""CLI contract for the manual SEC EDGAR collector (hk 6407)."""

from __future__ import annotations

import asyncio
import json
from datetime import date

import pytest

from app.core.config import settings
from scripts import collect_sec_edgar as cli

pytestmark = pytest.mark.unit


class _FakeSession:
    def __init__(self):
        self.commits = 0

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def commit(self):
        self.commits += 1


class _FakeSessionLocal:
    constructed = 0

    def __call__(self):
        type(self).constructed += 1
        return _FakeSession()


@pytest.fixture
def session_local(monkeypatch):
    fake = _FakeSessionLocal()
    fake.constructed = 0
    import app.core.db as db_mod

    monkeypatch.setattr(db_mod, "AsyncSessionLocal", fake)
    return fake


@pytest.fixture(autouse=True)
def _user_agent(monkeypatch):
    monkeypatch.setattr(settings, "sec_edgar_user_agent", "Test Agent test@example.com")


def _run(argv, monkeypatch, **kwargs):
    return asyncio.run(cli._run(cli.parse_args(argv)))


def test_parse_args_requires_symbols_or_ciks():
    with pytest.raises(SystemExit):
        cli.parse_args([])
    args = cli.parse_args(["--symbols", "NVDA", "MSFT"])
    assert args.symbols == ["NVDA", "MSFT"]
    assert args.commit is False and args.dry_run is False
    args = cli.parse_args(
        ["--ciks", "1045810"],
    )
    assert args.ciks == [1045810]
    args = cli.parse_args(
        [
            "--symbols",
            "NVDA",
            "--from-date",
            "2025-01-01",
            "--to-date",
            "2025-12-31",
            "--commit",
        ]
    )
    assert args.from_date == date(2025, 1, 1)
    assert args.to_date == date(2025, 12, 31)
    assert args.commit is True
    with pytest.raises(SystemExit):
        cli.parse_args(
            [
                "--symbols",
                "NVDA",
                "--from-date",
                "2026-01-01",
                "--to-date",
                "2025-01-01",
            ]
        )


def test_gate_off_exits_2_before_db_or_network(monkeypatch, session_local, capsys):
    monkeypatch.setattr(settings, "sec_edgar_user_agent", "")
    rc = _run(["--symbols", "NVDA", "--commit"], monkeypatch)
    assert rc == 2
    assert session_local.constructed == 0
    assert capsys.readouterr().out == ""


def test_ir_mode_needs_no_gate_and_collects_nothing(monkeypatch, session_local, capsys):
    monkeypatch.setattr(settings, "sec_edgar_user_agent", "")
    rc = _run(["--symbols", "AAPL", "MSFT", "--ir"], monkeypatch)
    assert rc == 0
    assert session_local.constructed == 0
    out = json.loads(capsys.readouterr().out)
    assert out["mode"] == "ir"
    assert out["issuers"]["AAPL"]["status"] == "unsupported"
    assert out["issuers"]["MSFT"]["status"] == "unsupported"
    assert "scraping" in out["issuers"]["AAPL"]["reason"]


def test_commit_path_calls_collect_and_maps_exit_codes(
    monkeypatch, session_local, capsys
):
    calls = {}

    async def fake_collect(db, **kwargs):
        calls.update(kwargs)
        calls["db"] = db
        return {
            "source": "sec",
            "issuers": {"NVDA": {"status": "collected", "filings": 4}},
            "totals": {"failed": 0, "unresolved": 0},
            "partition": {"status": "succeeded", "event_count": 4},
        }

    monkeypatch.setattr(cli, "collect_sec_filings", fake_collect)
    rc = _run(["--symbols", "NVDA", "--commit"], monkeypatch)
    assert rc == 0
    assert calls["commit"] is True
    assert calls["symbols"] == ["NVDA"]
    out = json.loads(capsys.readouterr().out)
    assert out["partition"]["status"] == "succeeded"

    async def failing_collect(db, **kwargs):
        return {
            "source": "sec",
            "issuers": {},
            "totals": {"failed": 1},
            "partition": {"status": "failed"},
        }

    monkeypatch.setattr(cli, "collect_sec_filings", failing_collect)
    assert _run(["--symbols", "NVDA", "--commit"], monkeypatch) == 1
