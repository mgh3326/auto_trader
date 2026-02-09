"""broker_type enum to varchar

Revision ID: c2f3e4b5d6e7
Revises: 1b7e2a9a0a9d
Create Date: 2025-12-08 00:00:00.000000

"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision: str = "c2f3e4b5d6e7"
down_revision: Union[str, Sequence[str], None] = "1b7e2a9a0a9d"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Convert broker_type from enum to VARCHAR(50)."""
    # Convert enum column to varchar
    op.alter_column(
        "broker_accounts",
        "broker_type",
        existing_type=sa.Enum("kis", "toss", "upbit", name="broker_type"),
        type_=sa.String(50),
        existing_nullable=False,
        postgresql_using="broker_type::text",
    )

    # Drop the old enum type
    op.execute("DROP TYPE IF EXISTS broker_type")


def downgrade() -> None:
    """Revert broker_type from VARCHAR(50) back to enum."""
    # Recreate the enum type
    broker_type_enum = sa.Enum("kis", "toss", "upbit", "samsung", name="broker_type")
    broker_type_enum.create(op.get_bind(), checkfirst=True)

    # Convert varchar back to enum
    op.alter_column(
        "broker_accounts",
        "broker_type",
        existing_type=sa.String(50),
        type_=broker_type_enum,
        existing_nullable=False,
        postgresql_using="broker_type::broker_type",
    )
