from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "4d9f0b2c7a11"
down_revision: str | Sequence[str] | None = "9f2c6db7a41e"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "asset_profiles",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("broker_account_id", sa.BigInteger(), nullable=True),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column(
            "instrument_type",
            postgresql.ENUM(
                "equity_kr",
                "equity_us",
                "crypto",
                "forex",
                "index",
                name="instrument_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column("profile", sa.String(length=24), nullable=False),
        sa.Column("sector", sa.Text(), nullable=True),
        sa.Column("tags", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("max_position_pct", sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column(
            "buy_allowed", sa.Boolean(), server_default=sa.text("true"), nullable=False
        ),
        sa.Column(
            "sell_mode",
            sa.String(length=20),
            server_default=sa.text("'any'"),
            nullable=False,
        ),
        sa.Column("note", sa.Text(), nullable=True),
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
        sa.CheckConstraint("tier BETWEEN 1 AND 4", name="asset_profiles_tier_range"),
        sa.CheckConstraint(
            "profile IN ('aggressive','balanced','conservative','exit','hold_only')",
            name="asset_profiles_profile_allowed",
        ),
        sa.CheckConstraint(
            "sell_mode IN ('any','rebalance_only','none')",
            name="asset_profiles_sell_mode_allowed",
        ),
        sa.CheckConstraint(
            "profile <> 'exit' OR buy_allowed = FALSE",
            name="asset_profiles_exit_buy_rule",
        ),
        sa.CheckConstraint(
            "profile <> 'hold_only' OR sell_mode = 'rebalance_only'",
            name="asset_profiles_hold_only_sell_mode_rule",
        ),
        sa.CheckConstraint(
            "tags IS NULL OR jsonb_typeof(tags) = 'array'",
            name="asset_profiles_tags_array_type",
        ),
        sa.ForeignKeyConstraint(
            ["broker_account_id"],
            ["broker_accounts.id"],
            name=op.f("fk_asset_profiles_broker_account_id_broker_accounts"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_asset_profiles_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_asset_profiles")),
    )
    op.create_index(
        "ix_asset_profiles_user_instrument_type",
        "asset_profiles",
        ["user_id", "instrument_type"],
        unique=False,
    )
    op.create_index(
        "ix_asset_profiles_user_profile",
        "asset_profiles",
        ["user_id", "profile"],
        unique=False,
    )
    op.create_index(
        "ix_asset_profiles_tags_gin",
        "asset_profiles",
        ["tags"],
        unique=False,
        postgresql_using="gin",
    )

    op.create_table(
        "tier_rule_params",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "instrument_type",
            postgresql.ENUM(
                "equity_kr",
                "equity_us",
                "crypto",
                "forex",
                "index",
                name="instrument_type",
                create_type=False,
            ),
            nullable=False,
        ),
        sa.Column("tier", sa.SmallInteger(), nullable=False),
        sa.Column("profile", sa.String(length=24), nullable=False),
        sa.Column(
            "param_type",
            sa.String(length=16),
            nullable=False,
        ),
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

    op.create_table(
        "market_filters",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("broker_account_id", sa.BigInteger(), nullable=True),
        sa.Column(
            "instrument_type",
            postgresql.ENUM(
                "equity_kr",
                "equity_us",
                "crypto",
                "forex",
                "index",
                name="instrument_type",
                create_type=False,
            ),
            nullable=False,
        ),
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
            ["broker_account_id"],
            ["broker_accounts.id"],
            name=op.f("fk_market_filters_broker_account_id_broker_accounts"),
            ondelete="CASCADE",
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

    op.create_table(
        "profile_change_log",
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("user_id", sa.BigInteger(), nullable=False),
        sa.Column("change_type", sa.String(length=32), nullable=False),
        sa.Column("target", sa.String(length=64), nullable=False),
        sa.Column("old_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("new_value", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("reason", sa.Text(), nullable=True),
        sa.Column("changed_by", sa.Text(), nullable=False),
        sa.Column(
            "changed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["user_id"],
            ["users.id"],
            name=op.f("fk_profile_change_log_user_id_users"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_profile_change_log")),
    )
    op.create_index(
        "ix_profile_change_log_user_changed_at",
        "profile_change_log",
        ["user_id", "changed_at"],
        unique=False,
    )
    op.create_index(
        "ix_profile_change_log_target_changed_at",
        "profile_change_log",
        ["target", "changed_at"],
        unique=False,
    )

    op.execute(
        """
        CREATE UNIQUE INDEX uq_asset_profiles_with_broker
        ON asset_profiles (user_id, broker_account_id, symbol, instrument_type)
        WHERE broker_account_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_asset_profiles_without_broker
        ON asset_profiles (user_id, symbol, instrument_type)
        WHERE broker_account_id IS NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_market_filters_with_broker
        ON market_filters (user_id, broker_account_id, instrument_type, filter_name)
        WHERE broker_account_id IS NOT NULL
        """
    )
    op.execute(
        """
        CREATE UNIQUE INDEX uq_market_filters_without_broker
        ON market_filters (user_id, instrument_type, filter_name)
        WHERE broker_account_id IS NULL
        """
    )


def downgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_market_filters_without_broker")
    op.execute("DROP INDEX IF EXISTS uq_market_filters_with_broker")
    op.execute("DROP INDEX IF EXISTS uq_asset_profiles_without_broker")
    op.execute("DROP INDEX IF EXISTS uq_asset_profiles_with_broker")

    op.execute("DROP INDEX IF EXISTS ix_profile_change_log_target_changed_at")
    op.execute("DROP INDEX IF EXISTS ix_profile_change_log_user_changed_at")
    op.execute("DROP INDEX IF EXISTS ix_profile_change_log_asset_profile_created_at")
    op.execute("DROP INDEX IF EXISTS ix_profile_change_log_user_created_at")
    op.drop_table("profile_change_log")

    op.drop_index(
        "ix_market_filters_user_instrument_enabled",
        table_name="market_filters",
    )
    op.drop_table("market_filters")

    op.drop_table("tier_rule_params")

    op.drop_index("ix_asset_profiles_tags_gin", table_name="asset_profiles")
    op.drop_index("ix_asset_profiles_user_profile", table_name="asset_profiles")
    op.drop_index(
        "ix_asset_profiles_user_instrument_type",
        table_name="asset_profiles",
    )
    op.drop_table("asset_profiles")
