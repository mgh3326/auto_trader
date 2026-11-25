"""hk 6510: validate the US-CHECK revisions of the momentum snapshot table.

Revision ID: 20261125_hk6510_us_movers_val
Revises: 20261125_hk6510_us_movers
Create Date: 2026-11-25

Runs ``VALIDATE CONSTRAINT`` on the two CHECKs the expand revision added
``NOT VALID``, so the existing log is scanned without the ADD holding ACCESS
EXCLUSIVE (same pattern as hk 6322). ``alembic/env.py`` runs pending revisions in
one transaction, which keeps the expand revision's lock for the scan; this table
is a small observation log with a single manual writer.

Downgrade is a no-op: the previous revision's downgrade restores the original
``kr``-only constraints.
"""

# expand-contract: contract-step 6510
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261125_hk6510_us_movers_val"
down_revision: str | Sequence[str] | None = "20261125_hk6510_us_movers"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLE = "invest_momentum_event_snapshots"
CHECKS = (
    "ck_invest_momentum_event_snapshots_market",
    "ck_invest_momentum_event_snapshots_source",
)


def upgrade() -> None:
    for name in CHECKS:
        op.execute(f"ALTER TABLE {TABLE} VALIDATE CONSTRAINT {name}")


def downgrade() -> None:
    pass
