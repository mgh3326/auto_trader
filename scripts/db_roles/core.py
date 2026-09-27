"""Guarded, operator-driven database role cutover stages.

Only catalog DDL is performed here.  The connection URL comes from an explicitly
named process environment variable.  No URL, password, or connection exception is
rendered in normal output.  Production use requires a separately approved record.
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import asyncpg

if __package__:
    from . import timescale_jobs
else:
    import timescale_jobs

GROUPS = ("at_app", "at_migration_owner", "nhplug_security_owner")
ALL_GROUPS = GROUPS + ("nhplug_operator",)
APP_SCHEMAS = ("public", "review", "research", "paper")
PROTECTED = {
    "nhplug_mock_key_version": ("nhplug_security_owner", "r", "ar"),
    "nhplug_success_proof_code": ("nhplug_security_owner", "r", "ar"),
    "nhplug_no_order_proof_code": ("nhplug_security_owner", "r", "ar"),
    "nhplug_mock_operator_authorization": ("nhplug_security_owner", "r", "ar"),
    "nhplug_mock_account_ref": ("at_migration_owner", "ar", ""),
    "nhplug_mock_account_binding": ("at_migration_owner", "ar", ""),
    "nhplug_mock_order_ledger": ("at_migration_owner", "arw", "r"),
    "kiwoom_authority_attempts": ("at_migration_owner", "ar", ""),
    "kiwoom_authority_cessation_receipts": ("at_migration_owner", "ar", ""),
}
PROTECTED_FUNCTIONS = {
    "nhplug_body_field": "tag text, v text",
    "nhplug_body_digest_v1": "op text, side text, sym text, qty bigint, px bigint, org text, scope text, acct text",
    "nhplug_consume_authorization": "auth_id uuid, expected_kind text, target bigint, account uuid, trading_day date, digest text, candidate text, consumer bigint",
    "nhplug_order_guard": "",
    "nhplug_append_only": "",
    "nhplug_key_registry_insert": "",
    "nhplug_auth_immutable": "",
    "reject_kiwoom_authority_evidence_mutation": "",
}
APP_EXECUTE_HELPERS = {"nhplug_body_field", "nhplug_body_digest_v1"}
SECURITY_FUNCTIONS = {"nhplug_consume_authorization", "nhplug_order_guard"}
MIGRATION_FUNCTIONS = {
    "nhplug_body_field",
    "nhplug_body_digest_v1",
    "reject_kiwoom_authority_evidence_mutation",
}
PRIVS = {
    "r": "SELECT",
    "a": "INSERT",
    "w": "UPDATE",
    "d": "DELETE",
    "U": "USAGE",
    "X": "EXECUTE",
}


class Stop(Exception):
    pass


def ident(value: str) -> str:
    if not isinstance(value, str) or not value or "\x00" in value:
        raise Stop("invalid SQL identifier")
    return '"' + value.replace('"', '""') + '"'


def qname(schema: str, name: str) -> str:
    return ident(schema) + "." + ident(name)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def load_approved(path: str, approved_hash: str) -> dict:
    if len(approved_hash) != 64 or any(
        c not in "0123456789abcdef" for c in approved_hash
    ):
        raise Stop("approved SHA-256 must be 64 lowercase hexadecimal characters")
    raw = Path(path).read_bytes()
    if digest(raw) != approved_hash:
        raise Stop("approved SHA-256 mismatch")
    value = json.loads(raw)
    if not isinstance(value, dict) or not value.get("approval_id"):
        raise Stop("approved record requires approval_id")
    return value


def save_journal(path: str, value: dict) -> None:
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    target = Path(path)
    temporary = target.with_name(target.name + ".prepare")
    if target.exists():
        raise FileExistsError(path)
    temporary.unlink(missing_ok=True)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.link(temporary, target)
        directory = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def replace_journal(path: str, value: dict) -> None:
    """Durably advance a prepared journal after a committed database step."""
    target = Path(path)
    temporary = target.with_name(target.name + ".next")
    temporary.unlink(missing_ok=True)
    payload = (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, "wb") as output:
            output.write(payload)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary, target)
        directory = os.open(str(target.parent), os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


def read_journal(path: str, stage: int, database: str) -> dict:
    value = json.loads(Path(path).read_text())
    if value.get("stage") != stage or value.get("database") != database:
        raise Stop("journal stage or database mismatch")
    return value


async def catalog_object(conn: asyncpg.Connection, item: dict) -> dict:
    kind, schema, name = item["kind"], item["schema"], item["name"]
    if schema not in APP_SCHEMAS and not (
        schema == "_timescaledb_internal" and item.get("timescale_internal") is True
    ):
        raise Stop("object schema outside reviewed scope")
    if kind == "schema":
        row = await conn.fetchrow(
            "SELECT n.oid, pg_get_userbyid(n.nspowner) owner, n.nspacl::text acl FROM pg_namespace n WHERE n.nspname=$1",
            name,
        )
        sql_name = ident(name)
    elif kind == "relation":
        row = await conn.fetchrow(
            "SELECT c.oid, pg_get_userbyid(c.relowner) owner, c.relacl::text acl, c.relkind::text relkind FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=$1 AND c.relname=$2",
            schema,
            name,
        )
        sql_name = qname(schema, name)
    elif kind == "function":
        args = item.get("identity_args")
        if not isinstance(args, str):
            raise Stop("function identity_args required")
        row = await conn.fetchrow(
            "SELECT p.oid, pg_get_userbyid(p.proowner) owner, p.proacl::text acl, p.prosecdef, p.proconfig, pg_get_function_identity_arguments(p.oid) actual_args FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=$1 AND p.proname=$2 AND pg_get_function_identity_arguments(p.oid)=$3",
            schema,
            name,
            args,
        )
        sql_name = qname(schema, name) + "(" + args + ")"
    elif kind == "type":
        row = await conn.fetchrow(
            "SELECT t.oid, pg_get_userbyid(t.typowner) owner, t.typacl::text acl, t.typtype::text typtype FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname=$1 AND t.typname=$2 AND (t.typrelid=0 OR (t.typtype='c' AND EXISTS (SELECT 1 FROM pg_class c WHERE c.oid=t.typrelid AND c.relkind='c')))",
            schema,
            name,
        )
        sql_name = qname(schema, name)
    else:
        raise Stop("unsupported object kind")
    if row is None:
        raise Stop("manifest object absent: " + kind + " " + sql_name)
    catalog = {
        "schema": "pg_namespace",
        "relation": "pg_class",
        "function": "pg_proc",
        "type": "pg_type",
    }[kind]
    member = await conn.fetchval(
        "SELECT EXISTS (SELECT 1 FROM pg_depend WHERE classid=$1::regclass AND objid=$2 AND deptype='e')",
        catalog,
        row["oid"],
    )
    if member:
        raise Stop("extension-owned object cannot be transferred: " + sql_name)
    return {**dict(row), "sql_name": sql_name, "kind": kind}


def item_key(item: dict) -> tuple[str, str, str, str]:
    return (
        item.get("kind", ""),
        item.get("schema", ""),
        item.get("name", ""),
        item.get("identity_args", ""),
    )


async def app_catalog_keys(
    conn: asyncpg.Connection, *, include_functions_and_types: bool
) -> set[tuple[str, str, str, str]]:
    keys = {("schema", name, name, "") for name in APP_SCHEMAS}
    rows = await conn.fetch(
        "SELECT n.nspname, c.relname FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace WHERE n.nspname=ANY($1::text[]) AND c.relkind IN ('r','p','v','m','S','f') AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e')",
        list(APP_SCHEMAS),
    )
    keys.update(("relation", r["nspname"], r["relname"], "") for r in rows)
    if include_functions_and_types:
        rows = await conn.fetch(
            "SELECT n.nspname, p.proname, pg_get_function_identity_arguments(p.oid) args FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace WHERE n.nspname=ANY($1::text[]) AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_proc'::regclass AND d.objid=p.oid AND d.deptype='e')",
            list(APP_SCHEMAS),
        )
        keys.update(("function", r["nspname"], r["proname"], r["args"]) for r in rows)
        rows = await conn.fetch(
            "SELECT n.nspname,t.typname FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace WHERE n.nspname=ANY($1::text[]) AND ((t.typrelid=0 AND t.typtype IN ('e','d','r','m')) OR (t.typtype='c' AND EXISTS (SELECT 1 FROM pg_class c WHERE c.oid=t.typrelid AND c.relkind='c'))) AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_type'::regclass AND d.objid=t.oid AND d.deptype='e')",
            list(APP_SCHEMAS),
        )
        keys.update(("type", r["nspname"], r["typname"], "") for r in rows)
    return keys


async def timescale_internal_keys(
    conn: asyncpg.Connection,
) -> set[tuple[str, str, str, str]]:
    rows = await conn.fetch("""
        SELECT c.relname FROM pg_class c
        JOIN pg_namespace n ON n.oid=c.relnamespace
        LEFT JOIN pg_index i ON i.indexrelid=c.oid
        WHERE n.nspname='_timescaledb_internal'
          AND c.relkind IN ('r','p','v','m','S','f','i','I')
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=c.oid AND d.deptype='e')
          AND NOT EXISTS (SELECT 1 FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=i.indrelid AND d.deptype='e')
    """)
    return {("relation", "_timescaledb_internal", r["relname"], "") for r in rows}


async def timescale_chunk_keys(
    conn: asyncpg.Connection,
) -> set[tuple[str, str, str, str]]:
    rows = await conn.fetch("""
        WITH chunk_relations AS (
          SELECT c.oid, c.relname FROM timescaledb_information.chunks ch
          JOIN pg_namespace n ON n.nspname=ch.chunk_schema
          JOIN pg_class c ON c.relnamespace=n.oid AND c.relname=ch.chunk_name
          WHERE ch.chunk_schema='_timescaledb_internal'
        )
        SELECT relname FROM chunk_relations
        UNION
        SELECT i.relname FROM chunk_relations cr
        JOIN pg_index x ON x.indrelid=cr.oid
        JOIN pg_class i ON i.oid=x.indexrelid
    """)
    keys = {("relation", "_timescaledb_internal", r["relname"], "") for r in rows}
    internal = await timescale_internal_keys(conn)
    if not keys <= internal:
        raise Stop("TimescaleDB chunk catalog escaped reviewed internal graph")
    return keys


async def verify_chunk_owners(conn: asyncpg.Connection, owner: str) -> None:
    for _, schema, name, _ in await timescale_chunk_keys(conn):
        obj = await catalog_object(
            conn,
            {
                "kind": "relation",
                "schema": schema,
                "name": name,
                "timescale_internal": True,
            },
        )
        if obj["owner"] != owner:
            raise Stop(
                "TimescaleDB chunk or chunk index owner differs: " + obj["sql_name"]
            )


async def require_complete_app_manifest(
    conn: asyncpg.Connection, items: list[dict], *, include_functions_and_types: bool
) -> None:
    manifest_keys = [
        item_key(item)
        for item in items
        if item.get("schema") in APP_SCHEMAS
        and (include_functions_and_types or item.get("kind") in ("schema", "relation"))
    ]
    if len(manifest_keys) != len(set(manifest_keys)):
        raise Stop("duplicate application object in manifest")
    actual = await app_catalog_keys(
        conn, include_functions_and_types=include_functions_and_types
    )
    if set(manifest_keys) != actual:
        raise Stop("application object inventory incomplete or stale; NEEDS_DESK_SQL")


async def require_complete_timescale_manifest(
    conn: asyncpg.Connection, items: list[dict]
) -> None:
    keys = [
        item_key(item)
        for item in items
        if item.get("schema") == "_timescaledb_internal"
    ]
    if len(keys) != len(set(keys)) or set(keys) != await timescale_internal_keys(conn):
        raise Stop(
            "Timescale internal graph manifest incomplete or stale; NEEDS_DESK_SQL"
        )
    if any(
        item.get("timescale_internal") is not True or item.get("kind") != "relation"
        for item in items
        if item.get("schema") == "_timescaledb_internal"
    ):
        raise Stop("Timescale internal graph must be signed as relations")


async def validate_target(conn: asyncpg.Connection, expected_database: str) -> None:
    database = await conn.fetchval("SELECT current_database()")
    if database != expected_database:
        raise Stop("connected database differs from approved target")
    version = await conn.fetchval("SHOW server_version_num")
    if int(version) < 160000:
        raise Stop("PostgreSQL 16 or later required for membership options")


async def role_snapshot(conn: asyncpg.Connection) -> dict:
    roles = await conn.fetch(
        "SELECT rolname, rolsuper, rolcanlogin, rolcreaterole, rolcreatedb, rolreplication, rolbypassrls, rolinherit FROM pg_roles WHERE rolname=ANY($1::text[]) ORDER BY rolname",
        list(ALL_GROUPS),
    )
    members = await conn.fetch(
        "SELECT p.rolname parent, c.rolname child, m.admin_option, m.inherit_option, m.set_option FROM pg_auth_members m JOIN pg_roles p ON p.oid=m.roleid JOIN pg_roles c ON c.oid=m.member WHERE p.rolname=ANY($1::text[]) OR c.rolname=ANY($1::text[]) ORDER BY p.rolname,c.rolname",
        list(ALL_GROUPS),
    )
    password_absent = await conn.fetchval(
        "SELECT rolpassword IS NULL FROM pg_authid WHERE rolname='at_migration_owner'"
    )
    return {
        "roles": [dict(r) for r in roles],
        "memberships": [dict(m) for m in members],
        "migration_password_absent": password_absent,
    }


def validate_groups(
    snapshot: dict, allow_missing: bool = False, allow_migration_nonlogin: bool = False
) -> None:
    by_name = {r["rolname"]: r for r in snapshot["roles"]}
    for name in ALL_GROUPS:
        role = by_name.get(name)
        if role is None:
            if allow_missing and name in GROUPS:
                continue
            raise Stop("required group role absent: " + name)
        if any(
            role[k]
            for k in (
                "rolsuper",
                "rolcreaterole",
                "rolcreatedb",
                "rolreplication",
                "rolbypassrls",
            )
        ):
            raise Stop("unsafe group role attributes: " + name)
        if name == "at_migration_owner":
            if not role["rolcanlogin"] and not allow_migration_nonlogin:
                raise Stop("migration owner must be LOGIN with HBA reject")
            if snapshot["migration_password_absent"] is not True:
                raise Stop("migration owner password must be absent")
        elif role["rolcanlogin"]:
            raise Stop("group role must be NOLOGIN: " + name)
    for m in snapshot["memberships"]:
        if m["child"] == "at_app" or (
            m["parent"] in ("at_migration_owner", "nhplug_security_owner")
            and m["child"] != "at_migration_runner"
        ):
            raise Stop("unexpected owner/application membership")
        if m["parent"] == "at_app" and (
            m["admin_option"] or m["set_option"] or not m["inherit_option"]
        ):
            raise Stop("at_app membership options violate PG16 contract")


async def hba_reject_gate(conn: asyncpg.Connection) -> None:
    # The reviewed first-match fragment covers local sockets, IPv4 and IPv6
    # host connections, and physical replication for each transport.
    errors = await conn.fetchval(
        "SELECT count(*) FROM pg_hba_file_rules WHERE error IS NOT NULL"
    )
    rules = await conn.fetch(
        "SELECT rule_number,type,database,user_name,address,netmask,auth_method FROM pg_hba_file_rules WHERE rule_number BETWEEN 1 AND 6 ORDER BY rule_number"
    )
    expected = [
        (1, "local", ["all"], ["at_migration_owner"], None, None, "reject"),
        (2, "local", ["replication"], ["at_migration_owner"], None, None, "reject"),
        (3, "host", ["all"], ["at_migration_owner"], "0.0.0.0", "0.0.0.0", "reject"),
        (4, "host", ["all"], ["at_migration_owner"], "::", "::", "reject"),
        (
            5,
            "host",
            ["replication"],
            ["at_migration_owner"],
            "0.0.0.0",
            "0.0.0.0",
            "reject",
        ),
        (6, "host", ["replication"], ["at_migration_owner"], "::", "::", "reject"),
    ]
    actual = [
        (
            r["rule_number"],
            r["type"],
            r["database"],
            r["user_name"],
            r["address"],
            r["netmask"],
            r["auth_method"],
        )
        for r in rules
    ]
    if errors or actual != expected:
        raise Stop(
            "first six loaded HBA rules must reject migration owner local and IPv4/IPv6 all and replication connections"
        )
    loaded = await conn.fetchval(
        "SELECT pg_conf_load_time() >= (pg_stat_file(current_setting('hba_file'))).modification"
    )
    if loaded is not True:
        raise Stop("HBA file changed after server configuration reload")


async def apply1(conn: asyncpg.Connection, args: argparse.Namespace) -> dict:
    await hba_reject_gate(conn)
    snap = await role_snapshot(conn)
    validate_groups(snap, allow_missing=True, allow_migration_nonlogin=True)
    created = [
        name for name in GROUPS if name not in {r["rolname"] for r in snap["roles"]}
    ]
    for name in created:
        login_clause = (
            "LOGIN PASSWORD NULL" if name == "at_migration_owner" else "NOLOGIN"
        )
        await conn.execute(
            "CREATE ROLE "
            + ident(name)
            + " "
            + login_clause
            + " NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION NOBYPASSRLS"
        )
    previous = next(
        (r for r in snap["roles"] if r["rolname"] == "at_migration_owner"), None
    )
    if previous is not None and not previous["rolcanlogin"]:
        await conn.execute("ALTER ROLE at_migration_owner LOGIN PASSWORD NULL")
    validate_groups(await role_snapshot(conn))
    return {
        "before": snap,
        "created": created,
        "migration_login_promoted": previous is not None
        and not previous["rolcanlogin"],
    }


async def rollback1(conn: asyncpg.Connection, journal: dict) -> None:
    snap = await role_snapshot(conn)
    validate_groups(snap, allow_missing=True, allow_migration_nonlogin=True)
    if (
        journal.get("migration_login_promoted")
        and "at_migration_owner" not in journal["created"]
        and next(r for r in snap["roles"] if r["rolname"] == "at_migration_owner")[
            "rolcanlogin"
        ]
    ):
        await conn.execute("ALTER ROLE at_migration_owner NOLOGIN")
    present = {r["rolname"] for r in snap["roles"]}
    for name in reversed(journal["created"]):
        if name not in present:
            continue
        if any(m["parent"] == name or m["child"] == name for m in snap["memberships"]):
            raise Stop("role acquired membership after stage 1: " + name)
        dependencies = await conn.fetchval(
            "SELECT count(*) FROM pg_shdepend d JOIN pg_roles r ON r.oid=d.refobjid WHERE r.rolname=$1",
            name,
        )
        if dependencies:
            raise Stop("role acquired object dependency after stage 1: " + name)
        await conn.execute("DROP ROLE " + ident(name))


async def timescale_gate(conn: asyncpg.Connection, record: dict) -> None:
    version = await conn.fetchval(
        "SELECT extversion FROM pg_extension WHERE extname='timescaledb'"
    )
    if version is None:
        if record.get("timescale_version"):
            raise Stop(
                "TimescaleDB extension absent contrary to signed record; NEEDS_DESK_SQL"
            )
        return
    expected = record.get("timescale_version")
    if expected != version:
        raise Stop("TimescaleDB version differs from signed record; NEEDS_DESK_SQL")
    if version != "2.22.1":
        raise Stop(
            "TimescaleDB policy transition requires separate approval for this version; NEEDS_DESK_SQL"
        )


async def linked_sequence_table(conn: asyncpg.Connection, oid: int) -> int | None:
    return await conn.fetchval(
        "SELECT d.refobjid FROM pg_depend d WHERE d.classid='pg_class'::regclass AND d.objid=$1 AND d.refclassid='pg_class'::regclass AND d.deptype IN ('a','i') LIMIT 1",
        oid,
    )


async def relation_type_owner(conn: asyncpg.Connection, oid: int) -> str | None:
    return await conn.fetchval(
        "SELECT pg_get_userbyid(t.typowner) FROM pg_type t WHERE t.typrelid=$1", oid
    )


async def composite_class_owner(conn: asyncpg.Connection, oid: int) -> str | None:
    return await conn.fetchval(
        "SELECT pg_get_userbyid(c.relowner) FROM pg_type t JOIN pg_class c ON c.oid=t.typrelid WHERE t.oid=$1 AND t.typtype='c' AND c.relkind='c'",
        oid,
    )


async def apply2(
    conn: asyncpg.Connection,
    args: argparse.Namespace,
    manifest: dict,
    *,
    dry_run: bool = False,
) -> dict:
    validate_groups(await role_snapshot(conn))
    await timescale_gate(conn, manifest)
    if (
        await conn.fetchval("SELECT to_regclass('review.nhplug_mock_order_ledger')")
        is None
    ):
        raise Stop("#711 absent; apply approved migration and repeat preflight first")
    items = manifest.get("objects")
    if not isinstance(items, list) or not items:
        raise Stop("signed ownership object manifest required")
    await require_complete_app_manifest(conn, items, include_functions_and_types=True)
    await require_complete_timescale_manifest(conn, items)
    seen = set()
    before = []
    for item in sorted(items, key=lambda x: 0 if x.get("kind") == "schema" else 1):
        key = (
            item.get("kind"),
            item.get("schema"),
            item.get("name"),
            item.get("identity_args"),
        )
        if key in seen:
            raise Stop("duplicate ownership manifest object")
        seen.add(key)
        obj = await catalog_object(conn, item)
        target = item.get("target_owner")
        if target not in ("at_migration_owner", "nhplug_security_owner"):
            raise Stop("invalid target owner")
        if obj["owner"] not in (item.get("expected_owner"), target):
            raise Stop("unexpected current object owner: " + obj["sql_name"])
        if (
            item["schema"] == "review"
            and item["name"] in PROTECTED
            and target != PROTECTED[item["name"]][0]
        ):
            raise Stop("protected object owner mismatch")
        if (
            item["schema"] == "review"
            and item["kind"] == "function"
            and item["name"] in PROTECTED_FUNCTIONS
        ):
            required_owner = (
                "at_migration_owner"
                if item["name"] in MIGRATION_FUNCTIONS
                else "nhplug_security_owner"
            )
            if (
                target != required_owner
                or item.get("identity_args") != PROTECTED_FUNCTIONS[item["name"]]
            ):
                raise Stop("protected function owner or signature mismatch")
            if bool(obj["prosecdef"]) != (item["name"] in SECURITY_FUNCTIONS):
                raise Stop("protected function SECURITY DEFINER drift")
        if item["kind"] == "relation" and item["schema"] in APP_SCHEMAS:
            ts_installed = await conn.fetchval(
                "SELECT EXISTS (SELECT 1 FROM pg_extension WHERE extname='timescaledb')"
            )
            if ts_installed:
                cagg = await conn.fetchval(
                    "SELECT EXISTS (SELECT 1 FROM timescaledb_information.continuous_aggregates WHERE view_schema=$1 AND view_name=$2)",
                    item["schema"],
                    item["name"],
                )
                if cagg != bool(item.get("continuous_aggregate", False)):
                    raise Stop(
                        "continuous aggregate classification differs from signed manifest"
                    )
        if (
            obj["kind"] == "function"
            and obj["prosecdef"]
            and obj["proconfig"] != ["search_path=pg_catalog, review"]
        ):
            raise Stop("SECURITY DEFINER search_path drift")
        type_owner = (
            await relation_type_owner(conn, obj["oid"])
            if item["kind"] == "relation"
            else None
        )
        if type_owner is not None and type_owner != obj["owner"]:
            raise Stop("relation row type owner differs from relation owner")
        composite_owner = (
            await composite_class_owner(conn, obj["oid"])
            if item["kind"] == "type"
            else None
        )
        if composite_owner is not None and composite_owner != obj["owner"]:
            raise Stop("standalone composite type and relation owners differ")
        before.append(
            {
                "item": item,
                "owner": obj["owner"],
                "acl": obj["acl"],
                "relation_type_owner": type_owner,
                "composite_class_owner": composite_owner,
            }
        )
    if dry_run:
        if any(
            entry["owner"] != entry["item"].get("expected_owner") for entry in before
        ):
            raise Stop("initial owner differs from signed manifest; NEEDS_DESK_SQL")
        return {"before": before, "manifest_sha256": args.sha256}
    for entry in before:
        item = entry["item"]
        if entry["owner"] == item["target_owner"]:
            continue
        if item["schema"] == "_timescaledb_internal":
            continue
        obj = await catalog_object(conn, item)
        kind = item["kind"]
        if kind == "relation":
            relkind = obj["relkind"]
            if relkind == "S" and await linked_sequence_table(conn, obj["oid"]):
                continue
            if item.get("continuous_aggregate") is True:
                keyword = "MATERIALIZED VIEW"
            else:
                keyword = {
                    "r": "TABLE",
                    "p": "TABLE",
                    "v": "VIEW",
                    "m": "MATERIALIZED VIEW",
                    "S": "SEQUENCE",
                    "f": "FOREIGN TABLE",
                }.get(relkind)
            if keyword is None:
                raise Stop("unsupported relation kind")
        else:
            keyword = {"schema": "SCHEMA", "function": "FUNCTION", "type": "TYPE"}[kind]
        await conn.execute(
            "ALTER "
            + keyword
            + " "
            + obj["sql_name"]
            + " OWNER TO "
            + ident(item["target_owner"])
        )
    for entry in before:
        item = entry["item"]
        obj = await catalog_object(conn, item)
        if obj["owner"] != item["target_owner"]:
            raise Stop("owner transition incomplete: " + obj["sql_name"])
        if item["kind"] == "relation" and await relation_type_owner(
            conn, obj["oid"]
        ) not in (None, item["target_owner"]):
            raise Stop(
                "relation row type owner transition incomplete: " + obj["sql_name"]
            )
        if item["kind"] == "type" and await composite_class_owner(
            conn, obj["oid"]
        ) not in (None, item["target_owner"]):
            raise Stop(
                "composite relation owner transition incomplete: " + obj["sql_name"]
            )
    return {"before": before, "manifest_sha256": args.sha256}


async def rollback2(conn: asyncpg.Connection, journal: dict) -> None:
    present_keys = await app_catalog_keys(
        conn, include_functions_and_types=True
    ) | await timescale_internal_keys(conn)
    dynamic_before = {tuple(key) for key in journal["dynamic_internal_keys"]}
    for index in reversed(range(len(journal["before"]))):
        entry = journal["before"][index]
        item = entry["item"]
        if item_key(item) not in present_keys and item_key(item) in dynamic_before:
            continue
        obj = await catalog_object(conn, item)
        expected_acl = (
            journal["owner_after_acl"][index]
            if obj["owner"] == item["target_owner"] and "owner_after_acl" in journal
            else entry.get("acl")
        )
        if "acl" in entry and obj["acl"] != expected_acl:
            raise Stop(
                "ownership rollback ACL differs from journal: " + obj["sql_name"]
            )
        if obj["owner"] == entry["owner"]:
            continue
        if obj["owner"] != item["target_owner"]:
            raise Stop("ownership changed since apply: " + obj["sql_name"])
        if entry["owner"] == item["target_owner"]:
            continue
        if item["schema"] == "_timescaledb_internal":
            continue
        if (
            item["kind"] == "relation"
            and obj["relkind"] == "S"
            and await linked_sequence_table(conn, obj["oid"])
        ):
            continue
        if item["kind"] == "relation":
            keyword = (
                "MATERIALIZED VIEW"
                if item.get("continuous_aggregate")
                else {
                    "r": "TABLE",
                    "p": "TABLE",
                    "v": "VIEW",
                    "m": "MATERIALIZED VIEW",
                    "S": "SEQUENCE",
                    "f": "FOREIGN TABLE",
                }.get(obj["relkind"])
            )
            if keyword is None:
                raise Stop("unsupported relation kind on rollback")
        else:
            keyword = {"schema": "SCHEMA", "function": "FUNCTION", "type": "TYPE"}[
                item["kind"]
            ]
        await conn.execute(
            "ALTER "
            + keyword
            + " "
            + obj["sql_name"]
            + " OWNER TO "
            + ident(entry["owner"])
        )
    for entry in journal["before"]:
        if (
            item_key(entry["item"]) not in present_keys
            and item_key(entry["item"]) in dynamic_before
        ):
            continue
        obj = await catalog_object(conn, entry["item"])
        if obj["owner"] != entry["owner"]:
            raise Stop("owner inverse incomplete: " + obj["sql_name"])
        if "acl" in entry and obj["acl"] != entry["acl"]:
            raise Stop("owner inverse ACL differs from journal: " + obj["sql_name"])
        if (
            entry["item"]["kind"] == "relation"
            and "relation_type_owner" in entry
            and await relation_type_owner(conn, obj["oid"])
            != entry["relation_type_owner"]
        ):
            raise Stop("relation row type owner inverse incomplete: " + obj["sql_name"])
        if (
            entry["item"]["kind"] == "type"
            and "composite_class_owner" in entry
            and await composite_class_owner(conn, obj["oid"])
            != entry["composite_class_owner"]
        ):
            raise Stop(
                "composite relation owner inverse incomplete: " + obj["sql_name"]
            )
    await verify_chunk_owners(conn, "mgh3326")


async def run_stage2(
    conn: asyncpg.Connection,
    args: argparse.Namespace,
    record: dict | None,
    existing: dict | None,
) -> None:
    if not await conn.fetchval("SELECT pg_try_advisory_lock(789, 2)"):
        raise Stop("another Stage 2 session holds the cutover lock")
    try:
        async with conn.transaction(readonly=True):
            await conn.execute("SET LOCAL statement_timeout='30s'")
            await validate_target(conn, args.database)
            await hba_reject_gate(conn)
            await timescale_gate(conn, record if record is not None else existing)
        if args.mode == "apply" and existing is None:
            async with conn.transaction(readonly=True):
                await conn.execute("SET LOCAL statement_timeout='30s'")
                owner_before = await apply2(conn, args, record, dry_run=True)
                dynamic_before = await timescale_chunk_keys(conn)
                for key in dynamic_before:
                    obj = await catalog_object(
                        conn,
                        {
                            "kind": key[0],
                            "schema": key[1],
                            "name": key[2],
                            "timescale_internal": True,
                        },
                    )
                    if obj["owner"] != "mgh3326":
                        raise Stop("initial TimescaleDB chunk owner needs desk review")
                jobs_before = await timescale_jobs.preflight(
                    conn, record.get("timescale_jobs")
                )
            existing = {
                "stage": 2,
                "database": args.database,
                "manifest_sha256": args.sha256,
                "timescale_version": record["timescale_version"],
                **owner_before,
                "dynamic_internal_keys": [list(key) for key in sorted(dynamic_before)],
                "jobs": [
                    {"before": job, "new_id": None, "restored_id": None}
                    for job in jobs_before
                ],
                "owners_applied": False,
                "owners_reverted": False,
                "rolled_back": False,
            }
            save_journal(args.journal, existing)
        if existing is None:
            existing = read_journal(args.journal, 2, args.database)
        if not isinstance(existing.get("jobs"), list) or not isinstance(
            existing.get("before"), list
        ):
            raise Stop("Stage 2 journal lacks prepared ownership and job snapshots")
        if not isinstance(existing.get("dynamic_internal_keys"), list):
            raise Stop("Stage 2 journal lacks prepared dynamic chunk inventory")
        dynamic_before = {tuple(key) for key in existing["dynamic_internal_keys"]}
        expected_keys = {item_key(entry["item"]) for entry in existing["before"]}
        if not dynamic_before <= expected_keys:
            raise Stop("Stage 2 journal dynamic chunk inventory is inconsistent")
        actual_keys = await app_catalog_keys(
            conn, include_functions_and_types=True
        ) | await timescale_internal_keys(conn)
        dynamic_current = await timescale_chunk_keys(conn)
        if (expected_keys - actual_keys) - dynamic_before or (
            actual_keys - expected_keys
        ) - dynamic_current:
            raise Stop(
                "stable catalog graph changed since Stage 2 journal; NEEDS_DESK_SQL"
            )
        for index, entry in enumerate(existing["before"]):
            if item_key(entry["item"]) not in actual_keys:
                continue
            obj = await catalog_object(conn, entry["item"])
            if "acl" in entry:
                expected_acl = (
                    existing["owner_after_acl"][index]
                    if obj["owner"] == entry["item"]["target_owner"]
                    and "owner_after_acl" in existing
                    else entry["acl"]
                )
                if obj["acl"] != expected_acl:
                    raise Stop(
                        "catalog ACL changed since Stage 2 journal: " + obj["sql_name"]
                    )
        timescale_jobs.classify(await timescale_jobs.snapshot(conn), existing["jobs"])
        if args.mode == "apply":
            present = [
                entry
                for entry in existing["before"]
                if item_key(entry["item"]) in actual_keys
            ]
            owners = [
                (entry, (await catalog_object(conn, entry["item"]))["owner"])
                for entry in present
            ]
            at_target = all(
                owner == entry["item"]["target_owner"] for entry, owner in owners
            )
            at_before = all(owner == entry["owner"] for entry, owner in owners)
            if at_target:
                await verify_chunk_owners(conn, "at_migration_owner")
                if "owner_after_acl" not in existing:
                    if actual_keys != expected_keys:
                        raise Stop("Stage 2 owner journal incomplete after chunk churn")
                    existing["owner_after_acl"] = [
                        (await catalog_object(conn, entry["item"]))["acl"]
                        for entry in existing["before"]
                    ]
                    replace_journal(args.journal, existing)
            elif at_before:
                if actual_keys != expected_keys:
                    raise Stop(
                        "chunk graph changed before owner transfer; refresh signed manifest"
                    )
                async with conn.transaction():
                    await conn.execute("SET LOCAL lock_timeout='3s'")
                    await conn.execute("SET LOCAL statement_timeout='30s'")
                    await apply2(conn, args, record)
                    existing["owner_after_acl"] = [
                        (await catalog_object(conn, entry["item"]))["acl"]
                        for entry in existing["before"]
                    ]
                    replace_journal(args.journal, existing)
            else:
                raise Stop("Stage 2 owners are mixed outside an atomic transfer")
            existing["owners_applied"] = True
            existing["owners_reverted"] = False
            existing["rolled_back"] = False
            replace_journal(args.journal, existing)
            for index in range(len(existing["jobs"])):
                new_id = await timescale_jobs.transition_one(
                    conn, existing["jobs"], index, reverse=False
                )
                existing["jobs"][index]["new_id"] = new_id
                existing["jobs"][index]["after"] = next(
                    j
                    for j in await timescale_jobs.snapshot(conn)
                    if j["job_id"] == new_id
                )
                replace_journal(args.journal, existing)
            states = timescale_jobs.classify(
                await timescale_jobs.snapshot(conn), existing["jobs"]
            )
            if any(state != "new" for state, _ in states):
                raise Stop("TimescaleDB job owner proof incomplete")
            for entry in existing["before"]:
                if item_key(entry["item"]) not in actual_keys:
                    continue
                obj = await catalog_object(conn, entry["item"])
                if obj["owner"] != entry["item"]["target_owner"]:
                    raise Stop(
                        "TimescaleDB graph or application owner proof incomplete"
                    )
            await verify_chunk_owners(conn, "at_migration_owner")
            existing["complete"] = True
            replace_journal(args.journal, existing)
        else:
            async with conn.transaction():
                await conn.execute("SET LOCAL lock_timeout='3s'")
                await conn.execute("SET LOCAL statement_timeout='30s'")
                await rollback2(conn, existing)
            existing["owners_reverted"] = True
            existing["owners_applied"] = False
            replace_journal(args.journal, existing)
            for index in reversed(range(len(existing["jobs"]))):
                old_id = await timescale_jobs.transition_one(
                    conn, existing["jobs"], index, reverse=True
                )
                existing["jobs"][index]["restored_id"] = old_id
                existing["jobs"][index]["restored"] = next(
                    j
                    for j in await timescale_jobs.snapshot(conn)
                    if j["job_id"] == old_id
                )
                replace_journal(args.journal, existing)
            states = timescale_jobs.classify(
                await timescale_jobs.snapshot(conn), existing["jobs"]
            )
            if any(state != "old" for state, _ in states):
                raise Stop("TimescaleDB policy rollback proof incomplete")
            existing["complete"] = False
            existing["rolled_back"] = True
            replace_journal(args.journal, existing)
    finally:
        await conn.execute("SELECT pg_advisory_unlock(789, 2)")


async def acl_rows(conn: asyncpg.Connection, item: dict) -> list[dict]:
    obj = await catalog_object(conn, item)
    kind = item["kind"]
    query = {
        "schema": "SELECT n.nspacl acl, n.nspowner owner FROM pg_namespace n WHERE n.oid=$1",
        "relation": "SELECT c.relacl acl, c.relowner owner FROM pg_class c WHERE c.oid=$1",
        "function": "SELECT p.proacl acl, p.proowner owner FROM pg_proc p WHERE p.oid=$1",
        "type": "SELECT t.typacl acl, t.typowner owner FROM pg_type t WHERE t.oid=$1",
    }[kind]
    row = await conn.fetchrow(query, obj["oid"])
    rows = await conn.fetch(
        "SELECT CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE gr.rolname END grantee, pg_get_userbyid(a.grantor) grantor, a.privilege_type, a.is_grantable FROM aclexplode(COALESCE($1::aclitem[], acldefault($2::\"char\",$3::oid))) a LEFT JOIN pg_roles gr ON gr.oid=a.grantee ORDER BY grantee,privilege_type",
        row["acl"],
        {
            "schema": b"n",
            "relation": b"r" if obj.get("relkind") != "S" else b"S",
            "function": b"f",
            "type": b"T",
        }[kind],
        row["owner"],
    )
    return [dict(r) for r in rows]


def acl_key(rows: list[dict]) -> list[tuple]:
    # PostgreSQL materializes implicit owner rights when the first explicit grant
    # is written.  Compare the rest of the ACL; ownership is checked separately.
    return sorted(
        (r["grantee"], r["grantor"], r["privilege_type"], r["is_grantable"])
        for r in rows
        if r["grantee"] != r["grantor"]
    )


async def set_acl(
    conn: asyncpg.Connection, item: dict, current: list[dict], target: list[dict]
) -> None:
    obj = await catalog_object(conn, item)
    keyword = (
        "TABLE"
        if item["kind"] == "relation" and obj.get("relkind") != "S"
        else {
            "schema": "SCHEMA",
            "relation": "SEQUENCE",
            "function": "FUNCTION",
            "type": "TYPE",
        }[item["kind"]]
    )
    managed = {
        "PUBLIC",
        "at_app",
        "nhplug_operator",
        "nhplug_security_owner",
        "at_migration_owner",
    }

    def unchanged(rows: list[dict]) -> list[tuple]:
        return sorted(
            (r["grantee"], r["grantor"], r["privilege_type"], r["is_grantable"])
            for r in rows
            if r["grantee"] not in managed | {obj["owner"]}
        )

    if unchanged(current) != unchanged(target):
        raise Stop("preserved ACL grant changed unexpectedly: " + obj["sql_name"])
    await conn.execute("SET LOCAL ROLE " + ident(obj["owner"]))
    for grantee in sorted(
        {r["grantee"] for r in current + target} & managed - {obj["owner"]}
    ):
        await conn.execute(
            "REVOKE ALL ON "
            + keyword
            + " "
            + obj["sql_name"]
            + " FROM "
            + ("PUBLIC" if grantee == "PUBLIC" else ident(grantee))
        )
    await conn.execute("RESET ROLE")
    by_grantee: dict[tuple[str, str, bool], list[str]] = {}
    for row in target:
        if row["grantee"] in managed and row["grantee"] != obj["owner"]:
            by_grantee.setdefault(
                (row["grantee"], row["grantor"], row["is_grantable"]), []
            ).append(row["privilege_type"])
    for (grantee, grantor, grant_option), privs in by_grantee.items():
        await conn.execute("SET LOCAL ROLE " + ident(grantor))
        await conn.execute(
            "GRANT "
            + ", ".join(sorted(set(privs)))
            + " ON "
            + keyword
            + " "
            + obj["sql_name"]
            + " TO "
            + ("PUBLIC" if grantee == "PUBLIC" else ident(grantee))
            + (" WITH GRANT OPTION" if grant_option else "")
        )
        await conn.execute("RESET ROLE")


def target_acl(before: list[dict], item: dict, owner: str) -> list[dict]:
    result = [
        r
        for r in before
        if r["grantee"] == owner
        or r["grantee"]
        not in ("PUBLIC", "at_app", "nhplug_operator", "nhplug_security_owner")
    ]
    schema, name, kind = item["schema"], item["name"], item["kind"]
    grants: dict[str, str] = {}
    if kind == "schema":
        if name in APP_SCHEMAS:
            grants["at_app"] = "U"
            if name == "review":
                grants["nhplug_operator"] = "U"
                grants["nhplug_security_owner"] = "U"
    elif kind == "relation" and schema == "review" and name in PROTECTED:
        _, grants["at_app"], grants["nhplug_operator"] = PROTECTED[name]
        if name == "nhplug_mock_order_ledger":
            grants["nhplug_security_owner"] = "rw"
    elif kind == "relation" and item.get("relkind") == "S":
        grants["at_app"] = item.get("app_privileges", "")
    elif kind == "function":
        grants["at_app"] = item.get("app_privileges", "")
        if schema == "review" and name in APP_EXECUTE_HELPERS:
            grants["nhplug_security_owner"] = "X"
    else:
        grants["at_app"] = item.get("app_privileges", "")
    for grantee, letters in grants.items():
        for letter in letters:
            if letter not in PRIVS:
                raise Stop("unsupported privilege in DML manifest")
            result.append(
                {
                    "grantee": grantee,
                    "grantor": owner,
                    "privilege_type": PRIVS[letter],
                    "is_grantable": False,
                }
            )
    return result


async def function_default_acl(conn: asyncpg.Connection, creator: str) -> str | None:
    return await conn.fetchval(
        "SELECT d.defaclacl::text FROM pg_default_acl d JOIN pg_roles r ON r.oid=d.defaclrole WHERE r.rolname=$1 AND d.defaclnamespace=0 AND d.defaclobjtype='f'",
        creator,
    )


async def database_acl_snapshot(conn: asyncpg.Connection) -> dict:
    row = await conn.fetchrow(
        "SELECT d.datacl::text raw, pg_get_userbyid(d.datdba) owner FROM pg_database d WHERE d.datname=current_database()"
    )
    entries = await conn.fetch("""
        SELECT CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE r.rolname END grantee,
               a.privilege_type, a.is_grantable, pg_get_userbyid(a.grantor) grantor
        FROM pg_database d,
             LATERAL aclexplode(COALESCE(d.datacl,acldefault('d',d.datdba))) a
        LEFT JOIN pg_roles r ON r.oid=a.grantee
        WHERE d.datname=current_database()
        ORDER BY grantee,a.privilege_type,a.is_grantable
    """)
    return {
        "raw": row["raw"],
        "owner": row["owner"],
        "entries": [dict(x) for x in entries],
    }


async def grant_database_connect(conn: asyncpg.Connection, manifest: dict) -> dict:
    before = await database_acl_snapshot(conn)
    policy = manifest.get("database_connect_policy")
    if not isinstance(policy, dict) or policy.get("revoke_public") is not True:
        raise Stop("signed database CONNECT policy missing; NEEDS_DESK_SQL")
    keep = policy.get("keep_roles")
    if (
        not isinstance(keep, list)
        or not {
            "at_app",
            "at_migration_owner",
            "at_migration_runner",
            "at_desk_login",
            "postgres",
        }.issubset(keep)
        or len(keep) != len(set(keep))
    ):
        raise Stop("database CONNECT keep-role inventory incomplete")
    roles = await conn.fetch(
        "SELECT rolname FROM pg_roles WHERE rolname=ANY($1::text[])", keep
    )
    if {r["rolname"] for r in roles} != set(keep):
        raise Stop("database CONNECT keep-role absent")
    active = await conn.fetch(
        "SELECT DISTINCT usename FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND usename IS NOT NULL"
    )
    if any(r["usename"] not in set(keep) | {before["owner"]} for r in active):
        raise Stop(
            "active database login missing from signed CONNECT keep-role inventory"
        )
    for entry in before["entries"]:
        if entry["privilege_type"] == "CONNECT" and entry["grantee"] not in set(
            keep
        ) | {"PUBLIC", before["owner"]}:
            raise Stop("unreviewed direct database CONNECT grantee; NEEDS_DESK_SQL")
    db = ident(await conn.fetchval("SELECT current_database()"))
    had_public = any(
        x["grantee"] == "PUBLIC" and x["privilege_type"] == "CONNECT"
        for x in before["entries"]
    )
    if had_public:
        await conn.execute("REVOKE CONNECT ON DATABASE " + db + " FROM PUBLIC")
    added = []
    for name in keep:
        if name == before["owner"]:
            continue
        direct = [
            x
            for x in before["entries"]
            if x["grantee"] == name and x["privilege_type"] == "CONNECT"
        ]
        if direct:
            if len(direct) != 1 or direct[0]["is_grantable"]:
                raise Stop("database CONNECT grant option requires desk review")
            continue
        await conn.execute("GRANT CONNECT ON DATABASE " + db + " TO " + ident(name))
        added.append(name)
    after = await database_acl_snapshot(conn)
    if any(
        x["grantee"] == "PUBLIC" and x["privilege_type"] == "CONNECT"
        for x in after["entries"]
    ):
        raise Stop("PUBLIC database CONNECT remains")
    for name in keep:
        if not await conn.fetchval(
            "SELECT has_database_privilege($1,current_database(),'CONNECT')", name
        ):
            raise Stop("approved role lacks database CONNECT: " + name)
    return {
        "before": before,
        "after": after,
        "had_public_connect": had_public,
        "added_roles": added,
        "keep_roles": keep,
    }


async def rollback_database_connect(conn: asyncpg.Connection, journal: dict) -> None:
    if await database_acl_snapshot(conn) != journal["after"]:
        raise Stop("database ACL changed since Stage 3 apply")
    db = ident(await conn.fetchval("SELECT current_database()"))
    for name in journal["added_roles"]:
        await conn.execute("REVOKE CONNECT ON DATABASE " + db + " FROM " + ident(name))
    if journal["had_public_connect"]:
        await conn.execute("GRANT CONNECT ON DATABASE " + db + " TO PUBLIC")
    current = await database_acl_snapshot(conn)
    if current["entries"] != journal["before"]["entries"]:
        raise Stop("database CONNECT inverse verification failed")


async def harden_creator_defaults(
    conn: asyncpg.Connection, manifest: dict
) -> list[dict]:
    expected = {
        "at_migration_owner": ["public", "review", "research", "paper"],
        "nhplug_security_owner": ["review"],
    }
    if manifest.get("future_creators") != expected:
        raise Stop("approved future creator roles and schemas missing; NEEDS_DESK_SQL")
    rows = []
    for creator, schemas in expected.items():
        for schema in schemas:
            for kind in ("TABLES", "SEQUENCES"):
                await conn.execute("SET LOCAL ROLE " + ident(creator))
                try:
                    await conn.execute(
                        "ALTER DEFAULT PRIVILEGES FOR ROLE "
                        + ident(creator)
                        + " IN SCHEMA "
                        + ident(schema)
                        + " REVOKE ALL ON "
                        + kind
                        + " FROM PUBLIC"
                    )
                finally:
                    await conn.execute("RESET ROLE")
                rows.append({"creator": creator, "schema": schema, "kind": kind})
    unexpected = await conn.fetchval(
        """
        SELECT count(*) FROM pg_default_acl d JOIN pg_roles r ON r.oid=d.defaclrole
        WHERE r.rolname=ANY($1::text[]) AND d.defaclobjtype IN ('r','S')
    """,
        list(expected),
    )
    if unexpected:
        raise Stop("table or sequence default ACL differs from reviewed empty baseline")
    return rows


async def harden_function_defaults(conn: asyncpg.Connection) -> list[dict]:
    before = []
    for creator in ("at_migration_owner", "nhplug_security_owner"):
        old = await function_default_acl(conn, creator)
        await conn.execute("SET LOCAL ROLE " + ident(creator))
        await conn.execute(
            "ALTER DEFAULT PRIVILEGES FOR ROLE "
            + ident(creator)
            + " REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
        )
        await conn.execute("RESET ROLE")
        new = await function_default_acl(conn, creator)
        public_execute = await conn.fetchval(
            "SELECT EXISTS (SELECT 1 FROM pg_default_acl d JOIN pg_roles r ON r.oid=d.defaclrole, LATERAL aclexplode(d.defaclacl) a WHERE r.rolname=$1 AND d.defaclnamespace=0 AND d.defaclobjtype='f' AND a.grantee=0 AND a.privilege_type='EXECUTE')",
            creator,
        )
        if public_execute:
            raise Stop("future function PUBLIC execute remains")
        before.append({"creator": creator, "old": old, "new": new})
    return before


async def rollback_function_defaults(
    conn: asyncpg.Connection, rows: list[dict]
) -> None:
    for row in reversed(rows):
        current = await function_default_acl(conn, row["creator"])
        if current == row["old"]:
            continue
        if current != row["new"]:
            raise Stop("default function ACL changed since apply")
        if row["old"] is None:
            await conn.execute("SET LOCAL ROLE " + ident(row["creator"]))
            await conn.execute(
                "ALTER DEFAULT PRIVILEGES FOR ROLE "
                + ident(row["creator"])
                + " GRANT EXECUTE ON FUNCTIONS TO PUBLIC"
            )
            await conn.execute("RESET ROLE")
        else:
            raise Stop("nondefault prior function ACL requires DBA-reviewed inverse")


async def apply3(
    conn: asyncpg.Connection, args: argparse.Namespace, manifest: dict
) -> dict:
    validate_groups(await role_snapshot(conn))
    if not Path(args.journal).exists():
        for creator in ("at_migration_owner", "nhplug_security_owner"):
            if await function_default_acl(conn, creator) is not None:
                raise Stop(
                    "existing creator default ACL requires reviewed inverse; NEEDS_DESK_SQL"
                )
        other_defaults = await conn.fetchval(
            "SELECT count(*) FROM pg_default_acl d JOIN pg_roles r ON r.oid=d.defaclrole WHERE r.rolname=ANY($1::text[])",
            ["at_migration_owner", "nhplug_security_owner"],
        )
        if other_defaults:
            raise Stop(
                "existing creator default ACL needs exact desk review; NEEDS_DESK_SQL"
            )
    if (
        await conn.fetchval("SELECT to_regclass('review.nhplug_mock_order_ledger')")
        is None
    ):
        raise Stop("#711 absent; apply approved migration and repeat preflight first")
    items = manifest.get("objects")
    if not isinstance(items, list) or not items:
        raise Stop("signed object-level DML manifest required")
    await require_complete_app_manifest(conn, items, include_functions_and_types=False)
    protected_names = {x.get("name") for x in items if x.get("schema") == "review"}
    if not set(PROTECTED).issubset(protected_names):
        raise Stop("#711 or ROB-1340 protected matrix incomplete")
    protected_functions = {
        x.get("name"): x.get("identity_args")
        for x in items
        if x.get("schema") == "review" and x.get("kind") == "function"
    }
    if any(
        protected_functions.get(name) != signature
        for name, signature in PROTECTED_FUNCTIONS.items()
    ):
        raise Stop("protected function execute manifest incomplete")
    for table in (
        "nhplug_mock_order_ledger",
        "kiwoom_authority_attempts",
        "kiwoom_authority_cessation_receipts",
    ):
        sequence = await conn.fetchval(
            "SELECT pg_get_serial_sequence($1,$2)", "review." + table, "id"
        )
        sequence_name = (
            await conn.fetchval(
                "SELECT relname FROM pg_class WHERE oid=to_regclass($1)", sequence
            )
            if sequence
            else None
        )
        if sequence_name is None or not any(
            x.get("kind") == "relation"
            and x.get("schema") == "review"
            and x.get("name") == sequence_name
            and x.get("app_privileges") == "U"
            for x in items
        ):
            raise Stop("protected identity sequence manifest incomplete: " + table)
    before = []
    preserved = manifest.get("preserved_acl_grantees")
    if (
        not isinstance(preserved, list)
        or not all(isinstance(x, str) and x for x in preserved)
        or len(preserved) != len(set(preserved))
    ):
        raise Stop("signed preserved ACL grantee inventory missing; NEEDS_DESK_SQL")
    managed = {
        "PUBLIC",
        "at_app",
        "nhplug_operator",
        "nhplug_security_owner",
        "at_migration_owner",
    }
    for item in sorted(items, key=lambda x: 0 if x.get("kind") == "schema" else 1):
        obj = await catalog_object(conn, item)
        if obj["owner"] != item.get("expected_owner"):
            raise Stop("grant target owner drift: " + obj["sql_name"])
        if item["kind"] == "relation":
            item["relkind"] = obj["relkind"]
        if (
            item["kind"] == "relation"
            and obj["relkind"] == "S"
            and item.get("app_privileges") not in ("", "U")
        ):
            raise Stop("sequences allow at_app USAGE only")
        if (
            item["kind"] == "relation"
            and obj["relkind"] != "S"
            and item.get("app_privileges", "")
            and any(x not in "rawd" for x in item["app_privileges"])
        ):
            raise Stop("ordinary relation DML manifest contains forbidden privilege")
        if item["kind"] == "function" and item["name"] in PROTECTED_FUNCTIONS:
            expected_execute = "X" if item["name"] in APP_EXECUTE_HELPERS else ""
            if item.get("app_privileges", "") != expected_execute:
                raise Stop(
                    "protected function EXECUTE matrix differs from approved helper exception"
                )
        if item["kind"] == "function" and item["name"] in PROTECTED_FUNCTIONS:
            required_owner = (
                "at_migration_owner"
                if item["name"] in MIGRATION_FUNCTIONS
                else "nhplug_security_owner"
            )
            if item["expected_owner"] != required_owner or bool(obj["prosecdef"]) != (
                item["name"] in SECURITY_FUNCTIONS
            ):
                raise Stop("protected function owner or SECURITY DEFINER drift")
        if (
            item["kind"] == "function"
            and obj["prosecdef"]
            and obj["proconfig"] != ["search_path=pg_catalog, review"]
        ):
            raise Stop("SECURITY DEFINER search_path drift")
        old = await acl_rows(conn, item)
        if any(
            row["grantee"] not in managed | {obj["owner"]} | set(preserved)
            for row in old
        ):
            raise Stop(
                "ACL grantee absent from signed preservation inventory: "
                + obj["sql_name"]
            )
        desired = target_acl(old, item, obj["owner"])
        before.append({"item": item, "old": old, "new": desired})
    for entry in before:
        await set_acl(conn, entry["item"], entry["old"], entry["new"])
        if acl_key(await acl_rows(conn, entry["item"])) != acl_key(entry["new"]):
            raise Stop("grant verification failed: " + entry["item"]["name"])
    database_acl = await grant_database_connect(conn, manifest)
    creator_defaults = await harden_creator_defaults(conn, manifest)
    defaults = await harden_function_defaults(conn)
    return {
        "before": before,
        "database_acl": database_acl,
        "creator_defaults": creator_defaults,
        "defaults": defaults,
        "preserved_acl_grantees": preserved,
        "manifest_sha256": args.sha256,
    }


async def rollback3(conn: asyncpg.Connection, journal: dict) -> None:
    active_app_sessions = await conn.fetchval("""
        SELECT count(*) FROM pg_stat_activity a
        JOIN pg_roles login ON login.rolname=a.usename
        JOIN pg_auth_members m ON m.member=login.oid
        JOIN pg_roles parent ON parent.oid=m.roleid
        WHERE a.datname=current_database() AND a.pid<>pg_backend_pid()
          AND parent.rolname='at_app'
    """)
    if active_app_sessions:
        raise Stop("active app sessions must drain before Stage 3 grant rollback")
    await rollback_function_defaults(conn, journal.get("defaults", []))
    if journal.get("database_acl"):
        await rollback_database_connect(conn, journal["database_acl"])
    for entry in reversed(journal["before"]):
        obj = await catalog_object(conn, entry["item"])
        if obj["owner"] != entry["item"]["expected_owner"]:
            raise Stop("grant rollback owner changed since Stage 3 apply")
        current = await acl_rows(conn, entry["item"])
        if acl_key(current) == acl_key(entry["old"]):
            continue
        if acl_key(current) != acl_key(entry["new"]):
            raise Stop("ACL changed since apply: " + entry["item"]["name"])
        await set_acl(conn, entry["item"], current, entry["old"])
        if acl_key(await acl_rows(conn, entry["item"])) != acl_key(entry["old"]):
            raise Stop("ACL inverse verification failed")


async def stage3_journal_state(conn: asyncpg.Connection, journal: dict) -> str:
    possible = {"before", "after"}
    for entry in journal["before"]:
        obj = await catalog_object(conn, entry["item"])
        if obj["owner"] != entry["item"]["expected_owner"]:
            raise Stop("grant journal owner changed")
        current = acl_key(await acl_rows(conn, entry["item"]))
        possible &= ({"before"} if current == acl_key(entry["old"]) else set()) | (
            {"after"} if current == acl_key(entry["new"]) else set()
        )
    for entry in journal.get("defaults", []):
        current = await function_default_acl(conn, entry["creator"])
        possible &= ({"before"} if current == entry["old"] else set()) | (
            {"after"} if current == entry["new"] else set()
        )
    if journal.get("database_acl"):
        current = (await database_acl_snapshot(conn))["entries"]
        record = journal["database_acl"]
        possible &= (
            {"before"} if current == record["before"]["entries"] else set()
        ) | ({"after"} if current == record["after"]["entries"] else set())
    if not possible:
        raise Stop("Stage 3 catalog state differs from both sides of prepared journal")
    return "after" if "after" in possible else "before"


async def apply4(conn: asyncpg.Connection, record: dict) -> dict:
    validate_groups(await role_snapshot(conn))
    await hba_reject_gate(conn)
    required = (
        "login_inventory",
        "secret_mapping_ref",
        "deployment_template_ref",
        "migration_runner_login",
        "migration_identity_proof",
        "consumer_inventory",
        "consumer_observations",
    )
    if any(not record.get(k) for k in required):
        raise Stop("credential cutover evidence incomplete; NEEDS_DESK_SQL")
    inventory = record["login_inventory"]
    if not isinstance(inventory, list) or not inventory:
        raise Stop("login inventory must be a nonempty list")
    names = set()
    for item in inventory:
        if not isinstance(item, dict):
            raise Stop("application login inventory entry malformed")
        login = item.get("login")
        if not login or login in names or item.get("group") != "at_app":
            raise Stop("application login inventory incomplete")
        names.add(login)
        role = await conn.fetchrow(
            "SELECT rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls,rolinherit FROM pg_roles WHERE rolname=$1",
            login,
        )
        if (
            role is None
            or not role["rolcanlogin"]
            or not role["rolinherit"]
            or any(
                role[k]
                for k in (
                    "rolsuper",
                    "rolcreaterole",
                    "rolcreatedb",
                    "rolreplication",
                    "rolbypassrls",
                )
            )
        ):
            raise Stop("application login attributes violate least privilege: " + login)
        memberships = await conn.fetch(
            "SELECT p.rolname, m.inherit_option, m.set_option, m.admin_option FROM pg_auth_members m JOIN pg_roles p ON p.oid=m.roleid JOIN pg_roles c ON c.oid=m.member WHERE c.rolname=$1",
            login,
        )
        if [dict(m) for m in memberships] != [
            {
                "rolname": "at_app",
                "inherit_option": True,
                "set_option": False,
                "admin_option": False,
            }
        ]:
            raise Stop(
                "application login membership differs from approved at_app-only contract"
            )
    runner = record["migration_runner_login"]
    if runner != "at_migration_runner" or runner in names:
        raise Stop("migration runner must be the dedicated at_migration_runner login")
    role = await conn.fetchrow(
        "SELECT rolcanlogin,rolinherit,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls FROM pg_roles WHERE rolname=$1",
        runner,
    )
    if (
        role is None
        or not role["rolcanlogin"]
        or role["rolinherit"]
        or any(
            role[k]
            for k in (
                "rolsuper",
                "rolcreaterole",
                "rolcreatedb",
                "rolreplication",
                "rolbypassrls",
            )
        )
    ):
        raise Stop("migration runner attributes violate least privilege")
    memberships = await conn.fetch(
        "SELECT p.rolname,m.inherit_option,m.set_option,m.admin_option FROM pg_auth_members m JOIN pg_roles p ON p.oid=m.roleid JOIN pg_roles c ON c.oid=m.member WHERE c.rolname=$1",
        runner,
    )
    if [dict(m) for m in memberships] != [
        {
            "rolname": "at_migration_owner",
            "inherit_option": False,
            "set_option": True,
            "admin_option": False,
        }
    ]:
        raise Stop("migration runner must SET ROLE without inheriting owner rights")
    if not await conn.fetchval(
        "SELECT has_database_privilege($1,current_database(),'CONNECT')", runner
    ):
        raise Stop("migration runner lacks database CONNECT")
    proof = record["migration_identity_proof"]
    if (
        not isinstance(proof, dict)
        or proof.get("session_user") != runner
        or proof.get("current_user") != "at_migration_owner"
        or proof.get("database") != record["database"]
        or not proof.get("observed_at")
    ):
        raise Stop("migration current_user evidence incomplete; NEEDS_DESK_SQL")
    expected_consumers = record["consumer_inventory"]
    observed = record["consumer_observations"]
    if (
        not isinstance(expected_consumers, list)
        or not expected_consumers
        or not all(isinstance(x, str) and x for x in expected_consumers)
        or not isinstance(observed, list)
        or len(set(expected_consumers)) != len(expected_consumers)
    ):
        raise Stop("consumer inventory malformed")
    if (
        not all(isinstance(x, dict) for x in observed)
        or {x.get("consumer") for x in observed} != set(expected_consumers)
        or len(observed) != len(expected_consumers)
    ):
        raise Stop("consumer observations do not cover signed inventory")
    for item in observed:
        if (
            item.get("session_user") not in names
            or item.get("current_user") not in (item["session_user"], "at_app")
            or item.get("database") != record["database"]
            or not item.get("observed_at")
        ):
            raise Stop("application session identity observation incomplete")
    if not await conn.fetchval(
        "SELECT has_database_privilege('at_app',current_database(),'CONNECT')"
    ):
        raise Stop("at_app lacks database CONNECT")
    desk = await conn.fetchrow(
        "SELECT rolcanlogin,rolinherit,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls FROM pg_roles WHERE rolname='at_desk_login'"
    )
    if (
        desk is None
        or not desk["rolcanlogin"]
        or desk["rolinherit"]
        or any(
            desk[k]
            for k in (
                "rolsuper",
                "rolcreaterole",
                "rolcreatedb",
                "rolreplication",
                "rolbypassrls",
            )
        )
    ):
        raise Stop("desk login attributes violate least privilege")
    desk_memberships = await conn.fetch(
        "SELECT p.rolname,m.inherit_option,m.set_option,m.admin_option FROM pg_auth_members m JOIN pg_roles p ON p.oid=m.roleid JOIN pg_roles c ON c.oid=m.member WHERE c.rolname='at_desk_login'"
    )
    if [dict(m) for m in desk_memberships] != [
        {
            "rolname": "nhplug_operator",
            "inherit_option": False,
            "set_option": True,
            "admin_option": False,
        }
    ] or not await conn.fetchval(
        "SELECT has_database_privilege('at_desk_login',current_database(),'CONNECT')"
    ):
        raise Stop("desk login must SET ROLE nhplug_operator and retain CONNECT")
    return {
        "checked_logins": sorted(names),
        "migration_runner": runner,
        "checked_consumers": sorted(expected_consumers),
    }


def evidence_time(value: object, label: str) -> datetime:
    if not isinstance(value, str):
        raise Stop(label + " requires an ISO timestamp with timezone")
    try:
        observed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise Stop(label + " requires an ISO timestamp with timezone") from exc
    if observed.tzinfo is None or observed.utcoffset() is None:
        raise Stop(label + " requires an ISO timestamp with timezone")
    return observed.astimezone(UTC)


async def apply5(conn: asyncpg.Connection, record: dict) -> dict:
    validate_groups(await role_snapshot(conn))
    await hba_reject_gate(conn)
    if not await conn.fetchval(
        "SELECT has_database_privilege('at_migration_owner',current_database(),'CONNECT')"
    ):
        raise Stop("TimescaleDB policy job owner lacks database CONNECT")
    required = (
        "legacy_login",
        "legacy_classification",
        "legacy_non_app_sessions",
        "app_input_removal_proof",
        "secret_rotation_proof",
        "timer_observation_proof",
        "backup_identity_proof",
        "prefect_identity_proof",
        "timescale_graph_proof",
        "timescale_job_proof",
        "new_login_observation_proof",
    )
    if any(
        k not in record or (k != "legacy_non_app_sessions" and not record[k])
        for k in required
    ):
        raise Stop("retirement evidence incomplete; NEEDS_DESK_SQL")
    if (
        record.get("database") != "auto_trader"
        or record["legacy_login"] != "postgres"
        or record["legacy_classification"] != "shared_infrastructure"
    ):
        raise Stop(
            "Stage 5 applies only to shared infrastructure postgres, without role retirement"
        )
    role = await conn.fetchrow(
        "SELECT oid,rolsuper,rolcanlogin FROM pg_roles WHERE rolname='postgres'"
    )
    if role is None or not role["rolsuper"] or not role["rolcanlogin"]:
        raise Stop("shared postgres must remain LOGIN SUPERUSER")
    backup = record["backup_identity_proof"]
    if (
        not isinstance(backup, dict)
        or backup.get("role") != "postgres"
        or backup.get("execution_user") != "root"
        or backup.get("database_dumps") != ["auto_trader", "handoffkeep"]
        or backup.get("globals_only") is not True
        or backup.get("independent_of_app_secret") is not True
        or not backup.get("observed_at")
    ):
        raise Stop("root backup postgres identity evidence incomplete")
    prefect = record["prefect_identity_proof"]
    if (
        not isinstance(prefect, dict)
        or prefect.get("role") != "postgres"
        or prefect.get("database") != "prefect"
        or not prefect.get("observed_at")
    ):
        raise Stop("separate Prefect postgres identity evidence incomplete")
    databases = await conn.fetch(
        "SELECT datname,datallowconn,has_database_privilege('postgres',datname,'CONNECT') AS can_connect FROM pg_database WHERE datname=ANY($1::text[])",
        ["auto_trader", "handoffkeep", "prefect"],
    )
    by_database = {r["datname"]: r for r in databases}
    if any(
        name not in by_database
        or not by_database[name]["datallowconn"]
        or not by_database[name]["can_connect"]
        for name in ("auto_trader", "handoffkeep", "prefect")
    ):
        raise Stop(
            "postgres CONNECT or database availability missing for backup or Prefect"
        )
    sessions = await conn.fetch(
        "SELECT pid,datname,application_name,client_addr::text AS client_addr,backend_type FROM pg_stat_activity WHERE usename='postgres' AND datname IS NOT NULL AND pid<>pg_backend_pid() ORDER BY pid"
    )
    classified = record["legacy_non_app_sessions"]
    if (
        not isinstance(classified, list)
        or len(classified) != len(sessions)
        or not all(
            isinstance(x, dict) and type(x.get("pid")) is int and x["pid"] > 0
            for x in classified
        )
    ):
        raise Stop("postgres sessions are unclassified; old app session proof absent")
    by_pid = {x["pid"]: x for x in classified}
    if len(by_pid) != len(classified):
        raise Stop("duplicate postgres session classification")
    for session in sessions:
        evidence = by_pid.get(session["pid"])
        if (
            evidence is None
            or evidence.get("datname") != session["datname"]
            or evidence.get("application_name") != session["application_name"]
            or evidence.get("client_addr") != session["client_addr"]
            or evidence.get("backend_type") != session["backend_type"]
            or evidence.get("classification")
            not in ("dba", "backup", "prefect", "timescale_scheduler")
        ):
            raise Stop("old application session or unclassified legacy session remains")
        classification = evidence["classification"]
        if classification == "timescale_scheduler":
            if (
                session["backend_type"] != "TimescaleDB Background Worker Scheduler"
                or session["application_name"]
                != "TimescaleDB Background Worker Scheduler"
            ):
                raise Stop(
                    "Timescale scheduler classification does not match backend identity"
                )
        elif session["backend_type"] != "client backend" or not evidence.get(
            "source_ref"
        ):
            raise Stop(
                "client session classification lacks independent source evidence"
            )
        if (
            session["datname"] == "auto_trader"
            and classification != "timescale_scheduler"
        ):
            app_name = (session["application_name"] or "").lower()
            if any(
                marker in app_name
                for marker in ("auto_trader", "taskiq", "scheduler", "mcp", "worker")
            ):
                raise Stop(
                    "legacy client application name matches an app consumer; classification rejected"
                )
        if classification == "backup" and session["datname"] not in (
            "auto_trader",
            "handoffkeep",
        ):
            raise Stop("backup session uses an unreviewed database")
        if classification == "prefect" and session["datname"] != "prefect":
            raise Stop("Prefect session classification uses wrong database")
    removed = record["app_input_removal_proof"]
    rotated = record["secret_rotation_proof"]
    observed = record["new_login_observation_proof"]
    if (
        not isinstance(removed, dict)
        or not isinstance(rotated, dict)
        or not isinstance(observed, list)
    ):
        raise Stop(
            "legacy input, secret rotation, and new login evidence must be structured"
        )
    consumers = removed.get("consumers")
    required_inputs = {".env.api", ".env.scheduler"}
    if (
        not isinstance(consumers, list)
        or len(consumers) != 2
        or not all(isinstance(x, str) for x in consumers)
        or set(consumers) != required_inputs
        or not removed.get("observed_at")
        or type(removed.get("old_app_sessions_after_drain")) is not int
        or removed["old_app_sessions_after_drain"] != 0
    ):
        raise Stop(
            "app input removal proof must cover only API and scheduler, with zero old app sessions"
        )
    if (
        not isinstance(rotated.get("completed_consumers"), list)
        or len(rotated["completed_consumers"]) != 2
        or not all(isinstance(x, str) for x in rotated["completed_consumers"])
        or set(rotated["completed_consumers"]) != required_inputs
        or rotated.get("postgres_credential_unchanged") is not True
        or not rotated.get("observed_at")
    ):
        raise Stop(
            "secret rotation must cover only API and scheduler and preserve postgres"
        )
    if (
        len(observed) != len(consumers)
        or not all(
            isinstance(x, dict) and isinstance(x.get("consumer"), str) for x in observed
        )
        or {x["consumer"] for x in observed} != set(consumers)
    ):
        raise Stop("new login observation does not cover every consumer")
    new_logins = set()
    for item in observed:
        login = item.get("session_user")
        if (
            not login
            or login in ("postgres", "at_migration_owner")
            or login in new_logins
            or item.get("current_user") != login
            or item.get("database") != record["database"]
            or not item.get("observed_at")
            or type(item.get("pid")) is not int
            or item["pid"] <= 0
        ):
            raise Stop("new application login identity evidence incomplete")
        new_logins.add(login)
        app_role = await conn.fetchrow(
            "SELECT rolcanlogin,rolsuper,rolcreaterole,rolcreatedb,rolreplication,rolbypassrls,rolinherit FROM pg_roles WHERE rolname=$1",
            login,
        )
        if (
            app_role is None
            or not app_role["rolcanlogin"]
            or not app_role["rolinherit"]
            or any(
                app_role[k]
                for k in (
                    "rolsuper",
                    "rolcreaterole",
                    "rolcreatedb",
                    "rolreplication",
                    "rolbypassrls",
                )
            )
        ):
            raise Stop("new application login attributes violate least privilege")
        membership = await conn.fetch(
            "SELECT p.rolname,m.admin_option,m.inherit_option,m.set_option FROM pg_auth_members m JOIN pg_roles p ON p.oid=m.roleid JOIN pg_roles c ON c.oid=m.member WHERE c.rolname=$1",
            login,
        )
        if [dict(m) for m in membership] != [
            {
                "rolname": "at_app",
                "admin_option": False,
                "inherit_option": True,
                "set_option": False,
            }
        ]:
            raise Stop("new application login lacks exact at_app-only membership")
        live = await conn.fetchrow(
            "SELECT pid,usename,datname,application_name,client_addr::text AS client_addr,backend_type FROM pg_stat_activity WHERE pid=$1",
            item["pid"],
        )
        if (
            live is None
            or live["usename"] != login
            or live["datname"] != record["database"]
            or live["backend_type"] != "client backend"
            or live["application_name"] != item.get("application_name")
            or live["client_addr"] != item.get("client_addr")
        ):
            raise Stop("new application login observation differs from live session")
    timer = record["timer_observation_proof"]
    if (
        not isinstance(timer, dict)
        or timer.get("installed_timers") != ["at-pg-backup.timer"]
        or timer.get("service_result") != "success"
        or timer.get("persistent_catchup_clear") is not True
    ):
        raise Stop("backup timer observation differs from operator preflight")
    now = datetime.now(UTC)
    rotation_at = evidence_time(rotated.get("observed_at"), "secret rotation")
    backup_success_at = evidence_time(
        timer.get("last_success_at"), "backup timer last success"
    )
    timer_observed_at = evidence_time(
        timer.get("observed_at"), "backup timer observation"
    )
    if not (
        rotation_at <= backup_success_at
        and now - timedelta(days=7) <= backup_success_at
        and backup_success_at <= timer_observed_at <= now + timedelta(minutes=1)
    ):
        raise Stop("backup timer success must follow rotation and be recently observed")
    await timescale_gate(conn, record)
    jobs = await timescale_jobs.snapshot(conn)
    for job in jobs:
        timescale_jobs.policy_kind(job)
        if job["owner"] != "at_migration_owner":
            raise Stop("TimescaleDB policy job owner proof incomplete")
    if record["timescale_job_proof"] != [[j["job_id"], j["owner"]] for j in jobs]:
        raise Stop("signed TimescaleDB job proof differs from catalog")
    graph_keys = await app_catalog_keys(
        conn, include_functions_and_types=True
    ) | await timescale_internal_keys(conn)
    graph_proof = []
    for kind, schema, name, args in sorted(graph_keys):
        item = {
            "kind": kind,
            "schema": schema,
            "name": name,
            "identity_args": args,
            "timescale_internal": schema == "_timescaledb_internal",
        }
        obj = await catalog_object(conn, item)
        if obj["owner"] not in ("at_migration_owner", "nhplug_security_owner"):
            raise Stop("TimescaleDB graph or app object owner remains legacy")
        graph_proof.append([kind, schema, name, args, obj["owner"]])
    if record["timescale_graph_proof"] != graph_proof:
        raise Stop("signed TimescaleDB graph proof differs from catalog")
    return {
        "legacy_login": "postgres",
        "action": "rotate .env.api and .env.scheduler only; postgres unchanged",
        "backup_connect_databases": ["auto_trader", "handoffkeep"],
        "prefect_connect": True,
        "checked_new_logins": sorted(new_logins),
        "classified_postgres_sessions": len(sessions),
    }


async def run(args: argparse.Namespace) -> None:
    dsn = os.environ.get(args.dsn_env)
    if not dsn:
        raise Stop("missing connection environment variable: " + args.dsn_env)
    record = None
    if args.stage in (2, 3, 4, 5) and args.mode == "apply":
        record = load_approved(args.manifest, args.sha256)
        if record.get("database") != args.database or record.get("stage") != args.stage:
            raise Stop("approved record target mismatch")
    existing = None
    if args.mode == "apply" and Path(args.journal).exists():
        existing = read_journal(args.journal, args.stage, args.database)
        if args.stage != 1 and existing.get("manifest_sha256") != args.sha256:
            raise Stop("existing journal approval differs")
    if args.mode == "rollback":
        journal = read_journal(args.journal, args.stage, args.database)
        existing = journal
    conn = await asyncpg.connect(
        dsn=dsn,
        timeout=5,
        server_settings={"application_name": "db_roles_stage_" + str(args.stage)},
    )
    try:
        if args.stage == 2:
            await run_stage2(conn, args, record, existing)
            print("stage", args.stage, args.mode, "completed against", args.database)
            return
        async with conn.transaction(readonly=args.stage in (4, 5)):
            await conn.execute("SET LOCAL lock_timeout = '3s'")
            await conn.execute("SET LOCAL statement_timeout = '30s'")
            await conn.execute("SELECT pg_advisory_xact_lock(789, $1)", args.stage)
            await validate_target(conn, args.database)
            if args.mode == "apply":
                if args.stage == 1:
                    result = await apply1(conn, args)
                elif args.stage == 3:
                    if (
                        existing is not None
                        and await stage3_journal_state(conn, existing) == "after"
                    ):
                        result = existing
                    else:
                        result = await apply3(conn, args, record)
                elif args.stage == 4:
                    result = await apply4(conn, record)
                else:
                    result = await apply5(conn, record)
                if existing is None:
                    save_journal(
                        args.journal,
                        {
                            "stage": args.stage,
                            "database": args.database,
                            "manifest_sha256": args.sha256,
                            **result,
                        },
                    )
            else:
                if args.stage == 1:
                    await rollback1(conn, journal)
                elif args.stage == 3:
                    await rollback3(conn, journal)
                else:
                    result = journal
                    print(
                        "stage has no SQL mutation; external credential rollback requires approved operator procedure"
                    )
        print("stage", args.stage, args.mode, "completed against", args.database)
    finally:
        await conn.close()


def main(stage: int, mode: str) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--dsn-env", default="DB_ROLES_DSN")
    parser.add_argument("--journal", required=True)
    parser.add_argument("--manifest")
    parser.add_argument("--sha256")
    args = parser.parse_args()
    args.stage, args.mode = stage, mode
    if (
        stage in (2, 3, 4, 5)
        and mode == "apply"
        and (not args.manifest or not args.sha256)
    ):
        parser.error("--manifest and --sha256 are required")
    try:
        asyncio.run(run(args))
        return 0
    except (Stop, FileNotFoundError, ValueError, KeyError, json.JSONDecodeError) as exc:
        print("STOP:", str(exc), file=sys.stderr)
        return 2
    except timescale_jobs.JobStop as exc:
        print("STOP:", str(exc), file=sys.stderr)
        return 2
    except asyncpg.PostgresError as exc:
        print(
            "STOP: PostgreSQL catalog transaction failed; SQLSTATE",
            exc.sqlstate,
            file=sys.stderr,
        )
        return 3
    except (OSError, TimeoutError):
        print("STOP: connection or journal I/O failed", file=sys.stderr)
        return 4
