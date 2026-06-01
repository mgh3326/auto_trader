"""rob403 watch conditions zone

Revision ID: c6358fdef6fd
Revises: 14fa36b85d0a
Create Date: 2026-06-01 17:51:00.028880

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = 'c6358fdef6fd'
down_revision: Union[str, Sequence[str], None] = '14fa36b85d0a'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

_S = "review"
_T = "investment_watch_alerts"
_OP_NAME = "ck_investment_watch_alerts_operator"
_COMBINE_NAME = "ck_investment_watch_alerts_combine"
_ET = "investment_watch_events"
_EV_OP_NAME = "ck_investment_watch_events_operator"


def upgrade() -> None:
    op.add_column(
        _T,
        sa.Column("threshold_high", sa.Numeric(20, 8), nullable=True),
        schema=_S,
    )
    op.add_column(
        _T,
        sa.Column(
            "conditions",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default=sa.text("'[]'::jsonb"),
        ),
        schema=_S,
    )
    op.add_column(
        _T,
        sa.Column(
            "combine",
            sa.Text(),
            nullable=False,
            server_default=sa.text("'and'"),
        ),
        schema=_S,
    )
    op.drop_constraint(_OP_NAME, _T, schema=_S, type_="check")
    op.create_check_constraint(
        _OP_NAME, _T, "operator IN ('above','below','between')", schema=_S
    )
    op.create_check_constraint(
        _COMBINE_NAME, _T, "combine IN ('and')", schema=_S
    )
    # events: between + threshold_high
    op.add_column(
        _ET,
        sa.Column("threshold_high", sa.Numeric(20, 8), nullable=True),
        schema=_S,
    )
    op.drop_constraint(_EV_OP_NAME, _ET, schema=_S, type_="check")
    op.create_check_constraint(
        _EV_OP_NAME, _ET, "operator IN ('above','below','between')", schema=_S
    )


def downgrade() -> None:
    op.drop_constraint(_EV_OP_NAME, _ET, schema=_S, type_="check")
    op.create_check_constraint(
        _EV_OP_NAME, _ET, "operator IN ('above','below')", schema=_S
    )
    op.drop_column(_ET, "threshold_high", schema=_S)
    op.drop_constraint(_COMBINE_NAME, _T, schema=_S, type_="check")
    op.drop_constraint(_OP_NAME, _T, schema=_S, type_="check")
    op.create_check_constraint(
        _OP_NAME, _T, "operator IN ('above','below')", schema=_S
    )
    op.drop_column(_T, "combine", schema=_S)
    op.drop_column(_T, "conditions", schema=_S)
    op.drop_column(_T, "threshold_high", schema=_S)
