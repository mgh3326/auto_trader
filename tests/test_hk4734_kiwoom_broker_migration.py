"""hk 4734 (3797 T-P4) — broker 'kiwoom' widening of the execution ledger CHECKs.

No database: the two migrations are imported and their ``op.execute`` statements
captured, then compared with the ORM tables and the schema Literals. The
PostgreSQL round trip (NOT VALID → VALIDATE) and the downgrade refusal live in
``tests/test_hk4734_kiwoom_broker_schema_db.py`` (CI).

The forward path must never validate inside the ACCESS EXCLUSIVE lock: the
CHECK is added ``NOT VALID`` here and validated by the separate, chained
revision (review r4577667952).
"""

from __future__ import annotations

import importlib.util
import typing
from pathlib import Path
from typing import Any

from alembic.config import Config
from sqlalchemy import CheckConstraint

from alembic import script
from app.models.execution_ledger import (
    ExecutionLedger,
    ExecutionLedgerReconcileRun,
)
from app.schemas.execution_ledger import Broker, ReconcileRunBroker

REPO = Path(__file__).resolve().parents[1]
VERSIONS = REPO / "alembic" / "versions"
MIGRATION_PATH = VERSIONS / "20261115_hk4734_kiwoom_broker.py"
VALIDATE_PATH = VERSIONS / "20261115_hk4734_kiwoom_validate.py"
REVISION = "20261115_hk4734_kiwoom_broker"
VALIDATE_REVISION = "20261115_hk4734_kiwoom_validate"
PARENT_REVISION = "20261116_hk4754_us_proposals"

LEDGER_CHECK = "ck_execution_ledger_execution_ledger_broker"
RUNS_CHECK = "ck_execution_ledger_reconcile_runs_runs_broker"


class _OpRecorder:
    """Capture the raw SQL a migration revision would execute."""

    def __init__(self) -> None:
        self.statements: list[str] = []

    def execute(self, statement: object, *args: object, **kwargs: object) -> None:
        self.statements.append(str(statement))


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _load_migration():
    return _load(MIGRATION_PATH, "hk4734_migration")


def _load_validate():
    return _load(VALIDATE_PATH, "hk4734_migration_validate")


def _run(module: Any, step: str) -> list[str]:
    recorder = _OpRecorder()
    module.op = recorder
    getattr(module, step)()
    return recorder.statements


def _check_sql(table: Any, name: str) -> str:
    checks = [
        constraint
        for constraint in table.constraints
        if isinstance(constraint, CheckConstraint) and constraint.name == name
    ]
    assert len(checks) == 1, f"{name} must exist exactly once"
    return str(checks[0].sqltext)


def test_revisions_chain_and_fit_the_version_table() -> None:
    migration = _load_migration()
    validate = _load_validate()
    assert migration.revision == REVISION
    assert migration.down_revision == PARENT_REVISION
    assert validate.revision == VALIDATE_REVISION
    assert validate.down_revision == REVISION
    # alembic_version.version_num is VARCHAR(32) on production databases.
    assert len(REVISION) <= 32
    assert len(VALIDATE_REVISION) <= 32


def test_the_migration_graph_has_exactly_one_head() -> None:
    # Pin the revision inside the single chain, not "is the head": a later
    # migration (e.g. hk 4902) must not break this test.
    directory = script.ScriptDirectory.from_config(Config(str(REPO / "alembic.ini")))
    heads = directory.get_heads()
    assert len(heads) == 1, heads
    ancestry = {rev.revision for rev in directory.walk_revisions("base", heads[0])}
    assert {REVISION, VALIDATE_REVISION} <= ancestry


def test_upgrade_adds_both_checks_not_valid_and_never_validates() -> None:
    statements = _run(_load_migration(), "upgrade")

    added = [statement for statement in statements if " ADD CONSTRAINT " in statement]
    assert len(added) == 2
    assert all(statement.rstrip().endswith("NOT VALID") for statement in added)
    assert any(
        LEDGER_CHECK in statement and "kiwoom" in statement for statement in added
    )
    assert any(RUNS_CHECK in statement and "kiwoom" in statement for statement in added)
    # The scan belongs to the follow-up revision, not this lock.
    assert not any("VALIDATE CONSTRAINT" in statement for statement in statements)
    # Both historical spellings are dropped before the widened one is added.
    assert any('"execution_ledger_broker"' in statement for statement in statements)
    assert any(
        '"ck_execution_ledger_reconcile_runs_execution_ledger_runs_broker"' in statement
        for statement in statements
    )
    assert any(
        '"execution_ledger_runs_broker"' in statement for statement in statements
    )


def test_downgrade_refuses_kiwoom_rows_and_restores_the_old_vocabulary() -> None:
    statements = _run(_load_migration(), "downgrade")

    assert "broker = 'kiwoom'" in statements[0]
    assert "review.execution_ledger" in statements[0]
    assert "review.execution_ledger_reconcile_runs" in statements[0]
    assert "restrict_violation" in statements[0]

    added = [statement for statement in statements if " ADD CONSTRAINT " in statement]
    assert len(added) == 2
    # The emergency path restores the constraints as production carried them.
    assert all("NOT VALID" not in statement for statement in added)
    assert any(
        LEDGER_CHECK in statement and "kiwoom" not in statement for statement in added
    )
    assert any(
        RUNS_CHECK in statement and "kiwoom" not in statement for statement in added
    )


def test_validate_revision_validates_both_checks() -> None:
    statements = _run(_load_validate(), "upgrade")

    validated = [
        statement for statement in statements if "VALIDATE CONSTRAINT" in statement
    ]
    assert len(validated) == 2
    assert any(LEDGER_CHECK in statement for statement in validated)
    assert any(RUNS_CHECK in statement for statement in validated)
    assert _run(_load_validate(), "downgrade") == []


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
