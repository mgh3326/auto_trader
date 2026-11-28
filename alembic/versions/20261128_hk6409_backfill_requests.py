"""hk 6409: append-only review.market_event_backfill_requests.

Additive. One new table for bounded official-event backfill requests (design
6400 §3.2). UPDATE, DELETE and TRUNCATE are rejected by triggers for every role.
``at_app`` receives SELECT and INSERT on the table and its id sequence only
when the role exists (hard rule 18). ``downgrade`` refuses while any row exists.
"""

# expand-contract: contract-step 6409

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261128_hk6409_backfill_req"
down_revision: str | Sequence[str] | None = "20261127_hk7092_run_reasons"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SCHEMA = "review"
TABLE = "market_event_backfill_requests"
TRIGGER_FUNCTION = "market_event_backfill_requests_reject_mutation"

RESULT_CHECK = """
(state IN ('requested', 'started') AND exit_code IS NULL AND failure_code IS NULL)
OR (state = 'done' AND exit_code IS NOT NULL AND exit_code = 0 AND failure_code IS NULL)
OR (state = 'failed' AND failure_code IS NOT NULL AND (
    (failure_code = 'exit_nonzero' AND exit_code IS NOT NULL AND exit_code <> 0)
    OR (failure_code IN ('timeout', 'spawn_failed') AND exit_code IS NULL)
))
"""

# Idempotent guard DDL. The test schema bootstrap reads this tuple (one source).
GUARD_DDL: tuple[str, ...] = (
    f"""CREATE OR REPLACE FUNCTION {SCHEMA}.{TRIGGER_FUNCTION}()
    RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
        RAISE EXCEPTION '% is append-only', TG_TABLE_NAME
            USING ERRCODE = 'restrict_violation';
    END
    $$""",
    f"DROP TRIGGER IF EXISTS trg_{TABLE}_append_only ON {SCHEMA}.{TABLE}",
    f"CREATE TRIGGER trg_{TABLE}_append_only "
    f"BEFORE UPDATE OR DELETE ON {SCHEMA}.{TABLE} "
    f"FOR EACH ROW EXECUTE FUNCTION {SCHEMA}.{TRIGGER_FUNCTION}()",
    f"DROP TRIGGER IF EXISTS trg_{TABLE}_no_truncate ON {SCHEMA}.{TABLE}",
    f"CREATE TRIGGER trg_{TABLE}_no_truncate BEFORE TRUNCATE ON {SCHEMA}.{TABLE} "
    f"FOR EACH STATEMENT EXECUTE FUNCTION {SCHEMA}.{TRIGGER_FUNCTION}()",
)
# Stage-4 role may not exist in development or CI databases.
GRANT_DDL: tuple[str, ...] = (
    f"""DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
    GRANT USAGE ON SCHEMA {SCHEMA} TO at_app;
    GRANT SELECT, INSERT ON {SCHEMA}.{TABLE} TO at_app;
    GRANT USAGE, SELECT ON SEQUENCE {SCHEMA}.{TABLE}_id_seq TO at_app;
    END IF; END $$""",
)


def upgrade() -> None:
    op.create_table(
        TABLE,
        sa.Column("id", sa.BigInteger(), primary_key=True, nullable=False),
        sa.Column("request_ref", sa.Text(), nullable=False),
        sa.Column("request_hash", sa.Text(), nullable=False),
        sa.Column("source", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("category", sa.Text(), nullable=False),
        sa.Column(
            "symbols",
            postgresql.ARRAY(sa.Text()),
            nullable=False,
            server_default=sa.text("'{}'::text[]"),
        ),
        sa.Column("from_date", sa.Date(), nullable=False),
        sa.Column("to_date", sa.Date(), nullable=False),
        sa.Column("reason_code", sa.Text(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("failure_code", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            nullable=False,
            server_default=sa.text("clock_timestamp()"),
        ),
        sa.UniqueConstraint(
            "request_hash", "state", name="uq_market_event_backfill_requests_state"
        ),
        sa.CheckConstraint(
            "state IN ('requested', 'started', 'done', 'failed')", name="state"
        ),
        sa.CheckConstraint(
            "from_date <= to_date AND to_date - from_date < 31", name="window"
        ),
        sa.CheckConstraint("cardinality(symbols) <= 20", name="symbols_bound"),
        sa.CheckConstraint("request_hash ~ '^[0-9a-f]{64}$'", name="request_hash"),
        sa.CheckConstraint(
            "btrim(reason) <> '' AND btrim(requested_by) <> ''", name="audit_nonblank"
        ),
        sa.CheckConstraint(RESULT_CHECK, name="result"),
        schema=SCHEMA,
    )
    op.create_index(
        "ix_market_event_backfill_requests_quota",
        TABLE,
        ["source", "state", "created_at"],
        schema=SCHEMA,
    )
    # expand-contract: contract-step 6409
    for statement in GUARD_DDL:
        op.execute(statement)
    # expand-contract: contract-step 6409
    for statement in GRANT_DDL:
        op.execute(statement)


def downgrade() -> None:
    connection = op.get_bind()
    connection.execute(
        sa.text(f"LOCK TABLE {SCHEMA}.{TABLE} IN ACCESS EXCLUSIVE MODE")
    )
    count = connection.execute(
        sa.text(f"SELECT count(*) FROM {SCHEMA}.{TABLE}")
    ).scalar_one()
    if count:
        raise RuntimeError(f"refusing to downgrade: {TABLE} contains records")
    # expand-contract: contract-step 6409
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_no_truncate ON {SCHEMA}.{TABLE}")
    # expand-contract: contract-step 6409
    op.execute(f"DROP TRIGGER IF EXISTS trg_{TABLE}_append_only ON {SCHEMA}.{TABLE}")
    op.drop_table(TABLE, schema=SCHEMA)
    # expand-contract: contract-step 6409
    op.execute(f"DROP FUNCTION IF EXISTS {SCHEMA}.{TRIGGER_FUNCTION}()")
