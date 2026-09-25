"""Add Toss DAY-expiry terminal status + broker expiry timestamp.

Revision ID: 20260925_task691_toss_expired
Revises: 20260908_task137_ctx_outcomes
Create Date: 2026-09-25

ROB-691 — Toss reports a broker-swept DAY order as REJECTED + canceledAt.
This migration widens the review.toss_live_order_ledger status CHECK with
'expired' and adds expired_at (TIMESTAMPTZ, nullable) so reconcile can record
the broker-provided expiry timestamp. It does not alter an execution ledger,
account table, proposal, watch, token, capability, scheduler, or any existing
safety gate.

Name drift: the ROB-538 migration created the status CHECK under its literal
name ``toss_live_ledger_status``, while ``Base.metadata.create_all`` renders
the same constraint through the ``ck_%(table_name)s_%(constraint_name)s``
naming convention as ``ck_toss_live_order_ledger_toss_live_ledger_status``.
The drop therefore covers both spellings with IF EXISTS, and the recreated
constraint is pinned via ``op.f`` to the ORM-canonical ``ck_`` spelling so
migrated and create_all schemas converge on one name.

Downgrade note: if any row already carries status='expired', the recreated
CHECK will reject it — downgrade only after such rows are resolved.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260925_task691_toss_expired"
down_revision: str | Sequence[str] | None = "20260908_task137_ctx_outcomes"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "toss_live_order_ledger"
_SCHEMA = "review"
_CONSTRAINT = "ck_toss_live_order_ledger_toss_live_ledger_status"
_CONSTRAINT_NAMES = (
    _CONSTRAINT,
    "toss_live_ledger_status",
)
_STATUS_WITH_EXPIRED = (
    "'accepted','rejected','pending','partial','filled','cancelled',"
    "'replaced','cancel_rejected','replace_rejected','anomaly','expired'"
)
_STATUS_WITHOUT_EXPIRED = (
    "'accepted','rejected','pending','partial','filled','cancelled',"
    "'replaced','cancel_rejected','replace_rejected','anomaly'"
)


def _drop_status_check() -> None:
    for name in _CONSTRAINT_NAMES:
        op.execute(
            f'ALTER TABLE review.toss_live_order_ledger '
            f'DROP CONSTRAINT IF EXISTS "{name}"'
        )


def upgrade() -> None:
    op.add_column(
        _TABLE,
        sa.Column("expired_at", sa.TIMESTAMP(timezone=True), nullable=True),
        schema=_SCHEMA,
    )
    _drop_status_check()
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        _TABLE,
        f"status IN ({_STATUS_WITH_EXPIRED})",
        schema=_SCHEMA,
    )


def downgrade() -> None:
    _drop_status_check()
    op.create_check_constraint(
        op.f(_CONSTRAINT),
        _TABLE,
        f"status IN ({_STATUS_WITHOUT_EXPIRED})",
        schema=_SCHEMA,
    )
    op.drop_column(_TABLE, "expired_at", schema=_SCHEMA)
