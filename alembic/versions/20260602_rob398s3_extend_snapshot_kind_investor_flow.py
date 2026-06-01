"""ROB-398 Slice 3 — add 'investor_flow' to investment_snapshots.snapshot_kind CHECK.

Pure additive CHECK extension. investor_flow collector emits rows with this kind;
existing rows unaffected. Mirrors 20260527_rob329 / 20260602_rob398s2.
Operator-gated: applied via ``alembic upgrade head``.

Merges two existing heads (20260602_rob398s2, 91097f38827e) into a single head.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260602_rob398s3"
down_revision: str | Sequence[str] | None = ("20260602_rob398s2", "91097f38827e")
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
    "'llm_input_frozen','pending_orders','validated_run_card',"
    "'kr_market_ranking'"
)
_NEW_KINDS = _OLD_KINDS + ",'investor_flow'"


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
