"""add crypto_instruments

Revision ID: 2efa08c3fb09
Revises: 20260520_rob279_p1
Create Date: 2026-05-20 21:46:30.546891

ROB-284 — master/source-of-truth table for venue/product/symbol identity.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '2efa08c3fb09'
down_revision: Union[str, Sequence[str], None] = '20260520_rob279_p1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "crypto_instruments",
        sa.Column("id", sa.BigInteger(), primary_key=True, autoincrement=True),
        sa.Column("venue", sa.Text(), nullable=False),
        sa.Column("product", sa.Text(), nullable=False),
        sa.Column("venue_symbol", sa.Text(), nullable=False),
        sa.Column("base_asset", sa.Text(), nullable=False),
        sa.Column("quote_asset", sa.Text(), nullable=False),
        sa.Column(
            "status",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'active'"),
        ),
        sa.Column("precision_price", sa.Integer(), nullable=True),
        sa.Column("precision_amount", sa.Integer(), nullable=True),
        sa.Column("tick_size", sa.Numeric(), nullable=True),
        sa.Column("lot_size", sa.Numeric(), nullable=True),
        sa.Column("min_notional", sa.Numeric(), nullable=True),
        sa.Column("listed_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("delisted_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.UniqueConstraint(
            "venue", "product", "venue_symbol",
            name="uq_crypto_instruments_venue_product_symbol",
        ),
        sa.CheckConstraint(
            "status IN ('active','delisted','halted')",
            name="ck_crypto_instruments_status",
        ),
    )
    op.create_index(
        "ix_crypto_instruments_venue_product_base",
        "crypto_instruments",
        ["venue", "product", "base_asset"],
    )
    op.create_index(
        "ix_crypto_instruments_base_quote",
        "crypto_instruments",
        ["base_asset", "quote_asset"],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_crypto_instruments_base_quote", table_name="crypto_instruments"
    )
    op.drop_index(
        "ix_crypto_instruments_venue_product_base",
        table_name="crypto_instruments",
    )
    op.drop_table("crypto_instruments")
