"""ROB-274 — add proposal-state fields to investment_report_items.

Revision ID: 20260520_rob274_p1
Revises: 20260519_rob269_p3a
Create Date: 2026-05-20

Adds 6 nullable columns + rewrites two CHECK constraints so that the
``watch_condition`` / ``valid_until`` invariants only apply to
``operation ∈ {create, modify}``. All additive; existing rows keep
operation=NULL which is treated as legacy/'create' by the frontend.

Note: the project's MetaData naming convention is
``ck_%(table_name)s_%(constraint_name)s`` which double-prefixes any
``ck_<table>_*`` name supplied by an Alembic migration and forces PG
to truncate/hash the identifier. The previous ROB-265 migration created
``ck_investment_report_items_watch_has_condition`` /
``ck_investment_report_items_watch_has_expiry`` using
``op.create_check_constraint`` with the convention-prefixed shape, so
the on-disk identifiers are the hashed forms
``ck_investment_report_items_ck_investment_report_items_w_421e`` and
``..._w_fdaa``. To stay safe across environments where the constraint
might exist under either the canonical or hashed name, we drop both
defensively via ``DROP CONSTRAINT IF EXISTS`` and recreate using
``op.f`` so future migrations can target the convention-rewritten name.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20260520_rob274_p1"
down_revision: str | None = "20260519_rob269_p3a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


# Canonical CHECK constraint names supplied to op.create_check_constraint.
_WATCH_CONDITION_CHECK = "ck_investment_report_items_watch_has_condition"
_WATCH_EXPIRY_CHECK = "ck_investment_report_items_watch_has_expiry"

# Truncated/hashed PostgreSQL identifiers actually present on databases
# where the ROB-265 migration was applied under the naming convention
# ``ck_%(table_name)s_%(constraint_name)s`` (which expands to
# ``ck_investment_report_items_ck_investment_report_items_watch_has_condition``,
# >63 chars → PG truncates and appends a 4-char hash suffix).
_WATCH_CONDITION_HASHED = "ck_investment_report_items_ck_investment_report_items_w_421e"
_WATCH_EXPIRY_HASHED = "ck_investment_report_items_ck_investment_report_items_w_fdaa"


def _drop_watch_check_if_exists(name: str, hashed_name: str) -> None:
    """Drop a watch-invariant CHECK whether the on-disk identifier is the
    canonical (op.f-style) form or the hashed form emitted by older Alembic
    runs under the naming-convention prefix.
    """
    op.execute(
        f'ALTER TABLE review.investment_report_items DROP CONSTRAINT IF EXISTS "{hashed_name}"'
    )
    op.execute(
        f'ALTER TABLE review.investment_report_items DROP CONSTRAINT IF EXISTS "{name}"'
    )
    # op.f-expanded form (ck_<table>_<table>_<name>) that this migration
    # itself uses on the create-side; drop it too so downgrade is symmetric
    # and re-applying upgrade after downgrade does not collide.
    op.execute(
        f"ALTER TABLE review.investment_report_items DROP CONSTRAINT IF EXISTS "
        f'"ck_investment_report_items_{name}"'
    )


def upgrade() -> None:
    # 1) new columns — all nullable, all JSONB except operation (text) and apply_policy (text).
    op.add_column(
        "investment_report_items",
        sa.Column("operation", sa.Text(), nullable=True),
        schema="review",
    )
    op.add_column(
        "investment_report_items",
        sa.Column(
            "target_ref",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )
    op.add_column(
        "investment_report_items",
        sa.Column(
            "current_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )
    op.add_column(
        "investment_report_items",
        sa.Column(
            "proposed_state",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )
    op.add_column(
        "investment_report_items",
        sa.Column(
            "diff",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        schema="review",
    )
    op.add_column(
        "investment_report_items",
        sa.Column("apply_policy", sa.Text(), nullable=True),
        schema="review",
    )

    # 2) operation CHECK — null permitted (legacy rows) plus the 6 lifecycle verbs.
    op.create_check_constraint(
        op.f("ck_investment_report_items_operation"),
        "investment_report_items",
        "operation IS NULL OR operation IN ("
        "'create','modify','cancel','keep','replace','review'"
        ")",
        schema="review",
    )

    # 3) apply_policy CHECK — null permitted; only one accepted value in this PR.
    op.create_check_constraint(
        op.f("ck_investment_report_items_apply_policy"),
        "investment_report_items",
        "apply_policy IS NULL OR apply_policy = 'requires_user_approval'",
        schema="review",
    )

    # 4) Rewrite watch_condition invariant: required only when operation IS NULL
    #    (legacy) OR operation IN ('create','modify'). cancel/keep/review do not
    #    require a new watch_condition.
    _drop_watch_check_if_exists(_WATCH_CONDITION_CHECK, _WATCH_CONDITION_HASHED)
    op.create_check_constraint(
        op.f(_WATCH_CONDITION_CHECK),
        "investment_report_items",
        "item_kind <> 'watch' "
        "OR operation IN ('cancel','keep','review') "
        "OR watch_condition IS NOT NULL",
        schema="review",
    )

    # 5) Same treatment for valid_until — required for create/modify/legacy,
    #    not required for cancel/keep/review (they reference an existing alert
    #    that already has its own validity window).
    _drop_watch_check_if_exists(_WATCH_EXPIRY_CHECK, _WATCH_EXPIRY_HASHED)
    op.create_check_constraint(
        op.f(_WATCH_EXPIRY_CHECK),
        "investment_report_items",
        "item_kind <> 'watch' "
        "OR operation IN ('cancel','keep','review') "
        "OR valid_until IS NOT NULL",
        schema="review",
    )

    # 6) Index by (operation, item_kind, status) for the frontend's
    #    proposal-grouped list query.
    op.create_index(
        "ix_investment_report_items_operation_kind",
        "investment_report_items",
        ["operation", "item_kind", "status"],
        schema="review",
    )


def downgrade() -> None:
    op.drop_index(
        "ix_investment_report_items_operation_kind",
        table_name="investment_report_items",
        schema="review",
    )

    # Restore original strict CHECKs (the form created by ROB-265). We drop
    # whatever this migration installed (hashed or canonical) and recreate
    # the pre-ROB-274 predicate so a future re-upgrade is idempotent.
    _drop_watch_check_if_exists(_WATCH_EXPIRY_CHECK, _WATCH_EXPIRY_HASHED)
    op.create_check_constraint(
        op.f(_WATCH_EXPIRY_CHECK),
        "investment_report_items",
        "item_kind <> 'watch' OR valid_until IS NOT NULL",
        schema="review",
    )

    _drop_watch_check_if_exists(_WATCH_CONDITION_CHECK, _WATCH_CONDITION_HASHED)
    op.create_check_constraint(
        op.f(_WATCH_CONDITION_CHECK),
        "investment_report_items",
        "item_kind <> 'watch' OR watch_condition IS NOT NULL",
        schema="review",
    )

    op.drop_constraint(
        op.f("ck_investment_report_items_apply_policy"),
        "investment_report_items",
        schema="review",
    )
    op.drop_constraint(
        op.f("ck_investment_report_items_operation"),
        "investment_report_items",
        schema="review",
    )
    op.drop_column("investment_report_items", "apply_policy", schema="review")
    op.drop_column("investment_report_items", "diff", schema="review")
    op.drop_column("investment_report_items", "proposed_state", schema="review")
    op.drop_column("investment_report_items", "current_state", schema="review")
    op.drop_column("investment_report_items", "target_ref", schema="review")
    op.drop_column("investment_report_items", "operation", schema="review")
