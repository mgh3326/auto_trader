"""A5: migration up/down on a throwaway scope + append-only enforcement.

Two layers, matching how this repo tests migrations:

1. Offline ``--sql`` render (unit): the exact DDL reaching Postgres for
   both upgrade and downgrade.
2. A real execution inside the run-owned test database, in a rolled-back
   transaction: the existing tables are dropped, ``upgrade()`` and
   ``downgrade()`` run for real, catalog assertions confirm each, and the
   rollback restores the bootstrap state — a throwaway scope that leaves
   no trace.
3. Live append-only checks (integration): UPDATE/DELETE/TRUNCATE on the
   bootstrapped tables must be rejected by the DB triggers.
"""

from __future__ import annotations

import importlib.util
import io
import logging
import pathlib
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory
from sqlalchemy import delete, text
from sqlalchemy.exc import DBAPIError

from alembic import command
from app.models.quotes_consumer import LadderTouchEvent, QuotesTriggerFiring


@pytest.fixture(autouse=True)
def _contain_alembic_logging_config():
    """Undo ``fileConfig``'s global logger sabotage (same reason as
    tests/services/watch_trigger_repricing/test_migration_render.py)."""
    loggers = logging.Logger.manager.loggerDict
    before = {
        name: (obj.disabled, obj.level)
        for name, obj in loggers.items()
        if isinstance(obj, logging.Logger)
    }
    root = logging.getLogger()
    root_level, root_handlers = root.level, list(root.handlers)
    try:
        yield
    finally:
        for name, obj in loggers.items():
            if not isinstance(obj, logging.Logger):
                continue
            if name in before:
                obj.disabled, obj.level = before[name]
            else:
                obj.disabled = False
        root.setLevel(root_level)
        root.handlers[:] = root_handlers


REPO_ROOT = pathlib.Path(__file__).resolve().parents[3]
MIGRATION_PATH = (
    REPO_ROOT / "alembic" / "versions" / "20260930_rob1120_quotes_consumer_records.py"
)
REVISION = "20260930_rob1120_quotes"
PARENT = "20260928_task847_h5_state"

spec = importlib.util.spec_from_file_location(
    "rob1120_quotes_consumer_migration", MIGRATION_PATH
)
assert spec is not None and spec.loader is not None
migration = importlib.util.module_from_spec(spec)
spec.loader.exec_module(migration)


def _config(buffer: io.StringIO | None = None) -> Config:
    config = Config(str(REPO_ROOT / "alembic.ini"), output_buffer=buffer)
    config.set_main_option("script_location", str(REPO_ROOT / "alembic"))
    return config


def _render(*, upgrade: bool) -> str:
    buffer = io.StringIO()
    config = _config(buffer)
    if upgrade:
        command.upgrade(config, f"{PARENT}:{REVISION}", sql=True)
    else:
        command.downgrade(config, f"{REVISION}:{PARENT}", sql=True)
    return buffer.getvalue()


# ---------------------------------------------------------------------------
# Offline render — the exact statements that reach Postgres
# ---------------------------------------------------------------------------
@pytest.mark.unit
def test_the_chain_still_has_exactly_one_head() -> None:
    script = ScriptDirectory.from_config(_config())
    heads = list(script.get_heads())
    assert len(heads) == 1, heads
    ancestry = {
        revision.revision for revision in script.iterate_revisions(heads[0], "base")
    }
    assert REVISION in ancestry
    assert PARENT in ancestry


@pytest.mark.unit
def test_upgrade_renders_both_tables_triggers_and_grant() -> None:
    sql = _render(upgrade=True)

    assert "CREATE TABLE review.quotes_trigger_firings" in sql
    assert "CREATE TABLE review.ladder_touch_events" in sql
    assert "CREATE OR REPLACE FUNCTION review.reject_quotes_consumer_mutation()" in sql
    for trigger in (
        "trg_quotes_trigger_firings_append_only",
        "trg_quotes_trigger_firings_truncate",
        "trg_ladder_touch_events_append_only",
        "trg_ladder_touch_events_truncate",
    ):
        assert f"CREATE TRIGGER {trigger}" in sql
    for index in (
        "ix_quotes_trigger_firings_type_ts",
        "ix_quotes_trigger_firings_symbol_ts",
        "ix_quotes_trigger_firings_kst_date",
        "ix_ladder_touch_events_rung",
        "ix_ladder_touch_events_symbol_ts",
        "ix_ladder_touch_events_type_ts",
    ):
        assert f"CREATE INDEX {index}" in sql
    # Conditional at_app GRANT (Stage-4 role may not exist in dev/CI).
    assert "pg_roles WHERE rolname = 'at_app'" in sql
    assert "GRANT SELECT, INSERT" in sql
    # Dedupe uniqueness is the redelivery floor.
    assert sql.count("UNIQUE (dedupe_key)") == 2


@pytest.mark.unit
def test_downgrade_renders_full_teardown() -> None:
    sql = _render(upgrade=False)

    assert "DROP TRIGGER" in sql
    for trigger in (
        "trg_quotes_trigger_firings_append_only",
        "trg_quotes_trigger_firings_truncate",
        "trg_ladder_touch_events_append_only",
        "trg_ladder_touch_events_truncate",
    ):
        assert trigger in sql
    assert "DROP FUNCTION IF EXISTS review.reject_quotes_consumer_mutation()" in sql
    assert "DROP TABLE review.ladder_touch_events" in sql
    assert "DROP TABLE review.quotes_trigger_firings" in sql


# ---------------------------------------------------------------------------
# Real up/down inside a rolled-back transaction (throwaway scope)
# ---------------------------------------------------------------------------
@pytest.mark.integration
@pytest.mark.asyncio
async def test_upgrade_downgrade_roundtrip_in_transaction(db_session) -> None:
    """Execute the real migration against the test DB, then roll back.

    The bootstrap already created these tables, so the exercise is:
    drop → upgrade → catalog asserts → downgrade → catalog asserts →
    rollback (restoring bootstrap state exactly).
    """
    from alembic.migration import MigrationContext
    from alembic.operations import Operations

    def _exercise(sync_session) -> None:
        conn = sync_session.connection()
        # Clear the bootstrap copies so the migration rebuilds them.
        conn.execute(
            text(
                "DROP TABLE IF EXISTS review.ladder_touch_events, "
                "review.quotes_trigger_firings CASCADE"
            )
        )
        ctx = MigrationContext.configure(conn)
        with Operations.context(ctx):
            migration.upgrade()
        tables = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'review' "
                    "AND tablename IN ('quotes_trigger_firings',"
                    " 'ladder_touch_events')"
                )
            )
        }
        assert tables == {"quotes_trigger_firings", "ladder_touch_events"}
        triggers = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT tgname FROM pg_trigger WHERE tgname LIKE "
                    "'trg_%quotes%' OR tgname LIKE 'trg_%ladder%'"
                )
            )
        }
        assert triggers == {
            "trg_quotes_trigger_firings_append_only",
            "trg_quotes_trigger_firings_truncate",
            "trg_ladder_touch_events_append_only",
            "trg_ladder_touch_events_truncate",
        }
        fn = conn.execute(
            text(
                "SELECT COUNT(*) FROM pg_proc p JOIN pg_namespace n ON "
                "n.oid = p.pronamespace WHERE n.nspname = 'review' AND "
                "p.proname = 'reject_quotes_consumer_mutation'"
            )
        ).scalar()
        assert fn == 1

        with Operations.context(ctx):
            migration.downgrade()
        left = {
            r[0]
            for r in conn.execute(
                text(
                    "SELECT tablename FROM pg_tables WHERE schemaname = 'review' "
                    "AND tablename IN ('quotes_trigger_firings',"
                    " 'ladder_touch_events')"
                )
            )
        }
        assert left == set()
        fn = conn.execute(
            text(
                "SELECT COUNT(*) FROM pg_proc p JOIN pg_namespace n ON "
                "n.oid = p.pronamespace WHERE n.nspname = 'review' AND "
                "p.proname = 'reject_quotes_consumer_mutation'"
            )
        ).scalar()
        assert fn == 0

    await db_session.run_sync(_exercise)
    # Everything above — drops included — is undone; bootstrap state remains.
    await db_session.rollback()


# ---------------------------------------------------------------------------
# Append-only enforcement on the bootstrapped tables
# ---------------------------------------------------------------------------
def _firing() -> QuotesTriggerFiring:
    return QuotesTriggerFiring(
        dedupe_key="append-only-probe-1",
        trigger_type="holding_spike",
        outcome="fired",
        symbol="ZZAPPEND",
        source_symbol=None,
        market="kr",
        session="krx_regular",
        reference_price=None,
        current_price=None,
        window="day",
        event_ts=datetime(2026, 9, 30, tzinfo=UTC),
        kst_date="2026-09-30",
        would_kick=False,
        suppress_reason=None,
        daily_would_kick_count=0,
        last_would_kick_at=None,
        not_evaluable_reason=None,
        source_ref=None,
        detail={},
    )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_firings_table_rejects_update_delete_truncate(db_session) -> None:
    row = _firing()
    db_session.add(row)
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text(
                "UPDATE review.quotes_trigger_firings SET symbol = 'X' "
                "WHERE dedupe_key = 'append-only-probe-1'"
            )
        )
    await db_session.rollback()
    with pytest.raises(DBAPIError):
        await db_session.execute(
            delete(QuotesTriggerFiring).where(
                QuotesTriggerFiring.dedupe_key == "append-only-probe-1"
            )
        )
    await db_session.rollback()
    with pytest.raises(DBAPIError):
        await db_session.execute(text("TRUNCATE review.quotes_trigger_firings"))
    await db_session.rollback()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_ladder_table_rejects_update_delete_truncate(db_session) -> None:
    row = LadderTouchEvent(
        dedupe_key="append-only-probe-2",
        order_ledger="toss_live_order_ledger",
        order_ledger_id=1,
        event_type="approach",
        market="kr",
        symbol="ZZAPPEND",
        side="buy",
        session="krx_regular",
        anchor_price=Decimal("1"),
        event_price=Decimal("1"),
        event_ts=datetime(2026, 9, 30, tzinfo=UTC),
        detail={},
    )
    db_session.add(row)
    await db_session.commit()

    with pytest.raises(DBAPIError):
        await db_session.execute(
            text(
                "UPDATE review.ladder_touch_events SET symbol = 'X' "
                "WHERE dedupe_key = 'append-only-probe-2'"
            )
        )
    await db_session.rollback()
    with pytest.raises(DBAPIError):
        await db_session.execute(
            delete(LadderTouchEvent).where(
                LadderTouchEvent.dedupe_key == "append-only-probe-2"
            )
        )
    await db_session.rollback()
    with pytest.raises(DBAPIError):
        await db_session.execute(text("TRUNCATE review.ladder_touch_events"))
    await db_session.rollback()


@pytest.mark.unit
def test_migration_revision_ids_match_file() -> None:
    assert migration.revision == REVISION
    assert migration.down_revision == PARENT
