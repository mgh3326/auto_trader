from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "alembic/versions/20260815_external_cash.py"


def test_external_cash_migration_is_additive_ddl_without_business_seed() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    upgrade = text.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert 'down_revision: str | Sequence[str] | None = "20260814_lcapprove_b1"' in text
    assert "external_cash_declarations" in upgrade
    assert "CREATE TRIGGER trg_external_cash_append_only" in upgrade
    assert "CREATE TRIGGER trg_external_cash_truncate_append_only" in upgrade
    assert "INSERT INTO" not in upgrade.upper()
    assert "op.bulk_insert" not in upgrade
    assert "640000" not in upgrade


def test_migration_downgrade_does_not_recreate_a_declaration() -> None:
    text = MIGRATION.read_text(encoding="utf-8")
    downgrade = text.split("def downgrade()", 1)[1]
    assert 'op.drop_table("external_cash_declarations", schema="review")' in downgrade
    assert "INSERT INTO" not in downgrade.upper()
    assert "op.bulk_insert" not in downgrade
