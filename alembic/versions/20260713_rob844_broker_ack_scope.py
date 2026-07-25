"""ROB-844 follow-up: scope broker acknowledgements by instrument.

The previously pushed ROB-844 revision created
``uq_binance_demo_ledger_broker_ack`` on
``(product, venue_host, broker_order_id)``. Binance numeric order ids may be
reused by a different symbol, so the immutable identity is
``(product, venue_host, instrument_id, broker_order_id)``.

This revision changes only the named index. It never updates or deletes ledger
history. Upgrade and downgrade each run the conflict detector for the target
shape *before* dropping the currently protective index, so an unsafe scope
change aborts without leaving the table unprotected.

Revision ID: 20260713_rob844_ack_scope
Revises: 20260713_rob844_root_reservation
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "20260713_rob844_ack_scope"
down_revision: str | Sequence[str] | None = "20260713_rob844_root_reservation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "binance_demo_order_ledger"
_INDEX = "uq_binance_demo_ledger_broker_ack"
_NEW_COLUMNS = ("product", "venue_host", "instrument_id", "broker_order_id")
_OLD_COLUMNS = ("product", "venue_host", "broker_order_id")


def _index_columns(conn) -> tuple[str, ...] | None:
    row = (
        conn.execute(
            text(
                "SELECT array_agg(attribute.attname ORDER BY key.ordinality) AS columns, "
                "index_meta.indisunique AS is_unique, "
                "pg_get_expr(index_meta.indpred, index_meta.indrelid) AS predicate "
                "FROM pg_index index_meta "
                "JOIN pg_class index_class ON index_class.oid = index_meta.indexrelid "
                "CROSS JOIN LATERAL unnest(index_meta.indkey) WITH ORDINALITY "
                "AS key(attnum, ordinality) "
                "JOIN pg_attribute attribute "
                "ON attribute.attrelid = index_meta.indrelid "
                "AND attribute.attnum = key.attnum "
                "WHERE index_class.oid = to_regclass(:index_name) "
                "GROUP BY index_meta.indisunique, index_meta.indpred, index_meta.indrelid"
            ),
            {"index_name": _INDEX},
        )
        .mappings()
        .one_or_none()
    )
    if row is None:
        return None
    predicate = str(row["predicate"] or "").lower()
    normalized_predicate = "".join(
        character for character in predicate if character.isalnum() or character == "_"
    )
    if not row["is_unique"] or normalized_predicate != "broker_order_idisnotnull":
        raise RuntimeError(
            f"ROB-844 broker-ack index {_INDEX} has an unexpected uniqueness or "
            "predicate definition; refusing an automatic replacement"
        )
    return tuple(row["columns"])


def _assert_no_conflicts(conn, *, columns: tuple[str, ...], direction: str) -> None:
    column_sql = ", ".join(columns)
    rows = conn.execute(
        text(
            f"SELECT {column_sql}, count(*) AS n FROM {_TABLE} "  # noqa: S608
            "WHERE broker_order_id IS NOT NULL "
            f"GROUP BY {column_sql} HAVING count(*) > 1 "
            f"ORDER BY {column_sql}"
        )
    ).mappings()
    conflicts = list(rows)
    if not conflicts:
        return
    detail = ", ".join(
        "(" + ", ".join(f"{column}={row[column]}" for column in columns) + ")"
        for row in conflicts
    )
    raise RuntimeError(
        f"ROB-844 broker-ack scope {direction} aborted before index replacement: "
        f"pre-existing rows conflict under target key ({column_sql}). History "
        "is preserved and the current index remains installed. Reconcile the "
        f"duplicate ledger rows, then retry. Conflicts: {detail}"
    )


def _create_index(columns: tuple[str, ...]) -> None:
    op.create_index(
        _INDEX,
        _TABLE,
        list(columns),
        unique=True,
        postgresql_where=text("broker_order_id IS NOT NULL"),
    )


def upgrade() -> None:
    conn = op.get_bind()
    _assert_no_conflicts(conn, columns=_NEW_COLUMNS, direction="upgrade")
    current = _index_columns(conn)
    if current == _NEW_COLUMNS:
        return
    if current not in (None, _OLD_COLUMNS):
        raise RuntimeError(
            f"ROB-844 broker-ack scope upgrade found unexpected columns {current!r}"
        )
    if current is not None:
        op.drop_index(_INDEX, table_name=_TABLE)
    _create_index(_NEW_COLUMNS)


def downgrade() -> None:
    conn = op.get_bind()
    # The wider old scope can conflict after two instruments legitimately use
    # the same numeric id. Check before dropping the new protective index.
    _assert_no_conflicts(conn, columns=_OLD_COLUMNS, direction="downgrade")
    current = _index_columns(conn)
    if current == _OLD_COLUMNS:
        return
    if current not in (None, _NEW_COLUMNS):
        raise RuntimeError(
            f"ROB-844 broker-ack scope downgrade found unexpected columns {current!r}"
        )
    if current is not None:
        op.drop_index(_INDEX, table_name=_TABLE)
    _create_index(_OLD_COLUMNS)
