"""Merge ROB-534 Toss symbol master and ROB-538 Toss live ledger heads.

Revision ID: 20260612_rob534_rob538_merge
Revises: 20260612_rob534, 20260612_rob538_toss_ledger
Create Date: 2026-06-12 17:00:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "20260612_rob534_rob538_merge"
down_revision: str | Sequence[str] | None = (
    "20260612_rob534",
    "20260612_rob538_toss_ledger",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
