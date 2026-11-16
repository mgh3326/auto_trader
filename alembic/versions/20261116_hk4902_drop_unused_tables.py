"""hk 4902: drop four unused, empty legacy tables.

Dropped (all in ``public``; the audit with the evidence for each is
``docs/research/4902-unused-table-audit.md``):

* ``tier_rule_params``   - trade_profile family, retired since ROB-488
* ``market_filters``     - trade_profile family, retired since ROB-488
* ``user_channels``      - created by the initial schema, never written
* ``user_research_notes`` - ROB-112 "schema only", no writer ever shipped

Each table has no runtime reader or writer in ``app/`` or ``scripts/`` and no
other table references it. ROB-1488 counted 0 rows in production on
2026-11-02; this migration does not trust that count. Upgrade takes an
``ACCESS EXCLUSIVE`` lock on each table and refuses to drop one that holds a
row, so a table that gained data since the audit is never dropped silently.
``DROP TABLE`` is issued without ``CASCADE``: an unknown dependent object
makes the migration fail instead of disappearing.

Production applies this revision with the separated migration runner after the
pi move and the final dump, under ``SET ROLE at_migration_owner`` (hard rule
18; ``at_migration_owner`` owns the existing tables after Stage 3, so it may
drop them). It is applied only on the operator's approval, never by the PR.

Downgrade recreates the four tables empty with their last known schema
(columns, constraints, indexes) and, when the ``at_app`` role exists, the
conditional DML grants that role holds on the legacy tables. The PostgreSQL
enum type ``notify_channel`` is not touched by upgrade, so it still exists
when ``user_channels`` is recreated; the downgrade creates it only if missing.

Revision ID: 20261116_hk4902_drop_unused
Revises: 20261115_hk4734_kiwoom_validate
Create Date: 2026-11-16
"""

# expand-contract: contract-step 4902
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261116_hk4902_drop_unused"
down_revision: str | Sequence[str] | None = "20261115_hk4734_kiwoom_validate"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# Order is irrelevant for the drop (no table references another one here) and
# is kept sorted so the static test can compare it with the audit document.
DROPPED_TABLES: tuple[str, ...] = (
    "market_filters",
    "tier_rule_params",
    "user_channels",
    "user_research_notes",
)


def _refuse_if_rows_exist(table: str) -> None:
    op.execute(f"LOCK TABLE public.{table} IN ACCESS EXCLUSIVE MODE")
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM public.{table}) THEN
                RAISE EXCEPTION 'hk4902_{table}_rows_retained';
            END IF;
        END $$
    """)


def upgrade() -> None:
    for table in DROPPED_TABLES:
        _refuse_if_rows_exist(table)
        op.execute(f"DROP TABLE public.{table}")


def _grant_at_app(table: str) -> None:
    """Hard rule 18: the recreated table is owned by the migration owner."""
    op.execute(f"""
        DO $$ BEGIN
            IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') THEN
                GRANT SELECT, INSERT, UPDATE, DELETE
                ON public.{table} TO at_app;
                EXECUTE 'GRANT USAGE, SELECT ON SEQUENCE '
                    || pg_get_serial_sequence('public.{table}', 'id')
                    || ' TO at_app';
            END IF;
        END $$
    """)


def downgrade() -> None:
    instrument_type = postgresql.ENUM(
        "equity_kr",
        "equity_us",
        "crypto",
        "forex",
        "index",
        name="instrument_type",
        create_type=False,
    )

    # tier_rule_params (4d9f0b2c7a11)
    op.create_table(
        "tier_rule_params",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_type", instrument_type, nullable=False),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column("profile", sa.String(length=24), nullable=False),
        sa.Column("param_type", sa.String(length=16), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("version", sa.Integer(), server_default=sa.text("1"), nullable=False),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint("tier BETWEEN 1 AND 4", name="tier_rule_params_tier_range"),
        sa.CheckConstraint(
            "profile IN ('aggressive','balanced','conservative','exit','hold_only')",
            name="tier_rule_params_profile_allowed",
        ),
        sa.CheckConstraint(
            "param_type IN ('buy','sell','stop','rebalance','common')",
            name="tier_rule_params_param_type_allowed",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_tier_rule_params_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_tier_rule_params")),
        sa.UniqueConstraint(
            "user_id",
            "instrument_type",
            "tier",
            "profile",
            "param_type",
            name="uq_tier_rule_params_key",
        ),
    )

    # market_filters (4d9f0b2c7a11, then a1b2c3d4e5f6 removed broker_account_id)
    op.create_table(
        "market_filters",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("instrument_type", instrument_type, nullable=False),
        sa.Column("filter_name", sa.String(length=32), nullable=False),
        sa.Column("params", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column(
            "enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column("updated_by", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "filter_name ~ '^[a-z][a-z0-9_]{0,29}$'",
            name="market_filters_filter_name_format",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_market_filters_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_market_filters")),
    )
    op.create_index(
        "ix_market_filters_user_instrument_enabled",
        "market_filters",
        ["user_id", "instrument_type", "enabled"],
        unique=False,
    )
    op.create_index(
        "uq_market_filters_user_instrument_filter",
        "market_filters",
        ["user_id", "instrument_type", "filter_name"],
        unique=True,
    )

    # user_channels (b3e58be9e79b); the enum type survives the upgrade.
    notify_channel = postgresql.ENUM(
        "telegram", "email", "webhook", name="notify_channel", create_type=False
    )
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_type t JOIN pg_namespace n ON n.oid = t.typnamespace
                WHERE t.typname = 'notify_channel' AND n.nspname = 'public'
            ) THEN
                CREATE TYPE public.notify_channel AS ENUM
                    ('telegram', 'email', 'webhook');
            END IF;
        END $$
    """)
    op.create_table(
        "user_channels",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("channel", notify_channel, nullable=False),
        sa.Column("handle", sa.Text(), nullable=False),
        sa.Column("is_verified", sa.Boolean(), nullable=False),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_user_channels_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_user_channels")),
    )

    # user_research_notes (f7891bf9789f)
    op.create_table(
        "user_research_notes",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("session_id", sa.Integer(), nullable=False),
        sa.Column("user_id", sa.Integer(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["session_id"], ["research_sessions.id"]),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_user_research_notes_id"), "user_research_notes", ["id"], unique=False
    )
    op.create_index(
        op.f("ix_user_research_notes_session_id"),
        "user_research_notes",
        ["session_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_user_research_notes_user_id"),
        "user_research_notes",
        ["user_id"],
        unique=False,
    )

    for table in DROPPED_TABLES:
        _grant_at_app(table)
