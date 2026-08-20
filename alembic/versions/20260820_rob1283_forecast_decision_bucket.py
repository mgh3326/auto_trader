"""ROB-1283 negative-class decision_bucket on trade_forecasts (additive)

Revision ID: 20260820_rob1283_bucket
Revises: 20260819_rob1286_claims
Create Date: 2026-08-20

Additive: one nullable column, one CHECK, one index on
``review.trade_forecasts``. No existing column, constraint or index is
altered, no row is rewritten, and no other table is touched.

Why this column exists (see ROB-1283 root cause): the report-item path that
used to carry ``decision_bucket`` stopped being called on 2026-06-15, while
``trade_forecasts`` has recorded continuously since 2026-07-03. The two
surfaces never overlapped, which is why every ``report_uuid`` link is NULL.
Putting the vocabulary on the surface sessions actually call makes a rejected
buy candidate a structured, scorable row in one call instead of a regex proxy
over free text.

NULL is the pre-existing state for every historical row and means "not
classified" -- never "not a rejection". This migration deliberately does NOT
backfill: the 06-15 -> 07-03 gap is reported as a gap by
``scripts/diagnose_negative_class_recording.py`` rather than papered over with
inferred values.

The CHECK is emitted as explicit DDL rather than via
``op.create_check_constraint``. That helper re-applies the metadata naming
convention to whatever name it is handed, so passing the ORM's rendered name
yields a doubly-mangled, hash-truncated
``ck_trade_forecasts_ck_trade_forecasts_ck_trade_forecast_54f4`` -- which then
does not match the ORM and cannot be found by ``downgrade``. Writing the SQL
directly pins the exact name the ORM renders
(``ck_%(table_name)s_%(constraint_name)s`` applied to a constraint already
named ``ck_trade_forecasts_...``, hence the doubled prefix). The four
constraints already deployed on this table carry that same doubled prefix, so
this matches the table it is altering instead of introducing drift.

Provenance: hand-written against the deployed schema, then verified by a full
``upgrade head`` / ``downgrade -1`` / ``upgrade head`` round trip on a
run-owned scratch database.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op
from app.models.decision_vocabulary import DECISION_BUCKETS, sql_in_list

# revision identifiers, used by Alembic.
revision: str = "20260820_rob1283_bucket"
down_revision: str | Sequence[str] | None = "20260819_rob1286_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_CHECK_NAME = "ck_trade_forecasts_ck_trade_forecasts_decision_bucket"
_INDEX_NAME = "ix_trade_forecasts_decision_bucket"
_IN_LIST = sql_in_list(DECISION_BUCKETS)


def upgrade() -> None:
    """Add review.trade_forecasts.decision_bucket (nullable, CHECKed, indexed)."""
    op.add_column(
        "trade_forecasts",
        sa.Column("decision_bucket", sa.Text(), nullable=True),
        schema="review",
    )
    op.execute(
        f"ALTER TABLE review.trade_forecasts ADD CONSTRAINT {_CHECK_NAME} "
        f"CHECK (decision_bucket IS NULL OR decision_bucket IN ({_IN_LIST}))"
    )
    op.create_index(
        _INDEX_NAME,
        "trade_forecasts",
        ["decision_bucket"],
        schema="review",
    )


def downgrade() -> None:
    """Drop the column and everything created with it."""
    op.drop_index(_INDEX_NAME, table_name="trade_forecasts", schema="review")
    op.execute(f"ALTER TABLE review.trade_forecasts DROP CONSTRAINT {_CHECK_NAME}")
    op.drop_column("trade_forecasts", "decision_bucket", schema="review")
