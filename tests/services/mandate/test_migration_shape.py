"""Migration 20261120_hk5528_mandate: offline render and parity with the Python twins.

No database: the migration is rendered as SQL and the vocabulary, ORM checks and
lock keys the DB triggers must agree with are compared with it.
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
from app.models.mandate import event_check_sql
from app.services.mandate import locks
from app.services.mandate import vocabulary as v

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "alembic/versions/20261120_hk5528_mandate_model.py"
TABLES = (
    "mandate_approval_requests",
    "mandate_approvals",
    "mandate_terms",
    "mandate_activation_profiles",
    "mandate_plans",
    "mandate_plan_tranches",
    "mandate_plan_events",
    "mandate_symbol_reservations",
    "mandate_symbol_reservation_releases",
)


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("hk5528_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(fn: str) -> str:
    module = _load()
    buffer = io.StringIO()
    context = MigrationContext.configure(
        dialect_name="postgresql",
        opts={"as_sql": True, "output_buffer": buffer, "target_metadata": Base.metadata},
    )
    with Operations.context(context):
        getattr(module, fn)()
    return buffer.getvalue()


def _norm(sql: str) -> str:
    return re.sub(r"\s+", " ", sql).strip()


def test_revision_chain_and_length() -> None:
    m = _load()
    assert m.revision == "20261120_hk5528_mandate"
    assert m.down_revision == "20261116_hk4902_drop_unused"
    assert len(m.revision) <= 32


def test_upgrade_is_additive_only() -> None:
    sql = _render("upgrade").upper()
    assert "DROP TABLE" not in sql and "DROP COLUMN" not in sql
    assert sql.count("CREATE TABLE") == 9
    assert "ALTER TABLE REVIEW.ORDER_PROPOSALS ADD COLUMN MANDATE_PLAN_ID" in sql
    assert "MANDATE_TRANCHE_INDEX" in sql


def test_i13_every_table_has_update_delete_and_truncate_triggers() -> None:
    ddl = _norm("\n".join(_load().GUARD_DDL))
    for table in TABLES:
        assert f"CREATE TRIGGER trg_{table}_immutable BEFORE UPDATE OR DELETE ON review.{table}" in ddl
        assert f"CREATE TRIGGER trg_{table}_truncate BEFORE TRUNCATE ON review.{table}" in ddl


def test_hard_rule_18_app_role_gets_select_and_insert_only() -> None:
    sql = _norm(_render("upgrade"))
    grant = sql[sql.index("DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles") :]
    for table in TABLES:
        assert f"GRANT SELECT, INSERT ON review.{table} TO at_app" in grant
    assert "UPDATE ON review.mandate" not in grant
    assert "DELETE ON review" not in grant
    assert "GRANT UPDATE" not in grant and "GRANT DELETE" not in grant
    assert "IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app')" in grant
    # helper functions the triggers call need EXECUTE for the app role
    assert "GRANT EXECUTE ON FUNCTION review.mandate_realm(text)" in grant
    assert "GRANT EXECUTE ON FUNCTION review.mandate_plan_auth_state(bigint)" in grant
    for table in TABLES:
        if table != "mandate_plan_tranches":
            assert f"SEQUENCE review.{table}_id_seq TO at_app" in grant
    assert "SELECT (mandate_plan_id, mandate_tranche_index)" in grant


def test_order_proposal_triggers_do_not_run_for_untagged_writes() -> None:
    """Acceptance 4: the WHEN clauses call no function, so an untagged write is untouched."""
    ddl = _norm("\n".join(_load().GUARD_DDL))
    insert = ddl[ddl.index("CREATE TRIGGER trg_order_proposals_mandate_insert") :]
    insert = insert[: insert.index("EXECUTE FUNCTION")]
    assert "WHEN (NEW.mandate_plan_id IS NOT NULL OR NEW.mandate_tranche_index IS NOT NULL)" in insert
    update = ddl[ddl.index("CREATE TRIGGER trg_order_proposals_mandate_update") :]
    update = update[: update.index("EXECUTE FUNCTION")]
    assert "BEFORE UPDATE OF mandate_plan_id, mandate_tranche_index" in update
    assert "WHEN (OLD.mandate_plan_id IS DISTINCT FROM NEW.mandate_plan_id" in update
    for when in (insert, update):
        assert "review." not in when.split("WHEN", 1)[1]
    guard = ddl[ddl.index("review.order_proposals_mandate_guard() RETURNS trigger") :]
    assert "NEW.symbol <> pl.symbol" in guard  # equality on the stored symbol
    assert "upper(" not in guard.lower() and "replace(" not in guard.lower()


def test_downgrade_refuses_rows_before_dropping_anything() -> None:
    sql = _render("downgrade")
    assert "LOCK TABLE review.mandate_approval_requests" in sql
    assert "mandate_plan_id IS NOT NULL" in sql
    assert sql.index("RAISE EXCEPTION") < sql.index("DROP INDEX")
    assert sql.index("RAISE EXCEPTION") < sql.index("DROP TABLE")
    for table in TABLES:
        assert f"DROP TABLE review.{table}" in sql


def test_event_checks_match_the_orm_definition_exactly() -> None:
    m = _load()
    assert tuple(m.EVENT_CHECKS) == tuple(event_check_sql())


def test_every_orm_check_is_in_the_migration_under_the_same_name_and_text() -> None:
    sql = _norm(_render("upgrade"))
    seen = 0
    for table in TABLES:
        orm = Base.metadata.tables[f"review.{table}"]
        for constraint in orm.constraints:
            if constraint.__class__.__name__ != "CheckConstraint":
                continue
            name = str(constraint.name)
            text_ = _norm(str(constraint.sqltext))
            assert f"CONSTRAINT {name} CHECK ({text_})" in sql, name
            seen += 1
    assert seen > 40


def test_orm_and_migration_agree_on_columns_and_unique_constraints() -> None:
    sql = _render("upgrade")
    for table in TABLES:
        orm = Base.metadata.tables[f"review.{table}"]
        start = sql.index(f"CREATE TABLE review.{table} (")
        block = sql[start : sql.index(";", start)]
        for column in orm.columns:
            assert re.search(rf"^\s+{column.name} ", block, re.M), (table, column.name)
            null_sql = "NOT NULL" if not column.nullable else ""
            line = next(
                l for l in block.splitlines() if re.match(rf"^\s+{column.name} ", l)
            )
            assert ("NOT NULL" in line) == (not column.nullable), (table, column.name)
            del null_sql
        for uq in (c for c in orm.constraints if c.__class__.__name__ == "UniqueConstraint"):
            assert f"CONSTRAINT {uq.name} UNIQUE" in block, uq.name
    for idx_name in (
        "uq_mandate_plan_events_terminal_proposal",
        "uq_mandate_plan_events_proposed",
        "uq_mandate_plan_events_fence_attempt",
        "uq_mandate_plan_events_fence_release",
    ):
        assert f"CREATE UNIQUE INDEX {idx_name}" in sql


def test_vocabulary_copy_in_the_migration_is_the_app_vocabulary() -> None:
    m = _load()
    assert m.AUTH_FROM == v.AUTH_FROM
    assert m.AUTH_AFTER == v.AUTH_AFTER
    assert m.REALM_BY_ACCOUNT_MODE == v.REALM_BY_ACCOUNT_MODE
    assert m.SEND_AUTHORITY == v.SEND_AUTHORITY
    assert m.CLEANUP_AUTH == v.CLEANUP_AUTH
    assert m.CLOSED_PARTIAL_AUTH == v.CLOSED_PARTIAL_AUTH
    assert m.TRANCHE_TERMINAL_EVENTS == v.TRANCHE_TERMINAL_EVENTS
    assert m.HOLDINGS_DISPOSITIONS == v.HOLDINGS_DISPOSITIONS


def test_the_event_trigger_embeds_the_python_transition_table() -> None:
    ddl = _norm("\n".join(_load().GUARD_DDL))
    for event, states in v.AUTH_FROM.items():
        array = ",".join(f"'{s}'" for s in states)
        assert f"WHEN '{event}' THEN ARRAY[{array}]::text[]" in ddl
    for event, state in v.AUTH_AFTER.items():
        assert f"WHEN '{event}' THEN '{state}'" in ddl
    assert "WHEN 'resumed' THEN rec.resume_target" in ddl
    assert "ARRAY['approved','active']::text[]" in ddl  # SEND_AUTHORITY


def test_i16_i24_the_trigger_refuses_a_fence_without_sending_authority() -> None:
    ddl = _norm("\n".join(_load().GUARD_DDL))
    assert (
        "IF NEW.event IN ('tranche_armed','tranche_proposed','tranche_send_fenced') "
        "AND NOT (st = ANY (ARRAY['approved','active']::text[]))"
    ) in ddl
    assert "the plan does not hold sending authority" in ddl


def test_i1_symbol_lock_key_is_the_same_string_in_sql_and_python() -> None:
    ddl = _norm("\n".join(_load().GUARD_DDL))
    key_sql = "'mandate_symbol:' || NEW.realm || ':' || NEW.market || ':' || NEW.symbol"
    assert key_sql in ddl
    assert "'mandate_symbol:' || res.realm || ':' || res.market || ':' || res.symbol" in ddl
    assert locks.symbol_lock_key("live", "equity_kr", "005930") == "mandate_symbol:live:equity_kr:005930"
    assert locks.basket_lock_key("mock", "basket-1") == "mandate_basket:mock:basket-1"
    assert "hashtextextended(" in ddl
    assert "READ COMMITTED" not in ddl or "read committed" in ddl


def test_lock_order_is_symbol_then_basket_then_sleeve() -> None:
    assert [lvl.name for lvl in locks.LOCK_ORDER] == ["SYMBOL", "BASKET", "SLEEVE"]
    assert list(locks.LOCK_ORDER) == sorted(locks.LOCK_ORDER)


def test_realm_helper_function_matches_the_python_map() -> None:
    ddl = _norm("\n".join(_load().GUARD_DDL))
    arms = " ".join(f"WHEN '{m}' THEN '{r}'" for m, r in v.REALM_BY_ACCOUNT_MODE.items())
    assert f"CASE p_account_mode {arms} END" in ddl
    assert v.realm_of("kis_live") == "live" and v.realm_of("kiwoom_mock") == "mock"
    for unsupported in ("upbit", "kis_mock", "db_simulated"):
        with pytest.raises(ValueError):
            v.realm_of(unsupported)


def test_release_row_is_written_by_the_released_event_itself() -> None:
    ddl = _norm("\n".join(_load().GUARD_DDL))
    assert "AFTER INSERT ON review.mandate_plan_events FOR EACH ROW WHEN (NEW.event = 'released')" in ddl
    assert "a reservation is released only with the plan released event" in ddl


def test_constraint_and_index_names_fit_the_postgres_limit() -> None:
    sql = _render("upgrade")
    for name in re.findall(r"CONSTRAINT (\S+)", sql):
        assert len(name) <= 63, name
    for name in re.findall(r"CREATE (?:UNIQUE )?INDEX (\S+)", sql):
        assert len(name) <= 63, name
