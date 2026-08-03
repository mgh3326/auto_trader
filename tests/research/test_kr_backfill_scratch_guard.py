"""Adversarial tests for the dry-run scratch-database guard (R3 design).

Written as attempts to *reach* an operational database. The guard interrogates
the live connection, so the stub reports whatever a misrouted or hostile server
would report — that, not the DSN string, is the input that decides.

R2's version of these tests passed while the code was still broken, because it
only exercised the guard function and never the order in which `main()` calls
it. Ordering now has its own suite (`test_kr_backfill_main_order.py`); this file
covers the guard's own decisions.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]
_SPEC = importlib.util.spec_from_file_location(
    "kr_backfill_dryrun_guard", REPO / "research/kr_backfill/dryrun_upsert.py"
)
dryrun = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(dryrun)

GOOD_DB = f"{dryrun.SCRATCH_PREFIX}deadbeefcafe"


class StubConn:
    """Answers the guard's reads with whatever the 'server' claims."""

    def __init__(
        self,
        database: str,
        *,
        server_addr: str | None = "127.0.0.1",
        user_tables: int = 0,
        owned: bool = True,
    ):
        self.database = database
        self.server_addr = server_addr
        self.user_tables = user_tables
        self.owned = owned
        self.writes: list[str] = []

    async def fetchval(self, sql: str, *args):
        if sql == dryrun.GUARD_FIRST_SQL:
            return self.database
        if sql == dryrun.GUARD_SERVER_ADDR_SQL:
            return self.server_addr
        if sql == dryrun.GUARD_EMPTY_DB_SQL:
            return self.user_tables
        if sql == dryrun.GUARD_OWNER_SQL:
            return self.owned
        raise AssertionError(f"unexpected query: {sql}")

    async def execute(self, sql: str, *args):
        self.writes.append(sql)
        return "OK"


async def _guard(conn, expected=GOOD_DB):
    return await dryrun.assert_scratch_target(conn, expected)


@pytest.mark.asyncio
async def test_happy_path_passes():
    conn = StubConn(GOOD_DB)
    result = await _guard(conn)
    assert result["current_database"] == GOOD_DB
    assert result["database_was_empty"] is True
    assert conn.writes == [], "the guard must not write in order to decide"


# --- attempts to reach an operational database -------------------------


@pytest.mark.asyncio
async def test_operational_database_is_refused():
    conn = StubConn("auto_trader", user_tables=412)
    with pytest.raises(dryrun.ScratchGuardViolation, match="deny-listed"):
        await _guard(conn, expected="auto_trader")
    assert conn.writes == []


@pytest.mark.asyncio
async def test_maintenance_database_is_refused():
    with pytest.raises(dryrun.ScratchGuardViolation, match="deny-listed"):
        await _guard(StubConn("postgres"), expected="postgres")


@pytest.mark.asyncio
async def test_database_without_the_prefix_is_refused():
    with pytest.raises(dryrun.ScratchGuardViolation, match="prefix"):
        await _guard(StubConn("test_db"), expected="test_db")


@pytest.mark.asyncio
async def test_a_different_scratch_database_is_refused():
    other = f"{dryrun.SCRATCH_PREFIX}000000000000"
    with pytest.raises(dryrun.ScratchGuardViolation, match="this run created"):
        await _guard(StubConn(other))


# --- the emptiness backstop: an unknown name that is actually in use ---


@pytest.mark.asyncio
async def test_a_populated_database_is_refused_even_with_a_perfect_name():
    """The case a deny-list cannot cover: a name nobody thought to forbid.

    This is the check that survives a misrouted proxy — whatever the database is
    called, if it holds user tables it is not a fresh scratch database.
    """
    with pytest.raises(dryrun.ScratchGuardViolation, match="user table"):
        await _guard(StubConn(GOOD_DB, user_tables=1))


@pytest.mark.asyncio
async def test_a_database_owned_by_someone_else_is_refused():
    with pytest.raises(dryrun.ScratchGuardViolation, match="not owned"):
        await _guard(StubConn(GOOD_DB, owned=False))


# --- server-level attempts ---------------------------------------------


@pytest.mark.asyncio
async def test_a_remote_server_is_refused():
    """Even a correctly named empty database on a remote host is refused."""
    with pytest.raises(dryrun.ScratchGuardViolation, match="non-loopback"):
        await _guard(StubConn(GOOD_DB, server_addr="10.0.0.7"))


@pytest.mark.asyncio
async def test_unix_socket_is_accepted():
    """NULL server address means a unix socket, which is local by construction."""
    result = await _guard(StubConn(GOOD_DB, server_addr=None))
    assert result["loopback_ok"] is True


# --- structural: the caller cannot choose the server -------------------


def test_dsn_is_built_from_fixed_host_and_port():
    dsn = dryrun._dsn("some_db")
    assert f"@{dryrun.SCRATCH_HOST}:{dryrun.SCRATCH_PORT}/" in dsn
    assert dsn.endswith("/some_db")


def test_credentials_are_injectable_but_the_server_is_not(monkeypatch):
    monkeypatch.setenv("SCRATCH_PG_USER", "someone")
    monkeypatch.setenv("SCRATCH_PG_PASSWORD", "secret")
    dsn = dryrun._dsn("db")
    assert "someone" in dsn
    # host/port still fixed — no env var can move them
    assert f"@{dryrun.SCRATCH_HOST}:{dryrun.SCRATCH_PORT}/" in dsn


def test_no_admin_url_argument_remains():
    source = (REPO / "research/kr_backfill/dryrun_upsert.py").read_text(
        encoding="utf-8"
    )
    assert "--admin-url" not in source


def test_marker_table_is_gone():
    """R2 proved a marker cannot exist without a write before the guard."""
    assert not hasattr(dryrun, "MARKER_TABLE")
    source = (REPO / "research/kr_backfill/dryrun_upsert.py").read_text(
        encoding="utf-8"
    )
    assert "_kr_dryrun_scratch_marker" not in source


def test_operational_database_is_deny_listed_by_name():
    assert "auto_trader" in dryrun.DENY_DATABASES


def test_upsert_statement_targets_research_schema():
    assert "research.kr_candles_1m" in dryrun.BACKFILL_UPSERT_SQL
    assert "public." not in dryrun.BACKFILL_UPSERT_SQL


def test_all_guard_statements_are_selects():
    for sql in (
        dryrun.GUARD_FIRST_SQL,
        dryrun.GUARD_SERVER_ADDR_SQL,
        dryrun.GUARD_EMPTY_DB_SQL,
        dryrun.GUARD_OWNER_SQL,
    ):
        assert sql.strip().upper().startswith("SELECT"), sql
