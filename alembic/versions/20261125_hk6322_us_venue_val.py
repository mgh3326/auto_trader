"""hk 6322: validate the Toss venue CHECK (follow-up revision).

Revision ID: 20261125_hk6322_us_venue_val
Revises: 20261125_hk6322_us_toss_venue
Create Date: 2026-11-25

Runs ``VALIDATE CONSTRAINT`` on ``ck_research_us_candles_1m_venue`` after the
NOT VALID expand step. The validation scans the table under SHARE UPDATE
EXCLUSIVE only, so writes continue. Downgrade is a no-op: the validated constraint
is the one the previous revision's downgrade replaces.
"""

# expand-contract: contract-step 6322
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261125_hk6322_us_venue_val"
down_revision: str | Sequence[str] | None = "20261125_hk6322_us_toss_venue"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

US_VENUE_CHECK = "ck_research_us_candles_1m_venue"


def upgrade() -> None:
    # alembic/env.py sets transaction_per_migration: the expand revision has already
    # committed, so its ACCESS EXCLUSIVE locks are gone and this scan takes only
    # SHARE UPDATE EXCLUSIVE (hk 6322, review r3).
    op.execute(
        f"ALTER TABLE research.us_candles_1m VALIDATE CONSTRAINT {US_VENUE_CHECK}"
    )


def downgrade() -> None:
    pass
