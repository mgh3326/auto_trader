"""Smoke test for the read-only approval batch health CLI (ROB-1488)."""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from app.services.order_proposals.batch_health import (
    BatchHealthReport,
    BatchHealthWindow,
    Status,
)
from scripts import diagnose_approval_batch_health as cli


class _FakeSession:
    async def execute(self, statement):
        return None

    async def __aenter__(self) -> _FakeSession:
        return self

    async def __aexit__(self, *exc: object) -> bool:
        return False


def _report(
    *, zero_member_batches: int = 0, member_added: int = 2
) -> BatchHealthReport:
    recent = BatchHealthWindow(
        days=7,
        manual_cards=5,
        member_added=member_added,
        member_processed=0,
        batches=5,
    )
    window = BatchHealthWindow(
        days=30,
        manual_cards=33,
        member_added=33,
        member_processed=0,
        batches=33,
    )
    status: Status = (
        "gap" if zero_member_batches else ("ok" if member_added else "review")
    )
    anomalies: tuple[str, ...] = ()
    if zero_member_batches:
        anomalies = ("approval_batch_without_members",)
    return BatchHealthReport(
        as_of=datetime(2026, 11, 2, tzinfo=UTC),
        recent=recent,
        window=window,
        members_total=354,
        zero_member_batches=zero_member_batches,
        status=status,
        anomalies=anomalies,
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_json_output_and_exit_zero(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: _FakeSession())

    async def fake_loader(db, **kwargs):
        return _report()

    monkeypatch.setattr(cli, "load_batch_health", fake_loader)

    rc = await cli.run(as_json=True)

    payload = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert payload["status"] == "ok"
    assert payload["recent"]["member_added"] == 2
    assert payload["notes"]["member_writer_column"] == "added_at"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_gap_exits_one_and_names_the_reason(monkeypatch, capsys) -> None:
    monkeypatch.setattr(cli, "AsyncSessionLocal", lambda: _FakeSession())

    async def fake_loader(db, **kwargs):
        return _report(zero_member_batches=1)

    monkeypatch.setattr(cli, "load_batch_health", fake_loader)

    rc = await cli.run(as_json=False)

    out = capsys.readouterr().out
    assert rc == 1
    assert "approval_batch_without_members" in out
    assert "member_writer_column=added_at" in out


@pytest.mark.asyncio
async def test_read_only_bounded_transaction_precedes_probe(monkeypatch, capsys):
    statements = []

    class Session(_FakeSession):
        async def execute(self, statement):
            statements.append(str(statement))

    monkeypatch.setattr(cli, "AsyncSessionLocal", Session)

    async def loader(db, **kwargs):
        assert statements == [
            "SET TRANSACTION READ ONLY",
            "SET LOCAL statement_timeout = '8000'",
        ]
        return _report(member_added=0)

    monkeypatch.setattr(cli, "load_batch_health", loader)
    assert await cli.run() == 0
    assert "REVIEW" in capsys.readouterr().out
