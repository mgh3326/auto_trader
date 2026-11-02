"""Static checks on the migration, the task and the buy hooks (ROB-1460)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from typing import Any

import pytest

from app.models.mechanical_loss_cut import LotExitPlan, MechanicalLossCutDecision

pytestmark = pytest.mark.unit

_ROOT = Path(__file__).resolve().parents[3]
_MIGRATION = _ROOT / "alembic/versions/20261102_rob1460_lot_exit_plans.py"


def _source() -> str:
    return _MIGRATION.read_text(encoding="utf-8")


def _load() -> Any:
    spec = importlib.util.spec_from_file_location("rob1460_static", _MIGRATION)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_migration_is_additive_and_grants_no_update_or_delete():
    source = _source()

    assert "op.alter_table" not in source
    assert "op.add_column" not in source
    grants = [line for line in source.splitlines() if "GRANT" in line]
    assert grants
    for line in grants:
        assert "UPDATE" not in line
        assert "DELETE" not in line


def test_migration_follows_the_previous_head():
    module = _load()

    assert module.down_revision == "20261101_rob1440_intent_gw"
    assert len(module.revision) <= 32


def test_downgrade_refuses_while_rows_exist_before_dropping():
    source = _source()
    downgrade = source[source.index("def downgrade") :]

    assert downgrade.index("DOWNGRADE_GUARD") < downgrade.index("drop_table")


def test_append_only_guard_covers_update_and_delete_on_both_tables():
    ddl = " ".join(_load().GUARD_DDL)

    for table in ("lot_exit_plans", "mechanical_loss_cut_decisions"):
        assert f"BEFORE UPDATE OR DELETE ON review.{table}" in ddl


@pytest.mark.parametrize("model", [LotExitPlan, MechanicalLossCutDecision])
def test_migration_columns_and_constraints_match_the_model(model):
    source = _source()

    for column in model.__table__.columns:
        assert f'"{column.name}"' in source, column.name
    for constraint in model.__table__.constraints:
        if constraint.name is not None and not constraint.name.startswith("pk_"):
            assert f'"{constraint.name}"' in source or constraint.name in source
    for index in model.__table__.indexes:
        assert f'"{index.name}"' in source


def test_once_per_trigger_is_a_database_constraint():
    constraint = next(
        item
        for item in MechanicalLossCutDecision.__table__.constraints
        if item.name == "uq_mechanical_loss_cut_decisions_trigger"
    )

    assert {column.name for column in constraint.columns} == {
        "mode",
        "account_mode",
        "market",
        "symbol",
        "trigger_key",
    }


def test_task_is_registered_without_a_schedule():
    source = (_ROOT / "app/tasks/mechanical_loss_cut_tasks.py").read_text()
    assert "schedule=" not in source
    assert "cron" not in source


@pytest.mark.parametrize(
    "path",
    [
        "app/mcp_server/tooling/order_execution.py",
        "app/mcp_server/tooling/orders_toss_variants.py",
        "app/services/order_proposals/revalidation.py",
    ],
)
def test_every_buy_path_writes_the_plan(path):
    assert "record_buy_exit_plan" in (_ROOT / path).read_text(encoding="utf-8")
