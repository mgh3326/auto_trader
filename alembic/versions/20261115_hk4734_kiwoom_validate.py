"""hk 4734 (3797 T-P4): validate the widened broker CHECKs (contract step).

Revision ID: 20261115_hk4734_kiwoom_validate
Revises: 20261115_hk4734_kiwoom_broker
Create Date: 2026-11-15

The parent revision adds both broker CHECKs as **NOT VALID**, so its
``ALTER TABLE`` only changed the catalog. This revision runs the scan that
makes them enforced constraints, in a lock that permits ordinary reads and
writes (``VALIDATE CONSTRAINT`` takes SHARE UPDATE EXCLUSIVE, not ACCESS
EXCLUSIVE). Both constraints are named exactly as the parent created them.

Downgrade is a no-op: PostgreSQL has no way to put a validated CHECK back into
``NOT VALID`` state, and the parent revision's downgrade drops both constraints
outright before it restores the previous vocabulary. Re-adding them as
NOT VALID here would cost another lock and another scan for no state change.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261115_hk4734_kiwoom_validate"
down_revision: str | Sequence[str] | None = "20261115_hk4734_kiwoom_broker"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Validate both broker CHECKs added NOT VALID by the parent revision."""
    # expand-contract: contract-step 4734
    op.execute(
        "ALTER TABLE review.execution_ledger VALIDATE CONSTRAINT "
        "ck_execution_ledger_execution_ledger_broker"
    )
    # expand-contract: contract-step 4734
    op.execute(
        "ALTER TABLE review.execution_ledger_reconcile_runs VALIDATE CONSTRAINT "
        "ck_execution_ledger_reconcile_runs_runs_broker"
    )


def downgrade() -> None:
    """Nothing to undo: a validated CHECK cannot be returned to NOT VALID."""
    pass
