from argparse import Namespace
from decimal import Decimal

import pytest

from app.services.account_value.report import ReportRow
from scripts import account_value_report, account_value_snapshot
from tests.services.account_value.test_snapshot import Reader, Session


class SessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *args):
        pass


@pytest.mark.asyncio
async def test_snapshot_cli_preview_uses_no_writes(monkeypatch, capsys):
    session = Session()
    monkeypatch.setattr(
        account_value_snapshot, "session_factory", lambda: SessionContext(session)
    )
    monkeypatch.setattr(
        account_value_snapshot, "CompleteBrokerReader", lambda *args: Reader()
    )
    await account_value_snapshot.run(
        Namespace(flow=None, account="upbit", market="crypto", commit=False)
    )
    assert session.calls == 0
    out = capsys.readouterr().out
    assert '"mode": "preview"' in out
    assert '"mark": null' in out


@pytest.mark.asyncio
async def test_report_cli_golden(monkeypatch, capsys):
    session = Session()
    monkeypatch.setattr(
        account_value_report, "session_factory", lambda: SessionContext(session)
    )

    async def load(*args, **kwargs):
        return [
            ReportRow(
                "upbit",
                "crypto",
                None,
                Decimal(100),
                Decimal(110),
                Decimal(0),
                Decimal("0.1"),
                Decimal(0),
            )
        ]

    monkeypatch.setattr(account_value_report, "load_weekly_report", load)
    from datetime import date

    await account_value_report.run(
        Namespace(start=date(2026, 1, 1), end=date(2026, 1, 8), flows_complete=True)
    )
    assert capsys.readouterr().out == (
        "account | market | sleeve | start | end | flows | return | benchmark | excess | basis | fx | unmeasured\n"
        "upbit | crypto | - | 100 | 110 | 0 | 10.00% | 0.00% | 10.00% | modified_dietz | - | -\n"
    )


@pytest.mark.asyncio
async def test_report_cli_builds_golden_from_fixture_snapshots(monkeypatch, capsys):
    from datetime import date, timedelta
    from types import SimpleNamespace

    from app.services.account_value import load_report

    start = date(2026, 1, 1)
    account = "upbit:upbit:" + "a" * 64
    snapshots = [
        SimpleNamespace(
            account=account,
            market="account",
            day=start + timedelta(days=i),
            holdings=[],
            cash={"KRW": "100"},
            reasons=[],
        )
        for i in range(8)
    ]

    class Result:
        def __init__(self, value):
            self.value = value

        def scalars(self):
            return self

        def all(self):
            return self.value

    class Database:
        def __init__(self):
            self.results = iter([snapshots, [], [(account, "account")]])

        async def execute(self, _query):
            return Result(next(self.results))

    async def no_sleeves(_session):
        return []

    async def no_prices(*args):
        return None, None

    monkeypatch.setattr(
        account_value_report, "session_factory", lambda: SessionContext(Database())
    )
    monkeypatch.setattr(load_report, "list_sleeves", no_sleeves)
    monkeypatch.setattr(load_report, "_benchmark", no_prices)
    await account_value_report.run(
        Namespace(start=start, end=start + timedelta(days=7), flows_complete=True)
    )
    assert capsys.readouterr().out == (
        "account | market | sleeve | start | end | flows | return | benchmark | excess | basis | fx | unmeasured\n"
        f"{account} | account | - | 100 | 100 | 0 | 0.00% | unknown | unknown | modified_dietz_account_krw | - | account_benchmark_unavailable\n"
    )


@pytest.mark.asyncio
async def test_flow_preview_requires_stable_id_and_does_not_write(monkeypatch, capsys):
    from uuid import uuid4

    session = Session()
    monkeypatch.setattr(
        account_value_snapshot, "session_factory", lambda: SessionContext(session)
    )
    account = "upbit:upbit:" + "a" * 64
    key = uuid4()
    args = account_value_snapshot.parser().parse_args(
        ["--flow", account, "KRW", "10", "2026-01-01", "--flow-id", str(key)]
    )
    await account_value_snapshot.run(args)
    assert session.calls == session.commits == 0
    assert '"write_status": "preview"' in capsys.readouterr().out
    missing = account_value_snapshot.parser().parse_args(
        ["--flow", account, "KRW", "10", "2026-01-01"]
    )
    with pytest.raises(ValueError, match="--flow-id"):
        await account_value_snapshot.run(missing)
