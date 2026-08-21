"""W5 durable Telegram callback inbox (additive)

Revision ID: 20260821_w5_callback_inbox
Revises: 20260820_rob1290_reconcile
Create Date: 2026-08-21

Additive: creates one new table in the ``review`` schema. No existing table,
column, constraint or index is altered, and nothing in ``review.order_proposals``
or its satellites is touched -- the durable inbox reads none of them and writes
none of them; it only decides *when* the existing callback core runs.

Applying this migration changes no behaviour on its own. All three W5 gates
(``ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED`` /
``..._WORKER_ENABLED`` / ``..._RECOVERY_SCHEDULE_ENABLED``) default to false,
so nothing writes this table until an operator arms them in the order the
runbook specifies.

The two constraints that carry the safety
-----------------------------------------
``ck_telegram_callback_inbox_terminal_scrubbed``
    A terminal row must have every authority/PII column NULL. This is what
    makes "the scrub happened" a database fact rather than a promise about the
    service layer: a future edit that keeps a nonce "just for debugging" fails
    the write.

``ck_telegram_callback_inbox_active_reconstructable``
    An active row must carry every column the worker needs to rebuild and
    re-gate the envelope, explicitly non-NULL, so a half-written row can never
    become a job the worker has to guess about.

``ck_telegram_callback_inbox_handler_marker_order``
    The three handler markers are causal facts. Completion implies entry; a
    recorded verdict implies both; and a queued (``pending``/``retry_wait``)
    row may remember none of them. Recovery *repairs* a job -- finalises it
    without re-running it -- on the strength of a recorded verdict, so a
    verdict that no handler entry ever produced has to be an impossible row.
    A pre-core ``discarded`` row legitimately carries no markers at all.

``ck_telegram_callback_inbox_retry_vocabulary``
    ``retry_wait`` implies ``error_class = 'pre_core_failure'`` and no
    outcome, and a ``pending`` row carries neither. The service writes that
    vocabulary itself rather than accepting it from a caller; this is the
    database saying the same thing, so the next writer cannot talk around it.

``ck_telegram_callback_inbox_processing_started_at``
    A ``processing`` row must say when it started. The recovery sweep decides
    whether to look at a claimed row by comparing that timestamp against a
    staleness window; a NULL makes the comparison permanently false, so the
    row would occupy "a worker owns this" forever while being invisible to the
    sweep. ``pending`` and ``retry_wait`` have not started and are unaffected.

Both are ``CASE WHEN ... THEN ... ELSE true END`` over ``IS NULL`` /
``IS NOT NULL`` predicates. Written as a bare ``state <> '...' OR col IS NULL``
they would evaluate to SQL ``UNKNOWN`` if any operand were NULL, and a CHECK
treats ``UNKNOWN`` as satisfied -- which is exactly how a scrub constraint
silently stops constraining anything.

Rollback ordering (see the runbook): this table is *active* once the ingress
gate is on. Do not downgrade it while jobs are in flight -- turn the ingress
gate off first, let the worker and recovery drain the backlog to terminal,
and only then consider dropping it.

Provenance: hand-written from ``app/models/telegram_callback_inbox.py``, whose
CHECK expressions are generated from the same
``app/services/order_proposals/callback_inbox/contracts.py`` vocabularies.
``alembic revision --autogenerate`` was not used: this repository has
pre-existing ORM/migration drift that autogenerate proposes to "fix" in
thousands of unrelated lines, which is emphatically not this migration's
business. The SQL below was rendered from the model's own constants and is
verified against a live PostgreSQL by
``tests/services/order_proposals/callback_inbox/test_migration.py``.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "20260821_w5_callback_inbox"
down_revision: str | Sequence[str] | None = "20260820_rob1290_reconcile"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TABLE = "telegram_callback_inbox"
_SCHEMA = "review"

_TERMINAL_SCRUBBED = (
    "CASE WHEN state IN ('dead_letter','discarded','succeeded') THEN "
    "callback_query_id IS NULL AND chat_id IS NULL AND message_id IS NULL "
    "AND telegram_user_id IS NULL AND action IS NULL AND subject_short IS NULL "
    "AND dispatch_attempt_id IS NULL AND membership_revision IS NULL "
    "AND membership_digest IS NULL AND nonce IS NULL "
    "AND terminal_state_pending IS NULL ELSE true END"
)

_HANDLER_MARKER_ORDER = (
    "(handler_completed_at IS NULL OR handler_entered_at IS NOT NULL)"
    " AND "
    "(terminal_state_pending IS NULL OR ("
    "handler_entered_at IS NOT NULL AND handler_completed_at IS NOT NULL))"
    " AND "
    "CASE WHEN state IN ('pending','retry_wait') THEN "
    "handler_entered_at IS NULL AND handler_completed_at IS NULL "
    "AND terminal_state_pending IS NULL ELSE true END"
)

_RETRY_VOCABULARY = (
    # ``IS NOT DISTINCT FROM``, not ``=``: with a NULL ``error_class`` the
    # equality is SQL UNKNOWN, and a CHECK treats UNKNOWN as satisfied -- so
    # ``=`` would have let a retry with no error class through. Caught by
    # `test_the_database_refuses_any_other_retry_vocabulary`.
    "CASE WHEN state = 'retry_wait' THEN "
    "error_class IS NOT DISTINCT FROM 'pre_core_failure' "
    "AND outcome IS NULL ELSE true END"
    " AND "
    "CASE WHEN state = 'pending' THEN "
    "error_class IS NULL AND outcome IS NULL ELSE true END"
)

_ACTIVE_RECONSTRUCTABLE = (
    "CASE WHEN state IN ('pending','processing','retry_wait') THEN "
    "chat_id IS NOT NULL AND action IS NOT NULL AND subject_short IS NOT NULL "
    "AND dispatch_attempt_id IS NOT NULL AND membership_revision IS NOT NULL "
    "AND membership_digest IS NOT NULL AND nonce IS NOT NULL ELSE true END"
)


def upgrade() -> None:
    """Create review.telegram_callback_inbox."""
    op.create_table(
        _TABLE,
        sa.Column("id", sa.BigInteger(), nullable=False),
        sa.Column("job_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("update_digest", sa.Text(), nullable=False),
        sa.Column(
            "state", sa.Text(), server_default=sa.text("'pending'"), nullable=False
        ),
        sa.Column(
            "attempt_count", sa.Integer(), server_default=sa.text("0"), nullable=False
        ),
        sa.Column(
            "max_attempts", sa.Integer(), server_default=sa.text("3"), nullable=False
        ),
        sa.Column("received_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("available_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("started_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column(
            "handler_entered_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column(
            "handler_completed_at", postgresql.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column("terminal_state_pending", sa.Text(), nullable=True),
        sa.Column("finished_at", postgresql.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("callback_query_id", sa.Text(), nullable=True),
        sa.Column("chat_id", sa.Text(), nullable=True),
        sa.Column("message_id", sa.BigInteger(), nullable=True),
        sa.Column("telegram_user_id", sa.Text(), nullable=True),
        sa.Column("action", sa.Text(), nullable=True),
        sa.Column("subject_short", sa.Text(), nullable=True),
        sa.Column("dispatch_attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("membership_revision", sa.Integer(), nullable=True),
        sa.Column("membership_digest", sa.Text(), nullable=True),
        sa.Column("nonce", sa.Text(), nullable=True),
        sa.Column("outcome", sa.Text(), nullable=True),
        sa.Column("error_class", sa.Text(), nullable=True),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "state IN ('dead_letter','discarded','pending','processing',"
            "'retry_wait','succeeded')",
            name=op.f("ck_telegram_callback_inbox_state"),
        ),
        sa.CheckConstraint(
            "terminal_state_pending IS NULL OR terminal_state_pending IN "
            "('dead_letter','discarded','succeeded')",
            name=op.f("ck_telegram_callback_inbox_terminal_state_pending"),
        ),
        sa.CheckConstraint(
            "attempt_count >= 0 AND attempt_count <= 3",
            name=op.f("ck_telegram_callback_inbox_attempt_count"),
        ),
        sa.CheckConstraint(
            "max_attempts > 0",
            name=op.f("ck_telegram_callback_inbox_max_attempts"),
        ),
        sa.CheckConstraint(
            "action IS NULL OR action IN ('op','dn','lc','vc','ba')",
            name=op.f("ck_telegram_callback_inbox_action"),
        ),
        sa.CheckConstraint(
            "outcome IS NULL OR outcome ~ '^[a-z0-9_]{1,64}$'",
            name=op.f("ck_telegram_callback_inbox_outcome"),
        ),
        sa.CheckConstraint(
            "error_class IS NULL OR error_class IN ('attempts_exhausted',"
            "'chat_revoked','envelope_invalid','handler_ambiguous',"
            "'handler_exception','pre_core_failure')",
            name=op.f("ck_telegram_callback_inbox_error_class"),
        ),
        sa.CheckConstraint(
            "CASE WHEN state = 'processing' THEN started_at IS NOT NULL "
            "ELSE true END",
            name=op.f("ck_telegram_callback_inbox_processing_started_at"),
        ),
        sa.CheckConstraint(
            _HANDLER_MARKER_ORDER,
            name=op.f("ck_telegram_callback_inbox_handler_marker_order"),
        ),
        sa.CheckConstraint(
            _RETRY_VOCABULARY,
            name=op.f("ck_telegram_callback_inbox_retry_vocabulary"),
        ),
        sa.CheckConstraint(
            _TERMINAL_SCRUBBED,
            name=op.f("ck_telegram_callback_inbox_terminal_scrubbed"),
        ),
        sa.CheckConstraint(
            _ACTIVE_RECONSTRUCTABLE,
            name=op.f("ck_telegram_callback_inbox_active_reconstructable"),
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_telegram_callback_inbox")),
        sa.UniqueConstraint("job_id", name="uq_telegram_callback_inbox_job_id"),
        sa.UniqueConstraint(
            "update_digest", name="uq_telegram_callback_inbox_update_digest"
        ),
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_telegram_callback_inbox_state_available",
        _TABLE,
        ["state", "available_at"],
        unique=False,
        schema=_SCHEMA,
    )


def downgrade() -> None:
    """Drop review.telegram_callback_inbox.

    Safe only once the ingress gate is off and the backlog has drained; see
    the module docstring and the runbook's rollback section.
    """
    op.drop_index(
        "ix_telegram_callback_inbox_state_available",
        table_name=_TABLE,
        schema=_SCHEMA,
    )
    op.drop_table(_TABLE, schema=_SCHEMA)
