"""hk 4734 (3797 T-P4): admit broker 'kiwoom' in the execution ledger (expand).

Revision ID: 20261115_hk4734_kiwoom_broker
Revises: 20261116_hk4754_us_proposals
Create Date: 2026-11-15

Two additive CHECK widenings only (design docs/design/3797-kiwoom-mock-ledger.md
§2.3, operator decision decision/2026-11-15/at-rule16-kiwoom-broker):

* ``review.execution_ledger.broker`` gains ``'kiwoom'`` — the Kiwoom mock
  fill rows booked by the full reconcile mode.
* ``review.execution_ledger_reconcile_runs.broker`` gains ``'kiwoom'`` — the
  run coverage row the same reconcile writes.

Each constraint is added **NOT VALID**, so PostgreSQL takes its ACCESS
EXCLUSIVE lock only for the catalog change and does not scan the existing
ledger while holding it. The scan is a separate step: the follow-up revision
``20261115_hk4734_kiwoom_broker_validate`` runs ``VALIDATE CONSTRAINT``, which
takes only SHARE UPDATE EXCLUSIVE and does not block ordinary reads or writes.
Do not fold the validation back into this revision.

The drops are unconditional (IF EXISTS) so a re-run after a partial failure
replaces the constraint instead of failing on the duplicate name. Production
databases carry the literal names from 2026_05_13_rob211 and 20260707_rob757,
so every historical spelling is dropped before the widened one is added, and
the widening cannot silently leave a narrow CHECK in place.

Downgrade restores the previous vocabulary exactly as production carried it
(both constraints valid, as before this revision) and refuses while any Kiwoom
row exists: the old CHECK would reject rows the application can no longer write
after a downgrade. It is the emergency path, so it pays the validation scan the
forward path avoids.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261115_hk4734_kiwoom_broker"
down_revision: str | Sequence[str] | None = "20261116_hk4754_us_proposals"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Widen both broker CHECKs without scanning the tables under the lock."""
    # expand-contract: contract-step 4734
    op.execute(
        "ALTER TABLE review.execution_ledger DROP CONSTRAINT IF EXISTS "
        '"ck_execution_ledger_execution_ledger_broker"'
    )
    # expand-contract: contract-step 4734
    op.execute(
        "ALTER TABLE review.execution_ledger DROP CONSTRAINT IF EXISTS "
        '"execution_ledger_broker"'
    )
    # expand-contract: contract-step 4734
    op.execute(
        "ALTER TABLE review.execution_ledger ADD CONSTRAINT "
        "ck_execution_ledger_execution_ledger_broker "
        "CHECK (broker IN ('kis', 'upbit', 'toss', 'kiwoom')) NOT VALID"
    )
    # expand-contract: contract-step 4734
    op.execute(
        "ALTER TABLE review.execution_ledger_reconcile_runs DROP CONSTRAINT "
        'IF EXISTS "ck_execution_ledger_reconcile_runs_runs_broker"'
    )
    # expand-contract: contract-step 4734
    op.execute(
        "ALTER TABLE review.execution_ledger_reconcile_runs DROP CONSTRAINT "
        'IF EXISTS "ck_execution_ledger_reconcile_runs_execution_ledger_runs_broker"'
    )
    # expand-contract: contract-step 4734
    op.execute(
        "ALTER TABLE review.execution_ledger_reconcile_runs DROP CONSTRAINT "
        'IF EXISTS "execution_ledger_runs_broker"'
    )
    # expand-contract: contract-step 4734
    op.execute(
        "ALTER TABLE review.execution_ledger_reconcile_runs ADD CONSTRAINT "
        "ck_execution_ledger_reconcile_runs_runs_broker "
        "CHECK (broker IN ('kis', 'upbit', 'kiwoom')) NOT VALID"
    )


def downgrade() -> None:
    """Restore the previous broker vocabulary (refuses while Kiwoom rows exist)."""
    op.execute(
        """
        DO $$
        BEGIN
            LOCK TABLE review.execution_ledger, review.execution_ledger_reconcile_runs
                IN ACCESS EXCLUSIVE MODE;
            IF EXISTS (
                SELECT 1 FROM review.execution_ledger WHERE broker = 'kiwoom'
            ) OR EXISTS (
                SELECT 1 FROM review.execution_ledger_reconcile_runs
                WHERE broker = 'kiwoom'
            ) THEN
                RAISE EXCEPTION
                    'execution ledger holds kiwoom rows; export before downgrading'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END;
        $$
        """
    )
    op.execute(
        "ALTER TABLE review.execution_ledger_reconcile_runs DROP CONSTRAINT "
        'IF EXISTS "ck_execution_ledger_reconcile_runs_runs_broker"'
    )
    op.execute(
        "ALTER TABLE review.execution_ledger_reconcile_runs DROP CONSTRAINT "
        'IF EXISTS "ck_execution_ledger_reconcile_runs_execution_ledger_runs_broker"'
    )
    op.execute(
        "ALTER TABLE review.execution_ledger_reconcile_runs DROP CONSTRAINT "
        'IF EXISTS "execution_ledger_runs_broker"'
    )
    op.execute(
        "ALTER TABLE review.execution_ledger_reconcile_runs ADD CONSTRAINT "
        "ck_execution_ledger_reconcile_runs_runs_broker "
        "CHECK (broker IN ('kis', 'upbit'))"
    )
    op.execute(
        "ALTER TABLE review.execution_ledger DROP CONSTRAINT IF EXISTS "
        '"ck_execution_ledger_execution_ledger_broker"'
    )
    op.execute(
        "ALTER TABLE review.execution_ledger DROP CONSTRAINT IF EXISTS "
        '"execution_ledger_broker"'
    )
    op.execute(
        "ALTER TABLE review.execution_ledger ADD CONSTRAINT "
        "ck_execution_ledger_execution_ledger_broker "
        "CHECK (broker IN ('kis', 'upbit', 'toss'))"
    )
