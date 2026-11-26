"""CLI contract for the manual KRX official daily collector (hk 6586)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path
from typing import Any

import pytest

from app.services.krx_official_daily.errors import (
    KrxServiceNotApprovedError,
    KrxTransportError,
    KrxUnauthorizedKeyError,
)
from scripts import collect_krx_official_daily as cli

pytestmark = pytest.mark.unit

SENTINEL = "SENTINEL-KEY-cli-0002"
FIXTURE = (
    Path(__file__).resolve().parents[1]
    / "fixtures"
    / "krx_official_daily"
    / "stk_bydd_trd_spec_derived.json"
)


class FakeClient:
    constructed: list[str] = []
    outcome: Any = None

    def __init__(self, *, auth_key: str) -> None:
        FakeClient.constructed.append(auth_key)

    async def fetch_daily(self, market: str, session_date: Any) -> Any:
        if isinstance(FakeClient.outcome, Exception):
            raise FakeClient.outcome
        return json.loads(FIXTURE.read_text())


@pytest.fixture(autouse=True)
def _reset_fake(monkeypatch):
    FakeClient.constructed = []
    FakeClient.outcome = None
    monkeypatch.setattr(cli, "KrxOpenApiClient", FakeClient)
    monkeypatch.setattr(sys, "argv", ["collect_krx_official_daily"])
    yield


def _run(argv: list[str], monkeypatch) -> int:
    monkeypatch.setattr(sys, "argv", ["collect_krx_official_daily", *argv])
    args = cli.parse_args(argv)
    return asyncio.run(cli.run(args))


def test_defaults_are_dry_run_both_markets_and_a_required_date() -> None:
    args = cli.parse_args(["--date", "2026-11-26"])
    assert args.dry_run is True
    assert args.markets == ("kospi", "kosdaq")
    args = cli.parse_args(["--date", "2026-11-26", "--commit", "--market", "kosdaq"])
    assert args.dry_run is False
    assert args.markets == ("kosdaq",)
    with pytest.raises(SystemExit):
        cli.parse_args([])


def test_missing_key_exits_2_before_any_client_is_built(
    monkeypatch, capsys, caplog
) -> None:
    monkeypatch.delenv(cli.KEY_ENV, raising=False)
    assert _run(["--date", "2026-11-26"], monkeypatch) == 2
    assert FakeClient.constructed == []
    out, _err = capsys.readouterr()
    assert out == ""
    assert cli.KEY_ENV in caplog.text


def test_dry_run_never_opens_a_database_session_and_never_prints_the_key(
    monkeypatch, capsys
) -> None:
    import app.core.db as db

    def refuse_session(*args, **kwargs):
        raise AssertionError("dry-run must not open a database session")

    monkeypatch.setattr(db, "AsyncSessionLocal", refuse_session)
    monkeypatch.setenv(cli.KEY_ENV, SENTINEL)
    assert _run(["--date", "2026-11-26"], monkeypatch) == 0
    out, err = capsys.readouterr()
    assert FakeClient.constructed == [SENTINEL]
    payload = json.loads(out)
    assert payload["status"] == "ok"
    assert payload["inserted"] is None
    assert payload["markets"]["kospi"]["rows"] == 2
    assert SENTINEL not in out and SENTINEL not in err


@pytest.mark.parametrize(
    ("error", "exit_code", "code"),
    [
        (KrxUnauthorizedKeyError(), 2, "unauthorized_key"),
        (KrxServiceNotApprovedError(), 3, "service_not_approved"),
        (KrxTransportError("http_500"), 1, "transport"),
    ],
)
def test_typed_failures_map_to_distinct_exit_codes_without_the_key(
    monkeypatch, capsys, error, exit_code, code
) -> None:
    monkeypatch.setenv(cli.KEY_ENV, SENTINEL)
    FakeClient.outcome = error
    assert _run(["--date", "2026-11-26", "--market", "kospi"], monkeypatch) == exit_code
    out, err = capsys.readouterr()
    assert json.loads(out)["error_code"] == code
    assert SENTINEL not in out and SENTINEL not in err


def test_commit_passes_one_session_to_the_collector(monkeypatch, capsys) -> None:
    seen: dict[str, Any] = {}

    class FakeSessionContext:
        async def __aenter__(self):
            seen["entered"] = True
            return "SESSION"

        async def __aexit__(self, *exc):
            return False

    import app.core.db as db

    monkeypatch.setattr(db, "AsyncSessionLocal", lambda: FakeSessionContext())
    monkeypatch.setenv(cli.KEY_ENV, SENTINEL)

    async def fake_collect(**kwargs):
        seen["session"] = kwargs["session"]
        return {
            "status": "ok",
            "exit_code": 0,
            "error_code": None,
            "markets": {},
            "inserted": 0,
        }

    monkeypatch.setattr(cli, "collect_official_daily", fake_collect)
    assert _run(["--date", "2026-11-26", "--commit"], monkeypatch) == 0
    assert seen == {"entered": True, "session": "SESSION"}
