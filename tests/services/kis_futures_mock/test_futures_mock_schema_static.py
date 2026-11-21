"""hk 5946 — 마이그레이션과 ORM 이 같은 표를 말하는가 (DB 없음, ast + 메타데이터).

PostgreSQL 왕복·거부 증명은 CI 의 test_futures_mock_ledger_db.py 가 한다.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from sqlalchemy import CheckConstraint, UniqueConstraint

from app.models.kis_futures_mock import (
    FUTURES_MOCK_ORDER_STATUSES,
    FUTURES_MOCK_PRODUCT_CODES,
    KISFuturesMockOrderLedger,
    KISFuturesMockSignalLedger,
)
from app.services.kis_futures_mock import constants as C  # noqa: N812

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "alembic/versions/20261121_hk5946_futures_mock.py"
SOURCE = MIGRATION.read_text(encoding="utf-8")


def ck_names(table) -> set[str]:
    return {c.name for c in table.constraints if isinstance(c, CheckConstraint)}


def migration_ck_names(prefix: str) -> set[str]:
    return set(re.findall(rf'op\.f\("({prefix}[a-z_]+)"\)', SOURCE))


def test_orm_check_names_match_the_migration_spelling():
    order = ck_names(KISFuturesMockOrderLedger.__table__)
    signal = ck_names(KISFuturesMockSignalLedger.__table__)
    assert order == migration_ck_names("ck_kis_futures_mock_order_ledger_")
    assert signal == migration_ck_names("ck_kis_futures_mock_signal_ledger_")
    assert "ck_kis_futures_mock_order_ledger_quantity_locked" in order
    assert "ck_kis_futures_mock_signal_ledger_strategy_nonblank" in signal


def test_unique_constraints_match():
    names = {
        c.name
        for t in (
            KISFuturesMockOrderLedger.__table__,
            KISFuturesMockSignalLedger.__table__,
        )
        for c in t.constraints
        if isinstance(c, UniqueConstraint)
    }
    assert names == set(re.findall(r'name="(uq_kis_futures_mock_[a-z_]+)"', SOURCE))


def test_migration_chain_is_a_single_additive_head_after_mandate_batches():
    tree = ast.parse(SOURCE)
    assigns = {
        t.id: ast.literal_eval(n.value)
        for n in tree.body
        if isinstance(n, (ast.Assign, ast.AnnAssign))
        for t in ([n.target] if isinstance(n, ast.AnnAssign) else n.targets)
        if isinstance(t, ast.Name)
        and isinstance(getattr(n, "value", None), ast.Constant)
    }
    assert assigns["revision"] == "20261121_hk5946_futures_mock"
    assert assigns["down_revision"] == "20261120_hk5648_mandate_batches"
    assert "DROP" not in SOURCE.replace("DOWNGRADE_GUARD", "") or "drop_table" in SOURCE
    # 기존 표를 바꾸지 않는다 (execution_ledger·order_proposals·order_send_intents).
    for table in ("execution_ledger", "order_proposals", "order_send_intents"):
        assert (
            f'"{table}"' not in SOURCE and f"ALTER TABLE review.{table}" not in SOURCE
        )


def test_database_check_repeats_the_locked_constants():
    assert FUTURES_MOCK_PRODUCT_CODES == (C.MINI_KOSPI200.code,)
    assert f"amount <= {int(C.LOCKED_LIMITS.max_contract_notional_krw)}" in SOURCE
    assert f"quantity > 0 AND quantity <= {C.LOCKED_LIMITS.max_order_qty}" in SOURCE
    assert "order_type = 'limit'" in SOURCE
    assert set(FUTURES_MOCK_ORDER_STATUSES) >= {"accepted", "rejected", "uncertain"}


def test_at_app_grant_is_conditional_and_has_no_delete():
    assert "pg_roles WHERE rolname = 'at_app'" in SOURCE
    grant = re.findall(r"GRANT ([A-Z, ]+) ON", SOURCE)
    assert grant and all("DELETE" not in g and "TRUNCATE" not in g for g in grant)


def test_downgrade_refuses_when_rows_exist():
    assert "restrict_violation" in SOURCE and "export before downgrading" in SOURCE
