"""add crypto_instrument_health

Revision ID: 4facd9697962
Revises: 5fa5a347d85b
Create Date: 2026-05-21 06:18:04.983767

ROB-285 — instrument-level health state for the Binance public adapter.
Lifecycle states: ``healthy`` (default), ``degraded``, ``rate_limited``,
``manual_backfill_required``. All writes via
``app.services.instrument_health.service.CryptoInstrumentHealthService``.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


# revision identifiers, used by Alembic.
revision: str = '4facd9697962'
down_revision: Union[str, Sequence[str], None] = '5fa5a347d85b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "crypto_instrument_health",
        sa.Column(
            "instrument_id",
            sa.BigInteger(),
            sa.ForeignKey(
                "crypto_instruments.id",
                name="fk_crypto_instrument_health_instrument_id_crypto_instruments",
            ),
            primary_key=True,
        ),
        sa.Column(
            "state",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'healthy'"),
        ),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column(
            "last_state_change_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("now()"),
        ),
        sa.Column(
            "last_closed_candle_time", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "attempts", sa.Integer(), nullable=False, server_default=sa.text("0")
        ),
        sa.Column("retry_after_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True
        ),
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
        sa.CheckConstraint(
            "state IN ('healthy','degraded','rate_limited','manual_backfill_required')",
            name="ck_crypto_instrument_health_state",
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table("crypto_instrument_health")
