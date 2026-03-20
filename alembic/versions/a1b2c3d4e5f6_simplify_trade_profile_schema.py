from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "a1b2c3d4e5f6"
down_revision: str | Sequence[str] | None = "4d9f0b2c7a11"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("DROP INDEX IF EXISTS uq_asset_profiles_with_broker")
    op.execute("DROP INDEX IF EXISTS uq_asset_profiles_without_broker")
    op.execute("DROP INDEX IF EXISTS uq_market_filters_with_broker")
    op.execute("DROP INDEX IF EXISTS uq_market_filters_without_broker")

    op.drop_constraint(
        op.f("fk_asset_profiles_broker_account_id_broker_accounts"),
        "asset_profiles",
        type_="foreignkey",
    )
    op.drop_constraint(
        op.f("fk_market_filters_broker_account_id_broker_accounts"),
        "market_filters",
        type_="foreignkey",
    )

    op.drop_column("asset_profiles", "broker_account_id")
    op.drop_column("market_filters", "broker_account_id")

    op.create_index(
        "uq_asset_profiles_user_symbol_instrument",
        "asset_profiles",
        ["user_id", "symbol", "instrument_type"],
        unique=True,
    )
    op.create_index(
        "uq_market_filters_user_instrument_filter",
        "market_filters",
        ["user_id", "instrument_type", "filter_name"],
        unique=True,
    )


def downgrade() -> None:
    op.drop_index(
        "uq_market_filters_user_instrument_filter",
        table_name="market_filters",
    )
    op.drop_index(
        "uq_asset_profiles_user_symbol_instrument",
        table_name="asset_profiles",
    )

    op.add_column(
        "market_filters",
        sa.Column("broker_account_id", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "asset_profiles",
        sa.Column("broker_account_id", sa.BigInteger(), nullable=True),
    )

    op.create_foreign_key(
        op.f("fk_asset_profiles_broker_account_id_broker_accounts"),
        "asset_profiles",
        "broker_accounts",
        ["broker_account_id"],
        ["id"],
        ondelete="CASCADE",
    )
    op.create_foreign_key(
        op.f("fk_market_filters_broker_account_id_broker_accounts"),
        "market_filters",
        "broker_accounts",
        ["broker_account_id"],
        ["id"],
        ondelete="CASCADE",
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
