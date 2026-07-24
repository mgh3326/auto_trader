"""Durable, caller-visible Telegram approval dispatch attempts.

Revision ID: 20260723_approval_dispatch
Revises: 20260722_rob1023_widen_runner
Create Date: 2026-07-23

This revision is additive and performs no data backfill. Existing proposals,
including still-pending approvals, retain NULL dispatch metadata until a new
dispatch attempt occurs.
"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20260723_approval_dispatch"
down_revision: str = "20260722_rob1023_widen_runner"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"


def upgrade() -> None:
    op.execute(
        "ALTER TABLE review.order_proposals "
        "ADD COLUMN IF NOT EXISTS approval_dispatch_state TEXT"
    )
    op.execute(
        "ALTER TABLE review.order_proposals "
        "ADD COLUMN IF NOT EXISTS approval_dispatch_attempt_id UUID"
    )
    op.execute(
        "ALTER TABLE review.order_proposals "
        "ADD COLUMN IF NOT EXISTS approval_dispatch_attempted_at TIMESTAMPTZ"
    )
    op.execute(
        "ALTER TABLE review.order_proposals "
        "ADD COLUMN IF NOT EXISTS approval_dispatch_failure_code TEXT"
    )
    op.execute(
        "ALTER TABLE review.order_proposals "
        "ADD COLUMN IF NOT EXISTS approval_dispatch_payload_chars BIGINT"
    )
    for definition in (
        "approval_dispatch_card_kind TEXT",
        "approval_dispatch_membership_revision INTEGER",
        "approval_dispatch_membership_digest TEXT",
        "approval_dispatch_published_at TIMESTAMPTZ",
    ):
        op.execute(
            f"ALTER TABLE review.order_proposals ADD COLUMN IF NOT EXISTS {definition}"
        )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'order_proposals_approval_dispatch_state'
                  AND conrelid = 'review.order_proposals'::regclass
            ) THEN
                ALTER TABLE review.order_proposals
                    ADD CONSTRAINT order_proposals_approval_dispatch_state
                    CHECK (
                        approval_dispatch_state IS NULL
                        OR approval_dispatch_state IN (
                            'pending',
                            'sent_current',
                            'sent_superseded',
                            'failed',
                            'partial_failed',
                            'failed_superseded'
                        )
                    );
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'order_proposals_approval_dispatch_card_kind'
                  AND conrelid = 'review.order_proposals'::regclass
            ) THEN
                ALTER TABLE review.order_proposals
                    ADD CONSTRAINT order_proposals_approval_dispatch_card_kind
                    CHECK (
                        approval_dispatch_card_kind IS NULL
                        OR approval_dispatch_card_kind IN (
                            'manual',
                            'reconfirm',
                            'auto_veto',
                            'loss_cut_confirmation'
                        )
                    );
            END IF;
        END
        $$;
        """
    )
    op.execute(
        """
        CREATE TABLE IF NOT EXISTS review.order_proposal_approval_dispatch_attempts (
            id BIGSERIAL PRIMARY KEY,
            attempt_id UUID NOT NULL,
            proposal_pk BIGINT NOT NULL,
            state TEXT NOT NULL,
            attempted_at TIMESTAMPTZ NOT NULL,
            completed_at TIMESTAMPTZ,
            payload_chars BIGINT NOT NULL,
            context_message_count INTEGER NOT NULL DEFAULT 0,
            message_id BIGINT,
            status_code INTEGER,
            telegram_error_code INTEGER,
            error_classification TEXT,
            failure_code TEXT,
            card_kind TEXT NOT NULL,
            membership_revision INTEGER NOT NULL,
            membership_digest TEXT NOT NULL,
            created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT now(),
            CONSTRAINT uq_order_proposal_approval_dispatch_attempt_id
                UNIQUE (attempt_id),
            CONSTRAINT fk_order_proposal_approval_dispatch_attempt_proposal
                FOREIGN KEY (proposal_pk)
                REFERENCES review.order_proposals(id) ON DELETE RESTRICT,
            CONSTRAINT order_proposal_approval_dispatch_attempt_state
                CHECK (
                    state IN (
                        'pending',
                        'sent_current',
                        'sent_superseded',
                        'failed',
                        'partial_failed',
                        'failed_superseded'
                    )
                ),
            CONSTRAINT order_proposal_approval_dispatch_attempt_card_kind
                CHECK (
                    card_kind IN (
                        'manual',
                        'reconfirm',
                        'auto_veto',
                        'loss_cut_confirmation'
                    )
                )
        )
        """
    )
    op.execute(
        """
        CREATE INDEX IF NOT EXISTS
            ix_order_proposal_approval_dispatch_attempts_proposal
        ON review.order_proposal_approval_dispatch_attempts
            (proposal_pk, attempted_at)
        """
    )
    for definition in (
        "approval_dispatch_state TEXT",
        "approval_dispatch_attempt_id UUID",
        "approval_dispatch_attempted_at TIMESTAMPTZ",
        "approval_dispatch_published_at TIMESTAMPTZ",
        "approval_dispatch_failure_code TEXT",
        "approval_dispatch_payload_chars BIGINT",
        "telegram_status_code INTEGER",
        "telegram_error_code INTEGER",
        "error_classification TEXT",
        "membership_revision INTEGER",
        "membership_digest TEXT",
        "membership_frozen_at TIMESTAMPTZ",
    ):
        op.execute(
            "ALTER TABLE review.order_proposal_approval_batches "
            f"ADD COLUMN IF NOT EXISTS {definition}"
        )
    op.execute(
        """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1
                FROM pg_constraint
                WHERE conname = 'order_proposal_approval_batches_dispatch_state'
                  AND conrelid =
                    'review.order_proposal_approval_batches'::regclass
            ) THEN
                ALTER TABLE review.order_proposal_approval_batches
                    ADD CONSTRAINT
                        order_proposal_approval_batches_dispatch_state
                    CHECK (
                        approval_dispatch_state IS NULL
                        OR approval_dispatch_state IN (
                            'pending',
                            'sent_current',
                            'sent_superseded',
                            'failed',
                            'partial_failed',
                            'failed_superseded'
                        )
                    );
            END IF;
        END
        $$;
        """
    )
    for definition in (
        "membership_revision INTEGER",
        "approval_dispatch_attempt_id_snapshot UUID",
        "approval_membership_revision_snapshot INTEGER",
        "approval_membership_digest_snapshot TEXT",
        "approval_card_kind_snapshot TEXT",
    ):
        op.execute(
            "ALTER TABLE review.order_proposal_approval_batch_members "
            f"ADD COLUMN IF NOT EXISTS {definition}"
        )


def downgrade() -> None:
    for column in (
        "approval_card_kind_snapshot",
        "approval_membership_digest_snapshot",
        "approval_membership_revision_snapshot",
        "approval_dispatch_attempt_id_snapshot",
        "membership_revision",
    ):
        op.execute(
            "ALTER TABLE review.order_proposal_approval_batch_members "
            f"DROP COLUMN IF EXISTS {column}"
        )
    op.execute(
        "ALTER TABLE review.order_proposal_approval_batches "
        "DROP CONSTRAINT IF EXISTS "
        "order_proposal_approval_batches_dispatch_state"
    )
    for column in (
        "membership_frozen_at",
        "membership_digest",
        "membership_revision",
        "error_classification",
        "telegram_error_code",
        "telegram_status_code",
        "approval_dispatch_payload_chars",
        "approval_dispatch_failure_code",
        "approval_dispatch_published_at",
        "approval_dispatch_attempted_at",
        "approval_dispatch_attempt_id",
        "approval_dispatch_state",
    ):
        op.execute(
            "ALTER TABLE review.order_proposal_approval_batches "
            f"DROP COLUMN IF EXISTS {column}"
        )
    op.execute("DROP TABLE IF EXISTS review.order_proposal_approval_dispatch_attempts")
    op.execute(
        "ALTER TABLE review.order_proposals "
        "DROP CONSTRAINT IF EXISTS order_proposals_approval_dispatch_card_kind"
    )
    op.execute(
        "ALTER TABLE review.order_proposals "
        "DROP CONSTRAINT IF EXISTS order_proposals_approval_dispatch_state"
    )
    for column in (
        "approval_dispatch_payload_chars",
        "approval_dispatch_published_at",
        "approval_dispatch_membership_digest",
        "approval_dispatch_membership_revision",
        "approval_dispatch_card_kind",
        "approval_dispatch_failure_code",
        "approval_dispatch_attempted_at",
        "approval_dispatch_attempt_id",
        "approval_dispatch_state",
    ):
        op.execute(
            f"ALTER TABLE {_SCHEMA}.order_proposals DROP COLUMN IF EXISTS {column}"
        )
