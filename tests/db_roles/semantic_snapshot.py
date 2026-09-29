"""Compare fixture rollback states by privileges and ownership, not ACL text."""

from __future__ import annotations

import argparse
import asyncio
import difflib
import json
import os
from pathlib import Path

import asyncpg

from scripts.db_roles import core, timescale_jobs
from tests.db_roles.fixture_approvals import check_dsn, check_fixture


async def snapshot(conn: asyncpg.Connection) -> dict:
    await check_fixture(conn)
    await core.hba_reject_gate(conn)
    objects = []
    keys = await core.app_catalog_keys(conn, include_functions_and_types=True)
    keys |= await core.timescale_internal_keys(conn)
    for kind, schema, name, args in sorted(keys):
        item = {
            "kind": kind,
            "schema": schema,
            "name": name,
            "identity_args": args,
            "timescale_internal": schema == "_timescaledb_internal",
        }
        obj = await core.catalog_object(conn, item)
        objects.append(
            {
                "key": [kind, schema, name, args],
                "owner": obj["owner"],
                "acl": [
                    list(grant)
                    for grant in await core.effective_acl_key(
                        conn, obj, item, obj["acl"]
                    )
                ],
                "row_type_owner": (
                    await core.relation_type_owner(conn, obj["oid"])
                    if kind == "relation"
                    else None
                ),
                "composite_owner": (
                    await core.composite_class_owner(conn, obj["oid"])
                    if kind == "type"
                    else None
                ),
            }
        )
    roles = await conn.fetch(
        "SELECT rolname,rolsuper,rolcanlogin,rolcreaterole,rolcreatedb,"
        "rolreplication,rolbypassrls,rolinherit FROM pg_roles WHERE rolname "
        "=ANY($1::text[]) ORDER BY rolname",
        [
            "mgh3326",
            "postgres",
            "nhplug_operator",
            "nhplug_security_owner",
            "at_app",
            "at_migration_owner",
            "at_api_login",
            "at_scheduler_login",
            "at_migration_runner",
            "at_desk_login",
        ],
    )
    memberships = await conn.fetch(
        "SELECT p.rolname parent,c.rolname child,m.admin_option,"
        "m.inherit_option,m.set_option FROM pg_auth_members m "
        "JOIN pg_roles p ON p.oid=m.roleid JOIN pg_roles c ON c.oid=m.member "
        "WHERE p.rolname=ANY($1::text[]) OR c.rolname=ANY($1::text[]) "
        "ORDER BY p.rolname,c.rolname",
        ["at_app", "at_migration_owner", "nhplug_operator", "nhplug_security_owner"],
    )
    database_acl = await conn.fetch(
        "SELECT CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE gr.rolname END grantee,"
        "pg_get_userbyid(a.grantor) grantor,a.privilege_type,a.is_grantable "
        "FROM pg_database d, "
        "LATERAL aclexplode(COALESCE(d.datacl,acldefault('d',d.datdba))) a "
        "LEFT JOIN pg_roles gr ON gr.oid=a.grantee "
        "WHERE d.datname=current_database()"
    )
    default_acls = await conn.fetch(
        "SELECT pg_get_userbyid(d.defaclrole) creator,"
        "COALESCE(n.nspname,'<database>') schema_name,"
        "d.defaclobjtype::text object_type,"
        "CASE WHEN a.grantee=0 THEN 'PUBLIC' ELSE gr.rolname END grantee,"
        "pg_get_userbyid(a.grantor) grantor,a.privilege_type,a.is_grantable "
        "FROM pg_default_acl d "
        "LEFT JOIN pg_namespace n ON n.oid=d.defaclnamespace, "
        "LATERAL aclexplode(COALESCE(d.defaclacl,"
        "acldefault(d.defaclobjtype,d.defaclrole))) a "
        "LEFT JOIN pg_roles gr ON gr.oid=a.grantee"
    )
    jobs = await timescale_jobs.snapshot(conn)
    job_semantics = [
        {"owner": job["owner"], **{key: job[key] for key in timescale_jobs.STATIC}}
        for job in jobs
    ]
    job_semantics.sort(key=lambda job: json.dumps(job, sort_keys=True))
    hba = await conn.fetch(
        "SELECT row_to_json(r)::text payload FROM pg_hba_file_rules r "
        "ORDER BY rule_number"
    )
    return {
        "objects": objects,
        "roles": [dict(row) for row in roles],
        "memberships": [dict(row) for row in memberships],
        "database_acl": [
            list(grant) for grant in core.acl_key([dict(row) for row in database_acl])
        ],
        "default_acl": sorted(
            [
                row["creator"],
                row["schema_name"],
                row["object_type"],
                row["grantee"],
                row["grantor"],
                row["privilege_type"],
                row["is_grantable"],
            ]
            for row in default_acls
            if row["grantee"] != row["grantor"]
        ),
        "jobs": job_semantics,
        "hba": [json.loads(row["payload"]) for row in hba],
    }


async def run(args: argparse.Namespace) -> None:
    dsn = os.environ["DB_ROLES_DSN"]
    check_dsn(dsn)
    conn = await asyncpg.connect(dsn)
    try:
        async with conn.transaction(readonly=True):
            actual = await snapshot(conn)
    finally:
        await conn.close()
    if args.output:
        Path(args.output).write_text(
            json.dumps(actual, sort_keys=True, indent=2) + "\n"
        )
        return
    expected = json.loads(Path(args.compare).read_text())
    if actual != expected:
        left = json.dumps(expected, sort_keys=True, indent=2).splitlines()
        right = json.dumps(actual, sort_keys=True, indent=2).splitlines()
        diff = difflib.unified_diff(left, right, fromfile="expected", tofile="actual")
        raise RuntimeError(
            "semantic rollback mismatch:\n" + "\n".join(list(diff)[:100])
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    target = parser.add_mutually_exclusive_group(required=True)
    target.add_argument("--output")
    target.add_argument("--compare")
    asyncio.run(run(parser.parse_args()))
