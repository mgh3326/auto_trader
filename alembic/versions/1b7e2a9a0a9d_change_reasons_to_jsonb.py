"""Change reasons column to JSONB

Revision ID: 1b7e2a9a0a9d
Revises: 7cff05b5aa4d
Create Date: 2025-12-04 12:55:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "1b7e2a9a0a9d"
down_revision: Union[str, Sequence[str], None] = "7cff05b5aa4d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Alter reasons to JSONB while preserving existing JSON text data."""
    op.alter_column(
        "stock_analysis_results",
        "reasons",
        existing_type=sa.Text(),
        type_=postgresql.JSONB(astext_type=sa.Text()),
        existing_nullable=True,
        postgresql_using=(
            "CASE WHEN reasons IS NULL OR trim(reasons) = '' "
            "THEN NULL ELSE reasons::jsonb END"
        ),
    )


def downgrade() -> None:
    """Revert reasons to TEXT."""
    op.alter_column(
        "stock_analysis_results",
        "reasons",
        existing_type=postgresql.JSONB(astext_type=sa.Text()),
        type_=sa.Text(),
        existing_nullable=True,
        postgresql_using="reasons::text",
    )
