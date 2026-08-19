from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
MIGRATION = ROOT / "alembic/versions/20260815_external_cash.py"
ADVISORY_MIGRATION = ROOT / "alembic/versions/20260815_funding_advisory.py"


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


def test_advisory_migration_is_stacked_additive_ddl_without_data() -> None:
    text = ADVISORY_MIGRATION.read_text(encoding="utf-8")
    upgrade = text.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    assert (
        'down_revision: str | Sequence[str] | None = "20260815_external_cash"' in text
    )
    for table in (
        "funding_advisories",
        "funding_advisory_revisions",
        "funding_advisory_deliveries",
        "funding_advisory_proposal_links",
    ):
        assert table in upgrade
    assert "fk_funding_delivery_revision" in upgrade
    assert "INSERT INTO" not in upgrade.upper()
    assert "op.bulk_insert" not in upgrade
    assert "640000" not in upgrade


def test_advisory_evidence_and_provenance_tables_are_append_only() -> None:
    text = ADVISORY_MIGRATION.read_text(encoding="utf-8")
    upgrade = text.split("def upgrade()", 1)[1].split("def downgrade()", 1)[0]
    for table in (
        "funding_advisory_revisions",
        "funding_advisory_proposal_links",
    ):
        assert f'"{table}",' in upgrade
    assert 'f"CREATE TRIGGER trg_{table}_append_only "' in upgrade
    assert 'f"CREATE TRIGGER trg_{table}_truncate_append_only "' in upgrade
    assert 'f"REVOKE UPDATE, DELETE, TRUNCATE ON review.{table} FROM PUBLIC"' in upgrade
