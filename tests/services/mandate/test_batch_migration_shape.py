"""Migration 20261120_hk5648_mandate_batches: offline render and parity, no database.

The triggers' behaviour is tested in ``test_batches_db.py`` (CI). Here the migration
is rendered as SQL and compared with the ORM, the Python twins and the design.
"""

from __future__ import annotations

import importlib.util
import io
import re
from pathlib import Path
from typing import Any

import pytest
from alembic.migration import MigrationContext
from alembic.operations import Operations

import app.models  # noqa: F401  (register ORM tables)
from app.models.base import Base
from app.models.mandate import MandatePlanBatch, MandatePlanBatchMember
from app.services.mandate import locks
from app.services.mandate import vocabulary as v

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "alembic/versions/20261120_hk5648_mandate_batches.py"
TABLES = ("mandate_plan_batches", "mandate_plan_batch_members")


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("hk5648_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(fn: str) -> str:
    module = _load()
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={
            "as_sql": True,
            "output_buffer": buffer,
            "target_metadata": Base.metadata,
        },
    )
    with Operations.context(context):
        getattr(module, fn)()
    return buffer.getvalue()


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def _ddl() -> str:
    return _norm("\n".join(_load().GUARD_DDL))


def test_revision_chain_is_a_single_step_after_the_sleeve_kiwoom_migration() -> None:
    m = _load()
    assert m.revision == "20261120_hk5648_mandate_batches"
    assert m.down_revision == "20261120_hk5637_sleeve_kiwoom"
    assert len(m.revision) <= 32


def test_upgrade_is_additive_only() -> None:
    sql = _render("upgrade").upper()
    assert "DROP TABLE" not in sql and "DROP COLUMN" not in sql
    assert "ALTER TABLE REVIEW.MANDATE_PLAN_EVENTS" not in sql
    assert sql.count("CREATE TABLE") == 2


def test_i13_both_tables_refuse_update_delete_and_truncate() -> None:
    ddl = _ddl()
    for table in TABLES:
        assert (
            f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON review.{table}"
            in ddl
        )
        assert (
            f"CREATE TRIGGER trg_{table}_truncate BEFORE TRUNCATE ON review.{table}"
            in ddl
        )


def test_hard_rule_18_app_role_gets_select_and_insert_only() -> None:
    sql = _norm(_render("upgrade"))
    grant = sql[sql.index("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles") :]
    for table in TABLES:
        assert f"GRANT SELECT, INSERT ON review.{table} TO at_app" in grant
        assert (
            f"GRANT USAGE, SELECT ON SEQUENCE review.{table}_id_seq TO at_app" in grant
        )
    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app')" in grant
    assert "GRANT UPDATE" not in grant and "GRANT DELETE" not in grant
    assert "UPDATE ON review" not in grant and "DELETE ON review" not in grant


def test_the_events_trigger_has_no_function_call_in_its_when_clause() -> None:
    """#789 Stage 4: an event without a card id must not run trigger code."""
    ddl = _ddl()
    assert (
        "CREATE TRIGGER trg_mandate_plan_events_batch_guard BEFORE INSERT ON "
        "review.mandate_plan_events FOR EACH ROW WHEN (NEW.batch_member_id IS NOT NULL) "
        "EXECUTE FUNCTION review.mandate_plan_events_batch_guard()"
    ) in ddl


def test_i22_the_batch_unique_key_and_the_open_batch_trigger() -> None:
    sql = _norm(_render("upgrade"))
    assert (
        "CONSTRAINT uq_mandate_plan_batches_basket_session "
        "UNIQUE (realm, basket_key, target_session)"
    ) in sql
    ddl = _ddl()
    start = ddl.index("CREATE OR REPLACE FUNCTION review.mandate_plan_batches_guard()")
    body = ddl[start : ddl.index("$$ LANGUAGE plpgsql", start)]
    # the basket lock (L2) first, then the open-batch check
    key = "'mandate_basket:' || NEW.realm || ':' || NEW.basket_key"
    assert key in body
    assert "the basket has an open batch" in body
    assert body.index(key) < body.index("the basket has an open batch")
    assert "b.expires_at > clock_timestamp()" in body
    assert "m.card_no = 1" in body
    assert "READ COMMITTED" in body.upper() or "read committed" in body


def test_the_basket_lock_key_is_the_same_string_in_sql_and_python() -> None:
    ddl = _ddl()
    assert "'mandate_basket:' || NEW.realm || ':' || NEW.basket_key" in ddl
    assert "'mandate_basket:' || b.realm || ':' || b.basket_key" in ddl
    assert locks.basket_lock_key("mock", "basket-1") == "mandate_basket:mock:basket-1"


def test_a_card_answers_exactly_the_events_of_the_design() -> None:
    ddl = _ddl()
    assert (
        "(mem.kind = 'new_plan' AND NEW.event IN ('approved', 'rejected')) "
        "OR (mem.kind = 'reapproval' AND NEW.event IN ('terms_rebound', 'retired'))"
    ) in ddl
    answers = {e for events in v.BATCH_ANSWER_EVENTS.values() for e in events}
    assert answers == {"approved", "rejected", "terms_rebound", "retired"}
    # the T1 CHECK that lets an event carry a card id names the same four events
    from app.models.mandate import event_check_sql

    check = dict(event_check_sql())["batch_member_id_allowed"]
    for event in ("approved", "rejected", "retired", "terms_rebound"):
        assert f"'{event}'" in check


def test_the_answer_guard_is_serialized_by_a_per_plan_advisory_lock() -> None:
    ddl = _ddl()
    start = ddl.index(
        "CREATE OR REPLACE FUNCTION review.mandate_plan_events_batch_guard()"
    )
    body = ddl[start : ddl.index("$$ LANGUAGE plpgsql", start)]
    lock = "'mandate_batch_member:' || mem.batch_id || ':' || mem.plan_id"
    assert lock in body
    # the lock comes before the already-answered read, so two answers cannot both pass
    assert body.index(lock) < body.index("the plan of this batch is already answered")
    assert "the batch has expired" in body
    assert "a newer card replaced this one" in body
    assert "the card clause is no longer the live clause" in body
    assert "before the deadline an approval takes effect on the target session" in body
    assert (
        "after the deadline an approval takes effect after the target session" in body
    )


def test_card_one_comes_only_with_its_batch_and_later_cards_keep_the_snapshot() -> None:
    ddl = _ddl()
    assert "b.created_at IS DISTINCT FROM transaction_timestamp()" in ddl
    assert "a plan joins a batch only in the batch own transaction" in ddl
    assert "a re-sent card keeps the snapshot of card 1" in ddl
    assert "cards of a plan are numbered in sequence" in ddl
    assert "an expired batch takes no new card" in ddl
    assert "the plan authority does not fit the card kind" in ddl


def test_the_answer_lookup_index_is_created_idempotently() -> None:
    ddl = _ddl()
    assert (
        "CREATE INDEX IF NOT EXISTS ix_mandate_plan_events_batch_member "
        "ON review.mandate_plan_events (batch_member_id) "
        "WHERE batch_member_id IS NOT NULL"
    ) in ddl


def test_downgrade_refuses_while_any_batch_row_or_answer_exists() -> None:
    guard = _norm(_load().DOWNGRADE_GUARD)
    assert "FROM review.mandate_plan_batches" in guard
    assert "FROM review.mandate_plan_batch_members" in guard
    assert "batch_member_id IS NOT NULL" in guard
    assert "restrict_violation" in guard
    sql = _norm(_render("downgrade"))
    assert sql.index("DO $$") < sql.index(
        "DROP TABLE review.mandate_plan_batch_members"
    )
    assert sql.index("DROP TABLE review.mandate_plan_batch_members") < sql.index(
        "DROP TABLE review.mandate_plan_batches"
    )
    assert "DROP TRIGGER IF EXISTS trg_mandate_plan_events_batch_guard" in sql


def test_constraint_and_index_names_fit_the_postgres_limit() -> None:
    sql = _render("upgrade")
    for name in re.findall(r"CONSTRAINT (\S+)", sql):
        assert len(name) <= 63, name
    for name in re.findall(r"CREATE (?:UNIQUE )?INDEX (?:IF NOT EXISTS )?(\S+)", sql):
        assert len(name) <= 63, name


@pytest.mark.parametrize(
    ("orm", "table"),
    [
        (MandatePlanBatch, "mandate_plan_batches"),
        (MandatePlanBatchMember, "mandate_plan_batch_members"),
    ],
)
def test_orm_and_migration_agree_on_columns_and_constraints(orm, table) -> None:
    """The ORM builds the test schema, the migration builds production: same shape."""
    sql = _norm(_render("upgrade"))
    start = sql.index(f"CREATE TABLE review.{table} (")
    block = sql[start : sql.index(");", start)]
    for column in orm.__table__.columns:
        assert re.search(rf"\b{column.name}\b", block), column.name
        nullable = "NOT NULL" in re.search(rf"\b{column.name} [^,]*", block).group(0)
        assert nullable == (not column.nullable), column.name
    for constraint in orm.__table__.constraints:
        if constraint.name:
            assert str(constraint.name) in block, constraint.name
    rendered_checks = set(re.findall(r"CONSTRAINT (ck_\S+) CHECK", block))
    orm_checks = {
        str(c.name)
        for c in orm.__table__.constraints
        if c.name and str(c.name).startswith("ck_")
    }
    assert rendered_checks == orm_checks
    index_names = {i.name for i in orm.__table__.indexes}
    for name in index_names:
        assert f"CREATE INDEX {name} ON review.{table}" in sql


def test_the_check_texts_of_the_orm_match_the_migration() -> None:
    sql = _norm(_render("upgrade"))
    for orm in (MandatePlanBatch, MandatePlanBatchMember):
        for c in orm.__table__.constraints:
            if c.name and str(c.name).startswith("ck_"):
                assert _norm(str(c.sqltext)) in sql, c.name


def _body(name: str) -> str:
    ddl = _ddl()
    start = ddl.index(f"CREATE OR REPLACE FUNCTION review.{name}()")
    return ddl[start : ddl.index("$$ LANGUAGE plpgsql", start)]


def test_i22_card_one_is_admitted_only_while_no_other_batch_of_the_basket_is_open() -> (
    None
):
    body = _body("mandate_plan_batch_members_guard")
    assert "o.id <> NEW.batch_id" in body
    assert "a plan joins only one open batch" in body
    # the check sits in the card-1 branch, under the basket lock, before the kind check
    assert body.index("'mandate_basket:'") < body.index(
        "a plan joins only one open batch"
    )
    assert body.index("a plan joins only one open batch") < body.index(
        "the plan authority does not fit the card kind"
    )


def test_a_resend_and_an_answer_serialize_on_the_same_per_plan_lock() -> None:
    key = "'mandate_batch_member:' || "
    card = _body("mandate_plan_batch_members_guard")
    answer = _body("mandate_plan_events_batch_guard")
    assert key + "NEW.batch_id || ':' || NEW.plan_id" in card
    assert key + "mem.batch_id || ':' || mem.plan_id" in answer
    # taken before either side reads the other's rows
    assert card.index(key) < card.index("SELECT max(card_no)")
    assert answer.index(key) < answer.index(
        "the plan of this batch is already answered"
    )


def test_the_answer_takes_the_sleeve_lock_and_rechecks_the_live_clause_for_every_event() -> (
    None
):
    body = _body("mandate_plan_events_batch_guard")
    sleeve = "'review.strategy_sleeves:' || mem.sleeve_id"
    assert sleeve in body
    assert locks_sleeve_key_matches()
    # sleeve lock, then the per-plan lock, then the checks
    assert body.index(sleeve) < body.index("'mandate_batch_member:'")
    live = body.index("the card clause is no longer the live clause")
    approval_only = body.index("IF NEW.event IN ('approved', 'terms_rebound') THEN")
    assert live < approval_only  # not inside the approval-only branch
    assert body.index(sleeve) < live


def locks_sleeve_key_matches() -> bool:
    """The SQL key is the string ``acquire_sleeve_lock`` hashes."""
    import inspect

    from app.services.strategy_sleeves import store

    return 'f"review.strategy_sleeves:{int(sleeve_id)}"' in inspect.getsource(
        store.acquire_sleeve_lock
    )


def test_expiry_and_the_deadline_are_read_from_the_wall_clock_not_the_transaction_start() -> (
    None
):
    """A transaction that began before the close must not answer after it."""
    for name in (
        "mandate_plan_batches_guard",
        "mandate_plan_batch_members_guard",
        "mandate_plan_events_batch_guard",
    ):
        body = _body(name)
        assert "now()" not in body, name
        assert "clock_timestamp()" in body, name
    answer = _body("mandate_plan_events_batch_guard")
    assert "IF clock_timestamp() >= mem.expires_at THEN" in answer
    assert "IF clock_timestamp() < mem.approval_deadline_at THEN" in answer
