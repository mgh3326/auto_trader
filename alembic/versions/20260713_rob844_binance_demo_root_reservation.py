"""ROB-844 Binance Demo root-exposure reservation + broker-ack uniqueness.

Additive, ``binance_demo_order_ledger`` only (ROB-298 Demo ledger). Adds two
defense-in-depth **partial** unique indexes that back the atomic root-entry
reservation:

* ``uq_binance_demo_ledger_open_root`` — at most one *blocking root* lifecycle
  per ``(product, instrument_id)``. Root == ``parent_client_order_id IS NULL``;
  close/reduce-only child legs carry a parent and are excluded, so they never
  consume a root exposure slot. Predicate is scoped to the blocking states
  (``planned``/``previewed``/``validated``/``submitted``/``filled``/``anomaly``)
  so ``closed``/``cancelled``/``reconciled`` free the slot for re-entry.
* ``uq_binance_demo_ledger_broker_ack`` — a non-null broker acknowledgement
  ``(product, venue_host, broker_order_id)`` may attach to exactly one row, so
  a replayed ack cannot populate a second row.

**History-preserving fail-safe (AC#7):** this migration NEVER deletes or mutates
existing ROB-298 Demo rows. If pre-existing rows already violate either
uniqueness, ``upgrade`` raises with the exact conflicting keys and stops BEFORE
creating any index — an operator must reconcile the conflicting Demo rows and
re-run. The downgrade drops only the two additive indexes.

Revision ID: 20260713_rob844_root_reservation
Revises: 20260713_rob858_toss_loss_cut
Create Date: 2026-07-13
"""

from __future__ import annotations

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "20260713_rob844_root_reservation"
down_revision: str | Sequence[str] | None = "20260713_rob858_toss_loss_cut"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "binance_demo_order_ledger"
_OPEN_ROOT_INDEX = "uq_binance_demo_ledger_open_root"
_BROKER_ACK_INDEX = "uq_binance_demo_ledger_broker_ack"

# Blocking root lifecycle states — MUST stay in lockstep with
# app.models.binance_demo_order_ledger.BLOCKING_ROOT_LIFECYCLE_STATES and the
# repository's OPEN_LIFECYCLE_STATES.
_BLOCKING_STATES_SQL = (
    "('planned','previewed','validated','submitted','filled','anomaly')"
)

_OPEN_ROOT_PREDICATE = (
    f"parent_client_order_id IS NULL AND lifecycle_state IN {_BLOCKING_STATES_SQL}"
)


def _assert_no_open_root_conflicts(conn) -> None:
    rows = conn.execute(
        text(
            f"SELECT product, instrument_id, count(*) AS n "  # noqa: S608 (static)
            f"FROM {_TABLE} "
            f"WHERE {_OPEN_ROOT_PREDICATE} "
            "GROUP BY product, instrument_id HAVING count(*) > 1 "
            "ORDER BY product, instrument_id"
        )
    ).fetchall()
    if rows:
        detail = ", ".join(
            f"(product={r.product}, instrument_id={r.instrument_id}, count={r.n})"
            for r in rows
        )
        raise RuntimeError(
            "ROB-844 upgrade aborted: pre-existing duplicate open-root lifecycles "
            f"violate {_OPEN_ROOT_INDEX}. History is preserved (nothing deleted). "
            "Operator remediation required — reconcile these blocking roots "
            "(close/cancel/reconcile the stale duplicate, or mark it anomaly and "
            "resolve) so at most one blocking root remains per (product, "
            f"instrument_id), then re-run the migration. Conflicts: {detail}"
        )


def _assert_no_broker_ack_conflicts(conn) -> None:
    rows = conn.execute(
        text(
            "SELECT product, venue_host, broker_order_id, count(*) AS n "  # noqa: S608
            f"FROM {_TABLE} "
            "WHERE broker_order_id IS NOT NULL "
            "GROUP BY product, venue_host, broker_order_id HAVING count(*) > 1 "
            "ORDER BY product, venue_host, broker_order_id"
        )
    ).fetchall()
    if rows:
        detail = ", ".join(
            f"(product={r.product}, venue_host={r.venue_host}, "
            f"broker_order_id={r.broker_order_id}, count={r.n})"
            for r in rows
        )
        raise RuntimeError(
            "ROB-844 upgrade aborted: pre-existing duplicate broker acknowledgements "
            f"violate {_BROKER_ACK_INDEX}. History is preserved (nothing deleted). "
            "Operator remediation required — a broker_order_id is attached to more "
            "than one ledger row; reconcile the mistaken/replayed row so each "
            "(product, venue_host, broker_order_id) maps to exactly one row, then "
            f"re-run the migration. Conflicts: {detail}"
        )


def upgrade() -> None:
    conn = op.get_bind()
    # Fail closed on pre-existing conflicts BEFORE creating any index, so the
    # operator gets an actionable message instead of a raw unique-violation and
    # no partial state is left behind. Data is never mutated here.
    _assert_no_open_root_conflicts(conn)
    _assert_no_broker_ack_conflicts(conn)

    op.create_index(
        _OPEN_ROOT_INDEX,
        _TABLE,
        ["product", "instrument_id"],
        unique=True,
        postgresql_where=text(_OPEN_ROOT_PREDICATE),
    )
    op.create_index(
        _BROKER_ACK_INDEX,
        _TABLE,
        ["product", "venue_host", "broker_order_id"],
        unique=True,
        postgresql_where=text("broker_order_id IS NOT NULL"),
    )


def downgrade() -> None:
    op.drop_index(_BROKER_ACK_INDEX, table_name=_TABLE)
    op.drop_index(_OPEN_ROOT_INDEX, table_name=_TABLE)
