"""#1250 — CLI contract for scripts/expire_kis_mock_rows_by_inference.py."""

from __future__ import annotations

import argparse
import datetime
import json

import pytest
import sqlalchemy as sa

from app.models.review import KISMockInferenceExpiryEvent, KISMockOrderLedger
from app.services import kis_mock_inference_expiry_service as service
from scripts import expire_kis_mock_rows_by_inference as cli
from tests.services._kis_mock_inference_fixtures import IDS, NOW, mock_row, purge

SECRET = "S3cretPassw0rd"
GOOD = [
    "--ids",
    "80,66,64,63",
    "--decision-ref",
    "Q-46",
    "--reason",
    "hk 706 c1092",
    "--actor",
    "desk",
]


def _args(*argv: str) -> argparse.Namespace:
    return cli._parser().parse_args(list(argv))


def _no_engine(*_a, **_k):  # pragma: no cover - must not be reached
    raise AssertionError("engine created")


@pytest.mark.unit
@pytest.mark.parametrize(
    "drop",
    ["--ids", "--decision-ref", "--reason", "--actor"],
)
def test_required_arguments(drop: str) -> None:
    argv = ["--database-url-env", "X", *GOOD]
    index = argv.index(drop)
    del argv[index : index + 2]
    with pytest.raises(SystemExit):
        cli._parser().parse_args(argv)
    with pytest.raises(SystemExit):
        cli._parser().parse_args(GOOD)  # no database named


@pytest.mark.unit
@pytest.mark.parametrize(
    ("flag", "value"),
    [
        ("--ids", "80,66,64,63,81"),
        ("--ids", "80,66,64"),
        ("--ids", "81"),
        ("--ids", "63-80"),
        ("--ids", "80,66,64,63,80"),
        ("--ids", "80, 66,64,63"),
        ("--decision-ref", "Q-47"),
        ("--decision-ref", "hk:task/706 Q-46"),
        ("--reason", "   "),
        ("--actor", ""),
    ],
)
def test_bad_input_exits_1_before_any_database_access(
    flag, value, capsys, monkeypatch
) -> None:
    monkeypatch.setattr(cli, "create_async_engine", _no_engine)
    argv = ["--database-url-env", "X", *GOOD, "--commit"]
    argv[argv.index(flag) + 1] = value
    code = cli.main(argv)
    assert code == cli.EXIT_ERROR
    assert "error" in json.loads(capsys.readouterr().err)


@pytest.mark.unit
def test_database_url_value_is_never_printed(capsys, monkeypatch) -> None:
    monkeypatch.setenv("T1250_DB_URL", f"not-a-url://{SECRET}@host")
    code = cli.main(["--database-url-env", "T1250_DB_URL", *GOOD])
    captured = capsys.readouterr()
    assert code == cli.EXIT_ERROR
    assert SECRET not in captured.out + captured.err
    assert "T1250_DB_URL" in captured.err

    code = cli.main(["--database-url", f"nope://{SECRET}", *GOOD])
    captured = capsys.readouterr()
    assert code == cli.EXIT_ERROR
    assert SECRET not in captured.out + captured.err


@pytest.mark.unit
def test_database_error_reports_only_the_class(capsys, monkeypatch) -> None:
    class Boom(Exception):
        pass

    def _engine(url):
        raise Boom(f"cannot connect to {url}")

    monkeypatch.setenv("T1250_DB_URL", f"postgresql+asyncpg://u:{SECRET}@h/db")
    monkeypatch.setattr(cli, "create_async_engine", _engine)
    code = cli.main(["--database-url-env", "T1250_DB_URL", *GOOD])
    captured = capsys.readouterr()
    assert code == cli.EXIT_ERROR
    assert SECRET not in captured.out + captured.err
    assert "database_error:Boom" in captured.err


# ------------------------------------------------------------ DB-backed


@pytest.mark.asyncio
@pytest.mark.integration
async def test_cli_preview_refusal_commit_noop_exit_codes(
    db_session, monkeypatch
) -> None:
    from app.core.db import AsyncSessionLocal

    monkeypatch.setattr(service, "_utcnow", lambda: NOW.astimezone(datetime.UTC))
    for ledger_id in IDS:
        db_session.add(
            mock_row(
                ledger_id,
                lifecycle_state="cancelled" if ledger_id == 64 else "accepted",
            )
        )
    await db_session.commit()
    base = ["--database-url-env", "UNUSED", *GOOD]
    try:
        code, payload = await cli.run(_args(*base), session_factory=AsyncSessionLocal)
        assert (code, payload["mode"], payload["status"]) == (
            cli.EXIT_REFUSED,
            "preview",
            "refused",
        )
        assert payload["refused_ids"] == [64]
        assert payload["waived_conditions"] == ["strategy_match"]

        code, payload = await cli.run(
            _args(*base, "--commit"), session_factory=AsyncSessionLocal
        )
        assert (code, payload["status"], payload["changed"]) == (
            cli.EXIT_REFUSED,
            "refused",
            0,
        )

        await db_session.execute(
            sa.update(KISMockOrderLedger)
            .where(KISMockOrderLedger.id == 64)
            .values(lifecycle_state="accepted")
        )
        await db_session.commit()

        code, payload = await cli.run(_args(*base), session_factory=AsyncSessionLocal)
        assert (code, payload["status"]) == (0, "eligible")

        code, payload = await cli.run(
            _args(*base, "--commit"), session_factory=AsyncSessionLocal
        )
        assert (code, payload["mode"], payload["status"], payload["changed"]) == (
            0,
            "commit",
            "committed",
            4,
        )
        assert payload["batch_id"]

        code, payload = await cli.run(
            _args(*base, "--commit"), session_factory=AsyncSessionLocal
        )
        assert (code, payload["status"], payload["changed"]) == (0, "noop", 0)

        await db_session.rollback()
        states = (
            await db_session.execute(
                sa.select(KISMockOrderLedger.id, KISMockOrderLedger.lifecycle_state)
                .where(KISMockOrderLedger.id.in_(IDS))
                .order_by(KISMockOrderLedger.id)
            )
        ).all()
        assert [tuple(s) for s in states] == [
            (63, "expired"),
            (64, "expired"),
            (66, "expired"),
            (80, "expired"),
        ]
        audit = (
            await db_session.execute(sa.select(KISMockInferenceExpiryEvent.ledger_id))
        ).scalars()
        assert sorted(audit) == [63, 64, 66, 80]
    finally:
        await purge(db_session, mock_ids=list(IDS))
