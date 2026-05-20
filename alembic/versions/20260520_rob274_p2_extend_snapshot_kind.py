"""ROB-274 — add 'pending_orders' to investment_snapshots.snapshot_kind CHECK.

Revision ID: 20260520_rob274_p2
Revises: 20260520_rob274_p1
Create Date: 2026-05-20

Pure CHECK extension. No data backfill. The new collector emits rows
with snapshot_kind='pending_orders'; existing rows are unaffected.

Note: the project's MetaData naming convention is
``ck_%(table_name)s_%(constraint_name)s`` which double-prefixes any
``ck_<table>_*`` name supplied directly to ``sa.CheckConstraint`` /
``op.create_check_constraint``. The ROB-269 foundation migration created
this CHECK as ``ck_investment_snapshots_snapshot_kind`` inside a
``sa.CheckConstraint`` table-level kwarg, so the on-disk identifier is
the convention-expanded form
``ck_investment_snapshots_ck_investment_snapshots_snapshot_kind``
(61 chars — under PG's 63-char limit, so no hash truncation occurred).
To stay safe across environments where the constraint might exist under
either the canonical or expanded name, we drop both defensively via
``DROP CONSTRAINT IF EXISTS`` and recreate using ``op.f`` so the
re-created constraint lands under the canonical convention-mangled name.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260520_rob274_p2"
down_revision: str | None = "20260520_rob274_p1"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Canonical CHECK constraint name supplied to op.create_check_constraint.
_SNAPSHOT_KIND_CHECK = "ck_investment_snapshots_snapshot_kind"
# Convention-expanded (``ck_%(table_name)s_%(constraint_name)s``) form that
# the ROB-269 foundation migration actually wrote to disk.
_SNAPSHOT_KIND_EXPANDED = (
    "ck_investment_snapshots_ck_investment_snapshots_snapshot_kind"
)

_OLD_KINDS = (
    "'portfolio','market','news','symbol',"
    "'candidate_universe','browser_probe','invest_page','journal',"
    "'watch_context','naver_remote_debug','toss_remote_debug',"
    "'llm_input_frozen'"
)
_NEW_KINDS = _OLD_KINDS + ",'pending_orders'"


def _drop_snapshot_kind_check_if_exists() -> None:
    """Drop the snapshot_kind CHECK whether the on-disk identifier is the
    canonical (op.f-style) form or the convention-expanded form emitted by
    the ROB-269 foundation migration.
    """
    op.execute(
        f'ALTER TABLE review.investment_snapshots DROP CONSTRAINT IF EXISTS "{_SNAPSHOT_KIND_EXPANDED}"'
    )
    op.execute(
        f'ALTER TABLE review.investment_snapshots DROP CONSTRAINT IF EXISTS "{_SNAPSHOT_KIND_CHECK}"'
    )


def upgrade() -> None:
    _drop_snapshot_kind_check_if_exists()
    op.create_check_constraint(
        op.f(_SNAPSHOT_KIND_CHECK),
        "investment_snapshots",
        f"snapshot_kind IN ({_NEW_KINDS})",
        schema="review",
    )


def downgrade() -> None:
    _drop_snapshot_kind_check_if_exists()
    op.create_check_constraint(
        op.f(_SNAPSHOT_KIND_CHECK),
        "investment_snapshots",
        f"snapshot_kind IN ({_OLD_KINDS})",
        schema="review",
    )
