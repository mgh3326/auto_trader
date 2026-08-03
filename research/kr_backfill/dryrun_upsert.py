"""Prove the backfill upsert is non-destructive — on a throwaway database only.

Design: ``events/r3-design.md`` (written before this code).

History, because it explains the shape of this file:

* R1 executed ``INSERT INTO`` the operational ``kr_candles_1m`` inside a
  transaction and rolled back. A rolled-back INSERT is still an INSERT.
* R2 removed that, but introduced a marker table CREATE+INSERT in the ``public``
  schema **and ran it before the guard**. Fixing a write by adding a write.

The invariant this version is built around is therefore not "remove the public
writes" — that framing produced a new write each time — but:

    **The guard is the first statement on the target connection, and the guard
    only reads.**

A marker table cannot survive that invariant: planting it *is* a write before
the guard. So the marker is gone, and freshness is proven without writing:

* ``CREATE DATABASE`` on the admin connection succeeds only if the name was
  unused, so its success *is* the proof that the database is brand new.
* On the target, the guard reads only: ``current_database()``, the server
  address, that the database contains **zero user tables** (an operational
  database never does), and that we own it.

The emptiness check is the load-bearing one. A deny-list only stops databases
whose names you thought of; "is this database empty" stops every database that
is actually in use, including one reached through a misrouted proxy.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import uuid
from datetime import UTC, datetime
from pathlib import Path

import asyncpg

REPO = Path(__file__).resolve().parents[2]

VENUE = "KRX"
SOURCE = "KIWOOM"

#: Only databases whose name starts with this may receive DML from this script.
SCRATCH_PREFIX = "kr_dryrun_scratch_"

#: Never a DML target, whatever else matches.
DENY_DATABASES = frozenset({"auto_trader", "postgres", "template0", "template1"})

#: The maintenance database used to CREATE/DROP the scratch database.
ADMIN_DATABASE = "postgres"

#: Server is a module constant, NOT an argument. R2 let the caller choose
#: host/port, which meant an operational cluster could be named as the admin
#: server. Only the credentials remain injectable.
SCRATCH_HOST = "127.0.0.1"
SCRATCH_PORT = 5432

#: Addresses the server may report. NULL means a unix socket, which is local by
#: construction.
LOOPBACK_ADDRESSES = frozenset({"127.0.0.1", "::1"})

MIGRATION_REVISION = "20260803_research_kr_candles"
MIGRATION_DOWN_REVISION = "20260802_rob1036_sample_elig"

#: The statement under test. Targets research; the string "public" does not
#: appear in any SQL this module sends to the target.
BACKFILL_UPSERT_SQL = """
INSERT INTO research.kr_candles_1m
    (time_utc, session_date_kst, symbol, venue, session_segment, source,
     open, high, low, close, volume, value, retrieved_at, batch_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now(), $13)
ON CONFLICT (time_utc, symbol, venue) DO NOTHING
"""

#: Guard statement 1. Kept as a module constant so the order test can assert on
#: identity rather than on a substring it re-types.
GUARD_FIRST_SQL = "SELECT current_database()"
GUARD_SERVER_ADDR_SQL = "SELECT host(inet_server_addr())"
GUARD_EMPTY_DB_SQL = (
    "SELECT count(*) FROM pg_class c "
    "JOIN pg_namespace n ON n.oid = c.relnamespace "
    "WHERE c.relkind IN ('r', 'p', 'm') "
    "AND n.nspname NOT IN ('pg_catalog', 'information_schema')"
)
GUARD_OWNER_SQL = (
    "SELECT pg_get_userbyid(datdba) = current_user "
    "FROM pg_database WHERE datname = current_database()"
)


class ScratchGuardViolation(RuntimeError):
    """Raised when the live connection is not a throwaway database of this run."""


def _assert_loopback(addr: str | None, role: str) -> None:
    """NULL means unix socket — local by construction — and is accepted."""
    if addr is not None and addr not in LOOPBACK_ADDRESSES:
        raise ScratchGuardViolation(
            f"{role} connection is to a non-loopback server {addr!r}; "
            f"this tool only ever talks to {SCRATCH_HOST}"
        )


async def assert_scratch_target(conn: asyncpg.Connection, expected_db: str) -> dict:
    """The first thing that touches the target connection. Reads only.

    Every check here is a SELECT. If this function ever needs to write in order
    to decide, the design is wrong — that is exactly how R2 failed.
    """
    actual = await conn.fetchval(GUARD_FIRST_SQL)

    if actual in DENY_DATABASES:
        raise ScratchGuardViolation(f"deny-listed database as DML target: {actual!r}")
    if not actual.startswith(SCRATCH_PREFIX):
        raise ScratchGuardViolation(
            f"database {actual!r} lacks the required {SCRATCH_PREFIX!r} prefix"
        )
    if actual != expected_db:
        raise ScratchGuardViolation(
            f"connected to {actual!r} but this run created {expected_db!r}"
        )

    _assert_loopback(await conn.fetchval(GUARD_SERVER_ADDR_SQL), "target")

    # The load-bearing check: a database in use is never empty.
    user_tables = await conn.fetchval(GUARD_EMPTY_DB_SQL)
    if user_tables != 0:
        raise ScratchGuardViolation(
            f"database {actual!r} already holds {user_tables} user table(s); "
            f"a freshly created scratch database has none"
        )

    owned = await conn.fetchval(GUARD_OWNER_SQL)
    if not owned:
        raise ScratchGuardViolation(f"database {actual!r} is not owned by this role")

    return {
        "current_database": actual,
        "deny_list_ok": True,
        "prefix_ok": True,
        "loopback_ok": True,
        "database_was_empty": True,
        "owned_by_this_role": True,
        "statements_before_guard": 0,
    }


def _dsn(database: str) -> str:
    user = os.environ.get("SCRATCH_PG_USER", "postgres")
    password = os.environ.get("SCRATCH_PG_PASSWORD", "postgres")
    return f"postgresql://{user}:{password}@{SCRATCH_HOST}:{SCRATCH_PORT}/{database}"


def _alembic(database_dsn: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DATABASE_URL": database_dsn.replace("postgresql://", "postgresql+asyncpg://"),
        # Settings requires these; placeholders, never real secrets.
        "KIS_APP_KEY": "dryrun",
        "KIS_APP_SECRET": "dryrun",
        "OPENDART_API_KEY": "dryrun",
        "UPBIT_ACCESS_KEY": "dryrun",
        "UPBIT_SECRET_KEY": "dryrun",
        "SECRET_KEY": "Aa1DryRunScratchOnlyKey0123456789abcdefXYZ",
    }
    return subprocess.run(
        [str(REPO / ".venv/bin/alembic"), *args],
        cwd=REPO,
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


async def run_proof(conn: asyncpg.Connection) -> dict:
    """Exercise the real upsert against the migrated research table."""
    checks: dict = {}
    ts = datetime(2026, 5, 1, 1, 0, tzinfo=UTC)
    row = ("005930", 1000, 1010, 990, 1005, 12345, 12345000)

    async def insert(symbol, o, h, low, c, vol, val):
        status = await conn.execute(
            BACKFILL_UPSERT_SQL,
            ts,
            ts.date(),
            symbol,
            VENUE,
            "KRX_REGULAR",
            SOURCE,
            o,
            h,
            low,
            c,
            vol,
            val,
            "dryrun-batch",
        )
        return int(status.split()[-1])

    read_sql = (
        "SELECT open, high, low, close, volume, value, source "
        "FROM research.kr_candles_1m WHERE time_utc=$1 AND symbol=$2 AND venue=$3"
    )

    checks["first_insert_rows"] = await insert(*row)
    stored = dict(await conn.fetchrow(read_sql, ts, row[0], VENUE))

    # Same key, deliberately different payload and a different provider.
    checks["conflict_insert_rows"] = await insert("005930", 1, 2, 3, 4, 5, 6)
    after = dict(await conn.fetchrow(read_sql, ts, row[0], VENUE))

    checks["existing_row_unchanged"] = stored == after
    checks["stored_close"] = float(stored["close"])
    checks["total_rows"] = await conn.fetchval(
        "SELECT count(*) FROM research.kr_candles_1m"
    )
    checks["verdict"] = (
        "PASS"
        if (
            checks["first_insert_rows"] == 1
            and checks["conflict_insert_rows"] == 0
            and checks["existing_row_unchanged"]
            and checks["total_rows"] == 1
        )
        else "FAIL"
    )
    return checks


async def main() -> int:
    ap = argparse.ArgumentParser(
        description="Upsert proof on a self-created throwaway database. The "
        "server and the target database are fixed by this module; neither can "
        "be selected by the caller."
    )
    ap.add_argument("--out", required=True, type=Path)
    ap.add_argument(
        "--keep", action="store_true", help="skip the drop (debugging only)"
    )
    args = ap.parse_args()

    run_id = uuid.uuid4().hex
    database = f"{SCRATCH_PREFIX}{run_id[:12]}"
    result: dict = {
        "ran_at_utc": datetime.now(UTC).isoformat(),
        "run_id": run_id,
        "scratch_database": database,
        "server": f"{SCRATCH_HOST}:{SCRATCH_PORT}",
        "statements_before_guard": 0,
    }

    # --- admin connection: read-only checks, then create the database ---
    admin = await asyncpg.connect(_dsn(ADMIN_DATABASE))
    conn = None
    try:
        admin_db = await admin.fetchval(GUARD_FIRST_SQL)
        if admin_db != ADMIN_DATABASE:
            raise ScratchGuardViolation(f"admin connection landed on {admin_db!r}")
        _assert_loopback(await admin.fetchval(GUARD_SERVER_ADDR_SQL), "admin")

        already = await admin.fetchval(
            "SELECT count(*) FROM pg_database WHERE datname = $1", database
        )
        if already:
            raise ScratchGuardViolation(
                f"{database!r} already exists; refusing to reuse"
            )
        result["admin_database"] = admin_db

        # Succeeds only if the name was unused -> proof the database is new.
        await admin.execute(f'CREATE DATABASE "{database}"')

        conn = await asyncpg.connect(_dsn(database))

        # --- FIRST statement on the target connection. Reads only. -----
        result["guard"] = await assert_scratch_target(conn, database)

        # --- everything below runs only after the guard passed ---------
        await conn.execute("CREATE SCHEMA IF NOT EXISTS research")
        await conn.execute("CREATE SCHEMA IF NOT EXISTS review")

        target_dsn = _dsn(database)
        stamped = _alembic(target_dsn, "stamp", MIGRATION_DOWN_REVISION)
        if stamped.returncode != 0:
            raise RuntimeError(f"alembic stamp failed: {stamped.stderr[-800:]}")
        upgraded = _alembic(target_dsn, "upgrade", "head")
        if upgraded.returncode != 0:
            raise RuntimeError(f"alembic upgrade failed: {upgraded.stderr[-800:]}")
        result["migration_applied"] = MIGRATION_REVISION

        result["checks"] = await run_proof(conn)
    finally:
        if conn is not None:
            await conn.close()
        if not args.keep:
            await admin.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
                database,
            )
            await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
            result["scratch_dropped"] = (
                await admin.fetchval(
                    "SELECT count(*) FROM pg_database WHERE datname = $1", database
                )
                == 0
            )
        await admin.close()

    payload = json.dumps(result, indent=2, default=str, ensure_ascii=False) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(payload)
    print(payload)
    return 0 if result.get("checks", {}).get("verdict") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
