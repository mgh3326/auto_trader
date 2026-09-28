"""Allow the distinct KIS mock expired lifecycle state for Q-46.

Revision ID: 20260928_task881_mock_expired
Revises: 20260926_task711_dispatch
"""

from __future__ import annotations

from alembic import op

revision = "20260928_task881_mock_expired"
down_revision = "20260926_task711_dispatch"
branch_labels = None
depends_on = None

_NAME = "kis_mock_ledger_lifecycle_state_allowed"
_TABLE = "kis_mock_order_ledger"
_SCHEMA = "review"
_OLD = (
    "'planned','previewed','submitted','accepted','pending','fill',"
    "'reconciled','stale','failed','anomaly','cancelled'"
)
_NEW = _OLD + ",'expired'"


def upgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _NAME, _TABLE, f"lifecycle_state IN ({_NEW})", schema=_SCHEMA
    )


def downgrade() -> None:
    op.drop_constraint(_NAME, _TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _NAME, _TABLE, f"lifecycle_state IN ({_OLD})", schema=_SCHEMA
    )
