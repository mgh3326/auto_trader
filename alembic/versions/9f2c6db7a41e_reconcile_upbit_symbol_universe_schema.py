from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "9f2c6db7a41e"
down_revision: str | Sequence[str] | None = "285dd836750a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_name = "upbit_symbol_universe"

    if table_name not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns(table_name)}
    indexes = {index["name"] for index in inspector.get_indexes(table_name)}

    is_legacy_shape = (
        "symbol" in columns
        and "market" in columns
        and "quote_currency" not in columns
        and "base_currency" not in columns
    )

    if is_legacy_shape:
        if "ix_upbit_symbol_universe_market_is_active" in indexes:
            op.drop_index(
                "ix_upbit_symbol_universe_market_is_active",
                table_name=table_name,
            )

        op.alter_column(
            table_name,
            "market",
            existing_type=sa.String(length=10),
            new_column_name="quote_currency",
        )
        op.alter_column(
            table_name,
            "symbol",
            existing_type=sa.String(length=20),
            new_column_name="market",
        )
        op.add_column(
            table_name,
            sa.Column("base_currency", sa.String(length=20), nullable=True),
        )

        op.execute(
            sa.text(
                """
                UPDATE upbit_symbol_universe
                SET quote_currency = COALESCE(
                    NULLIF(quote_currency, ''),
                    split_part(market, '-', 1)
                )
                """
            )
        )
        op.execute(
            sa.text(
                """
                UPDATE upbit_symbol_universe
                SET base_currency = split_part(market, '-', 2)
                """
            )
        )
        op.alter_column(
            table_name,
            "base_currency",
            existing_type=sa.String(length=20),
            nullable=False,
        )

    inspector = sa.inspect(bind)
    columns = {column["name"] for column in inspector.get_columns(table_name)}
    indexes = {index["name"] for index in inspector.get_indexes(table_name)}

    if (
        "quote_currency" in columns
        and "ix_upbit_symbol_universe_quote_is_active" not in indexes
    ):
        op.create_index(
            "ix_upbit_symbol_universe_quote_is_active",
            table_name,
            ["quote_currency", "is_active"],
            unique=False,
        )

    if (
        "base_currency" in columns
        and "ix_upbit_symbol_universe_base_is_active" not in indexes
    ):
        op.create_index(
            "ix_upbit_symbol_universe_base_is_active",
            table_name,
            ["base_currency", "is_active"],
            unique=False,
        )


def downgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    table_name = "upbit_symbol_universe"

    if table_name not in inspector.get_table_names():
        return

    columns = {column["name"] for column in inspector.get_columns(table_name)}
    indexes = {index["name"] for index in inspector.get_indexes(table_name)}

    if "ix_upbit_symbol_universe_base_is_active" in indexes:
        op.drop_index(
            "ix_upbit_symbol_universe_base_is_active",
            table_name=table_name,
        )

    if "ix_upbit_symbol_universe_quote_is_active" in indexes:
        op.drop_index(
            "ix_upbit_symbol_universe_quote_is_active",
            table_name=table_name,
        )

    is_reconciled_shape = (
        "market" in columns
        and "quote_currency" in columns
        and "base_currency" in columns
        and "symbol" not in columns
    )

    if not is_reconciled_shape:
        return

    op.drop_column(table_name, "base_currency")
    op.alter_column(
        table_name,
        "market",
        existing_type=sa.String(length=20),
        new_column_name="symbol",
    )
    op.alter_column(
        table_name,
        "quote_currency",
        existing_type=sa.String(length=10),
        new_column_name="market",
    )

    inspector = sa.inspect(bind)
    indexes = {index["name"] for index in inspector.get_indexes(table_name)}
    if "ix_upbit_symbol_universe_market_is_active" not in indexes:
        op.create_index(
            "ix_upbit_symbol_universe_market_is_active",
            table_name,
            ["market", "is_active"],
            unique=False,
        )
