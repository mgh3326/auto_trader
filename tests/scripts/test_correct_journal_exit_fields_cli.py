"""hk 5357 — CLI contract for scripts/correct_journal_exit_fields.py."""

from __future__ import annotations

import json

import pytest

from scripts import correct_journal_exit_fields as cli

pytestmark = pytest.mark.unit

DIGEST = "a" * 64
BASE = ["--database-url-env", "X", "--reason", "r", "--actor", "a"]


@pytest.fixture(autouse=True)
def no_connection(monkeypatch):
    def _no_engine(*_a, **_k):  # pragma: no cover - must not be reached
        raise AssertionError("engine created")

    monkeypatch.setattr(cli, "create_async_engine", _no_engine)
    monkeypatch.setenv("X", "postgresql+asyncpg://u:S3cretPassw0rd@h/db")


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


@pytest.mark.parametrize("ids", ["243-245", "24%", "243, 244", "0", "-1", "x", "1,1"])
def test_bad_ids_exit_1_before_any_connection(ids, capsys) -> None:
    assert cli.main([*BASE, "--ids", ids]) == cli.EXIT_ERROR
    assert "error" in json.loads(capsys.readouterr().err)


@pytest.mark.parametrize(
    ("reason", "actor"), [("  ", "a"), ("r", ""), ("r\nx", "a"), ("r", "a\tb")]
)
def test_blank_reason_or_actor_exit_1_before_any_connection(reason, actor) -> None:
    argv = [
        "--database-url-env",
        "X",
        "--ids",
        "1",
        "--reason",
        reason,
        "--actor",
        actor,
    ]
    assert cli.main(argv) == cli.EXIT_ERROR


@pytest.mark.parametrize(
    "digest", [[], ["--expect-digest", "abc"], ["--expect-digest", "A" * 64]]
)
def test_commit_needs_a_preview_digest_before_any_connection(digest, capsys) -> None:
    assert cli.main([*BASE, "--ids", "243", "--commit", *digest]) == cli.EXIT_ERROR
    assert "expect-digest" in capsys.readouterr().err


def test_missing_database_env_never_prints_a_value(monkeypatch, capsys) -> None:
    monkeypatch.delenv("X")
    assert cli.main([*BASE, "--ids", "243"]) == cli.EXIT_ERROR
    err = capsys.readouterr().err
    assert "X is not set" in err


def test_a_broken_url_is_reported_without_its_value(monkeypatch, capsys) -> None:
    monkeypatch.setenv("X", "not-a-url S3cretPassw0rd")
    assert cli.main([*BASE, "--ids", "243"]) == cli.EXIT_ERROR
    assert "S3cretPassw0rd" not in capsys.readouterr().err


@pytest.mark.asyncio
async def test_run_prints_a_preview_and_exit_code(monkeypatch) -> None:
    from types import SimpleNamespace

    from app.services import journal_exit_correction as jec

    plan = jec.BatchPlan((jec._ineligible(243, "no_matching_ledger_row"),))

    async def fake_preview(session, ids):
        return jec.BatchResult(plan.status, plan)

    class _Ctx:
        async def __aenter__(self):
            return object()

        async def __aexit__(self, *a):
            return False

    monkeypatch.setattr(cli, "preview", fake_preview)
    args = cli._parser().parse_args([*BASE, "--ids", "243"])
    code, payload = await cli.run(args, session_factory=lambda: _Ctx())
    assert code == cli.EXIT_REFUSED
    assert payload["mode"] == "preview"
    assert payload["journals"][0]["reason"] == "no_matching_ledger_row"
    assert payload["decision_ref"] == "hk 5357"
    _ = SimpleNamespace
