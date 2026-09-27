"""TimescaleDB 2.22.1 policy move using public policy APIs only.

The journal is written before the first change.  A policy is removed and
recreated in one database transaction.  Its generated ID is discovered from
the public jobs view after commit, so a crash between commit and journal
update is recoverable without assuming an ID sequence value.
"""

from __future__ import annotations

import json

import asyncpg


class JobStop(Exception):
    pass


TYPES = {
    "policy_retention": ("policy_retention_check", "Retention Policy ["),
    "policy_refresh_continuous_aggregate": (
        "policy_refresh_continuous_aggregate_check",
        "Refresh Continuous Aggregate Policy [",
    ),
}
STATIC = (
    "schedule_interval",
    "max_runtime",
    "max_retries",
    "retry_period",
    "proc_schema",
    "proc_name",
    "scheduled",
    "fixed_schedule",
    "config",
    "initial_start",
    "hypertable_schema",
    "hypertable_name",
    "check_schema",
    "check_name",
    "timezone",
)


async def snapshot(conn: asyncpg.Connection) -> list[dict]:
    rows = await conn.fetch("""
        SELECT row_to_json(j)::text AS payload, b.timezone
        FROM timescaledb_information.jobs j
        JOIN _timescaledb_config.bgw_job b ON b.id=j.job_id
        WHERE j.job_id >= 1000 ORDER BY j.job_id
    """)
    return [{**json.loads(r["payload"]), "timezone": r["timezone"]} for r in rows]


def policy_kind(job: dict) -> str:
    kind = job.get("proc_name")
    if kind not in TYPES or job.get("proc_schema") != "_timescaledb_functions":
        raise JobStop("unsupported TimescaleDB policy job type; NEEDS_DESK_SQL")
    check, prefix = TYPES[kind]
    if (
        job.get("check_schema") != "_timescaledb_functions"
        or job.get("check_name") != check
    ):
        raise JobStop("TimescaleDB policy check function drift; NEEDS_DESK_SQL")
    if not job.get("application_name", "").startswith(prefix):
        raise JobStop("TimescaleDB policy application name drift; NEEDS_DESK_SQL")
    if job.get("owner") not in ("mgh3326", "at_migration_owner"):
        raise JobStop("TimescaleDB policy owner outside reviewed transition")
    if not job.get("hypertable_schema") or not job.get("hypertable_name"):
        raise JobStop("TimescaleDB policy target relation absent")
    config = job.get("config")
    if not isinstance(config, dict):
        raise JobStop("TimescaleDB policy config is not an object")
    if kind == "policy_retention":
        if (
            set(config) != {"drop_after", "hypertable_id"}
            or not isinstance(config["drop_after"], str)
            or type(config["hypertable_id"]) is not int
        ):
            raise JobStop("unsupported retention policy config; NEEDS_DESK_SQL")
    elif (
        set(config) != {"start_offset", "end_offset", "mat_hypertable_id"}
        or type(config["mat_hypertable_id"]) is not int
        or any(
            config[k] is not None and not isinstance(config[k], str)
            for k in ("start_offset", "end_offset")
        )
    ):
        raise JobStop("unsupported continuous aggregate policy config; NEEDS_DESK_SQL")
    if (
        job.get("fixed_schedule") is not False
        or job.get("initial_start") is not None
        or job.get("timezone") is not None
    ):
        raise JobStop(
            "policy scheduling differs from reviewed 2.22.1 fixture; NEEDS_DESK_SQL"
        )
    if job.get("scheduled") is not True:
        raise JobStop("unscheduled policy requires separate review; NEEDS_DESK_SQL")
    if job.get("job_id", 0) < 1000:
        raise JobStop("internal TimescaleDB job is outside migration scope")
    return kind


def same_policy(left: dict, right: dict, *, owner: str) -> bool:
    return right.get("owner") == owner and all(
        left.get(k) == right.get(k) for k in STATIC
    )


def relation(job: dict) -> str:
    def quote(value: str) -> str:
        return '"' + value.replace('"', '""') + '"'

    return quote(job["hypertable_schema"]) + "." + quote(job["hypertable_name"])


async def preflight(conn: asyncpg.Connection, signed_pairs: object) -> list[dict]:
    if (
        await conn.fetchval(
            "SELECT extversion FROM pg_extension WHERE extname='timescaledb'"
        )
        != "2.22.1"
    ):
        raise JobStop(
            "TimescaleDB version needs separate operator approval; NEEDS_DESK_SQL"
        )
    jobs = await snapshot(conn)
    pairs = [[j["job_id"], j["owner"]] for j in jobs]
    if pairs != signed_pairs:
        raise JobStop("signed TimescaleDB job inventory differs; NEEDS_DESK_SQL")
    for job in jobs:
        policy_kind(job)
        if job["owner"] != "mgh3326":
            raise JobStop("initial policy owner differs from reviewed mgh3326 owner")
        target = await conn.fetchval("SELECT to_regclass($1)", relation(job))
        if target is None:
            raise JobStop("policy relation absent from catalog")
    return jobs


def candidates(jobs: list[dict], before: dict, owner: str) -> list[dict]:
    return [j for j in jobs if same_policy(before, j, owner=owner)]


def classify(jobs: list[dict], journal_jobs: list[dict]) -> list[tuple[str, dict]]:
    used: set[int] = set()
    result = []
    for entry in journal_jobs:
        before = entry["before"]
        old = candidates(jobs, before, "mgh3326")
        new = candidates(jobs, before, "at_migration_owner")
        if len(old) + len(new) != 1:
            raise JobStop(
                "policy transition state ambiguous after partial stage; NEEDS_DESK_SQL"
            )
        state, job = ("old", old[0]) if old else ("new", new[0])
        if job["job_id"] in used:
            raise JobStop("multiple journal entries match a single policy")
        used.add(job["job_id"])
        result.append((state, job))
    if used != {j["job_id"] for j in jobs}:
        raise JobStop("unreviewed TimescaleDB job appeared; NEEDS_DESK_SQL")
    return result


async def add_policy(conn: asyncpg.Connection, before: dict, owner: str) -> int:
    kind = policy_kind(before)
    name = relation(before)
    config = before["config"]
    await conn.execute('SET LOCAL ROLE "' + owner + '"')
    if kind == "policy_retention":
        new_id = await conn.fetchval(
            """
            SELECT add_retention_policy($1::regclass,
                drop_after => $2::text::interval,
                schedule_interval => $3::text::interval)
        """,
            name,
            config["drop_after"],
            before["schedule_interval"],
        )
    else:
        new_id = await conn.fetchval(
            """
            SELECT add_continuous_aggregate_policy($1::regclass,
                start_offset => $2::text::interval,
                end_offset => $3::text::interval,
                schedule_interval => $4::text::interval)
        """,
            name,
            config["start_offset"],
            config["end_offset"],
            before["schedule_interval"],
        )
    await conn.fetchrow(
        """
        SELECT * FROM alter_job($1::integer,
            schedule_interval => $2::text::interval,
            max_runtime => $3::text::interval,
            max_retries => $4::integer,
            retry_period => $5::text::interval,
            scheduled => $6::boolean,
            config => $7::jsonb,
            next_start => $8::text::timestamptz,
            fixed_schedule => $9::boolean)
    """,
        new_id,
        before["schedule_interval"],
        before["max_runtime"],
        before["max_retries"],
        before["retry_period"],
        before["scheduled"],
        json.dumps(config, sort_keys=True),
        before["next_start"],
        before["fixed_schedule"],
    )
    # SET LOCAL is undone by the surrounding per-job transaction on failure.
    # RESET on an aborted transaction would hide the original policy error.
    await conn.execute("RESET ROLE")
    after = await snapshot(conn)
    matched = [j for j in after if j["job_id"] == new_id]
    if len(matched) != 1 or not same_policy(before, matched[0], owner=owner):
        raise JobStop("recreated TimescaleDB policy differs from full snapshot")
    check, prefix = TYPES[kind]
    if matched[0]["application_name"] != prefix + str(new_id) + "]":
        raise JobStop("recreated TimescaleDB policy application name differs")
    return new_id


async def remove_policy(conn: asyncpg.Connection, job: dict) -> None:
    kind = policy_kind(job)
    name = relation(job)
    if kind == "policy_retention":
        await conn.execute("SELECT remove_retention_policy($1::regclass)", name)
    else:
        await conn.execute(
            "SELECT remove_continuous_aggregate_policy($1::regclass)", name
        )


async def transition_one(
    conn: asyncpg.Connection, entries: list[dict], index: int, *, reverse: bool
) -> int:
    entry = entries[index]
    before = entry["before"]
    async with conn.transaction():
        await conn.execute("SET LOCAL lock_timeout='3s'")
        await conn.execute("SET LOCAL statement_timeout='30s'")
        states = classify(await snapshot(conn), entries)
        state, actual = states[index]
        wanted = "old" if reverse else "new"
        if state == wanted:
            return actual["job_id"]
        await remove_policy(conn, actual)
        new_id = await add_policy(
            conn, before, "mgh3326" if reverse else "at_migration_owner"
        )
        if new_id == actual["job_id"]:
            raise JobStop("policy recreation did not allocate a new job ID")
        return new_id
