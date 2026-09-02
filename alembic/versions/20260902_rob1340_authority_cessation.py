"""Add append-only Kiwoom authority attempt and cessation receipts.

Revision ID: 20260902_rob1340_authority
Revises: 20260831_rob1338_kiwoom_coord
Create Date: 2026-09-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20260902_rob1340_authority"
down_revision: str | Sequence[str] | None = "20260831_rob1338_kiwoom_coord"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_TERMINAL_PROOF_SQL = (
    "CASE kind "
    "WHEN 'no_key_acquired_proven' THEN "
    "terminal_state = 'NO_KEY_ACQUIRED_PROVEN' "
    "AND acquired_key_count = 0 AND in_flight_unknown = false "
    "AND ((lock_statement_dispatched = false AND lock_definite_false = false) "
    "OR (lock_statement_dispatched = true AND lock_definite_false = true)) "
    "AND unlock_true_count = 0 AND post_release_matching_rows = 0 "
    "AND termination_returned_exact_true IS NULL "
    "AND observer_pid_absent IS NULL "
    "WHEN 'advisory_unlock' THEN "
    "terminal_state = 'CESSATION_RECEIPT_COMMITTED' "
    "AND lock_statement_dispatched = true AND lock_definite_false = false "
    "AND acquired_key_count > 0 AND in_flight_unknown = false "
    "AND unlock_true_count = acquired_key_count "
    "AND post_release_matching_rows = 0 "
    "AND termination_returned_exact_true IS NULL "
    "AND observer_pid_absent IS NULL "
    "WHEN 'backend_termination' THEN "
    "terminal_state = 'CESSATION_RECEIPT_COMMITTED' "
    "AND lock_statement_dispatched = true AND lock_definite_false = false "
    "AND (acquired_key_count > 0 OR in_flight_unknown = true) "
    "AND post_release_matching_rows IS NULL "
    "AND termination_returned_exact_true = true "
    "AND observer_pid_absent = true "
    "WHEN 'unresolved_hold' THEN "
    "terminal_state = 'UNRESOLVED_HOLD' "
    "AND lock_statement_dispatched = true AND lock_definite_false = false "
    "AND (acquired_key_count > 0 OR in_flight_unknown = true) "
    "AND post_release_matching_rows IS NULL "
    "AND termination_returned_exact_true IS NULL "
    "AND observer_pid_absent IS NULL END"
)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS review")
    op.create_table(
        "kiwoom_authority_attempts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("authority_attempt_id", sa.Text(), nullable=False),
        sa.Column("contract_version", sa.Text(), nullable=False),
        sa.Column("lane_id", sa.Text(), nullable=False),
        sa.Column("cycle_id", sa.Text(), nullable=False),
        sa.Column("order_attempt_id", sa.Text(), nullable=False),
        sa.Column("owner_binding_digest", sa.Text(), nullable=False),
        sa.Column("keyset_digest", sa.Text(), nullable=False),
        sa.Column("key_count", sa.Integer(), nullable=False),
        sa.Column("baseline_matching_rows", sa.Integer(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "contract_version = 'rob1340.v1'",
            name="contract_version_rob1340_v1",
        ),
        sa.CheckConstraint("lane_id = 'kr.kiwoom.mock'", name="lane_kr_kiwoom_mock"),
        sa.CheckConstraint("key_count > 0", name="key_count_positive"),
        sa.CheckConstraint(
            "baseline_matching_rows = 0", name="baseline_matching_rows_zero"
        ),
        sa.CheckConstraint(
            "owner_binding_digest ~ '^[0-9a-f]{64}$'",
            name="owner_binding_digest_sha256",
        ),
        sa.CheckConstraint(
            "keyset_digest ~ '^[0-9a-f]{64}$'", name="keyset_digest_sha256"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "authority_attempt_id", name="uq_kiwoom_authority_attempt_id"
        ),
        schema="review",
    )
    op.create_index(
        "ix_kiwoom_authority_attempt_cycle",
        "kiwoom_authority_attempts",
        ["cycle_id", "id"],
        schema="review",
    )

    op.create_table(
        "kiwoom_authority_cessation_receipts",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("authority_attempt_id", sa.Text(), nullable=False),
        sa.Column("contract_version", sa.Text(), nullable=False),
        sa.Column("lane_id", sa.Text(), nullable=False),
        sa.Column("cycle_id", sa.Text(), nullable=False),
        sa.Column("order_attempt_id", sa.Text(), nullable=False),
        sa.Column("claim_row_id", sa.BigInteger(), nullable=True),
        sa.Column("owner_binding_digest", sa.Text(), nullable=False),
        sa.Column("keyset_digest", sa.Text(), nullable=False),
        sa.Column("key_count", sa.Integer(), nullable=False),
        sa.Column("terminal_state", sa.Text(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("lock_statement_dispatched", sa.Boolean(), nullable=False),
        sa.Column("lock_definite_false", sa.Boolean(), nullable=False),
        sa.Column("acquired_key_count", sa.Integer(), nullable=False),
        sa.Column("in_flight_unknown", sa.Boolean(), nullable=False),
        sa.Column("unlock_true_count", sa.Integer(), nullable=False),
        sa.Column("post_release_matching_rows", sa.Integer(), nullable=True),
        sa.Column("termination_returned_exact_true", sa.Boolean(), nullable=True),
        sa.Column("observer_pid_absent", sa.Boolean(), nullable=True),
        sa.Column("receipt_digest", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "contract_version = 'rob1340.v1'",
            name="contract_rob1340_v1",
        ),
        sa.CheckConstraint("lane_id = 'kr.kiwoom.mock'", name="lane_kr_kiwoom_mock"),
        sa.CheckConstraint("key_count > 0", name="key_count_positive"),
        sa.CheckConstraint(
            "acquired_key_count >= 0 AND acquired_key_count <= key_count",
            name="acquired_count_bounded",
        ),
        sa.CheckConstraint(
            "unlock_true_count >= 0 AND unlock_true_count <= acquired_key_count",
            name="unlock_count_bounded",
        ),
        sa.CheckConstraint(
            "owner_binding_digest ~ '^[0-9a-f]{64}$'",
            name="owner_digest_sha256",
        ),
        sa.CheckConstraint(
            "keyset_digest ~ '^[0-9a-f]{64}$'", name="keyset_digest_sha256"
        ),
        sa.CheckConstraint(
            "receipt_digest ~ '^[0-9a-f]{64}$'", name="receipt_digest_sha256"
        ),
        sa.CheckConstraint(
            "terminal_state IN "
            "('NO_KEY_ACQUIRED_PROVEN','CESSATION_RECEIPT_COMMITTED',"
            "'UNRESOLVED_HOLD')",
            name="terminal_state",
        ),
        sa.CheckConstraint(
            "kind IN ('no_key_acquired_proven','advisory_unlock',"
            "'backend_termination','unresolved_hold')",
            name="cessation_kind",
        ),
        sa.CheckConstraint(_TERMINAL_PROOF_SQL, name="kind_exact_proof"),
        sa.ForeignKeyConstraint(
            ["authority_attempt_id"],
            ["review.kiwoom_authority_attempts.authority_attempt_id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "authority_attempt_id", name="uq_kiwoom_authority_receipt_attempt"
        ),
        sa.UniqueConstraint(
            "receipt_digest", name="uq_kiwoom_authority_receipt_digest"
        ),
        schema="review",
    )
    op.create_index(
        "ix_kiwoom_authority_receipt_cycle",
        "kiwoom_authority_cessation_receipts",
        ["cycle_id", "id"],
        schema="review",
    )

    op.execute(
        """
        CREATE FUNCTION review.reject_kiwoom_authority_evidence_mutation()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION
                'review.% is append-only; % rejected', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """
    )
    for table in (
        "kiwoom_authority_attempts",
        "kiwoom_authority_cessation_receipts",
    ):
        op.execute(
            f"CREATE TRIGGER trg_{table}_immutable "
            f"BEFORE UPDATE OR DELETE ON review.{table} "
            "FOR EACH ROW EXECUTE FUNCTION "
            "review.reject_kiwoom_authority_evidence_mutation()"
        )
        op.execute(
            f"CREATE TRIGGER trg_{table}_truncate_immutable "
            f"BEFORE TRUNCATE ON review.{table} "
            "FOR EACH STATEMENT EXECUTE FUNCTION "
            "review.reject_kiwoom_authority_evidence_mutation()"
        )
        op.execute(f"REVOKE UPDATE, DELETE, TRUNCATE ON review.{table} FROM PUBLIC")


def downgrade() -> None:
    for table in (
        "kiwoom_authority_cessation_receipts",
        "kiwoom_authority_attempts",
    ):
        op.execute(
            f"DROP TRIGGER IF EXISTS trg_{table}_truncate_immutable ON review.{table}"
        )
        op.execute(f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON review.{table}")
    op.execute(
        "DROP FUNCTION IF EXISTS review.reject_kiwoom_authority_evidence_mutation()"
    )
    op.drop_index(
        "ix_kiwoom_authority_receipt_cycle",
        table_name="kiwoom_authority_cessation_receipts",
        schema="review",
    )
    op.drop_table("kiwoom_authority_cessation_receipts", schema="review")
    op.drop_index(
        "ix_kiwoom_authority_attempt_cycle",
        table_name="kiwoom_authority_attempts",
        schema="review",
    )
    op.drop_table("kiwoom_authority_attempts", schema="review")
