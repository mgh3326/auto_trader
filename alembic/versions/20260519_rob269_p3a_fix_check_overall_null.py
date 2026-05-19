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

Drop + recreate is idempotent. Production may already have applied the
Phase 3 revision while missing this check constraint because earlier deploy
attempts or test fixtures managed the table shape independently; the follow-up
must therefore tolerate the old constraint being absent. The original Phase 3
``op.create_check_constraint`` also passed a name that already included the
``ck_investment_reports`` prefix, so SQLAlchemy's naming convention expanded
and truncated it to a generated PostgreSQL name; this migration must drop that
name too before recreating the final constraint with ``op.f``.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260519_rob269_p3a"
down_revision: str | None = "20260519_rob269_p3"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_CHECK_NAME = "ck_investment_reports_no_published_on_hard_stale"

# Phase 3 used ``op.create_check_constraint`` with ``_CHECK_NAME`` under the
# global ``ck_%(table_name)s_%(constraint_name)s`` naming convention. Alembic
# therefore emitted this truncated/hash-suffixed PostgreSQL identifier.
_GENERATED_CHECK_NAME = "ck_investment_reports_ck_investment_reports_no_publishe_b266"
_CHECK_NAMES = (_CHECK_NAME, _GENERATED_CHECK_NAME)

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
    """Drop the stale-gate CHECK if present.

    Alembic's generic ``drop_constraint`` emits ``ALTER TABLE ... DROP
    CONSTRAINT`` without ``IF EXISTS`` on the installed stack, which makes this
    follow-up migration fail on production databases whose schema drift already
    lacks the old check. Use explicit PostgreSQL DDL so the migration remains
    safe for both states.
    """

    for check_name in _CHECK_NAMES:
        op.execute(
            f'ALTER TABLE review.investment_reports DROP CONSTRAINT IF EXISTS "{check_name}"'
        )


def upgrade() -> None:
    _drop_check_if_exists()
    op.create_check_constraint(
        op.f(_CHECK_NAME), "investment_reports", _NEW_PREDICATE, schema="review"
    )


def downgrade() -> None:
    _drop_check_if_exists()
    op.create_check_constraint(
        op.f(_CHECK_NAME), "investment_reports", _OLD_PREDICATE, schema="review"
    )
