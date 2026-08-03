"""ROB-1036 — migration static contract + isolated upgrade/downgrade/upgrade.

The upgrade/downgrade acceptance runs against a throwaway database created for
this test alone (model: ``tests/services/paper_evaluation/test_migration.py``).
It never touches the runtime/production database.
"""

from __future__ import annotations

import ast
import os
import subprocess
from pathlib import Path
from uuid import uuid4

import asyncpg
import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import create_async_engine

from app.core.config import settings
from app.models.base import Base

pytestmark = pytest.mark.integration

REPO = Path(__file__).resolve().parents[3]
MIGRATION = REPO / "alembic/versions/20260802_rob1036_sample_elig.py"
REVISION = "20260802_rob1036_sample_elig"
DOWN_REVISION = "20260728_rob1109_watch_intent"
NEW_TABLES = (
    "sample_eligibility_decisions",
    "invalid_sample_cleanup_bindings",
    "invalid_sample_cleanup_lifecycle_events",
)


def _literal(tree: ast.AST, name: str):
    for node in getattr(tree, "body", []):
        if isinstance(node, ast.Assign) and any(
            isinstance(target, ast.Name) and target.id == name
            for target in node.targets
        ):
            return ast.literal_eval(node.value)
    raise AssertionError(f"missing {name!r}")


def test_migration_descends_from_its_parent_on_the_single_head_chain() -> None:
    tree = ast.parse(MIGRATION.read_text(encoding="utf-8"))
    assert _literal(tree, "revision") == REVISION
    assert _literal(tree, "down_revision") == DOWN_REVISION

    config = Config(str(REPO / "alembic.ini"))
    config.set_main_option("script_location", str(REPO / "alembic"))
    script = ScriptDirectory.from_config(config)
    # Exactly one head — the property that actually matters — rather than "this
    # revision IS the head", which stops being true the moment any later
    # migration lands and says nothing about this one.
    heads = script.get_heads()
    assert len(heads) == 1, heads
    ancestry = {rev.revision for rev in script.walk_revisions("base", heads[0])}
    assert REVISION in ancestry


def test_migration_is_purely_additive() -> None:
    """No existing table/column is altered, dropped, or backfilled."""

    source = MIGRATION.read_text(encoding="utf-8")
    tree = ast.parse(source)
    op_calls = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    }
    forbidden = {
        "alter_column",
        "drop_column",
        "add_column",
        "bulk_insert",
        "rename_table",
    }
    assert not (op_calls & forbidden), op_calls
    # upgrade() creates only the three new tables.
    created = [
        node.args[0].value
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "create_table"
        and node.args
        and isinstance(node.args[0], ast.Constant)
    ]
    assert sorted(created) == sorted(NEW_TABLES)
    # No data statement writes or rewrites a historical eligibility decision.
    # (The append-only trigger DDL legitimately contains "BEFORE UPDATE OR
    # DELETE ON review.x", so the check targets data statements specifically.)
    lowered = source.lower()
    for statement in ("insert into review.", "update review.", "delete from review."):
        assert statement not in lowered, statement


@pytest.mark.asyncio
async def test_isolated_upgrade_downgrade_upgrade() -> None:
    base_url = make_url(settings.DATABASE_URL)
    if base_url.get_backend_name() != "postgresql":
        pytest.skip("ROB-1036 migration acceptance requires PostgreSQL")

    database = f"rob1036_migration_{uuid4().hex}"
    admin = await asyncpg.connect(
        user=base_url.username,
        password=base_url.password,
        host=base_url.host,
        port=base_url.port,
        database="postgres",
    )
    await admin.execute(f'CREATE DATABASE "{database}"')
    target_url = base_url.set(database=database)
    target_url_text = target_url.render_as_string(hide_password=False)
    engine = create_async_engine(target_url_text)
    try:
        async with engine.begin() as connection:
            for schema in ("paper", "research", "review"):
                await connection.execute(
                    text(f'CREATE SCHEMA IF NOT EXISTS "{schema}"')
                )
            await connection.run_sync(Base.metadata.create_all)
            # Reconstruct the pre-ROB-1036 boundary: current metadata already
            # contains this revision's tables, so drop them before stamping.
            for table in NEW_TABLES:
                await connection.execute(text(f"DROP TABLE review.{table}"))

        env = {**os.environ, "DATABASE_URL": target_url_text}

        def alembic(*args: str) -> subprocess.CompletedProcess[str]:
            return subprocess.run(
                [str(REPO / ".venv/bin/alembic"), *args],
                cwd=REPO,
                env=env,
                text=True,
                capture_output=True,
                check=False,
            )

        stamped = alembic("stamp", DOWN_REVISION)
        assert stamped.returncode == 0, stamped.stderr

        # Upgrade to THIS revision, not head: later migrations are not under
        # test here, and replaying them would collide with tables create_all
        # already built. It also keeps "downgrade -1" pointed at this revision.
        upgraded = alembic("upgrade", REVISION)
        assert upgraded.returncode == 0, upgraded.stderr

        async with engine.connect() as connection:
            for table in NEW_TABLES:
                exists = await connection.scalar(
                    text(
                        "SELECT to_regclass(:name) IS NOT NULL",
                    ),
                    {"name": f"review.{table}"},
                )
                assert exists is True, table
            triggers = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_trigger t "
                    "JOIN pg_class c ON c.oid = t.tgrelid "
                    "JOIN pg_namespace n ON n.oid = c.relnamespace "
                    "WHERE n.nspname = 'review' AND NOT t.tgisinternal "
                    "AND t.tgname LIKE 'trg_rob1036_%'"
                )
            )
            # Two triggers (row + truncate) per table.
            assert triggers == 2 * len(NEW_TABLES)

        # The append-only trigger is live right after the upgrade.
        async with engine.begin() as connection:
            await connection.execute(
                text(
                    "INSERT INTO review.sample_eligibility_decisions "
                    "(subject_kind, subject_ref, contract_version, revision_no, "
                    "supersedes_revision_no, forecast_outcome_observability, "
                    "calibration_eligibility, trade_performance_eligibility, "
                    "operational_reliability_eligibility, decision_reason, "
                    "decided_by, evidence, evidence_hash) VALUES "
                    "('forecast', 'mig-check', 'uber-invalid-sample-eligibility.v1', "
                    "1, NULL, 'observable', 'calibration_include', "
                    "'trade_performance_include', 'operational_include', 'seed', "
                    "'test', '{}'::jsonb, :digest)"
                ),
                {"digest": "0" * 64},
            )
        with pytest.raises(DBAPIError):
            async with engine.begin() as connection:
                await connection.execute(
                    text(
                        "UPDATE review.sample_eligibility_decisions "
                        "SET decision_reason = 'tampered'"
                    )
                )

        downgraded = alembic("downgrade", "-1")
        assert downgraded.returncode == 0, downgraded.stderr

        async with engine.connect() as connection:
            for table in NEW_TABLES:
                exists = await connection.scalar(
                    text("SELECT to_regclass(:name) IS NOT NULL"),
                    {"name": f"review.{table}"},
                )
                assert exists is False, table
            function_left = await connection.scalar(
                text(
                    "SELECT count(*) FROM pg_proc p "
                    "JOIN pg_namespace n ON n.oid = p.pronamespace "
                    "WHERE n.nspname = 'review' "
                    "AND p.proname = 'reject_invalid_sample_mutation'"
                )
            )
            assert function_left == 0

        re_upgraded = alembic("upgrade", REVISION)
        assert re_upgraded.returncode == 0, re_upgraded.stderr

        async with engine.connect() as connection:
            for table in NEW_TABLES:
                exists = await connection.scalar(
                    text("SELECT to_regclass(:name) IS NOT NULL"),
                    {"name": f"review.{table}"},
                )
                assert exists is True, table
    finally:
        await engine.dispose()
        await admin.execute(
            "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = $1",
            database,
        )
        await admin.execute(f'DROP DATABASE IF EXISTS "{database}"')
        await admin.close()
