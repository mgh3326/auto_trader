"""ROB-443 Phase 1: add funding_rate to invest_crypto_screener_snapshots.

Additive (nullable): the Binance USD-M perp funding rate (lastFundingRate, a
ratio e.g. 0.0001). A crypto-native sentiment signal with no stock analog. NULL
for Upbit-only coins without a Binance perp (fail-closed). Powers the funding
squeeze/overheated screener presets (follow-up PR). open_interest / long_short
columns are deferred to their own enrichment PR.

Revision ID: 20260607_rob443
Revises: 20260605_rob440
Create Date: 2026-06-07
"""

from __future__ import annotations

import sqlalchemy as sa

from alembic import op

revision = "20260607_rob443"
down_revision = "20260605_rob440"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "invest_crypto_screener_snapshots",
        sa.Column("funding_rate", sa.Numeric(12, 8), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("invest_crypto_screener_snapshots", "funding_rate")
