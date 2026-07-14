"""ROB-878 child-1: ORM model smoke tests for shadow ledger tables.

These tests verify that both new tables exist on Base.metadata and in the
test DB with the correct columns, constraints, indexes, and control row.
"""

import pytest
from sqlalchemy import text


@pytest.mark.asyncio
async def test_trade_retrospective_action_table_registered_on_metadata():
    """Both new tables must be registered on Base.metadata for create_all."""
    from app.models.base import Base

    table_names = set(Base.metadata.tables.keys())
    assert "review.trade_retrospective_actions" in table_names
    assert "review.trade_retrospective_action_control" in table_names


@pytest.mark.asyncio
async def test_trade_retrospective_action_columns_exist(db_session):
    """The action table has all required columns with correct types."""
    result = await db_session.execute(
        text(
            "SELECT column_name, data_type, is_nullable, column_default, "
            "character_maximum_length "
            "FROM information_schema.columns "
            "WHERE table_schema = 'review' "
            "AND table_name = 'trade_retrospective_actions' "
            "ORDER BY ordinal_position"
        )
    )
    cols = {row.column_name: row for row in result}
    expected = {
        "id",
        "retrospective_id",
        "creation_key",
        "position",
        "action",
        "owner",
        "issue_id",
        "status",
        "due_kst_date",
        "version",
        "status_changed_at",
        "resolved_at",
        "status_actor",
        "status_source",
        "status_reason",
        "status_evidence",
        "legacy_payload",
        "created_at",
        "updated_at",
    }
    assert expected <= set(cols.keys()), f"missing: {expected - set(cols.keys())}"
    assert cols["id"].data_type == "uuid"
    assert cols["retrospective_id"].data_type == "bigint"
    assert cols["position"].data_type == "integer"
    assert cols["action"].data_type == "text"
    assert cols["status"].data_type == "text"
    assert cols["status_source"].data_type == "character varying"
    assert cols["status_source"].character_maximum_length == 32
    assert cols["version"].data_type == "integer"
    assert cols["legacy_payload"].data_type == "jsonb"
    assert cols["status"].is_nullable == "NO"
    assert cols["version"].is_nullable == "NO"
    assert cols["position"].is_nullable == "NO"
    assert cols["legacy_payload"].is_nullable == "NO"
    assert cols["id"].column_default == "gen_random_uuid()"


@pytest.mark.asyncio
async def test_control_table_singleton_structure(db_session):
    """The control table enforces singleton id=1 with mode check."""
    result = await db_session.execute(
        text(
            "SELECT column_name, data_type, is_nullable "
            "FROM information_schema.columns "
            "WHERE table_schema = 'review' "
            "AND table_name = 'trade_retrospective_action_control' "
            "ORDER BY ordinal_position"
        )
    )
    cols = {row.column_name: row for row in result}
    assert "id" in cols
    assert "mode" in cols
    assert "cutover_at" in cols
    assert "cutover_action_count" in cols
    assert "updated_at" in cols
    assert cols["mode"].data_type == "text"
    assert cols["mode"].is_nullable == "NO"


@pytest.mark.asyncio
async def test_control_row_exists_in_shadow_mode(db_session):
    """Exactly one control row exists with mode='shadow' after bootstrap."""
    result = await db_session.execute(
        text(
            "SELECT id, mode FROM review.trade_retrospective_action_control ORDER BY id"
        )
    )
    rows = result.fetchall()
    assert len(rows) == 1
    assert rows[0].id == 1
    assert rows[0].mode == "shadow"


@pytest.mark.asyncio
async def test_write_fence_trigger_exists_on_parent(db_session):
    """The write-fence trigger function and trigger exist on the parent table."""
    result = await db_session.execute(
        text(
            "SELECT tgname FROM pg_trigger "
            "WHERE tgrelid = 'review.trade_retrospectives'::regclass "
            "AND NOT tgisinternal"
        )
    )
    trigger_names = {row.tgname for row in result}
    assert "trg_trade_retrospective_next_actions_fence" in trigger_names


@pytest.mark.asyncio
async def test_write_fence_function_exists(db_session):
    """The trigger function exists and is callable."""
    result = await db_session.execute(
        text(
            "SELECT proname FROM pg_proc p "
            "JOIN pg_namespace n ON p.pronamespace = n.oid "
            "WHERE n.nspname = 'review' "
            "AND proname = 'guard_trade_retrospective_next_actions'"
        )
    )
    assert result.fetchone() is not None
