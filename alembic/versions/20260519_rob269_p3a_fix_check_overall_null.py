"""rob-269 phase 3 follow-up: tighten ck_investment_reports_no_published_on_hard_stale NULL semantics

Revision ID: 20260519_rob269_p3a
Revises: 20260519_rob269_p3
Create Date: 2026-05-19

The original Phase 3 CHECK accepted ``published`` rows whose
``snapshot_freshness_summary`` was set but missing an ``overall`` key (or
had ``overall`` explicitly null) because PostgreSQL CHECK accepts UNKNOWN
results: the IN comparison against NULL returned NULL, which short-
circuited the OR chain to NULL → CHECK accepts.

The corrected form requires either ``snapshot_freshness_summary IS NULL``
(legacy bypass) OR a definitively non-NULL ``overall`` whose value is in
the allow-set. The ``IS NOT NULL`` guard collapses missing-key and
JSON-null cases to FALSE (reject), no UNKNOWN.

Drop + recreate is idempotent (PostgreSQL has no ``ALTER CONSTRAINT`` for
CHECK predicates without IF EXISTS gymnastics).
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260519_rob269_p3a"
down_revision: str | None = "20260519_rob269_p3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CHECK_NAME = "ck_investment_reports_no_published_on_hard_stale"

# Old (Phase 3 initial) predicate — kept as the downgrade target.
_OLD_PREDICATE = (
    "status <> 'published' "
    "OR snapshot_freshness_summary IS NULL "
    "OR (snapshot_freshness_summary->>'overall') IN ('fresh','soft_stale','partial')"
)

# New (corrected) predicate — explicit IS NOT NULL guard short-circuits the
# OR chain to FALSE when ``overall`` is missing or JSON-null.
_NEW_PREDICATE = (
    "status <> 'published' "
    "OR snapshot_freshness_summary IS NULL "
    "OR ("
    "(snapshot_freshness_summary->>'overall') IS NOT NULL "
    "AND (snapshot_freshness_summary->>'overall') IN "
    "('fresh','soft_stale','partial')"
    ")"
)


def _drop_check_if_exists() -> None:
    # Production schemas may have the Phase 3 metadata columns but be missing
    # the original check constraint (for example after fixture/manual drift).
    # Keep this migration tolerant so it can still converge by creating the
    # corrected constraint below.
    op.execute(
        f"ALTER TABLE review.investment_reports DROP CONSTRAINT IF EXISTS {_CHECK_NAME}"
    )


def upgrade() -> None:
    _drop_check_if_exists()
    op.create_check_constraint(
        _CHECK_NAME, "investment_reports", _NEW_PREDICATE, schema="review"
    )


def downgrade() -> None:
    _drop_check_if_exists()
    op.create_check_constraint(
        _CHECK_NAME, "investment_reports", _OLD_PREDICATE, schema="review"
    )
