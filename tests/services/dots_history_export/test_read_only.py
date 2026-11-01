"""ROB-1438 acceptance 3, without a database: read-only session, filter, surface.

``test_export_db.py`` and ``test_quarantine_readers_db.py`` prove the same facts
against PostgreSQL in CI.
"""

from __future__ import annotations

import ast
import json
import os
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy import Select
from sqlalchemy.dialects import postgresql

from app.services.dots_history_export import reader
from app.services.dots_history_export.publish import (
    MANIFEST_NAME,
    OutputDirectoryError,
    resolve_output_directory,
)
from scripts import export_dots_history as cli
from tests.services.dots_history_export._fixtures import SALT, WINDOW

pytestmark = pytest.mark.unit

REPO_ROOT = Path(__file__).resolve().parents[3]
STATEMENTS = (
    reader.kis_live_statement,
    reader.live_statement,
    reader.toss_statement,
    reader.kis_mock_statement,
    reader.alpaca_statement,
    reader.paper_pending_statement,
    reader.paper_trades_statement,
    reader.execution_ledger_statement,
    reader.proposals_statement,
    reader.rungs_statement,
)
NEVER_READ = (
    "raw_response",
    "raw_payload_json",
    "raw_responses",
    "response_message",
    "response_code",
    "krx_fwdg_ord_orgno",
    "thesis",
    "notes",
    "rationale",
    "reason",
    "indicators_snapshot",
    "preview_payload",
    "last_reconcile_detail",
    "approval_nonce",
    "approved_by_telegram_user_id",
    "approved_by_subject",
    "raw_symbol",
)


def _sql(statement: Select) -> str:
    return str(statement.compile(dialect=postgresql.dialect()))


class _Result:
    def __init__(self, value: Any = None) -> None:
        self._value = value

    def scalar_one(self) -> Any:
        return self._value

    def mappings(self) -> _Result:
        return self

    def all(self) -> list[dict[str, Any]]:
        return []


class _RecordingSession:
    def __init__(self, read_only: str = "on") -> None:
        self.read_only = read_only
        self.statements: list[Any] = []
        self.rolled_back = 0

    async def execute(self, statement: Any) -> _Result:
        self.statements.append(statement)
        if "transaction_read_only" in str(statement):
            return _Result(self.read_only)
        return _Result()

    async def rollback(self) -> None:
        self.rolled_back += 1

    async def __aenter__(self) -> _RecordingSession:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        return None


@pytest.mark.asyncio
async def test_the_read_only_transaction_is_set_before_anything_is_read() -> None:
    session = _RecordingSession()
    await reader.read_sources(session, WINDOW)  # type: ignore[arg-type]
    first, second, *selects = session.statements
    assert str(first) == "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY"
    assert str(second) == "SHOW transaction_read_only"
    assert len(selects) == len(STATEMENTS)
    assert all(isinstance(statement, Select) for statement in selects)


@pytest.mark.asyncio
async def test_a_writable_transaction_stops_the_run_before_any_select() -> None:
    session = _RecordingSession(read_only="off")
    with pytest.raises(reader.ReadOnlyViolation):
        await reader.read_sources(session, WINDOW)  # type: ignore[arg-type]
    assert len(session.statements) == 2


def test_the_fill_statement_carries_the_quarantine_filter() -> None:
    sql = _sql(reader.execution_ledger_statement(WINDOW))
    assert "review.execution_ledger.quarantined_at IS NULL" in sql


@pytest.mark.parametrize("statement", STATEMENTS, ids=lambda f: f.__name__)
def test_no_statement_selects_a_payload_prose_or_private_column(statement) -> None:
    selected = {column.name for column in statement(WINDOW).selected_columns}
    assert not selected & set(NEVER_READ)


def test_a_paper_trade_cannot_multiply_through_its_pending_orders() -> None:
    statement = reader.paper_trades_statement(WINDOW)
    assert "pending_order_id" in {c.name for c in statement.selected_columns}
    sql = _sql(statement)
    assert "JOIN paper.paper_pending_orders" not in sql
    assert "min(paper.paper_pending_orders.id)" in sql


def _surface_files() -> list[Path]:
    package = REPO_ROOT / "app/services/dots_history_export"
    return [*sorted(package.glob("*.py")), REPO_ROOT / "scripts/export_dots_history.py"]


def test_the_tool_imports_no_broker_or_network_client() -> None:
    banned = ("app.services.brokers", "httpx", "requests", "aiohttp", "app.mcp_server")
    for path in _surface_files():
        for node in ast.walk(ast.parse(path.read_text("utf-8"))):
            names: list[str] = []
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [node.module or ""]
            for name in names:
                assert not name.startswith(banned), f"{path.name} imports {name}"


def test_the_tool_has_no_write_call_or_write_sql() -> None:
    write_methods = {"commit", "add_all", "flush", "merge"}
    session_write_methods = {"add", "delete"}
    write_builders = {"insert", "update", "delete"}
    for path in _surface_files():
        tree = ast.parse(path.read_text("utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func = node.func
                if isinstance(func, ast.Attribute):
                    assert func.attr not in write_methods, f"{path.name}: {func.attr}"
                    receiver = getattr(func.value, "id", None)
                    if receiver == "session":
                        assert func.attr not in session_write_methods, path.name
                if isinstance(func, ast.Name):
                    assert func.id not in write_builders, f"{path.name}: {func.id}"
            if isinstance(node, ast.ImportFrom) and node.module == "sqlalchemy":
                imported = {alias.name for alias in node.names}
                assert not imported & write_builders, path.name


def _args(tmp_path: Path, **overrides: Any) -> Any:
    salt = tmp_path / "salt"
    salt.write_bytes(SALT + b"\n")
    os.chmod(salt, 0o600)
    argv = [
        "--database-url-env",
        "UNUSED_FOR_INJECTED_FACTORY",
        "--salt-file",
        str(overrides.get("salt_file", salt)),
        "--out",
        str(overrides.get("out", tmp_path / "export")),
        "--since",
        "2026-06-01",
        "--until",
        "2026-07-01",
        "--code-commit",
        "c" * 40,
    ]
    return cli._parser().parse_args(argv)


@pytest.mark.asyncio
async def test_a_run_over_empty_ledgers_writes_a_manifest_with_zero_rows(
    tmp_path: Path, capsys
) -> None:
    session = _RecordingSession()
    manifest = await cli.run(
        _args(tmp_path), session_factory=lambda: session, repo_root=REPO_ROOT
    )
    assert session.rolled_back == 1
    assert all(table["row_count"] == 0 for table in manifest["tables"].values())
    assert manifest["code_commit"] == "c" * 40
    assert manifest["window"]["since_utc"] == "2026-06-01T00:00:00+00:00"
    on_disk = json.loads((tmp_path / "export" / MANIFEST_NAME).read_text("utf-8"))
    assert on_disk["salt_fingerprint"] == manifest["salt_fingerprint"]
    assert SALT.decode() not in capsys.readouterr().out


@pytest.mark.asyncio
async def test_a_salt_file_others_can_read_is_refused(tmp_path: Path) -> None:
    args = _args(tmp_path)
    os.chmod(args.salt_file, 0o644)
    with pytest.raises(ValueError, match="chmod 600"):
        await cli.run(
            args, session_factory=lambda: _RecordingSession(), repo_root=REPO_ROOT
        )


def test_a_salt_file_inside_the_repository_is_refused(tmp_path: Path) -> None:
    fake_repo = tmp_path / "repo"
    fake_repo.mkdir()
    inside = fake_repo / "salt"
    inside.write_bytes(SALT)
    args = _args(tmp_path, salt_file=inside)
    with pytest.raises(ValueError, match="outside the repository"):
        cli.load_salt(args, repo_root=fake_repo)


def test_a_salt_from_the_environment_is_never_in_the_error(monkeypatch) -> None:
    monkeypatch.setenv("DOTS_SALT_FOR_TEST", "too-short-salt-value")
    args = cli._parser().parse_args(
        ["--database-url-env", "D", "--salt-env", "DOTS_SALT_FOR_TEST", "--out", "/x"]
    )
    assert cli.load_salt(args, repo_root=REPO_ROOT) == b"too-short-salt-value"


def test_output_inside_the_repository_is_refused() -> None:
    with pytest.raises(OutputDirectoryError):
        resolve_output_directory(REPO_ROOT / "dots-out", repo_root=REPO_ROOT)


def test_a_non_empty_output_directory_is_refused(tmp_path: Path) -> None:
    (tmp_path / "old.csv").write_text("x", "utf-8")
    with pytest.raises(OutputDirectoryError):
        resolve_output_directory(tmp_path, repo_root=REPO_ROOT)


def test_a_database_url_problem_never_quotes_the_url(monkeypatch) -> None:
    monkeypatch.setenv("DOTS_DB_FOR_TEST", "mysql://user:hunter2@host/db")
    with pytest.raises(ValueError) as raised:
        cli._database_url("DOTS_DB_FOR_TEST")
    assert "hunter2" not in str(raised.value)
    assert raised.value.__cause__ is None


def test_a_url_without_the_asyncpg_driver_is_refused(monkeypatch) -> None:
    monkeypatch.setenv("DOTS_DB_FOR_TEST", "postgresql://user:hunter2@host/db")
    with pytest.raises(ValueError, match="postgresql\\+asyncpg") as raised:
        cli._database_url("DOTS_DB_FOR_TEST")
    assert "hunter2" not in str(raised.value)


def test_an_asyncpg_url_is_accepted(monkeypatch) -> None:
    monkeypatch.setenv("DOTS_DB_FOR_TEST", "postgresql+asyncpg://user:pw@host/db")
    assert cli._database_url("DOTS_DB_FOR_TEST").startswith("postgresql+asyncpg://")


def test_default_window_is_the_last_six_months() -> None:
    now = datetime(2026, 11, 1, tzinfo=UTC)
    args = cli._parser().parse_args(
        ["--database-url-env", "D", "--salt-env", "S", "--out", "/x"]
    )
    window = cli.resolve_window(args, now=now)
    assert window.until == now
    assert window.since == datetime(2026, 5, 1, tzinfo=UTC)


def test_six_months_before_the_31st_clamps_to_the_month_end() -> None:
    assert reader.default_since(datetime(2026, 8, 31, tzinfo=UTC)) == datetime(
        2026, 2, 28, tzinfo=UTC
    )


def test_an_inverted_window_is_refused() -> None:
    args = cli._parser().parse_args(
        [
            "--database-url-env",
            "D",
            "--salt-env",
            "S",
            "--out",
            "/x",
            "--since",
            "2026-07-01",
            "--until",
            "2026-06-01",
        ]
    )
    with pytest.raises(ValueError, match="before"):
        cli.resolve_window(args, now=datetime(2026, 11, 1, tzinfo=UTC))


def test_a_missing_commit_answer_refuses_the_run(monkeypatch) -> None:
    monkeypatch.setattr(cli, "_git", lambda *_a: None)
    with pytest.raises(ValueError, match="--code-commit"):
        cli.resolve_code_commit(None)
