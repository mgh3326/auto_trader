"""Create append-only KR market close summaries.

Revision ID: 20261126_hk6750
Revises: 20261126_hk6757_auction
Create Date: 2026-11-26
"""

# expand-contract: contract-step 6750

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261126_hk6750"
down_revision: str | None = "20261126_hk6757_auction"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "kr_market_close_summaries",
        sa.Column("session_date", sa.Date(), primary_key=True, nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column("facts", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "interpretation", postgresql.JSONB(astext_type=sa.Text()), nullable=False
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
    )
    # expand-contract: contract-step 6750
    op.execute(
        """
        CREATE OR REPLACE FUNCTION public.kr_market_close_summaries_reject_mutation()
        RETURNS trigger LANGUAGE plpgsql AS $$
        BEGIN
            RAISE EXCEPTION 'kr_market_close_summaries is append-only'
                USING ERRCODE = 'restrict_violation';
        END
        $$
        """
    )
    # expand-contract: contract-step 6750
    op.execute(
        "CREATE TRIGGER trg_kr_market_close_summaries_append_only "
        "BEFORE UPDATE OR DELETE ON kr_market_close_summaries "
        "FOR EACH ROW EXECUTE FUNCTION "
        "public.kr_market_close_summaries_reject_mutation()"
    )
    # expand-contract: contract-step 6750
    op.execute(
        "CREATE TRIGGER trg_kr_market_close_summaries_truncate "
        "BEFORE TRUNCATE ON kr_market_close_summaries "
        "FOR EACH STATEMENT EXECUTE FUNCTION "
        "public.kr_market_close_summaries_reject_mutation()"
    )
    # Stage-4 role may not exist in dev/CI databases.
    # expand-contract: contract-step 6750
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        "THEN GRANT SELECT, INSERT ON kr_market_close_summaries TO at_app; "
        "END IF; END $$"
    )


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text("LOCK TABLE public.kr_market_close_summaries IN ACCESS EXCLUSIVE MODE")
    )
    count = connection.execute(
        sa.text("SELECT count(*) FROM public.kr_market_close_summaries")
    ).scalar_one()
    if count:
        raise RuntimeError(
            "refusing to downgrade: kr_market_close_summaries contains records"
        )
    op.execute(
        "DROP TRIGGER trg_kr_market_close_summaries_truncate ON kr_market_close_summaries"
    )
    op.execute(
        "DROP TRIGGER trg_kr_market_close_summaries_append_only ON kr_market_close_summaries"
    )
    op.drop_table("kr_market_close_summaries")
    op.execute("DROP FUNCTION public.kr_market_close_summaries_reject_mutation()")
