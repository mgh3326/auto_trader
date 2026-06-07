"""ROB-443 Phase 1 (follow-up): add open_interest / long_short to crypto snapshots.

Additive (nullable), crypto-native derivative columns sourced per-symbol from the
USD-M perp endpoints (open interest + global long/short account ratio). NULL for
Upbit-only coins without a perp (fail-closed). Powers the crypto_oi_surge and
crypto_long_short_skew presets.

Revision ID: 20260607_rob443b
Revises: 20260607_rob443
Create Date: 2026-06-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260607_rob443b"
down_revision = "20260607_rob443"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invest_crypto_screener_snapshots",
        sa.Column("open_interest_usd", sa.Numeric(28, 2), nullable=True),
    )
    op.add_column(
        "invest_crypto_screener_snapshots",
        sa.Column("oi_change_24h", sa.Numeric(10, 4), nullable=True),
    )
    op.add_column(
        "invest_crypto_screener_snapshots",
        sa.Column("long_short_account_ratio", sa.Numeric(10, 4), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invest_crypto_screener_snapshots", "long_short_account_ratio")
    op.drop_column("invest_crypto_screener_snapshots", "oi_change_24h")
    op.drop_column("invest_crypto_screener_snapshots", "open_interest_usd")
