"""ROB-878 child-1: migration + backfill contract tests.

Tests verify the migration module metadata, DB constraints, indexes, and
edge-case behavior (NULL/JSON-null/empty, malformed data, missing status,
unknown-key preservation, write-fence shadow/canonical, parent immutability).

Edge-case tests use rolled-back transactions against the test DB engine
and re-run the relevant SQL patterns from the migration in a controlled scope.
"""

import ast
import importlib.util
import io
import json
from pathlib import Path

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.core.db import engine
from app.models.base import Base

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "20260714_rob878_trade_retrospective_actions_shadow.py"
)
_SPEC = importlib.util.spec_from_file_location(
    "rob878_shadow_ledger_migration", _MIGRATION_PATH
)
assert _SPEC is not None and _SPEC.loader is not None
_MIGRATION = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MIGRATION)


def _run_migration_step(sync_conn, step):
    """Run one op.execute-only migration helper on the active test connection."""
    original_execute = _MIGRATION.op.execute
    _MIGRATION.op.execute = lambda statement: sync_conn.execute(text(statement))
    try:
        step()
    finally:
        _MIGRATION.op.execute = original_execute


def _run_downgrade_guard(sync_conn):
    """Execute only downgrade's leading safety block, ignoring destructive DDL."""

    class GuardOnlyOperations:
        def __init__(self):
            self.execute_count = 0

        def execute(self, statement):
            self.execute_count += 1
            if self.execute_count == 1:
                sync_conn.execute(text(statement))

        def drop_table(self, *args, **kwargs):
            return None

        def drop_index(self, *args, **kwargs):
            return None

    original_op = _MIGRATION.op
    _MIGRATION.op = GuardOnlyOperations()
    try:
        _MIGRATION.downgrade()
    finally:
        _MIGRATION.op = original_op


async def _insert_action_row(
    conn,
    *,
    retrospective_id: int,
    position: int,
    action: str = "test action",
    creation_key: str | None = None,
    status: str = "open",
    version: int = 1,
    status_actor: str = "migration:rob-878",
    status_source: str = "migration",
):
    return await conn.execute(
        text(
            "INSERT INTO review.trade_retrospective_actions "
            "(retrospective_id, creation_key, position, action, status, version, "
            " status_actor, status_source) "
            "VALUES (:retrospective_id, CAST(:creation_key AS uuid), :position, "
            " :action, :status, :version, :status_actor, :status_source) "
            "RETURNING id"
        ),
        {
            "retrospective_id": retrospective_id,
            "creation_key": creation_key,
            "position": position,
            "action": action,
            "status": status,
            "version": version,
            "status_actor": status_actor,
            "status_source": status_source,
        },
    )


# ---------------------------------------------------------------------------
# Migration module metadata
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_migration_revision_metadata():
    """The migration module has correct revision chain."""
    source = _MIGRATION_PATH.read_text()
    tree = ast.parse(source)
    assignments = {
        node.targets[0].id: node.value.value
        for node in tree.body
        if isinstance(node, ast.Assign)
        and len(node.targets) == 1
        and isinstance(node.targets[0], ast.Name)
        and isinstance(node.value, ast.Constant)
        and node.targets[0].id in ("revision", "down_revision")
    }
    assert assignments.get("revision") == "20260714_rob878_shadow"
    assert assignments.get("down_revision") == "20260714_rob849_paper_cohort"


def test_offline_upgrade_renders_valid_server_defaults():
    """Production Alembic DDL must not double-quote SQL default expressions."""
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={
            "as_sql": True,
            "output_buffer": output,
            "target_metadata": Base.metadata,
        },
    )
    original_op = _MIGRATION.op
    _MIGRATION.op = Operations(context)
    try:
        _MIGRATION.upgrade()
    finally:
        _MIGRATION.op = original_op

    sql = output.getvalue()
    assert "status TEXT DEFAULT 'open' NOT NULL" in sql
    assert "status_source VARCHAR(32) NOT NULL" in sql
    assert "version INTEGER DEFAULT 1 NOT NULL" in sql
    assert "legacy_payload JSONB DEFAULT '{}'::jsonb NOT NULL" in sql
    assert "DEFAULT '''" not in sql
    assert "CONSTRAINT ck_trade_retrospective_actions_status CHECK" in sql
    assert "ck_trade_retrospective_actions_ck_trade_retrospective" not in sql


def test_offline_downgrade_locks_tables_parent_first():
    """Downgrade serializes with parent writers and child/control mutations."""
    output = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": output},
    )
    original_op = _MIGRATION.op
    _MIGRATION.op = Operations(context)
    try:
        _MIGRATION.downgrade()
    finally:
        _MIGRATION.op = original_op

    sql = " ".join(output.getvalue().split())
    assert (
        "LOCK TABLE review.trade_retrospectives, "
        "review.trade_retrospective_action_control, "
        "review.trade_retrospective_actions IN SHARE ROW EXCLUSIVE MODE"
    ) in sql


@pytest.mark.asyncio
async def test_migration_downgrade_upgrade_round_trip():
    """The real PostgreSQL DDL can round-trip and restore a valid shadow ledger."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:

            def _round_trip(sync_conn):
                context = MigrationContext.configure(
                    sync_conn,
                    opts={"target_metadata": Base.metadata},
                )
                original_op = _MIGRATION.op
                _MIGRATION.op = Operations(context)
                try:
                    _MIGRATION.downgrade()
                    _MIGRATION.upgrade()
                finally:
                    _MIGRATION.op = original_op

            await conn.run_sync(_round_trip)

            mode = (
                await conn.execute(
                    text(
                        "SELECT mode FROM "
                        "review.trade_retrospective_action_control WHERE id = 1"
                    )
                )
            ).scalar_one()
            assert mode == "shadow"

            counts = (
                await conn.execute(
                    text(
                        "SELECT "
                        "(SELECT COALESCE(sum(CASE "
                        "   WHEN jsonb_typeof(next_actions) = 'array' "
                        "   THEN jsonb_array_length(next_actions) ELSE 0 END), 0) "
                        " FROM review.trade_retrospectives), "
                        "(SELECT count(*) "
                        " FROM review.trade_retrospective_actions)"
                    )
                )
            ).one()
            assert counts[0] == counts[1]

            constraints = await conn.execute(
                text(
                    "SELECT conname FROM pg_constraint "
                    "WHERE conrelid = "
                    "'review.trade_retrospective_actions'::regclass "
                    "AND contype = 'c'"
                )
            )
            constraint_names = {row.conname for row in constraints}
            assert {
                "ck_trade_retrospective_actions_status",
                "ck_trade_retrospective_actions_status_source",
                "ck_trade_retrospective_actions_version",
                "ck_trade_retrospective_actions_position_col",
                "ck_trade_retrospective_actions_resolved_terminal",
                "ck_trade_retrospective_actions_reason_required",
                "ck_trade_retrospective_actions_evidence_required",
            } <= constraint_names
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_downgrade_rejects_missing_control_row():
    """Downgrade is allowed only with an existing exact shadow authority."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "DELETE FROM review.trade_retrospective_action_control WHERE id = 1"
                )
            )
            with pytest.raises(
                Exception,
                match="cannot downgrade.*control row is missing",
            ):
                await conn.run_sync(_run_downgrade_guard)
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_downgrade_rejects_canonical_mode():
    """Canonical identities are authoritative and may only roll forward."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "UPDATE review.trade_retrospective_action_control "
                    "SET mode = 'canonical' WHERE id = 1"
                )
            )
            with pytest.raises(
                Exception,
                match="cannot downgrade.*mode must be shadow",
            ):
                await conn.run_sync(_run_downgrade_guard)
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_downgrade_rejects_non_migration_action():
    """A version-1 canonical write is still protected by provenance."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990022, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "'[]'::jsonb)"
                )
            )
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospective_actions "
                    "(retrospective_id, position, action, status_actor, status_source) "
                    "VALUES (990022, 0, 'canonical write', 'operator:test', 'web')"
                )
            )
            with pytest.raises(
                Exception,
                match="cannot downgrade.*non-migration provenance",
            ):
                await conn.run_sync(_run_downgrade_guard)
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_downgrade_rejects_version_greater_than_one():
    """Even migration-provenance rows become non-discardable after version 1."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990028, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "'[]'::jsonb)"
                )
            )
            await _insert_action_row(
                conn,
                retrospective_id=990028,
                position=0,
                version=2,
            )
            with pytest.raises(
                Exception,
                match="cannot downgrade.*version > 1",
            ):
                await conn.run_sync(_run_downgrade_guard)
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_downgrade_rejects_creation_key_history():
    """A caller-created idempotency key proves the row is not pure backfill."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990033, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "'[]'::jsonb)"
                )
            )
            await _insert_action_row(
                conn,
                retrospective_id=990033,
                position=0,
                creation_key="00000000-0000-0000-0000-000000087833",
            )
            with pytest.raises(
                Exception,
                match="cannot downgrade.*non-migration provenance",
            ):
                await conn.run_sync(_run_downgrade_guard)
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# Constraint and index verification
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_action_table_check_constraints(db_session):
    """All design-specified CHECK constraints exist on the action table."""
    result = await db_session.execute(
        text(
            "SELECT conname FROM pg_constraint "
            "WHERE conrelid = 'review.trade_retrospective_actions'::regclass "
            "AND contype = 'c' ORDER BY conname"
        )
    )
    names = {row.conname for row in result}
    expected = {
        "ck_trade_retrospective_actions_status",
        "ck_trade_retrospective_actions_status_source",
        "ck_trade_retrospective_actions_version",
        "ck_trade_retrospective_actions_position_col",
        "ck_trade_retrospective_actions_resolved_terminal",
        "ck_trade_retrospective_actions_reason_required",
        "ck_trade_retrospective_actions_evidence_required",
    }
    assert expected <= names, f"missing: {expected - names}"


@pytest.mark.asyncio
async def test_deferrable_position_uniqueness(db_session):
    """The (retrospective_id, position) uniqueness is deferrable initially deferred."""
    result = await db_session.execute(
        text(
            "SELECT conname, condeferrable, condeferred FROM pg_constraint "
            "WHERE conrelid = 'review.trade_retrospective_actions'::regclass "
            "AND contype = 'u'"
        )
    )
    position_found = False
    for row in result:
        if "position" in row.conname:
            position_found = True
            assert row.condeferrable, f"{row.conname} should be deferrable"
            assert row.condeferred, f"{row.conname} should be initially deferred"
    assert position_found, "no deferrable position unique constraint found"


@pytest.mark.asyncio
async def test_indexes_exist(db_session):
    """All design-specified indexes have the expected keys and predicates."""
    result = await db_session.execute(
        text(
            "SELECT i.relname AS indexname, ix.indisunique, "
            "pg_get_indexdef(ix.indexrelid) AS definition, "
            "pg_get_expr(ix.indpred, ix.indrelid) AS predicate "
            "FROM pg_class t "
            "JOIN pg_namespace n ON n.oid = t.relnamespace "
            "JOIN pg_index ix ON ix.indrelid = t.oid "
            "JOIN pg_class i ON i.oid = ix.indexrelid "
            "WHERE n.nspname = 'review' "
            "AND t.relname = 'trade_retrospective_actions'"
        )
    )
    indexes = {row.indexname: row for row in result}
    expected = {
        "ix_trade_retrospective_actions_parent_position",
        "ix_trade_retrospective_actions_due_active",
        "uq_trade_retrospective_actions_creation_key",
        "ix_trade_retrospective_actions_issue_id",
        "ix_trade_retrospective_actions_status_updated",
    }
    assert expected <= indexes.keys(), f"missing: {expected - indexes.keys()}"

    definitions = {
        name: row.definition.replace('"', "") for name, row in indexes.items()
    }
    assert (
        "(retrospective_id, position, id)"
        in definitions["ix_trade_retrospective_actions_parent_position"]
    )
    assert (
        "(due_kst_date, id)" in definitions["ix_trade_retrospective_actions_due_active"]
    )
    assert (
        "(retrospective_id, creation_key)"
        in definitions["uq_trade_retrospective_actions_creation_key"]
    )
    assert "(issue_id)" in definitions["ix_trade_retrospective_actions_issue_id"]
    assert (
        "(status, updated_at, id)"
        in definitions["ix_trade_retrospective_actions_status_updated"]
    )

    assert indexes["uq_trade_retrospective_actions_creation_key"].indisunique
    due_predicate = indexes["ix_trade_retrospective_actions_due_active"].predicate
    assert "open" in due_predicate and "in_progress" in due_predicate
    assert (
        "creation_key IS NOT NULL"
        in indexes["uq_trade_retrospective_actions_creation_key"].predicate
    )
    assert (
        "issue_id IS NOT NULL"
        in indexes["ix_trade_retrospective_actions_issue_id"].predicate
    )


@pytest.mark.asyncio
async def test_parent_delete_cascades_to_action_rows():
    """Deleting a retrospective removes its shadow children atomically."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990025, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "'[]'::jsonb)"
                )
            )
            await _insert_action_row(
                conn,
                retrospective_id=990025,
                position=0,
            )
            await conn.execute(
                text("DELETE FROM review.trade_retrospectives WHERE id = 990025")
            )
            count = (
                await conn.execute(
                    text(
                        "SELECT count(*) "
                        "FROM review.trade_retrospective_actions "
                        "WHERE retrospective_id = 990025"
                    )
                )
            ).scalar_one()
            assert count == 0
        finally:
            await trans.rollback()


@pytest.mark.parametrize(
    "case",
    [
        {"status": "unknown"},
        {"status_source": "unknown"},
        {"version": 0},
        {"position": -1},
        {"status": "done"},
        {"status": "open", "resolved": True},
        {"status": "obsolete", "resolved": True, "reason": "   "},
        {"status": "expired", "resolved": True, "reason": "expired"},
        {
            "status": "expired",
            "resolved": True,
            "reason": "expired",
            "evidence": "[]",
        },
    ],
    ids=[
        "status",
        "status-source",
        "version",
        "position",
        "done-resolved-at",
        "active-resolved-at",
        "terminal-reason",
        "expired-evidence-null",
        "expired-evidence-non-object",
    ],
)
@pytest.mark.asyncio
async def test_action_check_constraints_reject_invalid_rows(case):
    """Every lifecycle CHECK rejects a representative invalid row."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990026, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "'[]'::jsonb)"
                )
            )
            sp = await conn.begin_nested()
            try:
                with pytest.raises(IntegrityError):
                    await conn.execute(
                        text(
                            "INSERT INTO review.trade_retrospective_actions "
                            "(retrospective_id, position, action, status, version, "
                            " resolved_at, status_actor, status_source, "
                            " status_reason, status_evidence) "
                            "VALUES (990026, :position, 'invalid row', :status, "
                            " :version, CASE WHEN :resolved THEN now() ELSE NULL END, "
                            " 'migration:rob-878', :status_source, :reason, "
                            " CAST(:evidence AS jsonb))"
                        ),
                        {
                            "position": case.get("position", 0),
                            "status": case.get("status", "open"),
                            "version": case.get("version", 1),
                            "resolved": case.get("resolved", False),
                            "status_source": case.get("status_source", "migration"),
                            "reason": case.get("reason"),
                            "evidence": case.get("evidence"),
                        },
                    )
            finally:
                await sp.rollback()
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_creation_key_is_unique_per_parent():
    """Creation retries deduplicate within one parent but not across parents."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES "
                    "(990026, 'TEST', 'equity_kr', 'kis_mock', 'filled', '[]'::jsonb), "
                    "(990027, 'TEST', 'equity_kr', 'kis_mock', 'filled', '[]'::jsonb)"
                )
            )
            key = "00000000-0000-0000-0000-000000087800"
            await _insert_action_row(
                conn,
                retrospective_id=990026,
                position=0,
                creation_key=key,
            )

            sp = await conn.begin_nested()
            try:
                with pytest.raises(IntegrityError):
                    await _insert_action_row(
                        conn,
                        retrospective_id=990026,
                        position=1,
                        creation_key=key,
                    )
            finally:
                await sp.rollback()

            await _insert_action_row(
                conn,
                retrospective_id=990027,
                position=0,
                creation_key=key,
            )

            sp = await conn.begin_nested()
            try:
                await _insert_action_row(
                    conn,
                    retrospective_id=990026,
                    position=0,
                )
                with pytest.raises(IntegrityError):
                    await conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            finally:
                await sp.rollback()

            sp = await conn.begin_nested()
            try:
                await _insert_action_row(
                    conn,
                    retrospective_id=99999999,
                    position=0,
                )
                with pytest.raises(IntegrityError):
                    await conn.execute(text("SET CONSTRAINTS ALL IMMEDIATE"))
            finally:
                await sp.rollback()
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# Preflight edge cases
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_preflight_rejects_non_array_next_actions():
    """A non-array next_actions value fails preflight."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990001, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    '\'{"action": "not an array"}\'::jsonb)'
                )
            )
            with pytest.raises(
                Exception,
                match="retrospective 990001.*not an array",
            ):
                await conn.run_sync(
                    lambda sync_conn: _run_migration_step(
                        sync_conn, _MIGRATION._run_preflight
                    )
                )
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_preflight_rejects_invalid_due_date():
    """A syntactically ISO but impossible due_kst_date fails preflight."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990008, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    '\'[{"action": "bad date", "due_kst_date": "2026-02-31"}]\'::jsonb)'
                )
            )
            with pytest.raises(
                Exception,
                match=r"retrospective 990008 action\[0\].*invalid due_kst_date",
            ):
                await conn.run_sync(
                    lambda sync_conn: _run_migration_step(
                        sync_conn, _MIGRATION._run_preflight
                    )
                )
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_preflight_rejects_unknown_status():
    """An unknown status value fails preflight."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990009, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    '\'[{"action": "bad status", "status": "wip"}]\'::jsonb)'
                )
            )
            with pytest.raises(
                Exception,
                match=r"retrospective 990009 action\[0\].*unknown status",
            ):
                await conn.run_sync(
                    lambda sync_conn: _run_migration_step(
                        sync_conn, _MIGRATION._run_preflight
                    )
                )
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_preflight_rejects_blank_action():
    """A blank action fails preflight."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990010, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    '\'[{"action": "  "}]\'::jsonb)'
                )
            )
            with pytest.raises(
                Exception,
                match=r"retrospective 990010 action\[0\].*blank action",
            ):
                await conn.run_sync(
                    lambda sync_conn: _run_migration_step(
                        sync_conn, _MIGRATION._run_preflight
                    )
                )
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_preflight_rejects_non_object_element():
    """Every malformed element reports its retrospective and zero-based ordinal."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990018, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    '\'[{"action": "valid first"}, 42]\'::jsonb)'
                )
            )
            with pytest.raises(
                Exception,
                match=r"retrospective 990018 action\[1\].*not an object",
            ):
                await conn.run_sync(
                    lambda sync_conn: _run_migration_step(
                        sync_conn, _MIGRATION._run_preflight
                    )
                )
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_preflight_rejects_non_string_action():
    """Action text must not be coerced from another JSON type."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990023, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "'[{\"action\": 42}]'::jsonb)"
                )
            )
            with pytest.raises(
                Exception,
                match=r"retrospective 990023 action\[0\].*must be a string",
            ):
                await conn.run_sync(
                    lambda sync_conn: _run_migration_step(
                        sync_conn, _MIGRATION._run_preflight
                    )
                )
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# Backfill semantics — shared SQL fragments mirror the migration's backfill
# ---------------------------------------------------------------------------
_BACKFILL_COLS = (
    "(id, retrospective_id, position, action, status, version, "
    " status_changed_at, resolved_at, status_actor, status_source, "
    " status_reason, status_evidence, legacy_payload, created_at, updated_at)"
)

_BACKFILL_SELECT = """
    SELECT gen_random_uuid(), t.id, (elem.ordinality - 1)::integer,
     btrim(elem.value->>'action'),
     CASE WHEN btrim(COALESCE(elem.value->>'status','')) = ''
       THEN 'open' ELSE elem.value->>'status' END,
     1,
     t.updated_at,
     CASE WHEN elem.value->>'status' = 'done' THEN t.updated_at ELSE NULL END,
     'migration:rob-878', 'migration', NULL,
     CASE WHEN elem.value->>'status' = 'done' THEN
       jsonb_build_object('schema_version', 1, 'kind', 'legacy_status',
       'source', 'migration', 'reference', 'review.trade_retrospectives.next_actions',
       'observed_at', t.updated_at,
       'summary', 'historical done; exact completion time unavailable')
     ELSE NULL END,
     elem.value,
     t.created_at, t.updated_at
    FROM review.trade_retrospectives t,
     jsonb_array_elements(t.next_actions) WITH ORDINALITY AS elem(value, ordinality)
"""


async def _backfill_for(conn, retro_id: int):
    await conn.execute(
        text(
            f"INSERT INTO review.trade_retrospective_actions "
            f"{_BACKFILL_COLS} "
            f"{_BACKFILL_SELECT} WHERE t.id = {retro_id}"
        )
    )


@pytest.mark.asyncio
async def test_null_like_next_actions_produce_zero_rows():
    """SQL NULL, JSON null, and [] each backfill as zero actions."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES "
                    "(990019, 'TEST', 'equity_kr', 'kis_mock', 'filled', NULL), "
                    "(990020, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    " 'null'::jsonb), "
                    "(990021, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    " '[]'::jsonb)"
                )
            )
            await conn.execute(text("DELETE FROM review.trade_retrospective_actions"))
            await conn.run_sync(
                lambda sync_conn: _run_migration_step(
                    sync_conn, _MIGRATION._run_preflight
                )
            )
            await conn.run_sync(
                lambda sync_conn: _run_migration_step(
                    sync_conn, _MIGRATION._run_backfill
                )
            )
            count = (
                await conn.execute(
                    text(
                        "SELECT count(*) "
                        "FROM review.trade_retrospective_actions "
                        "WHERE retrospective_id BETWEEN 990019 AND 990021"
                    )
                )
            ).scalar_one()
            assert count == 0
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_backfill_maps_fields_ordinals_and_parent_timestamps():
    """A past due date stays active and every mapped field retains its source."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions, "
                    " created_at, updated_at) "
                    "VALUES (990029, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "CAST(:actions AS jsonb), "
                    "'2026-01-02T03:04:05Z', '2026-06-07T08:09:10Z')"
                ),
                {
                    "actions": json.dumps(
                        [
                            {
                                "action": "  past-due active  ",
                                "owner": "alice",
                                "issue_id": "ROB-MAP",
                                "due_kst_date": "2020-01-01",
                            },
                            {"action": "blank defaults open", "status": "   "},
                            {"action": "still running", "status": "in_progress"},
                            {
                                "action": "finished",
                                "status": "done",
                                "unknown_key": {"preserved": True},
                            },
                        ]
                    )
                },
            )
            await conn.execute(text("DELETE FROM review.trade_retrospective_actions"))
            await conn.run_sync(
                lambda sync_conn: _run_migration_step(
                    sync_conn, _MIGRATION._run_preflight
                )
            )
            await conn.run_sync(
                lambda sync_conn: _run_migration_step(
                    sync_conn, _MIGRATION._run_backfill
                )
            )
            await conn.run_sync(
                lambda sync_conn: _run_migration_step(
                    sync_conn, _MIGRATION._assert_parity
                )
            )

            result = await conn.execute(
                text(
                    "SELECT a.position, a.action, a.owner, a.issue_id, a.status, "
                    "a.due_kst_date::text AS due_kst_date, "
                    "a.creation_key, a.version, a.resolved_at, "
                    "a.status_actor, a.status_source, a.status_reason, "
                    "a.status_evidence, a.legacy_payload, "
                    "a.status_changed_at = t.updated_at AS status_time_matches, "
                    "a.resolved_at = t.updated_at AS resolved_time_matches, "
                    "a.created_at = t.created_at AS created_time_matches, "
                    "a.updated_at = t.updated_at AS updated_time_matches "
                    "FROM review.trade_retrospective_actions a "
                    "JOIN review.trade_retrospectives t "
                    "ON t.id = a.retrospective_id "
                    "WHERE a.retrospective_id = 990029 "
                    "ORDER BY a.position"
                )
            )
            rows = result.fetchall()
            assert [row.position for row in rows] == [0, 1, 2, 3]
            assert rows[0].action == "past-due active"
            assert rows[0].owner == "alice"
            assert rows[0].issue_id == "ROB-MAP"
            assert rows[0].status == "open"
            assert rows[0].due_kst_date == "2020-01-01"
            assert rows[1].status == "open"
            assert rows[1].due_kst_date is None
            assert rows[2].status == "in_progress"
            assert rows[3].status == "done"
            assert all(row.resolved_at is None for row in rows[:3])
            assert all(row.status_evidence is None for row in rows[:3])
            assert rows[3].resolved_time_matches
            assert rows[3].status_evidence["kind"] == "legacy_status"
            assert rows[3].status_evidence["source"] == "migration"
            assert (
                rows[3].status_evidence["reference"]
                == "review.trade_retrospectives.next_actions"
            )
            assert (
                "exact completion time unavailable"
                in rows[3].status_evidence["summary"]
            )
            assert rows[3].status_evidence["observed_at"]
            assert rows[3].legacy_payload["unknown_key"] == {"preserved": True}
            assert all(row.status != "expired" for row in rows)
            assert all(row.creation_key is None for row in rows)
            assert all(row.version == 1 for row in rows)
            assert all(row.status_actor == "migration:rob-878" for row in rows)
            assert all(row.status_source == "migration" for row in rows)
            assert all(row.status_reason is None for row in rows)
            assert all(row.status_time_matches for row in rows)
            assert all(row.created_time_matches for row in rows)
            assert all(row.updated_time_matches for row in rows)
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_missing_status_backfills_to_open():
    """An element with no status key gets status='open' in backfill."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990002, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    '\'[{"action": "no status action"}]\'::jsonb)'
                )
            )
            await _backfill_for(conn, 990002)
            result = await conn.execute(
                text(
                    "SELECT status FROM review.trade_retrospective_actions "
                    "WHERE retrospective_id = 990002"
                )
            )
            row = result.fetchone()
            assert row is not None
            assert row.status == "open"
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_blank_status_backfills_to_open():
    """An element with empty-string status gets status='open' in backfill."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990012, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    '\'[{"action": "blank status", "status": ""}]\'::jsonb)'
                )
            )
            await _backfill_for(conn, 990012)
            result = await conn.execute(
                text(
                    "SELECT status FROM review.trade_retrospective_actions "
                    "WHERE retrospective_id = 990012"
                )
            )
            row = result.fetchone()
            assert row is not None
            assert row.status == "open"
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_existing_status_preserved():
    """Existing open/in_progress/done statuses are preserved."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990013, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    '\'[{"action": "a1", "status": "open"},'
                    ' {"action": "a2", "status": "in_progress"},'
                    ' {"action": "a3", "status": "done"}]\'::jsonb)'
                )
            )
            await _backfill_for(conn, 990013)
            result = await conn.execute(
                text(
                    "SELECT status FROM review.trade_retrospective_actions "
                    "WHERE retrospective_id = 990013 ORDER BY position"
                )
            )
            statuses = [row.status for row in result]
            assert statuses == ["open", "in_progress", "done"]
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_legacy_payload_preserves_unknown_keys():
    """The entire original JSONB element is preserved in legacy_payload."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            original = json.dumps(
                [
                    {
                        "action": "do thing",
                        "owner": "alice",
                        "custom_key": "custom_value",
                        "another": 42,
                    }
                ]
            )
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990003, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "CAST(:actions AS jsonb))"
                ),
                {"actions": original},
            )
            await _backfill_for(conn, 990003)
            result = await conn.execute(
                text(
                    "SELECT legacy_payload FROM review.trade_retrospective_actions "
                    "WHERE retrospective_id = 990003"
                )
            )
            payload = result.fetchone().legacy_payload
            assert payload.get("custom_key") == "custom_value"
            assert payload.get("another") == 42
            assert payload.get("action") == "do thing"
            assert payload.get("owner") == "alice"
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_backfill_provenance_actor_and_source():
    """Backfilled rows have actor=migration:rob-878 and source=migration."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990014, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    '\'[{"action": "provenance check"}]\'::jsonb)'
                )
            )
            await _backfill_for(conn, 990014)
            result = await conn.execute(
                text(
                    "SELECT status_actor, status_source, version "
                    "FROM review.trade_retrospective_actions "
                    "WHERE retrospective_id = 990014"
                )
            )
            row = result.fetchone()
            assert row.status_actor == "migration:rob-878"
            assert row.status_source == "migration"
            assert row.version == 1
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_done_status_gets_resolved_at_and_evidence():
    """Historical done rows get approximate resolved_at and evidence envelope."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions, "
                    " created_at, updated_at) "
                    "VALUES (990015, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    '\'[{"action": "done thing", "status": "done"}]\'::jsonb, '
                    " '2026-01-01T00:00:00Z', '2026-06-01T00:00:00Z')"
                )
            )
            await _backfill_for(conn, 990015)
            result = await conn.execute(
                text(
                    "SELECT resolved_at, status_evidence FROM review.trade_retrospective_actions "
                    "WHERE retrospective_id = 990015"
                )
            )
            row = result.fetchone()
            assert row.resolved_at is not None
            evidence = row.status_evidence
            assert evidence["schema_version"] == 1
            assert evidence["kind"] == "legacy_status"
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_parity_rejects_backfilled_field_mismatch():
    """Parity checks field values and ordinal, not only aggregate row counts."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990016, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    '\'[{"action": "parity source", "owner": "alice"}]\'::jsonb)'
                )
            )
            await conn.execute(text("DELETE FROM review.trade_retrospective_actions"))
            await conn.run_sync(
                lambda sync_conn: _run_migration_step(
                    sync_conn, _MIGRATION._run_backfill
                )
            )
            await conn.execute(
                text(
                    "UPDATE review.trade_retrospective_actions "
                    "SET action = 'corrupted shadow row' "
                    "WHERE retrospective_id = 990016"
                )
            )

            with pytest.raises(
                Exception,
                match=r"ROB-878 parity mismatch.*retrospective 990016 action\[0\]",
            ):
                await conn.run_sync(
                    lambda sync_conn: _run_migration_step(
                        sync_conn, _MIGRATION._assert_parity
                    )
                )
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# Write-fence behavior
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_shadow_mode_permits_parent_json_write(db_session):
    """In shadow mode, direct writes to next_actions are permitted."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990004, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "'[]'::jsonb)"
                )
            )
            await conn.execute(
                text(
                    "UPDATE review.trade_retrospectives SET next_actions = "
                    '\'[{"action": "new"}]\'::jsonb WHERE id = 990004'
                )
            )
            result = await conn.execute(
                text(
                    "SELECT next_actions FROM review.trade_retrospectives WHERE id = 990004"
                )
            )
            assert result.fetchone() is not None
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_canonical_mode_rejects_parent_json_write():
    """In canonical mode, direct writes to next_actions without the GUC marker fail."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990005, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "'[]'::jsonb)"
                )
            )
            await conn.execute(
                text(
                    "UPDATE review.trade_retrospective_action_control "
                    "SET mode = 'canonical' WHERE id = 1"
                )
            )
            sp = await conn.begin_nested()
            with pytest.raises(Exception, match="canonical mode.*rejected"):
                await conn.execute(
                    text(
                        "UPDATE review.trade_retrospectives SET next_actions = "
                        '\'[{"action": "blocked"}]\'::jsonb WHERE id = 990005'
                    )
                )
            await sp.rollback()
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_canonical_mode_permits_write_with_guc_marker():
    """In canonical mode, writes with the projection-writer GUC marker succeed."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990006, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "'[]'::jsonb)"
                )
            )
            await conn.execute(
                text(
                    "UPDATE review.trade_retrospective_action_control "
                    "SET mode = 'canonical' WHERE id = 1"
                )
            )
            await conn.execute(
                text("SET LOCAL app.retrospective_action_projection_writer = 'v1'")
            )
            await conn.execute(
                text(
                    "UPDATE review.trade_retrospectives SET next_actions = "
                    '\'[{"action": "allowed"}]\'::jsonb WHERE id = 990006'
                )
            )
        finally:
            await conn.execute(
                text(
                    "UPDATE review.trade_retrospective_action_control "
                    "SET mode = 'shadow' WHERE id = 1"
                )
            )
            await trans.rollback()


@pytest.mark.asyncio
async def test_canonical_mode_rejects_parent_insert_with_actions():
    """Old writers cannot create a populated parent after canonical cutover."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "UPDATE review.trade_retrospective_action_control "
                    "SET mode = 'canonical' WHERE id = 1"
                )
            )
            sp = await conn.begin_nested()
            try:
                with pytest.raises(
                    Exception,
                    match="canonical mode.*insert rejected",
                ):
                    await conn.execute(
                        text(
                            "INSERT INTO review.trade_retrospectives "
                            "(id, symbol, instrument_type, account_mode, outcome, "
                            " next_actions) "
                            "VALUES (990030, 'TEST', 'equity_kr', 'kis_mock', "
                            "'filled', '[{\"action\": \"blocked\"}]'::jsonb)"
                        )
                    )
            finally:
                await sp.rollback()
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_canonical_mode_permits_update_when_actions_unchanged():
    """The fence blocks projection drift, not unrelated parent maintenance."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990031, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    '\'[{"action": "unchanged"}]\'::jsonb)'
                )
            )
            await conn.execute(
                text(
                    "UPDATE review.trade_retrospective_action_control "
                    "SET mode = 'canonical' WHERE id = 1"
                )
            )
            await conn.execute(
                text(
                    "UPDATE review.trade_retrospectives "
                    "SET symbol = 'TEST2' WHERE id = 990031"
                )
            )
            symbol = (
                await conn.execute(
                    text(
                        "SELECT symbol FROM review.trade_retrospectives "
                        "WHERE id = 990031"
                    )
                )
            ).scalar_one()
            assert symbol == "TEST2"
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_canonical_mode_rejects_wrong_projection_marker():
    """Only the exact v1 compatibility marker can bypass the fence."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990032, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "'[]'::jsonb)"
                )
            )
            await conn.execute(
                text(
                    "UPDATE review.trade_retrospective_action_control "
                    "SET mode = 'canonical' WHERE id = 1"
                )
            )
            sp = await conn.begin_nested()
            try:
                await conn.execute(
                    text("SET LOCAL app.retrospective_action_projection_writer = 'v0'")
                )
                with pytest.raises(
                    Exception,
                    match="canonical mode.*update rejected",
                ):
                    await conn.execute(
                        text(
                            "UPDATE review.trade_retrospectives SET next_actions = "
                            '\'[{"action": "blocked"}]\'::jsonb '
                            "WHERE id = 990032"
                        )
                    )
            finally:
                await sp.rollback()
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_missing_control_row_fails_parent_json_write_closed():
    """Absent database authority must never silently behave as shadow mode."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990017, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "'[]'::jsonb)"
                )
            )
            await conn.execute(
                text(
                    "DELETE FROM review.trade_retrospective_action_control WHERE id = 1"
                )
            )

            sp = await conn.begin_nested()
            try:
                with pytest.raises(
                    Exception,
                    match="control row is missing.*fail closed",
                ):
                    await conn.execute(
                        text(
                            "UPDATE review.trade_retrospectives SET next_actions = "
                            '\'[{"action": "must be blocked"}]\'::jsonb '
                            "WHERE id = 990017"
                        )
                    )
            finally:
                await sp.rollback()
        finally:
            await trans.rollback()


# ---------------------------------------------------------------------------
# Parent JSONB immutability
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_parent_json_immutable_after_backfill():
    """Parent next_actions JSONB is byte-for-byte unchanged after backfill."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            original = json.dumps([{"action": "check thing", "status": "open"}])
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990007, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "CAST(:actions AS jsonb))"
                ),
                {"actions": original},
            )
            before = (
                await conn.execute(
                    text(
                        "SELECT next_actions FROM review.trade_retrospectives WHERE id = 990007"
                    )
                )
            ).scalar_one()
            await _backfill_for(conn, 990007)
            after = (
                await conn.execute(
                    text(
                        "SELECT next_actions FROM review.trade_retrospectives WHERE id = 990007"
                    )
                )
            ).scalar_one()
            assert before == after
        finally:
            await trans.rollback()
