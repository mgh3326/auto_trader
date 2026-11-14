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


def test_revoked_confirmation_does_not_cover_its_window():
    revoked = SimpleNamespace(
        start_day=START,
        end_day=END,
        confirmed_at=T,
        revoked_at=T + timedelta(minutes=1),
    )
    assert not covered(START, END, [revoked], [])


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
async def test_revoke_cli_preview_and_commit(monkeypatch, capsys):
    session = Session()
    monkeypatch.setattr(
        account_value_snapshot, "session_factory", lambda: SessionContext(session)
    )
    seen = []

    async def revoke(session, confirmation_id, revoked_by, reason, *, commit):
        seen.append((confirmation_id, revoked_by, reason, commit))
        return "revoked" if commit else "preview"

    monkeypatch.setattr(account_value_snapshot, "record_revocation", revoke)
    argv = ["--revoke-confirmation", "42", "operator", "잘못된 기간"]
    for commit in (False, True):
        args = account_value_snapshot.parser().parse_args(
            argv + (["--commit"] if commit else [])
        )
        await account_value_snapshot.run(args)
        output = capsys.readouterr().out
        assert f'"mode": "{"commit" if commit else "preview"}"' in output
        assert f'"write_status": "{"revoked" if commit else "preview"}"' in output
    assert seen == [
        (42, "operator", "잘못된 기간", False),
        (42, "operator", "잘못된 기간", True),
    ]


@pytest.mark.asyncio
async def test_record_revocation_preview_refusals_and_commit():
    from app.services.account_value.completeness import record_revocation

    class Result:
        def __init__(self, value):
            self.value = value

        def scalar_one_or_none(self):
            return self.value

    class Session:
        def __init__(self, confirmation=True, revoked=False):
            self.results = iter([42 if confirmation else None, 42 if revoked else None])
            self.statements = []
            self.commits = 0

        async def execute(self, statement, params=None):
            self.statements.append(statement)
            return Result(next(self.results, None))

        async def commit(self):
            self.commits += 1

    preview = Session()
    assert await record_revocation(preview, 42, "operator", "mistake") == "preview"
    assert len(preview.statements) == 2 and preview.commits == 0

    committed = Session()
    assert (
        await record_revocation(committed, 42, "operator", "mistake", commit=True)
        == "revoked"
    )
    assert len(committed.statements) == 3 and committed.commits == 1

    with pytest.raises(ValueError, match="unknown confirmation"):
        await record_revocation(Session(confirmation=False), 404, "operator", "mistake")
    with pytest.raises(ValueError, match="already revoked"):
        await record_revocation(Session(revoked=True), 42, "operator", "mistake")

    invalid = Session()
    with pytest.raises(ValueError, match="invalid revocation"):
        await record_revocation(invalid, 0, "operator", "mistake")
    assert not invalid.statements and invalid.commits == 0


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
