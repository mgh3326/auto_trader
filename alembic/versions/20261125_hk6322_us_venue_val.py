"""hk 6322: validate the Toss venue CHECK (follow-up revision).

Revision ID: 20261125_hk6322_us_venue_val
Revises: 20261125_hk6322_us_toss_venue
Create Date: 2026-11-25

Runs ``VALIDATE CONSTRAINT`` on ``ck_research_us_candles_1m_venue`` after the
NOT VALID expand step.

Lock behaviour, stated plainly: ``alembic/env.py`` runs every pending revision in
ONE transaction (no ``transaction_per_migration``). So the scan below runs while
the expand revision's ACCESS EXCLUSIVE locks (ADD CONSTRAINT, DROP NOT NULL) are
still held. That is accepted for this table: ``research.us_candles_1m`` was
created one revision earlier by hk 6145, holds at most a few days of rows, and
its only writer (the session-minutes collector) is an unregistered DAG copy, so
nothing is blocked in practice. Making each revision commit on its own is a
runner-level change for every migration and is tracked as a separate task.

Downgrade is a no-op: the validated constraint is the one the previous revision's
downgrade replaces.
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
    op.execute(
        f"ALTER TABLE research.us_candles_1m VALIDATE CONSTRAINT {US_VENUE_CHECK}"
    )


def downgrade() -> None:
    pass
