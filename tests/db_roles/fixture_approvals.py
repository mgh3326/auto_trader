"""Generate signed local-only approval fixtures from a disposable database.

This generator refuses non-loopback targets, non-fixture databases, and any
TimescaleDB version other than the pinned 2.22.1 reproduction image.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

import asyncpg

from scripts.db_roles import core


def check_dsn(dsn: str) -> None:
    parsed = urlsplit(dsn)
    if parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise RuntimeError("fixture approval generation requires loopback")
    if parsed.path != "/auto_trader":
        raise RuntimeError("fixture approval generation requires auto_trader")
    if parsed.username != "mgh3326":
        raise RuntimeError("fixture approval generation requires fixture bootstrap role")


async def check_fixture(conn: asyncpg.Connection) -> None:
    row = await conn.fetchrow(
        "SELECT current_database() database_name, "
        "current_setting('server_version_num')::int version_num, "
        "(SELECT extversion FROM pg_extension WHERE extname='timescaledb') ts_version, "
        "(SELECT oid FROM pg_roles WHERE rolname='mgh3326') bootstrap_oid, "
        "to_regclass('public.t789_ticks') ticks, "
        "to_regclass('review.nhplug_mock_order_ledger') nhplug"
    )
    if (
        row["database_name"] != "auto_trader"
        or not 170000 <= row["version_num"] < 180000
        or row["ts_version"] != "2.22.1"
        or row["bootstrap_oid"] != 10
        or row["ticks"] is None
        or row["nhplug"] is None
    ):
        raise RuntimeError("database is not the pinned disposable fixture")


async def catalog_items(conn: asyncpg.Connection, *, functions_and_types: bool) -> list[dict]:
    items: list[dict] = []
    for kind, schema, name, identity_args in sorted(
        await core.app_catalog_keys(
            conn, include_functions_and_types=functions_and_types
        )
    ):
        item = {"kind": kind, "schema": schema, "name": name}
        if kind == "function":
            item["identity_args"] = identity_args
        obj = await core.catalog_object(conn, item)
        item["expected_owner"] = obj["owner"]
        if kind == "relation":
            item["relkind"] = obj["relkind"]
            if schema in core.APP_SCHEMAS:
                item["continuous_aggregate"] = bool(
                    await conn.fetchval(
                        "SELECT EXISTS (SELECT 1 FROM "
                        "timescaledb_information.continuous_aggregates "
                        "WHERE view_schema=$1 AND view_name=$2)",
                        schema,
                        name,
                    )
                )
        target = "at_migration_owner"
        if kind == "relation" and schema == "review" and name in core.PROTECTED:
            target = core.PROTECTED[name][0]
        if kind == "function" and schema == "review" and name in core.PROTECTED_FUNCTIONS:
            if name not in core.MIGRATION_FUNCTIONS:
                target = "nhplug_security_owner"
        item["target_owner"] = target
        items.append(item)
    return items


async def internal_items(conn: asyncpg.Connection) -> list[dict]:
    items = []
    for kind, schema, name, identity_args in sorted(await core.timescale_internal_keys(conn)):
        item = {
            "kind": kind,
            "schema": schema,
            "name": name,
            "identity_args": identity_args,
            "timescale_internal": True,
            "target_owner": "at_migration_owner",
        }
        obj = await core.catalog_object(conn, item)
        item["expected_owner"] = obj["owner"]
        item["relkind"] = obj["relkind"]
        items.append(item)
    return items


async def job_inventory(conn: asyncpg.Connection) -> list[list[str | int]]:
    rows = await conn.fetch(
        "SELECT job_id, owner::text owner FROM timescaledb_information.jobs "
        "WHERE job_id >= 1000 ORDER BY job_id"
    )
    return [[r["job_id"], r["owner"]] for r in rows]


async def graph_inventory(conn: asyncpg.Connection) -> list[list[str | None]]:
    keys = await core.app_catalog_keys(conn, include_functions_and_types=True)
    keys |= await core.timescale_internal_keys(conn)
    rows = []
    for kind, schema, name, identity_args in sorted(keys):
        item = {
            "kind": kind,
            "schema": schema,
            "name": name,
            "identity_args": identity_args,
            "timescale_internal": schema == "_timescaledb_internal",
        }
        obj = await core.catalog_object(conn, item)
        rows.append([kind, schema, name, identity_args, obj["owner"]])
    return rows


async def stage_record(conn: asyncpg.Connection, stage: int) -> dict:
    record: dict = {
        "approval_id": f"disposable-t789-stage-{stage}",
        "database": "auto_trader",
        "stage": stage,
    }
    if stage == 2:
        record["objects"] = await catalog_items(conn, functions_and_types=True)
        record["objects"].extend(await internal_items(conn))
        record["timescale_version"] = "2.22.1"
        record["timescale_jobs"] = await job_inventory(conn)
        record["timescale_graph_proof"] = "operator_verified"
    elif stage == 3:
        record["future_creators"] = {
            "at_migration_owner": ["public", "review", "research"],
            "nhplug_security_owner": ["review"],
        }
        record["database_connect_policy"] = {
            "revoke_public": True,
            "keep_roles": [
                "at_app",
                "at_migration_owner",
                "at_migration_runner",
                "postgres",
            ],
        }
        items = await catalog_items(conn, functions_and_types=False)
        for item in items:
            if item["kind"] != "relation":
                continue
            if item["relkind"] == "S":
                if item["name"] == "stock_info_id_seq":
                    item["app_privileges"] = "U"
                elif item["schema"] == "review" and item["name"] in {
                    "nhplug_mock_order_ledger_id_seq",
                    "kiwoom_authority_attempts_id_seq",
                    "kiwoom_authority_cessation_receipts_id_seq",
                }:
                    item["app_privileges"] = "U"
                else:
                    item["app_privileges"] = ""
            elif item["schema"] == "public" and item["name"] == "stock_info":
                item["app_privileges"] = "raw"
            else:
                item["app_privileges"] = ""
        for name, signature in core.PROTECTED_FUNCTIONS.items():
            item = {
                "kind": "function",
                "schema": "review",
                "name": name,
                "identity_args": signature,
                "app_privileges": "X" if name in core.APP_EXECUTE_HELPERS else "",
            }
            item["expected_owner"] = (await core.catalog_object(conn, item))["owner"]
            items.append(item)
        record["objects"] = items
        managed = {
            "PUBLIC",
            "at_app",
            "nhplug_operator",
            "nhplug_security_owner",
            "at_migration_owner",
        }
        preserved = set()
        for item in items:
            obj = await core.catalog_object(conn, item)
            for row in await core.acl_rows(conn, item):
                if row["grantee"] not in managed | {obj["owner"]}:
                    preserved.add(row["grantee"])
        record["preserved_acl_grantees"] = sorted(preserved)
    elif stage == 4:
        record.update(
            {
                "login_inventory": [
                    {"login": "at_api_login", "group": "at_app"},
                    {"login": "at_scheduler_login", "group": "at_app"},
                ],
                "secret_mapping_ref": "disposable-fixture-no-secret",
                "deployment_template_ref": "disposable-fixture",
                "migration_runner_login": "at_migration_runner",
                "migration_identity_proof": {
                    "session_user": "at_migration_runner",
                    "current_user": "at_migration_owner",
                    "database": "auto_trader",
                    "observed_at": "disposable-fixture",
                },
                "consumer_inventory": [".env.api", ".env.scheduler"],
                "consumer_observations": [
                    {
                        "consumer": ".env.api",
                        "session_user": "at_api_login",
                        "current_user": "at_api_login",
                        "database": "auto_trader",
                        "observed_at": "disposable-fixture",
                    },
                    {
                        "consumer": ".env.scheduler",
                        "session_user": "at_scheduler_login",
                        "current_user": "at_scheduler_login",
                        "database": "auto_trader",
                        "observed_at": "disposable-fixture",
                    },
                ],
            }
        )
    elif stage == 5:
        sessions = await conn.fetch(
            "SELECT pid, datname, application_name, client_addr::text AS client_addr, "
            "backend_type FROM pg_stat_activity "
            "WHERE usename='postgres' AND datname IS NOT NULL "
            "AND pid<>pg_backend_pid() ORDER BY pid"
        )
        clients = await conn.fetch(
            "SELECT pid,usename,application_name,client_addr::text AS client_addr "
            "FROM pg_stat_activity WHERE datname=current_database() "
            "AND usename IN ('at_api_login','at_scheduler_login') "
            "ORDER BY usename"
        )
        if [r["usename"] for r in clients] != ["at_api_login", "at_scheduler_login"]:
            raise RuntimeError("two live application backends required for Stage 5")
        record.update(
            {
                "legacy_login": "postgres",
                "legacy_classification": "shared_infrastructure",
                "legacy_non_app_sessions": [
                    {**dict(s), "classification": "dba", "source_ref": "disposable-fixture"}
                    for s in sessions
                ],
                "app_input_removal_proof": {
                    "consumers": [".env.api", ".env.scheduler"],
                    "observed_at": "disposable-fixture",
                    "old_app_sessions_after_drain": 0,
                },
                "secret_rotation_proof": {
                    "completed_consumers": [".env.api", ".env.scheduler"],
                    "observed_at": "disposable-fixture",
                    "postgres_credential_unchanged": True,
                },
                "timer_observation_proof": {
                    "installed_timers": ["at-pg-backup.timer"],
                    "observed_at": "disposable-fixture",
                },
                "backup_identity_proof": {
                    "role": "postgres",
                    "execution_user": "root",
                    "database_dumps": ["auto_trader", "handoffkeep"],
                    "globals_only": True,
                    "independent_of_app_secret": True,
                    "observed_at": "disposable-fixture",
                },
                "prefect_identity_proof": {
                    "role": "postgres",
                    "database": "prefect",
                    "observed_at": "disposable-fixture",
                },
                "timescale_graph_proof": await graph_inventory(conn),
                "timescale_job_proof": await job_inventory(conn),
                "new_login_observation_proof": [
                    {
                        "consumer": ".env.api" if r["usename"] == "at_api_login" else ".env.scheduler",
                        "session_user": r["usename"],
                        "current_user": r["usename"],
                        "database": "auto_trader",
                        "observed_at": "disposable-fixture",
                        "pid": r["pid"],
                        "application_name": r["application_name"],
                        "client_addr": r["client_addr"],
                    }
                    for r in clients
                ],
                "timescale_version": "2.22.1",
            }
        )
    else:
        raise RuntimeError("unsupported fixture stage")
    return record


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--stage", type=int, required=True, choices=(2, 3, 4, 5))
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    dsn = os.environ["DB_ROLES_DSN"]
    check_dsn(dsn)
    conn = await asyncpg.connect(dsn=dsn, timeout=5)
    try:
        await check_fixture(conn)
        record = await stage_record(conn, args.stage)
    finally:
        await conn.close()
    raw = (json.dumps(record, indent=2, sort_keys=True) + "\n").encode()
    args.output.write_bytes(raw)
    print(hashlib.sha256(raw).hexdigest())


if __name__ == "__main__":
    asyncio.run(main())
