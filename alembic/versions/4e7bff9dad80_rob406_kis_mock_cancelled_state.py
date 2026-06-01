"""rob406 kis_mock cancelled state

Revision ID: 4e7bff9dad80
Revises: 14fa36b85d0a
Create Date: 2026-06-01 17:10:38.250007

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4e7bff9dad80'
down_revision: Union[str, Sequence[str], None] = '14fa36b85d0a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


_OLD = (
    "'planned','previewed','submitted','accepted','pending','fill',"
    "'reconciled','stale','failed','anomaly'"
)
_NEW = (
    "'planned','previewed','submitted','accepted','pending','fill',"
    "'reconciled','stale','failed','anomaly','cancelled'"
)
_NAME = "kis_mock_ledger_lifecycle_state_allowed"
_TABLE = "kis_mock_order_ledger"
_SCHEMA = "review"


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
