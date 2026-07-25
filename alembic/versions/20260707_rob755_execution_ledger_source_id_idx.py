"""ROB-755 add execution ledger source/id polling index.

Revision ID: 20260707_rob755_source_id_idx
Revises: 20260706_rob745_codex_created_by
Create Date: 2026-07-07
"""

from __future__ import annotations

from alembic import op

revision = "20260707_rob755_source_id_idx"
down_revision = "20260706_rob745_codex_created_by"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_execution_ledger_source_id",
        "execution_ledger",
        ["source", "id"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_execution_ledger_source_id",
        table_name="execution_ledger",
        schema="review",
    )
