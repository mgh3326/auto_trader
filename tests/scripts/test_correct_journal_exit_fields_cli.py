"""hk 5357 — CLI contract for scripts/correct_journal_exit_fields.py."""

from __future__ import annotations

import json

import pytest

from app.services import journal_exit_correction as jec
from scripts import correct_journal_exit_fields as cli

pytestmark = pytest.mark.unit

DIGEST = "a" * 64


def entry(jid: int) -> dict:
    return {
        "journal_id": jid,
        "evidence_ref": f"hk:doc/reconcile-result-{jid}",
        "record_order_id": f"ORD{jid}",
        "record_closed_journal_ids": [jid],
        "ledger": {"venue": "krx", "broker_order_id": f"ORD{jid}", "fill_seq": 0},
        "allocated_quantity": "2",
    }


@pytest.fixture
def base(tmp_path):
    path = tmp_path / "evidence.json"
    path.write_text(json.dumps([entry(243)]))
    return [
        "--database-url-env",
        "X",
        "--reason",
        "r",
        "--actor",
        "a",
        "--evidence-file",
        str(path),
    ]


@pytest.fixture(autouse=True)
def no_connection(monkeypatch):
    def _no_engine(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("engine created")

    monkeypatch.setattr(cli, "create_async_engine", _no_engine)
    monkeypatch.setenv("X", "postgresql+asyncpg://u:S3cretPassw0rd@h/db")


@pytest.mark.parametrize(
    "argv",
    [
        [
            "--database-url-env",
            "X",
            "--reason",
            "r",
            "--actor",
            "a",
            "--evidence-file",
            "e",
        ],
        [
            "--database-url-env",
            "X",
            "--ids",
            "1",
            "--actor",
            "a",
            "--evidence-file",
            "e",
        ],
        [
            "--database-url-env",
            "X",
            "--ids",
            "1",
            "--reason",
            "r",
            "--evidence-file",
            "e",
        ],
        ["--ids", "1", "--reason", "r", "--actor", "a", "--evidence-file", "e"],
        ["--database-url-env", "X", "--ids", "1", "--reason", "r", "--actor", "a"],
    ],
)
def test_required_arguments(argv) -> None:
    with pytest.raises(SystemExit):
        cli._parser().parse_args(argv)


@pytest.mark.parametrize("ids", ["243-245", "24%", "243, 244", "0", "-1", "x", "1,1"])
def test_bad_ids_exit_1_before_any_connection(ids, base, capsys) -> None:
    assert cli.main([*base, "--ids", ids]) == cli.EXIT_ERROR
    assert "error" in json.loads(capsys.readouterr().err)


@pytest.mark.parametrize(
    ("reason", "actor"), [("  ", "a"), ("r", ""), ("r\nx", "a"), ("r", "a\tb")]
)
def test_blank_reason_or_actor_exit_1_before_any_connection(
    reason, actor, tmp_path
) -> None:
    path = tmp_path / "e.json"
    path.write_text(json.dumps([entry(243)]))
    argv = [
        "--database-url-env",
        "X",
        "--ids",
        "243",
        "--reason",
        reason,
        "--actor",
        actor,
        "--evidence-file",
        str(path),
    ]
    assert cli.main(argv) == cli.EXIT_ERROR


@pytest.mark.parametrize(
    "content",
    [
        None,
        "not json",
        "[]",
        json.dumps([entry(1)]),
        json.dumps([{"journal_id": 243, "ledger_id": 5}]),
    ],
)
def test_missing_or_bad_evidence_exit_1_before_any_connection(
    content, tmp_path, capsys
) -> None:
    path = tmp_path / "e.json"
    if content is not None:
        path.write_text(content)
    argv = [
        "--database-url-env",
        "X",
        "--ids",
        "243",
        "--reason",
        "r",
        "--actor",
        "a",
        "--evidence-file",
        str(path),
    ]
    assert cli.main(argv) == cli.EXIT_ERROR
    assert "evidence" in capsys.readouterr().err


@pytest.mark.parametrize(
    "digest", [[], ["--expect-digest", "abc"], ["--expect-digest", "A" * 64]]
)
def test_commit_needs_a_preview_digest_before_any_connection(
    digest, base, capsys
) -> None:
    assert cli.main([*base, "--ids", "243", "--commit", *digest]) == cli.EXIT_ERROR
    assert "expect-digest" in capsys.readouterr().err


def test_missing_database_env_never_prints_a_value(monkeypatch, base, capsys) -> None:
    monkeypatch.delenv("X")
    assert cli.main([*base, "--ids", "243"]) == cli.EXIT_ERROR
    assert "X is not set" in capsys.readouterr().err


@pytest.mark.parametrize(
    "url",
    [
        "not-a-url S3cretPassw0rd",
        "postgresql://u:S3cretPassw0rd@h/db",  # bare: resolves to a sync driver
        "postgresql+psycopg2://u:S3cretPassw0rd@h/db",
        "mysql://u:S3cretPassw0rd@h/db",
    ],
)
def test_only_an_asyncpg_url_is_accepted_and_never_echoed(
    url, monkeypatch, base, capsys
) -> None:
    monkeypatch.setenv("X", url)
    assert cli.main([*base, "--ids", "243"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "S3cretPassw0rd" not in err
    assert "postgresql+asyncpg://" in err


@pytest.mark.asyncio
async def test_run_prints_a_preview_and_exit_code(monkeypatch, base) -> None:
    plan = jec.BatchPlan((jec._ineligible(243, "ledger_row_not_found"),))

    async def fake_preview(session, ids, evidence):
        assert set(evidence) == {243}
        return jec.BatchResult(plan.status, plan)

    class _Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(cli, "preview", fake_preview)
    args = cli._parser().parse_args([*base, "--ids", "243"])
    code, payload = await cli.run(args, session_factory=lambda: _Ctx())
    assert code == cli.EXIT_REFUSED
    assert payload["mode"] == "preview"
    assert payload["journals"][0]["reason"] == "ledger_row_not_found"
    assert payload["decision_ref"] == "hk 5357"
