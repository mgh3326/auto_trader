"""ROB-1290 r2 — allow the awaiting_reconcile claim terminal (additive)

Revision ID: 20260820_rob1290_reconcile
Revises: 20260819_rob1286_claims
Create Date: 2026-08-20

Widens one CHECK constraint on ``review.watch_event_repricing_claims`` so the
``state`` column accepts ``'awaiting_reconcile'``. Nothing else changes: no
column is added, dropped or retyped, no index moves, no data is rewritten, and
no other table is touched.

Why the state exists
--------------------
An ambiguous spawn -- the create call raised and could not be reconciled --
leaves it unknown whether ``order_proposal_create`` committed. ROB-1286 handled
that by keeping the claim ``started`` and logging that it would not be retried.
But ``started`` is exactly the state the lease expires, so the TTL sweep wrote
``expired_unprocessed`` and the next tick re-claimed at ``generation + 1`` and
judged the fire again. If the first call had committed and only its
acknowledgement was lost, that is two proposals from one fire reaching the
auto-approve lane.

``awaiting_reconcile`` is terminal, so the TTL sweep (which only matches
``state = 'started'``) cannot reach it and both claim stores refuse to
re-claim an event holding it. The fire goes to an operator instead.

Ordering note
-------------
This must be applied before the flow is armed. That is not a live-deployment
hazard today: ``WATCH_TRIGGER_REPRICING_ENABLED`` defaults false, no schedule
registers the tick anywhere in this repo, and the entrypoint's default spawner
creates nothing -- so no process writes this state until an operator turns the
feature on, by which time both this migration and ROB-1286's have been applied
together.
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260820_rob1290_reconcile"
down_revision: str | Sequence[str] | None = "20260819_rob1286_claims"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "watch_event_repricing_claims"
_SCHEMA = "review"
# Short name on purpose. Alembic applies ``Base.metadata``'s naming
# convention (``ck_%(table_name)s_%(constraint_name)s``) to *both*
# ``drop_constraint`` and ``create_check_constraint``, so passing the full
# ``ck_watch_event_repricing_claims_state`` produces a double-prefixed,
# truncated name -- ``ck_watch_event_repricing_claims_ck_watch_event_...``
# -- and the DROP fails at runtime because no such constraint exists.
# Verified by rendering this migration offline (``alembic upgrade --sql``).
_CONSTRAINT = "state"
_FULL_CONSTRAINT_NAME = "ck_watch_event_repricing_claims_state"

_STATES_AFTER = (
    "'started', 'proposal_created', 'rejected_with_reason', "
    "'expired_unprocessed', 'awaiting_reconcile'"
)
_STATES_BEFORE = (
    "'started', 'proposal_created', 'rejected_with_reason', 'expired_unprocessed'"
)


def upgrade() -> None:
    """Accept the awaiting_reconcile terminal."""
    op.drop_constraint(_CONSTRAINT, _TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        sa.text(f"state IN ({_STATES_AFTER})"),
        schema=_SCHEMA,
    )


def downgrade() -> None:
    """Narrow the constraint back.

    Rows already written as ``awaiting_reconcile`` would violate the narrower
    constraint, so they are first moved to ``expired_unprocessed`` -- the
    closest pre-existing "terminal, unresolved" state. That is lossy on
    purpose: the alternative is a downgrade that fails outright, and this one
    at least leaves the fire visible as unjudged rather than as done.
    """
    op.execute(
        sa.text(
            f"UPDATE {_SCHEMA}.{_TABLE} SET state = 'expired_unprocessed' "
            "WHERE state = 'awaiting_reconcile'"
        )
    )
    op.drop_constraint(_CONSTRAINT, _TABLE, schema=_SCHEMA, type_="check")
    op.create_check_constraint(
        _CONSTRAINT,
        _TABLE,
        sa.text(f"state IN ({_STATES_BEFORE})"),
        schema=_SCHEMA,
    )
