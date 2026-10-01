"""#1175 — CLI contract for scripts/quarantine_execution_ledger_rows.py."""

from __future__ import annotations

import argparse
import json
import uuid

import pytest
from sqlalchemy import select

from app.models.execution_ledger import (
    ExecutionLedger,
    ExecutionLedgerQuarantineEvent,
)
from app.schemas.execution_ledger import ExecutionLedgerUpsert
from scripts import quarantine_execution_ledger_rows as cli
from tests.services.execution_ledger._quarantine_fixtures import (
    purge_test_ledger_rows,
    row_kwargs,
)

SECRET = "S3cretPassw0rd"


def _args(*argv: str) -> argparse.Namespace:
    return cli._parser().parse_args(list(argv))


@pytest.mark.unit
@pytest.mark.parametrize(
    "argv",
    [
        ["--database-url-env", "X", "--reason", "r", "--actor", "a"],  # no ids
        ["--database-url-env", "X", "--ids", "1", "--actor", "a"],  # no reason
        ["--database-url-env", "X", "--ids", "1", "--reason", "r"],  # no actor
        ["--ids", "1", "--reason", "r", "--actor", "a"],  # no database
    ],
)
def test_required_arguments(argv) -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(argv)


@pytest.mark.unit
@pytest.mark.parametrize("ids", ["58051-58064", "5805%", "58051, 58052", "0", "-1"])
def test_pattern_ids_exit_1_before_any_database_access(
    ids, capsys, monkeypatch
) -> None:
    def _no_engine(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("engine created")

    monkeypatch.setattr(cli, "create_async_engine", _no_engine)
    code = cli.main(
        ["--database-url-env", "X", "--ids", ids, "--reason", "r", "--actor", "a"]
    )
    assert code == cli.EXIT_ERROR
    assert "error" in json.loads(capsys.readouterr().err)


@pytest.mark.unit
def test_database_url_value_is_never_printed(capsys, monkeypatch) -> None:
    monkeypatch.setenv("T1175_DB_URL", f"not-a-url://{SECRET}@host")
    code = cli.main(
        [
            "--database-url-env",
            "T1175_DB_URL",
            "--ids",
            "1",
            "--reason",
            "r",
            "--actor",
            "a",
        ]
    )
    captured = capsys.readouterr()
    assert code == cli.EXIT_ERROR
    assert SECRET not in captured.out + captured.err
    assert "T1175_DB_URL" in captured.err

    code = cli.main(
        [
            "--database-url",
            f"nope://{SECRET}",
            "--ids",
            "1",
            "--reason",
            "r",
            "--actor",
            "a",
        ]
    )
    captured = capsys.readouterr()
    assert code == cli.EXIT_ERROR
    assert SECRET not in captured.out + captured.err


@pytest.mark.unit
def test_database_error_reports_only_the_class(capsys, monkeypatch) -> None:
    class Boom(Exception):
        pass

    def _engine(url):
        raise Boom(f"cannot connect to {url}")

    monkeypatch.setenv("T1175_DB_URL", f"postgresql+asyncpg://u:{SECRET}@h/db")
    monkeypatch.setattr(cli, "create_async_engine", _engine)
    code = cli.main(
        [
            "--database-url-env",
            "T1175_DB_URL",
            "--ids",
            "1",
            "--reason",
            "r",
            "--actor",
            "a",
        ]
    )
    captured = capsys.readouterr()
    assert code == cli.EXIT_ERROR
    assert SECRET not in captured.out + captured.err
    assert "database_error:Boom" in captured.err


# ------------------------------------------------------------ DB-backed


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cli_preview_commit_noop_and_refusal_exit_codes(db_session) -> None:
    from app.core.db import AsyncSessionLocal

    tag = uuid.uuid4().hex[:5].upper()
    symbol = f"Q{tag}"
    rows = []
    for index, cntg in enumerate(("1", "1", "2")):
        row = ExecutionLedger(
            **ExecutionLedgerUpsert(
                **row_kwargs(symbol=symbol, order_no=f"Q{tag}{index:04d}", cntg_yn=cntg)
            ).model_dump()
        )
        db_session.add(row)
        rows.append(row)
    await db_session.flush()
    phantom_a, phantom_b, real_fill = (int(r.id) for r in rows)
    await db_session.commit()
    base = [
        "--database-url-env",
        "UNUSED",
        "--reason",
        "hk 1172 accept notice",
        "--actor",
        "desk",
    ]
    ids = f"{phantom_a},{phantom_b}"
    try:
        code, payload = await cli.run(
            _args(*base, "--ids", ids), session_factory=AsyncSessionLocal
        )
        assert (code, payload["mode"], payload["status"]) == (0, "preview", "eligible")
        assert [r["verdict"] for r in payload["rows"]] == ["accept_notice"] * 2

        code, payload = await cli.run(
            _args(*base, "--ids", f"{ids},{real_fill}", "--commit"),
            session_factory=AsyncSessionLocal,
        )
        assert (code, payload["status"]) == (cli.EXIT_REFUSED, "refused")
        assert payload["refused_ids"] == [real_fill]
        assert payload["rows"][2]["verdict"] == "fill_notice_cntg_yn_2"

        code, payload = await cli.run(
            _args(*base, "--ids", ids, "--commit"), session_factory=AsyncSessionLocal
        )
        assert (code, payload["mode"], payload["status"]) == (0, "commit", "committed")
        assert payload["changed"] == 2
        assert payload["batch_id"]

        code, payload = await cli.run(
            _args(*base, "--ids", ids, "--commit"), session_factory=AsyncSessionLocal
        )
        assert (code, payload["status"], payload["changed"]) == (0, "noop", 0)

        await db_session.rollback()
        states = (
            await db_session.execute(
                select(ExecutionLedger.id, ExecutionLedger.quarantined_by)
                .where(ExecutionLedger.symbol == symbol)
                .order_by(ExecutionLedger.id)
            )
        ).all()
        assert [tuple(s) for s in states] == [
            (phantom_a, "desk"),
            (phantom_b, "desk"),
            (real_fill, None),
        ]
        audit = (
            await db_session.execute(
                select(ExecutionLedgerQuarantineEvent.ledger_id).where(
                    ExecutionLedgerQuarantineEvent.ledger_id.in_(
                        [phantom_a, phantom_b, real_fill]
                    )
                )
            )
        ).scalars()
        assert sorted(audit) == [phantom_a, phantom_b]
    finally:
        await purge_test_ledger_rows(db_session, ExecutionLedger.symbol == symbol)
