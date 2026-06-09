"""rob405a journal mock roundtrip

Revision ID: d8ed14023ef2
Revises: rob337_rob403_merge_heads
"""

import sqlalchemy as sa

from alembic import op

revision = "d8ed14023ef2"
down_revision = "rob337_rob403_merge_heads"
branch_labels = None
depends_on = None

_S = "review"
_T = "trade_journals"
_C = "trade_journals_account_type"


def upgrade() -> None:
    op.add_column(_T, sa.Column("correlation_id", sa.Text(), nullable=True), schema=_S)
    op.create_index(
        "ix_trade_journals_correlation_id", _T, ["correlation_id"], schema=_S
    )
    op.drop_constraint(_C, _T, schema=_S, type_="check")
    op.create_check_constraint(
        _C, _T, "account_type IN ('live','paper','mock')", schema=_S
    )


def downgrade() -> None:
    op.drop_constraint(_C, _T, schema=_S, type_="check")
    op.create_check_constraint(_C, _T, "account_type IN ('live','paper')", schema=_S)
    op.drop_index("ix_trade_journals_correlation_id", _T, schema=_S)
    op.drop_column(_T, "correlation_id", schema=_S)
