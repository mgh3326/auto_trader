"""ROB-1470 migration shape (offline render) and dispatch wiring (no database)."""

from __future__ import annotations

import ast
import importlib.util
import inspect
import io
import textwrap
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations

from app.models.base import Base
from app.models.order_proposals import OrderProposal
from app.models.strategy_sleeves import StrategySleeve, StrategySleeveVersion
from app.services.order_proposals import dispatch
from app.services.order_proposals.auto_approve_audit import (
    _KNOWN_REASON_CODES,
    build_auto_approve_rejection_attempt,
    build_auto_approve_rejection_card_block,
)
from app.services.strategy_sleeves.vocabulary import (
    CANARY_RULES,
    SleeveGateReason,
)

pytestmark = pytest.mark.unit

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "alembic/versions/20261102_rob1470_strategy_sleeves.py"


def _load_migration() -> Any:
    spec = importlib.util.spec_from_file_location("rob1470_migration", MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _render(fn: str) -> str:
    module = _load_migration()
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


def _table_block(sql: str, table: str) -> str:
    start = sql.index(f"CREATE TABLE review.{table} (")
    return sql[start : sql.index(";", start)]


def test_migration_follows_the_current_head() -> None:
    module = _load_migration()
    assert module.revision == "20261102_rob1470_sleeves"
    assert module.down_revision == "20261101_rob1440_intent_gw"
    assert len(module.revision) <= 32  # alembic_version.version_num


def test_migration_is_additive_only() -> None:
    sql = _render("upgrade")
    upper = sql.upper()
    assert "DROP TABLE" not in upper
    assert "DROP COLUMN" not in upper
    alters = [
        line for line in sql.splitlines() if line.upper().startswith("ALTER TABLE")
    ]
    assert alters, "the nullable sleeve_id column and its FK"
    for line in alters:
        assert "review.order_proposals" in line
        assert "ADD" in line.upper()
    assert "ADD COLUMN sleeve_id BIGINT" in sql
    grants = sql.split("DO $$ BEGIN IF EXISTS", 1)[1]
    assert "DELETE" not in grants
    assert "UPDATE" not in grants


@pytest.mark.parametrize("model", [StrategySleeve, StrategySleeveVersion])
def test_migration_columns_and_checks_match_the_model(model: type) -> None:
    table: sa.Table = model.__table__  # type: ignore[attr-defined]
    block = _table_block(_render("upgrade"), table.name)
    for column in table.columns:
        assert f"{column.name} " in block, column.name
    checks = [c for c in table.constraints if isinstance(c, sa.CheckConstraint)]
    assert checks
    for check in checks:
        assert str(check.sqltext) in block, str(check.sqltext)
        assert f"CONSTRAINT {check.name} CHECK" in block, check.name


def test_order_proposals_model_carries_the_same_nullable_fk() -> None:
    column = OrderProposal.__table__.c.sleeve_id  # type: ignore[attr-defined]
    assert column.nullable is True
    [fk] = column.foreign_keys
    assert fk.target_fullname == "review.strategy_sleeves.id"
    assert fk.ondelete == "RESTRICT"
    assert "fk_order_proposals_sleeve_id_strategy_sleeves" in _render("upgrade")


def test_guards_cover_every_write_on_both_tables_and_the_tag() -> None:
    sql = _render("upgrade")
    for statement in (
        "BEFORE UPDATE OR DELETE ON review.strategy_sleeves",
        "BEFORE TRUNCATE ON review.strategy_sleeves",
        "BEFORE INSERT OR UPDATE OR DELETE ON review.strategy_sleeve_versions",
        "BEFORE TRUNCATE ON review.strategy_sleeve_versions",
        "BEFORE INSERT ON review.order_proposals",
        "BEFORE UPDATE OF sleeve_id ON review.order_proposals",
    ):
        assert statement in sql, statement


def test_proposal_triggers_run_no_code_for_an_untagged_write() -> None:
    """The WHEN clauses call no function and skip every sleeve_id IS NULL row."""
    ddl = "\n".join(_load_migration().GUARD_DDL)
    assert "WHEN (NEW.sleeve_id IS NOT NULL)" in ddl
    assert "WHEN (OLD.sleeve_id IS DISTINCT FROM NEW.sleeve_id)" in ddl
    for line in ddl.splitlines():
        if line.strip().startswith("FOR EACH ROW WHEN"):
            assert "review." not in line and "(" not in line.split("WHEN", 1)[1][2:]


def test_version_guard_requires_the_next_version() -> None:
    ddl = "\n".join(_load_migration().GUARD_DDL)
    assert "NEW.version <> coalesce(last_version, 0) + 1" in ddl


def test_downgrade_refuses_rows_before_dropping_anything() -> None:
    sql = _render("downgrade")
    assert "LOCK TABLE review.strategy_sleeves" in sql
    assert "sleeve_id IS NOT NULL" in sql
    assert sql.index("RAISE EXCEPTION") < sql.index("DROP INDEX")
    assert sql.index("RAISE EXCEPTION") < sql.index("DROP TABLE")


def test_every_canary_rule_runs_in_a_sleeve_market() -> None:
    for rule in CANARY_RULES.values():
        assert rule.markets <= {"equity_kr", "equity_us", "crypto"}
        assert rule.design_section.startswith("4.")


# ---------------------------------------------------------------- dispatch wiring


def _dispatch_tree() -> ast.AsyncFunctionDef:
    tree = ast.parse(textwrap.dedent(inspect.getsource(dispatch.dispatch_proposal)))
    [node] = [n for n in tree.body if isinstance(n, ast.AsyncFunctionDef)]
    return node


def _call_line(node: ast.AST, name: str) -> int:
    lines = [
        n.lineno
        for n in ast.walk(node)
        if isinstance(n, ast.Call)
        and (
            (isinstance(n.func, ast.Name) and n.func.id == name)
            or (isinstance(n.func, ast.Attribute) and n.func.attr == name)
        )
    ]
    assert lines, name
    return min(lines)


def test_sleeve_gate_runs_before_revalidation_and_daily_budget() -> None:
    node = _dispatch_tree()
    gate = _call_line(node, "evaluate_sleeve_for_dispatch")
    assert gate < _call_line(node, "revalidate_fn")
    assert gate < _call_line(node, "auto_approved_daily_notional")


def test_sleeve_gate_is_reached_only_for_a_tagged_proposal() -> None:
    node = _dispatch_tree()
    [branch] = [
        n
        for n in ast.walk(node)
        if isinstance(n, ast.If)
        and any(
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Name)
            and c.func.id == "evaluate_sleeve_for_dispatch"
            for c in ast.walk(n)
        )
        and "sleeve_id" in ast.unparse(n.test)
    ]
    assert ast.unparse(branch.test) == (
        "limits is not None and getattr(group, 'sleeve_id', None) is not None"
    )
    # A refused verdict ends the auto lane: limits becomes None in the branch.
    assert "limits = None" in ast.unparse(branch)


def test_every_sleeve_reason_survives_the_audit_projection_onto_the_card() -> None:
    assert {r.value for r in SleeveGateReason} <= _KNOWN_REASON_CODES
    decisions = dispatch._sleeve_fallback_decisions(
        rungs=[type("R", (), {"rung_index": 0, "state": "pending_approval"})()],
        verdict=type(
            "V",
            (),
            {
                "reason": SleeveGateReason.BUDGET_EXCEEDED,
                "details": {
                    "sleeve_id": "7",
                    "sleeve_version": "3",
                    "sleeve_state": "live",
                    "sleeve_used": "500",
                    "sleeve_order_notional": "501",
                    "sleeve_used_after": "1001",
                    "sleeve_budget_cap": "1000",
                    "sleeve_net_pnl": "-12.5",
                    "sleeve_loss_limit": "100",
                },
            },
        )(),
        policy_version="2026-11-02.1",
    )
    attempt = build_auto_approve_rejection_attempt(
        decisions=decisions, now=datetime(2026, 11, 2, tzinfo=UTC)
    )
    assert attempt is not None
    [rung] = attempt["rungs"]
    assert rung["reason_code"] == "sleeve_budget_exceeded"
    for key in (
        "sleeve_id",
        "sleeve_used_after",
        "sleeve_budget_cap",
        "sleeve_net_pnl",
        "sleeve_loss_limit",
        "sleeve_state",
    ):
        assert key in rung["inputs"], key
    card = build_auto_approve_rejection_card_block(
        {"auto_approve_rejections": [attempt]}
    )
    assert card is not None and "sleeve_budget_exceeded" in card


def test_preregistration_rules_are_in_the_schema() -> None:
    sql = _render("upgrade")
    assert "CONSTRAINT ck_strategy_sleeve_versions_live_requires_prereg CHECK" in sql
    ddl = "\n".join(_load_migration().GUARD_DDL)
    assert "pre-registration is frozen once the sleeve has been live" in ddl
    for column in (
        "prereg_metric",
        "prereg_min_effect_pct",
        "prereg_horizon_sessions",
        "prereg_max_duration_days",
    ):
        assert f"v.{column} IS DISTINCT FROM NEW.{column}" in ddl, column
