"""hk 7037: validate the KIS_DAY venue CHECK (follow-up revision).

Revision ID: 20261127_hk7037_us_kis_day_val
Revises: 20261127_hk7037_us_kis_day
Create Date: 2026-11-27

Runs ``VALIDATE CONSTRAINT`` on ``ck_research_us_candles_1m_venue`` after the NOT
VALID expand step. Same lock behaviour as ``20261125_hk6322_us_venue_val``: the
runner executes pending revisions in one transaction, and the table is small with
one unregistered writer (the session-minutes DAG copy), so the scan is accepted.

Downgrade is a no-op: the previous revision's downgrade replaces the constraint.
"""

# expand-contract: contract-step 7037
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261127_hk7037_us_kis_day_val"
down_revision: str | Sequence[str] | None = "20261127_hk7037_us_kis_day"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

US_VENUE_CHECK = "ck_research_us_candles_1m_venue"


def upgrade() -> None:
    op.execute(
        f"ALTER TABLE research.us_candles_1m VALIDATE CONSTRAINT {US_VENUE_CHECK}"
    )


def downgrade() -> None:
    pass
