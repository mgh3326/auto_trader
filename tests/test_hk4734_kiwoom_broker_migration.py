"""hk 4734 (3797 T-P4) — broker 'kiwoom' widening of the execution ledger CHECKs.

No database: the migration is parsed with ``ast``/``importlib`` and compared with
the ORM tables and the schema Literals. The PostgreSQL round trip and the
downgrade refusal live in ``tests/test_hk4734_kiwoom_broker_schema_db.py`` (CI).
"""

from __future__ import annotations

import importlib.util
import typing
from pathlib import Path

from sqlalchemy import CheckConstraint

from app.models.execution_ledger import (
    ExecutionLedger,
    ExecutionLedgerReconcileRun,
)
from app.schemas.execution_ledger import Broker, ReconcileRunBroker

REPO = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO / "alembic" / "versions" / "20261115_hk4734_kiwoom_broker.py"
REVISION = "20261115_hk4734_kiwoom_broker"
PARENT_REVISION = "20261114_hk4702_trend_state"

LEDGER_CHECK = "ck_execution_ledger_execution_ledger_broker"
RUNS_CHECK = "ck_execution_ledger_reconcile_runs_runs_broker"


def _load_migration():
    spec = importlib.util.spec_from_file_location("hk4734_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _check_sql(table, name: str) -> str:
    checks = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == name
    ]
    assert len(checks) == 1, f"{name} must exist exactly once"
    return str(checks[0].sqltext)


def test_revision_chains_from_the_single_head_and_fits_version_table() -> None:
    migration = _load_migration()
    assert migration.revision == REVISION
    assert migration.down_revision == PARENT_REVISION
    # alembic_version.version_num is VARCHAR(32) on production databases.
    assert len(REVISION) <= 32


def test_upgrade_admits_kiwoom_in_both_checks_and_downgrade_restores_them() -> None:
    migration = _load_migration()

    assert "kiwoom" in migration._NEW_LEDGER_BROKERS
    assert "kiwoom" not in migration._OLD_LEDGER_BROKERS
    assert "kiwoom" in migration._NEW_RUNS_BROKERS
    assert "kiwoom" not in migration._OLD_RUNS_BROKERS
    # The old spellings production carries are dropped too, so the widening
    # cannot silently leave a narrow CHECK in place.
    assert "execution_ledger_broker" in migration._LEDGER_CHECK_NAMES
    assert "execution_ledger_runs_broker" in migration._RUNS_CHECK_NAMES


def test_downgrade_refuses_while_any_kiwoom_row_exists() -> None:
    guard = _load_migration().DOWNGRADE_GUARD
    assert "broker = 'kiwoom'" in guard
    assert "review.execution_ledger" in guard
    assert "review.execution_ledger_reconcile_runs" in guard
    assert "restrict_violation" in guard


def test_orm_checks_match_the_widened_vocabulary() -> None:
    ledger_sql = _check_sql(ExecutionLedger.__table__, LEDGER_CHECK)
    assert "'kiwoom'" in ledger_sql
    assert "'kis'" in ledger_sql and "'upbit'" in ledger_sql and "'toss'" in ledger_sql

    runs_sql = _check_sql(ExecutionLedgerReconcileRun.__table__, RUNS_CHECK)
    assert "'kiwoom'" in runs_sql
    assert "'kis'" in runs_sql and "'upbit'" in runs_sql


def test_schema_literals_admit_kiwoom_only_where_the_checks_do() -> None:
    assert typing.get_args(Broker) == ("kis", "upbit", "toss", "kiwoom")
    assert typing.get_args(ReconcileRunBroker) == ("kis", "upbit", "kiwoom")
