"""ROB-878 child-1: migration + backfill contract tests.

Tests verify the migration module metadata, DB constraints, indexes, and
edge-case behavior (NULL/JSON-null/empty, malformed data, missing status,
unknown-key preservation, write-fence shadow/canonical, parent immutability).

Edge-case tests use rolled-back transactions against the test DB engine
and re-run the relevant SQL patterns from the migration in a controlled scope.
"""

import ast
import json
from pathlib import Path

import pytest
from sqlalchemy import text

from app.core.db import engine

_MIGRATION_PATH = (
    Path(__file__).resolve().parent.parent
    / "alembic"
    / "versions"
    / "20260714_rob878_trade_retrospective_actions_shadow.py"
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
    assert assignments.get("down_revision") == "20260713_rob848_paper_validation"


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
    """All design-specified indexes exist."""
    result = await db_session.execute(
        text(
            "SELECT indexname FROM pg_indexes "
            "WHERE schemaname = 'review' "
            "AND tablename = 'trade_retrospective_actions'"
        )
    )
    names = {row.indexname for row in result}
    expected = {
        "ix_trade_retrospective_actions_parent_position",
        "ix_trade_retrospective_actions_due_active",
        "uq_trade_retrospective_actions_creation_key",
        "ix_trade_retrospective_actions_issue_id",
        "ix_trade_retrospective_actions_status_updated",
    }
    assert expected <= names, f"missing: {expected - names}"


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
                    "'{\"action\": \"not an array\"}'::jsonb)"
                )
            )
            with pytest.raises(Exception, match="not an array|preflight"):
                await conn.execute(
                    text(
                        "DO $$ DECLARE r RECORD; BEGIN "
                        "FOR r IN SELECT id, next_actions FROM review.trade_retrospectives "
                        "WHERE id = 990001 LOOP "
                        "IF jsonb_typeof(r.next_actions) NOT IN ('array', 'null') THEN "
                        "RAISE EXCEPTION 'retrospective %: not an array', r.id; "
                        "END IF; END LOOP; END; $$"
                    )
                )
        finally:
            await trans.rollback()


@pytest.mark.asyncio
async def test_preflight_rejects_invalid_due_date():
    """A non-ISO due_kst_date fails preflight."""
    async with engine.connect() as conn:
        trans = await conn.begin()
        try:
            await conn.execute(
                text(
                    "INSERT INTO review.trade_retrospectives "
                    "(id, symbol, instrument_type, account_mode, outcome, next_actions) "
                    "VALUES (990008, 'TEST', 'equity_kr', 'kis_mock', 'filled', "
                    "'[{\"action\": \"bad date\", \"due_kst_date\": \"2026/07/14\"}]'::jsonb)"
                )
            )
            with pytest.raises(Exception, match="invalid due_kst_date|preflight"):
                await conn.execute(
                    text(
                        "DO $$ DECLARE r RECORD; elem JSONB; raw_due TEXT; "
                        "BEGIN "
                        "FOR r IN SELECT id, next_actions FROM review.trade_retrospectives "
                        "WHERE id = 990008 LOOP "
                        "FOR elem IN SELECT * FROM jsonb_array_elements(r.next_actions) LOOP "
                        "raw_due := elem->>'due_kst_date'; "
                        "IF raw_due IS NOT NULL AND btrim(raw_due) <> '' "
                        "AND raw_due !~ '^\\d{4}-\\d{2}-\\d{2}$' THEN "
                        "RAISE EXCEPTION 'invalid due_kst_date: %', raw_due; "
                        "END IF; END LOOP; END LOOP; END; $$"
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
                    "'[{\"action\": \"bad status\", \"status\": \"wip\"}]'::jsonb)"
                )
            )
            with pytest.raises(Exception, match="unknown status|preflight"):
                await conn.execute(
                    text(
                        "DO $$ DECLARE r RECORD; elem JSONB; raw_status TEXT; "
                        "BEGIN "
                        "FOR r IN SELECT id, next_actions FROM review.trade_retrospectives "
                        "WHERE id = 990009 LOOP "
                        "FOR elem IN SELECT * FROM jsonb_array_elements(r.next_actions) LOOP "
                        "raw_status := elem->>'status'; "
                        "IF raw_status IS NOT NULL AND btrim(raw_status) <> '' "
                        "AND raw_status NOT IN ('open','in_progress','done') THEN "
                        "RAISE EXCEPTION 'unknown status: %', raw_status; "
                        "END IF; END LOOP; END LOOP; END; $$"
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
                    "'[{\"action\": \"  \"}]'::jsonb)"
                )
            )
            with pytest.raises(Exception, match="blank action|preflight"):
                await conn.execute(
                    text(
                        "DO $$ DECLARE r RECORD; elem JSONB; "
                        "BEGIN "
                        "FOR r IN SELECT id, next_actions FROM review.trade_retrospectives "
                        "WHERE id = 990010 LOOP "
                        "FOR elem IN SELECT * FROM jsonb_array_elements(r.next_actions) LOOP "
                        "IF btrim(COALESCE(elem->>'action', '')) = '' THEN "
                        "RAISE EXCEPTION 'blank action'; "
                        "END IF; END LOOP; END LOOP; END; $$"
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
                    "'[{\"action\": \"no status action\"}]'::jsonb)"
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
                    "'[{\"action\": \"blank status\", \"status\": \"\"}]'::jsonb)"
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
                    "'[{\"action\": \"a1\", \"status\": \"open\"},"
                    " {\"action\": \"a2\", \"status\": \"in_progress\"},"
                    " {\"action\": \"a3\", \"status\": \"done\"}]'::jsonb)"
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
                    "'[{\"action\": \"provenance check\"}]'::jsonb)"
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
                    "'[{\"action\": \"done thing\", \"status\": \"done\"}]'::jsonb, "
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
                    "'[{\"action\": \"new\"}]'::jsonb WHERE id = 990004"
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
                        "'[{\"action\": \"blocked\"}]'::jsonb WHERE id = 990005"
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
                text(
                    "SET LOCAL app.retrospective_action_projection_writer = 'v1'"
                )
            )
            await conn.execute(
                text(
                    "UPDATE review.trade_retrospectives SET next_actions = "
                    "'[{\"action\": \"allowed\"}]'::jsonb WHERE id = 990006"
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
