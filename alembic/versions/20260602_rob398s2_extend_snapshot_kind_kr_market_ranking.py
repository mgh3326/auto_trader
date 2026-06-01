"""ROB-398 Slice 2 — add 'kr_market_ranking' to investment_snapshots.snapshot_kind CHECK.

Pure additive CHECK extension (no data backfill, no new column). The
kr_market_ranking collector emits snapshot rows with this kind; existing rows
are unaffected. Mirrors 20260527_rob329 (validated_run_card).

Operator-gated: ships in the PR, applied separately via ``alembic upgrade head``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260602_rob398s2"
down_revision: str | None = "075d44e505b9"  # alembic heads 의 실제 결과값
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SNAPSHOT_KIND_CHECK = "ck_investment_snapshots_snapshot_kind"
_SNAPSHOT_KIND_EXPANDED = (
    "ck_investment_snapshots_ck_investment_snapshots_snapshot_kind"
)

_OLD_KINDS = (
    "'portfolio','market','news','symbol',"
    "'candidate_universe','browser_probe','invest_page','journal',"
    "'watch_context','naver_remote_debug','toss_remote_debug',"
    "'llm_input_frozen','pending_orders','validated_run_card'"
)
_NEW_KINDS = _OLD_KINDS + ",'kr_market_ranking'"


def _drop_snapshot_kind_check_if_exists() -> None:
    op.execute(
        f'ALTER TABLE review.investment_snapshots DROP CONSTRAINT IF EXISTS "{_SNAPSHOT_KIND_EXPANDED}"'
    )
    op.execute(
        f'ALTER TABLE review.investment_snapshots DROP CONSTRAINT IF EXISTS "{_SNAPSHOT_KIND_CHECK}"'
    )


def upgrade() -> None:
    _drop_snapshot_kind_check_if_exists()
    op.create_check_constraint(
        op.f(_SNAPSHOT_KIND_CHECK),
        "investment_snapshots",
        f"snapshot_kind IN ({_NEW_KINDS})",
        schema="review",
    )


def downgrade() -> None:
    _drop_snapshot_kind_check_if_exists()
    op.create_check_constraint(
        op.f(_SNAPSHOT_KIND_CHECK),
        "investment_snapshots",
        f"snapshot_kind IN ({_OLD_KINDS})",
        schema="review",
    )
