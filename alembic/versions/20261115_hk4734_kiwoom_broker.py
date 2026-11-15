"""hk 4734 (3797 T-P4): admit broker 'kiwoom' in the execution ledger.

Revision ID: 20261115_hk4734_kiwoom_broker
Revises: 20261114_hk4702_trend_state
Create Date: 2026-11-15

Two additive CHECK widenings only (design docs/design/3797-kiwoom-mock-ledger.md
§2.3, operator decision decision/2026-11-15/at-rule16-kiwoom-broker):

* ``review.execution_ledger.broker`` gains ``'kiwoom'`` — the Kiwoom mock
  fill rows booked by the full reconcile mode.
* ``review.execution_ledger_reconcile_runs.broker`` gains ``'kiwoom'`` — the
  run coverage row the same reconcile writes.

Nothing else changes: the idempotency key, the external-ingest
``source='websocket'`` forcing, per-item savepoints and ledger-commit authority
are untouched. No new object is created, so no ``at_app`` grant is needed; the
table-level privileges already cover these columns.

Constraint names: the ORM applies ``ck_%(table_name)s_%(constraint_name)s``, so
the ORM-canonical spellings are pinned here with ``op.f``. Production databases
carry the literal names from 2026_05_13_rob211 and 20260707_rob757, so both
spellings are dropped with IF EXISTS before the widened constraint is added.

Downgrade refuses while any Kiwoom row exists: the old CHECK would reject the
rows the application can no longer write after a downgrade.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261115_hk4734_kiwoom_broker"
down_revision: str | Sequence[str] | None = "20261114_hk4702_trend_state"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_LEDGER_TABLE = "execution_ledger"
_RUNS_TABLE = "execution_ledger_reconcile_runs"

_LEDGER_CHECK = "ck_execution_ledger_execution_ledger_broker"
_LEDGER_CHECK_NAMES = (_LEDGER_CHECK, "execution_ledger_broker")
_OLD_LEDGER_BROKERS = ("kis", "upbit", "toss")
_NEW_LEDGER_BROKERS = (*_OLD_LEDGER_BROKERS, "kiwoom")

_RUNS_CHECK = "ck_execution_ledger_reconcile_runs_runs_broker"
_RUNS_CHECK_NAMES = (
    _RUNS_CHECK,
    # The ORM's previous 65-character name (PostgreSQL truncated it to 63) and
    # the literal production name from 2026_05_13_rob211.
    "ck_execution_ledger_reconcile_runs_execution_ledger_runs_broker",
    "execution_ledger_runs_broker",
)
_OLD_RUNS_BROKERS = ("kis", "upbit")
_NEW_RUNS_BROKERS = (*_OLD_RUNS_BROKERS, "kiwoom")

DOWNGRADE_GUARD = f"""
DO $$
BEGIN
    LOCK TABLE {_SCHEMA}.{_LEDGER_TABLE}, {_SCHEMA}.{_RUNS_TABLE}
        IN ACCESS EXCLUSIVE MODE;
    IF EXISTS (SELECT 1 FROM {_SCHEMA}.{_LEDGER_TABLE} WHERE broker = 'kiwoom')
       OR EXISTS (
           SELECT 1 FROM {_SCHEMA}.{_RUNS_TABLE} WHERE broker = 'kiwoom'
       ) THEN
        RAISE EXCEPTION 'execution ledger holds kiwoom rows; export before downgrading'
            USING ERRCODE = 'restrict_violation';
    END IF;
END;
$$
"""


def _brokers(values: Sequence[str]) -> str:
    return f"broker IN ({', '.join(repr(value) for value in values)})"


def _drop_checks(table: str, names: Sequence[str]) -> None:
    for name in names:
        op.execute(f'ALTER TABLE {_SCHEMA}.{table} DROP CONSTRAINT IF EXISTS "{name}"')


def _create_check(name: str, table: str, expression: str) -> None:
    op.create_check_constraint(op.f(name), table, expression, schema=_SCHEMA)


def upgrade() -> None:
    _drop_checks(_LEDGER_TABLE, _LEDGER_CHECK_NAMES)
    _create_check(_LEDGER_CHECK, _LEDGER_TABLE, _brokers(_NEW_LEDGER_BROKERS))

    _drop_checks(_RUNS_TABLE, _RUNS_CHECK_NAMES)
    _create_check(_RUNS_CHECK, _RUNS_TABLE, _brokers(_NEW_RUNS_BROKERS))


def downgrade() -> None:
    op.execute(DOWNGRADE_GUARD)

    _drop_checks(_RUNS_TABLE, _RUNS_CHECK_NAMES)
    _create_check(_RUNS_CHECK, _RUNS_TABLE, _brokers(_OLD_RUNS_BROKERS))

    _drop_checks(_LEDGER_TABLE, _LEDGER_CHECK_NAMES)
    _create_check(_LEDGER_CHECK, _LEDGER_TABLE, _brokers(_OLD_LEDGER_BROKERS))
