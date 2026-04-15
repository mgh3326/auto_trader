"""add metadata jsonb to trade_journals

Revision ID: 0f2f4afaf556
Revises: 0aa8b1405ef4
Create Date: 2026-04-15 09:52:58.258448

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB


# revision identifiers, used by Alembic.
revision: str = '0f2f4afaf556'
down_revision: Union[str, Sequence[str], None] = '0aa8b1405ef4'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column(
        "trade_journals",
        sa.Column("metadata", JSONB, nullable=True),
        schema="review",
    )


def downgrade() -> None:
    op.drop_column("trade_journals", "metadata", schema="review")
