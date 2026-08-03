"""Prove the backfill upsert is non-destructive — on a throwaway database only.

History: an earlier version of this file executed
``INSERT INTO public.kr_candles_1m`` inside a transaction against the
**operational** database and relied on a rollback sentinel. That was a real
violation: a rolled-back INSERT is still an INSERT (WAL, locks, dead tuple), and
the operational table must not be written at all. See
``events/incident-public-dml-20260803.md``.

This version cannot reach an operational database:

* No ``public.*`` DML statement exists in this file. The backfill target is
  ``research.kr_candles_1m`` (the storage decision moved it there), so a
  ``public`` write has no reason to exist.
* **The DML target database is generated here, not accepted from the caller.**
  There is no argument that names it. The admin connection is forced to the
  ``postgres`` maintenance database, so a caller cannot aim it at an
  operational database either.
* Before any DML, :func:`assert_scratch_target` interrogates the **live
  connection** — ``current_database()``, a deny-list, the required scratch name
  prefix, and a marker row written by this very run. String parsing of the DSN
  is not treated as proof.
* Any violation raises :class:`ScratchGuardViolation`. Nothing is skipped
  quietly.

The scratch database is built by running the real alembic migration, so the
upsert is proven against the actual shipped DDL rather than a hand-copy.
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

#: Never a DML target, whatever else matches. Belt on top of the prefix rule.
DENY_DATABASES = frozenset({"auto_trader", "postgres", "template0", "template1"})

#: The maintenance database used to CREATE/DROP the scratch database. Forced —
#: a caller cannot redirect the admin connection at an operational database.
ADMIN_DATABASE = "postgres"

MIGRATION_REVISION = "20260803_research_kr_candles"
MIGRATION_DOWN_REVISION = "20260802_rob1036_sample_elig"

#: The statement under test. Targets research, never public.
BACKFILL_UPSERT_SQL = """
INSERT INTO research.kr_candles_1m
    (time_utc, session_date_kst, symbol, venue, session_segment, source,
     open, high, low, close, volume, value, retrieved_at, batch_id)
VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, now(), $13)
ON CONFLICT (time_utc, symbol, venue) DO NOTHING
"""

MARKER_TABLE = "public._kr_dryrun_scratch_marker"


class ScratchGuardViolation(RuntimeError):
    """Raised when the live connection is not a throwaway database of this run."""


async def assert_scratch_target(
    conn: asyncpg.Connection, expected_db: str, run_id: str
) -> dict:
    """Interrogate the live connection. Never trust the DSN string alone."""
    actual = await conn.fetchval("SELECT current_database()")

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

    marker_exists = await conn.fetchval(
        f"SELECT to_regclass('{MARKER_TABLE}') IS NOT NULL"
    )
    if not marker_exists:
        raise ScratchGuardViolation(
            f"{MARKER_TABLE} missing; not a scratch database of this run"
        )

    marker_run = await conn.fetchval(f"SELECT run_id FROM {MARKER_TABLE}")
    if marker_run != run_id:
        raise ScratchGuardViolation(
            f"marker run_id {marker_run!r} does not match this run {run_id!r}"
        )

    return {
        "current_database": actual,
        "prefix_ok": True,
        "deny_list_ok": True,
        "marker_run_id_matches": True,
    }


def _admin_dsn(admin_url: str) -> str:
    """Force the admin connection onto the maintenance database."""
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(admin_url.replace("postgresql+asyncpg://", "postgresql://"))
    return urlunsplit((parts.scheme, parts.netloc, f"/{ADMIN_DATABASE}", "", ""))


def _target_dsn(admin_url: str, database: str) -> str:
    from urllib.parse import urlsplit, urlunsplit

    parts = urlsplit(admin_url.replace("postgresql+asyncpg://", "postgresql://"))
    return urlunsplit((parts.scheme, parts.netloc, f"/{database}", "", ""))


def _alembic(database_dsn: str, *args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "DATABASE_URL": database_dsn.replace("postgresql://", "postgresql+asyncpg://"),
        # Settings requires these; values are placeholders, never real secrets.
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

    checks["first_insert_rows"] = await insert(*row)

    stored = dict(
        await conn.fetchrow(
            "SELECT open, high, low, close, volume, value, source "
            "FROM research.kr_candles_1m WHERE time_utc=$1 AND symbol=$2 AND venue=$3",
            ts,
            row[0],
            VENUE,
        )
    )

    # Same key, deliberately different payload and a different provider.
    checks["conflict_insert_rows"] = await insert("005930", 1, 2, 3, 4, 5, 6)

    after = dict(
        await conn.fetchrow(
            "SELECT open, high, low, close, volume, value, source "
            "FROM research.kr_candles_1m WHERE time_utc=$1 AND symbol=$2 AND venue=$3",
            ts,
            row[0],
            VENUE,
        )
    )
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
        description="Upsert proof on a self-created throwaway database. "
        "There is deliberately no option to choose the DML target database."
    )
    ap.add_argument(
        "--admin-url",
        default="postgresql://postgres:postgres@localhost:5432/postgres",
        help=f"server to create the scratch DB on; forced onto the {ADMIN_DATABASE!r} database",
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
        "targets_public_schema": False,
        "operational_db_touched": False,
    }

    admin = await asyncpg.connect(_admin_dsn(args.admin_url))
    admin_db = await admin.fetchval("SELECT current_database()")
    if admin_db != ADMIN_DATABASE:
        await admin.close()
        raise ScratchGuardViolation(f"admin connection landed on {admin_db!r}")
    result["admin_database"] = admin_db

    conn = None
    try:
        await admin.execute(f'CREATE DATABASE "{database}"')
        target_dsn = _target_dsn(args.admin_url, database)

        conn = await asyncpg.connect(target_dsn)
        await conn.execute(
            f"CREATE TABLE {MARKER_TABLE} (run_id TEXT NOT NULL, created_at TIMESTAMPTZ DEFAULT now())"
        )
        await conn.execute(f"INSERT INTO {MARKER_TABLE} (run_id) VALUES ($1)", run_id)
        await conn.execute("CREATE SCHEMA IF NOT EXISTS research")
        await conn.execute("CREATE SCHEMA IF NOT EXISTS review")

        stamped = _alembic(target_dsn, "stamp", MIGRATION_DOWN_REVISION)
        if stamped.returncode != 0:
            raise RuntimeError(f"alembic stamp failed: {stamped.stderr[-800:]}")
        upgraded = _alembic(target_dsn, "upgrade", "head")
        if upgraded.returncode != 0:
            raise RuntimeError(f"alembic upgrade failed: {upgraded.stderr[-800:]}")
        result["migration_applied"] = MIGRATION_REVISION

        # --- the guard, immediately before any DML --------------------
        result["guard"] = await assert_scratch_target(conn, database, run_id)

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
            remaining = await admin.fetchval(
                "SELECT count(*) FROM pg_database WHERE datname = $1", database
            )
            result["scratch_dropped"] = remaining == 0
        await admin.close()

    payload = json.dumps(result, indent=2, default=str, ensure_ascii=False) + "\n"
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(payload)
    print(payload)
    return 0 if result.get("checks", {}).get("verdict") == "PASS" else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
