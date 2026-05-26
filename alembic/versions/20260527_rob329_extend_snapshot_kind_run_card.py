"""ROB-329 — add 'validated_run_card' to investment_snapshots.snapshot_kind CHECK.

Revision ID: 20260527_rob329
Revises: 20260526_rob321_p4a
Create Date: 2026-05-27

Pure CHECK extension. No data backfill, no new column. The run-card ingest
(``app.services.investment_snapshots.run_card_ingest``) emits rows with
snapshot_kind='validated_run_card'; existing rows are unaffected.

This mirrors 20260520_rob274_p2 (which added 'pending_orders'): the ROB-269
foundation migration wrote this CHECK under the convention-expanded name
``ck_investment_snapshots_ck_investment_snapshots_snapshot_kind``, so we drop
both the canonical and expanded identifiers defensively before recreating.

Operator-gated: ships in the PR but is applied separately via
``alembic upgrade head`` (no auto-apply, no production backfill).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260527_rob329"
down_revision: str | None = "20260526_rob321_p4a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Canonical CHECK constraint name supplied to op.create_check_constraint.
_SNAPSHOT_KIND_CHECK = "ck_investment_snapshots_snapshot_kind"
# Convention-expanded (``ck_%(table_name)s_%(constraint_name)s``) form that
# the ROB-269 foundation migration actually wrote to disk.
_SNAPSHOT_KIND_EXPANDED = (
    "ck_investment_snapshots_ck_investment_snapshots_snapshot_kind"
)

# State after 20260520_rob274_p2 (12 foundation kinds + 'pending_orders').
_OLD_KINDS = (
    "'portfolio','market','news','symbol',"
    "'candidate_universe','browser_probe','invest_page','journal',"
    "'watch_context','naver_remote_debug','toss_remote_debug',"
    "'llm_input_frozen','pending_orders'"
)
_NEW_KINDS = _OLD_KINDS + ",'validated_run_card'"


def _drop_snapshot_kind_check_if_exists() -> None:
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
