"""ROB-816 proposal loss-cut binding columns (review schema, additive).

Revision ID: 20260711_rob816_exit_binding
Revises: 20260710_rob816_order_proposals
Create Date: 2026-07-11
"""
from __future__ import annotations

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

revision: str = "20260711_rob816_exit_binding"
down_revision: Union[str, Sequence[str], None] = "20260710_rob816_order_proposals"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("order_proposals", sa.Column("exit_intent", sa.Text(), nullable=True), schema="review")
    op.add_column("order_proposals", sa.Column("exit_reason", sa.Text(), nullable=True), schema="review")
    op.add_column("order_proposals", sa.Column("retrospective_id", sa.BigInteger(), nullable=True), schema="review")
    op.add_column("order_proposals", sa.Column("approval_issue_id", sa.Text(), nullable=True), schema="review")


def downgrade() -> None:
    op.drop_column("order_proposals", "approval_issue_id", schema="review")
    op.drop_column("order_proposals", "retrospective_id", schema="review")
    op.drop_column("order_proposals", "exit_reason", schema="review")
    op.drop_column("order_proposals", "exit_intent", schema="review")
