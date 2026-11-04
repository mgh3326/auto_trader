"""ROB-1487: index the two ``max()`` partition probes behind hot MCP reads.

Both hot reads ask Postgres for "the newest partition for this market" and
neither had a usable index, so each call scanned the whole table/partition:

1. ``SELECT max(snapshot_at) FROM invest_momentum_event_snapshots
   WHERE market = 'kr'`` — market was not a leading column in any existing
   index (all of them lead with surface/symbol/trading_date), so this was a
   sequential scan on every ``get_momentum_candidates`` call.
   ``ix_invest_momentum_event_snapshots_market_snapshot_at`` lets the planner
   answer the aggregate with a backward index-only scan
   (``Limit -> Index Only Scan Backward``), independent of table size.

2. ``SELECT max(snapshot_date) FROM invest_screener_snapshots
   WHERE market = 'kr' AND support_computed_at IS NOT NULL`` — the existing
   ``(market, snapshot_date)`` index cannot stop at the max while also applying
   the support predicate, and the existing partial index keys on
   ``dist_to_support_pct``. ``ix_invest_screener_snapshots_market_support_date``
   is partial on ``support_computed_at IS NOT NULL`` so it both answers the
   probe and serves the follow-up ``market = $1 AND snapshot_date = $2 AND
   support_computed_at IS NOT NULL`` row read.

Both statements are plain (not CONCURRENTLY) on purpose. Alembic runs this
file inside the migration transaction (``alembic/env.py`` ->
``do_run_migrations`` -> ``context.begin_transaction()``), where
``CREATE INDEX CONCURRENTLY`` is illegal. The lock consequence is documented
here so the operator can decide to build them ahead of time:

* ``CREATE INDEX`` takes a ``SHARE`` lock: concurrent SELECTs keep running,
  INSERT/UPDATE/DELETE on the table block until the build finishes.
* Build cost is O(rows). Sizes are not hard-coded here — measure before the
  upgrade with::

      SELECT relname, reltuples::bigint, pg_size_pretty(pg_total_relation_size(oid))
      FROM pg_class
      WHERE relname IN ('invest_momentum_event_snapshots',
                        'invest_screener_snapshots');

  The screener index is partial (only rows with ``support_computed_at``), so it
  is smaller than its table. If either table is large enough that the write
  block matters, create the same two indexes with ``CREATE INDEX CONCURRENTLY
  IF NOT EXISTS`` manually beforehand — this migration is idempotent
  (``IF NOT EXISTS``) and will then be a no-op.

Both statements use ``IF NOT EXISTS`` because the schema acceptance path builds
current ``Base.metadata`` first, stamps an older revision and upgrades to head
(see ``tests/_schema_bootstrap.py``); without the guards the upgrade would fail
on already-present indexes.

Revision ID: 20261103_rob1487_perf_idx
Revises: 20261102_rob1474_mock_orders
Create Date: 2026-11-02 00:00:00
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261103_rob1487_perf_idx"
down_revision: str | Sequence[str] | None = "20261102_rob1474_mock_orders"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_MOMENTUM_INDEX = "ix_invest_momentum_event_snapshots_market_snapshot_at"
_SCREENER_INDEX = "ix_invest_screener_snapshots_market_support_date"


def upgrade() -> None:
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_MOMENTUM_INDEX} "
        "ON invest_momentum_event_snapshots (market, snapshot_at)"
    )
    op.execute(
        f"CREATE INDEX IF NOT EXISTS {_SCREENER_INDEX} "
        "ON invest_screener_snapshots (market, snapshot_date) "
        "WHERE support_computed_at IS NOT NULL"
    )


def downgrade() -> None:
    op.execute(f"DROP INDEX IF EXISTS {_SCREENER_INDEX}")
    op.execute(f"DROP INDEX IF EXISTS {_MOMENTUM_INDEX}")
