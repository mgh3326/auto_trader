"""hk 4703 (3797 T-P1) — static and in-memory checks for the Kiwoom mock schema.

No database: the migration is parsed with ``ast``, the ORM tables are built in
memory, and the two sides are compared by name and vocabulary. Refusal and
round-trip proofs against PostgreSQL live in
``tests/test_kiwoom_mock_ledger_schema_db.py`` and run only in CI.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path

from sqlalchemy import CheckConstraint

from app.models.order_proposals import OrderProposal
from app.models.review import (
    KIWOOM_MOCK_ORDER_STATUSES,
    KIWOOM_MOCK_TERMINAL_STATUSES,
    KiwoomMockOrderLedger,
    KiwoomMockSignalLedger,
)

REPO = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO / "alembic" / "versions" / "20261115_hk4703_kiwoom_ledger.py"
PARENT_REVISION = "20261113_hk4282_flow_confirm"
REVISION = "20261115_hk4703_kiwoom_ledger"
PROPOSAL_CHECK = "ck_order_proposals_order_proposals_account_mode"

ORDER_CHECKS = {
    "ck_kiwoom_mock_order_ledger_broker",
    "ck_kiwoom_mock_order_ledger_account_mode",
    "ck_kiwoom_mock_order_ledger_market",
    "ck_kiwoom_mock_order_ledger_instrument_type",
    "ck_kiwoom_mock_order_ledger_currency",
    "ck_kiwoom_mock_order_ledger_account_market_currency",
    "ck_kiwoom_mock_order_ledger_operation_kind",
    "ck_kiwoom_mock_order_ledger_side",
    "ck_kiwoom_mock_order_ledger_order_type",
    "ck_kiwoom_mock_order_ledger_kr_limit_only",
    "ck_kiwoom_mock_order_ledger_status",
    "ck_kiwoom_mock_order_ledger_accepted_has_broker_order_id",
}
SIGNAL_CHECKS = {
    "ck_kiwoom_mock_signal_ledger_account_mode",
    "ck_kiwoom_mock_signal_ledger_decision",
    "ck_kiwoom_mock_signal_ledger_outcome_state",
    "ck_kiwoom_mock_signal_ledger_side",
    "ck_kiwoom_mock_signal_ledger_strategy_nonblank",
    "ck_kiwoom_mock_signal_ledger_source_nonblank",
    "ck_kiwoom_mock_signal_ledger_correlation_nonblank",
}


def _load_migration():
    spec = importlib.util.spec_from_file_location("hk4703_migration", MIGRATION_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _string_constants(tree: ast.AST) -> set[str]:
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _op_method_names(tree: ast.AST) -> set[str]:
    return {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "op"
    }


def test_revision_chains_from_the_single_head_and_fits_version_table() -> None:
    migration = _load_migration()
    assert migration.revision == REVISION
    assert migration.down_revision == PARENT_REVISION
    # alembic_version.version_num is VARCHAR(32) on production databases.
    assert len(REVISION) <= 32

    children = []
    for path in sorted((REPO / "alembic" / "versions").glob("*.py")):
        if path.name == MIGRATION_PATH.name:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in tree.body:
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
                name, value = node.target.id, node.value
            elif isinstance(node, ast.Assign) and len(node.targets) == 1:
                target = node.targets[0]
                name = target.id if isinstance(target, ast.Name) else None
                value = node.value
            else:
                continue
            if name != "down_revision" or value is None:
                continue
            parents = ast.literal_eval(value)
            if isinstance(parents, str):
                parents = [parents]
            if PARENT_REVISION in (parents or []):
                children.append(path.name)
    assert children == [], (
        f"a second migration also revises {PARENT_REVISION}: {children}"
    )


def test_migration_uses_only_additive_or_reversible_operations() -> None:
    tree = ast.parse(MIGRATION_PATH.read_text(encoding="utf-8"))
    used = _op_method_names(tree)
    assert used <= {
        "create_table",
        "create_index",
        "create_check_constraint",
        "drop_table",
        "drop_index",
        "execute",
        "f",
    }, used
    # Nothing that could rewrite an existing column or constraint of another table.
    assert not used & {"alter_column", "add_column", "drop_column", "drop_constraint"}


def test_migration_pins_the_orm_canonical_check_names() -> None:
    constants = _string_constants(ast.parse(MIGRATION_PATH.read_text(encoding="utf-8")))
    migration_checks = {
        value for value in constants if value.startswith("ck_kiwoom_mock_")
    }
    assert migration_checks == ORDER_CHECKS | SIGNAL_CHECKS
    assert PROPOSAL_CHECK in constants


def test_orm_check_names_match_the_migration_pins() -> None:
    order_names = {
        c.name
        for c in KiwoomMockOrderLedger.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    signal_names = {
        c.name
        for c in KiwoomMockSignalLedger.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    assert order_names == ORDER_CHECKS
    assert signal_names == SIGNAL_CHECKS


def test_order_proposals_orm_admits_kiwoom_mock_and_not_kiwoom_mock_us() -> None:
    checks = {
        c.name: str(c.sqltext)
        for c in OrderProposal.__table__.constraints
        if isinstance(c, CheckConstraint)
    }
    definition = checks[PROPOSAL_CHECK]
    assert "'kiwoom_mock'" in definition
    assert "kiwoom_mock_us" not in definition
    for mode in ("kis_live", "kis_mock", "toss_live", "upbit", "db_simulated"):
        assert f"'{mode}'" in definition


def test_status_vocabulary_matches_the_migration() -> None:
    migration = _load_migration()
    assert tuple(KIWOOM_MOCK_ORDER_STATUSES) == tuple(migration._STATUSES)
    assert tuple(KIWOOM_MOCK_TERMINAL_STATUSES) == tuple(migration._TERMINAL_STATUSES)
    assert "uncertain" in KIWOOM_MOCK_ORDER_STATUSES
    assert "uncertain" not in KIWOOM_MOCK_TERMINAL_STATUSES


def test_orm_declares_the_partial_open_operation_unique_index() -> None:
    indexes = {index.name: index for index in KiwoomMockOrderLedger.__table__.indexes}
    index = indexes["uq_kiwoom_mock_order_ledger_open_operation"]
    assert index.unique is True
    assert [column.name for column in index.columns] == ["original_order_id"]
    where = str(index.dialect_options["postgresql"]["where"])
    assert "operation_kind IN ('modify','cancel')" in where
    for terminal in KIWOOM_MOCK_TERMINAL_STATUSES:
        assert f"'{terminal}'" in where
