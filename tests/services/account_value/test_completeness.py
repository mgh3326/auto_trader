from datetime import UTC, date, datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services.account_value.completeness import covered
from scripts import account_value_snapshot
from tests.services.account_value.test_cli import SessionContext
from tests.services.account_value.test_snapshot import Session

START = date(2026, 1, 1)
END = START + timedelta(days=3)
T = datetime(2026, 1, 5, tzinfo=UTC)
ACCOUNT = "upbit:upbit:" + "a" * 64


def confirmation(start, end, at=T):
    return SimpleNamespace(start_day=start, end_day=end, confirmed_at=at)


def test_coverage_gaps_overlap_and_later_flow():
    whole = confirmation(START, END)
    assert covered(START, END, [whole], [])
    assert not covered(
        START,
        END,
        [confirmation(START, START), confirmation(START + timedelta(days=2), END)],
        [],
    )
    overlap = [
        confirmation(START, START + timedelta(days=2)),
        confirmation(START + timedelta(days=1), END),
    ]
    assert covered(START, END, overlap, [])
    flow = SimpleNamespace(
        day=START + timedelta(days=1), created_at=T + timedelta(seconds=1)
    )
    assert not covered(START, END, overlap, [flow])
    assert covered(
        START,
        END,
        overlap + [confirmation(flow.day, flow.day, T + timedelta(seconds=2))],
        [flow],
    )


@pytest.mark.asyncio
async def test_cli_preview_and_commit_masks_account(monkeypatch, capsys):
    session = Session()
    monkeypatch.setattr(
        account_value_snapshot, "session_factory", lambda: SessionContext(session)
    )
    seen = []

    async def record(session, account, start, end, confirmed_by, reason, *, commit):
        seen.append((account, start, end, confirmed_by, reason, commit))
        return "inserted" if commit else "preview"

    monkeypatch.setattr(account_value_snapshot, "record_confirmation", record)
    argv = ["--confirm-flows", ACCOUNT, str(START), str(END), "operator", "checked"]
    for commit in (False, True):
        args = account_value_snapshot.parser().parse_args(
            argv + (["--commit"] if commit else [])
        )
        await account_value_snapshot.run(args)
        output = capsys.readouterr().out
        assert ACCOUNT not in output and "a" * 64 not in output
        assert ('"inserted"' if commit else '"preview"') in output
    assert [item[-1] for item in seen] == [False, True]


@pytest.mark.asyncio
async def test_record_confirmation_preview_writes_only_on_commit():
    from app.services.account_value.completeness import record_confirmation

    class Session:
        def __init__(self):
            self.statements = []
            self.commits = 0

        async def execute(self, statement, params=None):
            self.statements.append(statement)

        async def commit(self):
            self.commits += 1

    session = Session()
    assert (
        await record_confirmation(session, ACCOUNT, START, END, "operator", "checked")
        == "preview"
    )
    assert session.statements == [] and session.commits == 0
    assert (
        await record_confirmation(
            session, ACCOUNT, START, END, "operator", "checked", commit=True
        )
        == "inserted"
    )
    # advisory account lock + the append
    assert len(session.statements) == 2 and session.commits == 1
    with pytest.raises(ValueError):
        await record_confirmation(
            session, ACCOUNT, END, START, "operator", "checked", commit=True
        )
    assert len(session.statements) == 2 and session.commits == 1
