"""Merge the independent Toss corpus and KR-B0 mock-runner migration heads.

No schema object is created or changed here.  The merge preserves the already
applied Toss history instead of rewriting its parent after a newer main-branch
migration landed.
"""

from __future__ import annotations

from collections.abc import Sequence

revision: str = "20260805_toss_merge"
down_revision: str | Sequence[str] | None = (
    "20260804_toss_phase2",
    "20260805_kis_mock_runner",
)
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Join histories only; each parent owns its own additive schema changes."""


def downgrade() -> None:
    """Split histories only; no schema object belongs to this merge revision."""
