"""Migration 20261120_hk5651_mandate_fence: offline render and parity (hk 3795 T4c).

No database. The two coordination tables, the three guard triggers, the backfill and
the hard-rule-18 grant are checked as rendered SQL; the ORM twins are compared to it.
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

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "alembic/versions/20261120_hk5651_mandate_fence.py"
TABLES = ("mandate_basket_authority", "mandate_cap_mutex")


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("hk5651_migration", MIGRATION)
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


def test_revision_chain_follows_the_sleeve_kiwoom_head() -> None:
    m = _load()
    assert m.revision == "20261120_hk5651_mandate_fence"
    assert m.down_revision == "20261120_hk5637_sleeve_kiwoom"
    assert len(m.revision) <= 32


def test_upgrade_is_additive_only() -> None:
    sql = _norm(_render("upgrade")).upper()
    for forbidden in ("DROP TABLE", "DROP COLUMN", "ALTER TABLE", "DELETE FROM REVIEW"):
        assert forbidden not in sql, forbidden
    # the only existing object touched is the plan-events table, by one new trigger
    assert "ON REVIEW.MANDATE_PLAN_EVENTS" in sql
    assert sql.count("CREATE TABLE") == 2


def test_hard_rule_18_the_app_role_gets_no_delete_and_only_a_conditional_grant() -> (
    None
):
    sql = _norm(_render("upgrade"))
    grant = re.search(
        r"DO \$\$ BEGIN IF EXISTS \(SELECT 1 FROM pg_roles.*?END \$\$", sql
    )
    assert grant is not None, "the at_app grant must be conditional on the role"
    for table in TABLES:
        assert (
            f"GRANT SELECT, INSERT, UPDATE ON review.{table} TO at_app"
            in grant.group(0)
        )
    assert "DELETE" not in grant.group(0).upper().replace("NEVER DELETE", "")
    assert "GRANT" not in re.sub(r"DO \$\$ BEGIN.*?END \$\$", "", sql)


def test_the_coordination_rows_cannot_be_deleted_truncated_or_changed_for_any_role() -> (
    None
):
    m = _load()
    ddl = _norm(" ".join(m.GUARD_DDL))
    assert "BEFORE UPDATE OR DELETE ON review.mandate_basket_authority" in ddl
    assert "BEFORE TRUNCATE ON review.mandate_basket_authority" in ddl
    assert "BEFORE UPDATE OR DELETE ON review.mandate_cap_mutex" in ddl
    assert "BEFORE TRUNCATE ON review.mandate_cap_mutex" in ddl
    # the authority counter only counts up by exactly one
    assert "NEW.authority_seq IS DISTINCT FROM OLD.authority_seq + 1" in ddl


def test_every_orm_check_and_column_is_in_the_migration() -> None:
    sql = _render("upgrade")
    for table in TABLES:
        orm = Base.metadata.tables[f"review.{table}"]
        start = sql.index(f"CREATE TABLE review.{table} (")
        block = sql[start : sql.index(";", start)]
        for column in orm.columns:
            line = next(
                ln for ln in block.splitlines() if re.match(rf"^\s+{column.name} ", ln)
            )
            assert ("NOT NULL" in line) == (not column.nullable), (table, column.name)
        for constraint in orm.constraints:
            if constraint.__class__.__name__ != "CheckConstraint":
                continue
            text_ = _norm(str(constraint.sqltext))
            assert f"CONSTRAINT {constraint.name} CHECK ({text_})" in _norm(sql)


def test_i16_the_automatic_fence_trigger_fires_after_the_events_own_guard() -> None:
    m = _load()
    ddl = _norm(" ".join(m.GUARD_DDL))
    name = "trg_mandate_plan_events_guard_fence_profile"
    assert name > "trg_mandate_plan_events_guard"  # BEFORE ROW triggers fire by name
    assert (
        f"CREATE TRIGGER {name} BEFORE INSERT ON review.mandate_plan_events "
        "FOR EACH ROW WHEN (NEW.event = 'tranche_send_fenced' AND NEW.approval_mode = 'auto')"
    ) in ddl
    # no function call in WHEN: untagged and human writes never run trigger code
    when = re.search(r"WHEN \((.*?)\) EXECUTE", ddl)
    assert when is not None and "(" not in when.group(1).replace("'", "")


def test_x9_the_trigger_enforces_the_latest_profile_and_the_live_clause() -> None:
    body = _norm(" ".join(_load().GUARD_DDL))
    for fragment in (
        "ORDER BY ap.profile_version DESC LIMIT 1",
        "latest.id <> NEW.activation_profile_id",
        "NEW.tranche_index > latest.max_tranche_index",
        "latest.effective_session > NEW.kst_day",
        "latest.expires_at <= clock_timestamp()",
        "cur.state <> 'live'",
        "cur.terms_id <> latest.terms_id",
        "bound_terms IS DISTINCT FROM latest.terms_id",
    ):
        assert fragment in body, fragment


def test_x15_every_basket_that_exists_gets_an_authority_row() -> None:
    m = _load()
    backfill = _norm(m.BACKFILL_AUTHORITY)
    assert "INSERT INTO review.mandate_basket_authority (realm, basket_key)" in backfill
    assert "FROM review.mandate_terms t" in backfill
    assert "ON CONFLICT DO NOTHING" in backfill
    assert backfill in _norm(_render("upgrade"))


def test_downgrade_drops_only_what_this_migration_made() -> None:
    sql = _norm(_render("downgrade"))
    assert "DROP TABLE review.mandate_cap_mutex" in sql
    assert "DROP TABLE review.mandate_basket_authority" in sql
    assert (
        "mandate_plan_events_guard_fence_profile ON review.mandate_plan_events" in sql
    )
    assert "mandate_plan_events;" not in sql.replace(
        "ON review.mandate_plan_events", ""
    )


def test_constraint_names_fit_the_postgres_limit() -> None:
    for table in TABLES:
        orm = Base.metadata.tables[f"review.{table}"]
        for constraint in orm.constraints:
            assert len(str(constraint.name)) <= 63, constraint.name
