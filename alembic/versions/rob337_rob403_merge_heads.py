"""merge rob337_add_watch_recommendation + c6358fdef6fd (ROB-403) heads

Revision ID: rob337_rob403_merge_heads
Revises: rob337_add_watch_recommendation, c6358fdef6fd
Create Date: 2026-06-01

Both revisions branched from 14fa36b85d0a and merged to main in parallel
(ROB-337 Slice 1 watch_recommendation column + ROB-403 watch_conditions
zone), producing two alembic heads. This merge revision unifies them so
the revision graph has a single final head. No schema change.
"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "rob337_rob403_merge_heads"
down_revision: Union[str, Sequence[str], None] = (
    "rob337_add_watch_recommendation",
    "c6358fdef6fd",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """No-op: pure merge of two heads."""


def downgrade() -> None:
    """No-op: pure merge of two heads."""
