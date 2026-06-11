"""Merge ROB-516 session context and ROB-512 symbol sector heads.

Revision ID: 20260611_rob516_rob512_merge
Revises: 20260611_rob516, rob512_symbol_sectors
Create Date: 2026-06-11 14:30:00.000000
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "20260611_rob516_rob512_merge"
down_revision: str | Sequence[str] | None = (
    "20260611_rob516",
    "rob512_symbol_sectors",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
