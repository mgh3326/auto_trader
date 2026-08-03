"""Execution-order test for the dry-run CLI: the guard must be statement #1.

The R2 review found the failure by mocking the connection and replaying
`main()`, which surfaced four statements running on the target *before* the
guard. Static scanning alone could not have shown that ordering, so the same
technique is reproduced here as a standing test.

Every statement the target connection receives is recorded, in order. The
assertions are about the *sequence*, not about any single string.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "kr_backfill_dryrun_order", REPO / "research/kr_backfill/dryrun_upsert.py"
)
dryrun = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dryrun)


class StopAfterGuard(RuntimeError):
    """Ends the run at the first write so the test needs no real database."""


class RecordingConn:
    """Records every statement, in order, and can lie about the database."""

    def __init__(self, *, database: str, user_tables: int = 0, owned: bool = True):
        self.database = database
        self.user_tables = user_tables
        self.owned = owned
        self.statements: list[tuple[str, str]] = []  # (kind, sql)
        self.stop_on_execute = False

    # -- asyncpg surface -------------------------------------------------
    async def fetchval(self, sql: str, *args):
        self.statements.append(("read", sql))
        if sql == dryrun.GUARD_FIRST_SQL:
            return self.database
        if sql == dryrun.GUARD_SERVER_ADDR_SQL:
            return "127.0.0.1"
        if sql == dryrun.GUARD_EMPTY_DB_SQL:
            return self.user_tables
        if sql == dryrun.GUARD_OWNER_SQL:
            return self.owned
        if "pg_database" in sql:
            return 0
        return None

    async def execute(self, sql: str, *args):
        self.statements.append(("write", sql))
        if self.stop_on_execute:
            raise StopAfterGuard(sql)
        return "CREATE"

    async def fetchrow(self, sql: str, *args):
        self.statements.append(("read", sql))
        return None

    async def close(self):
        return None

    # -- helpers ---------------------------------------------------------
    @property
    def writes(self) -> list[str]:
        return [sql for kind, sql in self.statements if kind == "write"]

    def statements_before(self, sql: str) -> list[tuple[str, str]]:
        for index, (_kind, recorded) in enumerate(self.statements):
            if recorded == sql:
                return self.statements[:index]
        raise AssertionError(f"{sql!r} was never executed")


def _patch_connections(monkeypatch, admin: RecordingConn, target: RecordingConn):
    handed_out: list[RecordingConn] = []

    async def fake_connect(dsn, *a, **kw):
        conn = admin if not handed_out else target
        handed_out.append(conn)
        return conn

    monkeypatch.setattr(dryrun.asyncpg, "connect", fake_connect)
    return handed_out


async def _run_main(monkeypatch, tmp_path, target: RecordingConn) -> Exception | None:
    admin = RecordingConn(database=dryrun.ADMIN_DATABASE)
    _patch_connections(monkeypatch, admin, target)
    monkeypatch.setattr(
        dryrun.sys, "argv", ["dryrun_upsert.py", "--out", str(tmp_path / "out.json")]
    )
    try:
        await dryrun.main()
    except Exception as exc:  # noqa: BLE001 — the assertions inspect it
        return exc
    return None


# --- the exact scenario the reviewer used ------------------------------


@pytest.mark.asyncio
async def test_no_statement_reaches_a_misrouted_target_before_the_guard(
    monkeypatch, tmp_path
):
    """Target reports the operational database, as in the R2 finding."""
    target = RecordingConn(database="auto_trader", user_tables=412)

    exc = await _run_main(monkeypatch, tmp_path, target)

    assert isinstance(exc, dryrun.ScratchGuardViolation), exc
    assert "deny-listed" in str(exc)

    # The whole point: nothing was written to it.
    assert target.writes == [], f"statements ran before the guard: {target.writes}"
    # And the very first thing asked of it was the guard's read.
    assert target.statements[0] == ("read", dryrun.GUARD_FIRST_SQL)


@pytest.mark.asyncio
async def test_a_populated_database_is_refused_before_any_write(monkeypatch, tmp_path):
    """Name looks fine, but the database is in use — emptiness is the backstop."""
    target = RecordingConn(
        database=f"{dryrun.SCRATCH_PREFIX}000000000000", user_tables=87
    )
    # main() generates its own name, so force the name check to agree.
    monkeypatch.setattr(dryrun.uuid, "uuid4", lambda: _FixedUUID("000000000000"))

    exc = await _run_main(monkeypatch, tmp_path, target)

    assert isinstance(exc, dryrun.ScratchGuardViolation), exc
    assert "user table" in str(exc)
    assert target.writes == []


@pytest.mark.asyncio
async def test_guard_is_the_first_statement_on_the_happy_path(monkeypatch, tmp_path):
    """When the guard passes, it still ran before every write."""
    target = RecordingConn(database=f"{dryrun.SCRATCH_PREFIX}111111111111")
    target.stop_on_execute = True
    monkeypatch.setattr(dryrun.uuid, "uuid4", lambda: _FixedUUID("111111111111"))

    exc = await _run_main(monkeypatch, tmp_path, target)
    assert isinstance(exc, StopAfterGuard), exc

    kinds = [kind for kind, _ in target.statements]
    first_write = kinds.index("write")
    assert set(kinds[:first_write]) == {"read"}, target.statements
    assert target.statements[0] == ("read", dryrun.GUARD_FIRST_SQL)

    # The first write is a research schema creation, never a public one.
    assert target.statements[first_write][1] == "CREATE SCHEMA IF NOT EXISTS research"


@pytest.mark.asyncio
async def test_no_public_reference_is_ever_sent_to_the_target(monkeypatch, tmp_path):
    target = RecordingConn(database=f"{dryrun.SCRATCH_PREFIX}222222222222")
    target.stop_on_execute = True
    monkeypatch.setattr(dryrun.uuid, "uuid4", lambda: _FixedUUID("222222222222"))

    await _run_main(monkeypatch, tmp_path, target)

    leaked = [sql for _kind, sql in target.statements if "public." in sql.lower()]
    assert not leaked, f"'public.' was sent to the target: {leaked}"


@pytest.mark.asyncio
async def test_guard_itself_only_reads(monkeypatch, tmp_path):
    """A guard that writes to decide is the R2 mistake in another costume."""
    target = RecordingConn(database=f"{dryrun.SCRATCH_PREFIX}333333333333")
    target.stop_on_execute = True
    monkeypatch.setattr(dryrun.uuid, "uuid4", lambda: _FixedUUID("333333333333"))

    await _run_main(monkeypatch, tmp_path, target)

    guard_sqls = {
        dryrun.GUARD_FIRST_SQL,
        dryrun.GUARD_SERVER_ADDR_SQL,
        dryrun.GUARD_EMPTY_DB_SQL,
        dryrun.GUARD_OWNER_SQL,
    }
    for kind, sql in target.statements:
        if sql in guard_sqls:
            assert kind == "read", f"guard statement executed as a write: {sql}"


class _FixedUUID:
    def __init__(self, hex_prefix: str):
        self.hex = hex_prefix + "0" * (32 - len(hex_prefix))
