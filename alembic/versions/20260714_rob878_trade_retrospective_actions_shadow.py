"""ROB-878 child-1: retrospective action shadow ledger (schema + backfill).

Revision ID: 20260714_rob878_shadow
Revises: 20260713_rob848_paper_validation
Create Date: 2026-07-14

Expand-only shadow release: creates the canonical child table, singleton
control row (mode=shadow), parent JSON write-fence trigger, preflight
validation, backfill from existing next_actions JSONB, and parity assertion.
No canonical reads, no mutation, no cutover. Parent JSONB stays authoritative
and byte-for-byte unchanged.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision = "20260714_rob878_shadow"
down_revision = "20260713_rob848_paper_validation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_SCHEMA = "review"
_ACTIONS_TABLE = "trade_retrospective_actions"
_CONTROL_TABLE = "trade_retrospective_action_control"


_TRIGGER_FUNCTION_DDL = """
CREATE OR REPLACE FUNCTION review.guard_trade_retrospective_next_actions()
RETURNS trigger AS $$
DECLARE
    ctrl_mode TEXT;
    writer_marker TEXT;
BEGIN
    SELECT mode INTO ctrl_mode
    FROM review.trade_retrospective_action_control WHERE id = 1;

    IF ctrl_mode IS NULL OR ctrl_mode = 'shadow' THEN
        RETURN NEW;
    END IF;

    writer_marker := current_setting(
        'app.retrospective_action_projection_writer', true);

    IF writer_marker IS NULL OR writer_marker <> 'v1' THEN
        IF TG_OP = 'INSERT' THEN
            IF NEW.next_actions IS NOT NULL THEN
                RAISE EXCEPTION
                    'canonical mode: direct next_actions insert rejected; '
                    'use the action repository'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        ELSE
            IF NEW.next_actions IS DISTINCT FROM OLD.next_actions THEN
                RAISE EXCEPTION
                    'canonical mode: direct next_actions update rejected; '
                    'use the action repository'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END IF;
    END IF;

    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.create_table(
        _ACTIONS_TABLE,
        sa.Column(
            "id",
            postgresql.UUID(as_uuid=True),
            server_default=sa.text("gen_random_uuid()"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("retrospective_id", sa.BigInteger(), nullable=False),
        sa.Column("creation_key", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("action", sa.Text(), nullable=False),
        sa.Column("owner", sa.Text(), nullable=True),
        sa.Column("issue_id", sa.Text(), nullable=True),
        sa.Column(
            "status", sa.Text(), nullable=False, server_default="'open'"
        ),
        sa.Column("due_kst_date", sa.Date(), nullable=True),
        sa.Column(
            "version", sa.Integer(), nullable=False, server_default="1"
        ),
        sa.Column(
            "status_changed_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column("resolved_at", sa.TIMESTAMP(timezone=True), nullable=True),
        sa.Column("status_actor", sa.VARCHAR(128), nullable=False),
        sa.Column("status_source", sa.Text(), nullable=False),
        sa.Column("status_reason", sa.Text(), nullable=True),
        sa.Column(
            "status_evidence",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=True,
        ),
        sa.Column(
            "legacy_payload",
            postgresql.JSONB(astext_type=sa.Text()),
            nullable=False,
            server_default="'{}'::jsonb",
        ),
        sa.Column(
            "created_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('open','in_progress','done','obsolete','expired')",
            name="ck_trade_retrospective_actions_status",
        ),
        sa.CheckConstraint(
            "status_source IN ('migration','retrospective_save','web','mcp','triage','reconciler')",
            name="ck_trade_retrospective_actions_status_source",
        ),
        sa.CheckConstraint(
            "version >= 1", name="ck_trade_retrospective_actions_version"
        ),
        sa.CheckConstraint(
            "position >= 0",
            name="ck_trade_retrospective_actions_position_col",
        ),
        sa.CheckConstraint(
            "(status IN ('done','obsolete','expired') AND resolved_at IS NOT NULL) "
            "OR (status IN ('open','in_progress') AND resolved_at IS NULL)",
            name="ck_trade_retrospective_actions_resolved_terminal",
        ),
        sa.CheckConstraint(
            "(status NOT IN ('obsolete','expired')) "
            "OR (status_reason IS NOT NULL AND btrim(status_reason) <> '' "
            "AND length(status_reason) <= 2000)",
            name="ck_trade_retrospective_actions_reason_required",
        ),
        sa.CheckConstraint(
            "(status <> 'expired') "
            "OR (status_evidence IS NOT NULL "
            "AND jsonb_typeof(status_evidence) = 'object')",
            name="ck_trade_retrospective_actions_evidence_required",
        ),
        sa.ForeignKeyConstraint(
            ["retrospective_id"],
            ["review.trade_retrospectives.id"],
            ondelete="CASCADE",
            name="fk_trade_retrospective_actions_retrospective",
            deferrable=True,
            initially="DEFERRED",
        ),
        sa.UniqueConstraint(
            "retrospective_id",
            "position",
            name="uq_trade_retrospective_actions_position",
            deferrable=True,
            initially="DEFERRED",
        ),
        schema=_SCHEMA,
    )

    op.create_index(
        "ix_trade_retrospective_actions_parent_position",
        _ACTIONS_TABLE,
        ["retrospective_id", "position", "id"],
        schema=_SCHEMA,
    )
    op.create_index(
        "ix_trade_retrospective_actions_due_active",
        _ACTIONS_TABLE,
        ["due_kst_date", "id"],
        schema=_SCHEMA,
        postgresql_where=sa.text("status IN ('open', 'in_progress')"),
    )
    op.create_index(
        "uq_trade_retrospective_actions_creation_key",
        _ACTIONS_TABLE,
        ["retrospective_id", "creation_key"],
        schema=_SCHEMA,
        unique=True,
        postgresql_where=sa.text("creation_key IS NOT NULL"),
    )
    op.create_index(
        "ix_trade_retrospective_actions_issue_id",
        _ACTIONS_TABLE,
        ["issue_id"],
        schema=_SCHEMA,
        postgresql_where=sa.text("issue_id IS NOT NULL"),
    )
    op.create_index(
        "ix_trade_retrospective_actions_status_updated",
        _ACTIONS_TABLE,
        ["status", "updated_at", "id"],
        schema=_SCHEMA,
    )

    op.create_table(
        _CONTROL_TABLE,
        sa.Column(
            "id", sa.SmallInteger(), primary_key=True, nullable=False
        ),
        sa.Column("mode", sa.Text(), nullable=False),
        sa.Column(
            "cutover_at", sa.TIMESTAMP(timezone=True), nullable=True
        ),
        sa.Column("cutover_action_count", sa.Integer(), nullable=True),
        sa.Column(
            "updated_at",
            sa.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "id = 1",
            name="ck_trade_retrospective_action_control_singleton",
        ),
        sa.CheckConstraint(
            "mode IN ('shadow','canonical')",
            name="ck_trade_retrospective_action_control_mode",
        ),
        schema=_SCHEMA,
    )

    op.execute(
        "INSERT INTO review.trade_retrospective_action_control (id, mode) "
        "VALUES (1, 'shadow')"
    )

    op.execute(_TRIGGER_FUNCTION_DDL)
    op.execute(
        "DROP TRIGGER IF EXISTS trg_trade_retrospective_next_actions_fence "
        "ON review.trade_retrospectives"
    )
    op.execute(
        "CREATE TRIGGER trg_trade_retrospective_next_actions_fence "
        "BEFORE INSERT OR UPDATE ON review.trade_retrospectives "
        "FOR EACH ROW EXECUTE FUNCTION "
        "review.guard_trade_retrospective_next_actions()"
    )

    _run_preflight()
    _run_backfill()
    _assert_parity()


def _run_preflight() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            r RECORD;
            elem JSONB;
            idx INTEGER;
            raw_status TEXT;
            raw_due TEXT;
        BEGIN
            FOR r IN
                SELECT id, next_actions
                FROM review.trade_retrospectives
                WHERE next_actions IS NOT NULL
                  AND jsonb_typeof(next_actions) <> 'null'
            LOOP
                IF jsonb_typeof(r.next_actions) <> 'array' THEN
                    RAISE EXCEPTION
                        'retrospective %: next_actions is not an array (type=%)',
                        r.id, jsonb_typeof(r.next_actions);
                END IF;
                idx := 0;
                FOR elem IN SELECT * FROM jsonb_array_elements(r.next_actions)
                LOOP
                    IF jsonb_typeof(elem) <> 'object' THEN
                        RAISE EXCEPTION
                            'retrospective % action[%]: element is not an object',
                            r.id, idx;
                    END IF;
                    IF btrim(COALESCE(elem->>'action', '')) = '' THEN
                        RAISE EXCEPTION
                            'retrospective % action[%]: blank action',
                            r.id, idx;
                    END IF;
                    raw_status := elem->>'status';
                    IF raw_status IS NOT NULL
                       AND btrim(raw_status) <> ''
                       AND raw_status NOT IN ('open','in_progress','done') THEN
                        RAISE EXCEPTION
                            'retrospective % action[%]: unknown status "%"',
                            r.id, idx, raw_status;
                    END IF;
                    raw_due := elem->>'due_kst_date';
                    IF raw_due IS NOT NULL
                       AND btrim(raw_due) <> ''
                       AND raw_due !~ '^\\d{4}-\\d{2}-\\d{2}$' THEN
                        RAISE EXCEPTION
                            'retrospective % action[%]: invalid due_kst_date "%"',
                            r.id, idx, raw_due;
                    END IF;
                    idx := idx + 1;
                END LOOP;
            END LOOP;
        END;
        $$
        """
    )


def _run_backfill() -> None:
    op.execute(
        """
        INSERT INTO review.trade_retrospective_actions (
            id, retrospective_id, creation_key, position, action,
            owner, issue_id, status, due_kst_date, version,
            status_changed_at, resolved_at,
            status_actor, status_source, status_reason, status_evidence,
            legacy_payload, created_at, updated_at
        )
        SELECT
            gen_random_uuid(),
            t.id,
            NULL,
            (elem.ordinality - 1)::integer,
            btrim(elem.value->>'action'),
            elem.value->>'owner',
            elem.value->>'issue_id',
            CASE
                WHEN btrim(COALESCE(elem.value->>'status', '')) = ''
                    THEN 'open'
                ELSE elem.value->>'status'
            END,
            CASE
                WHEN btrim(COALESCE(elem.value->>'due_kst_date', '')) = ''
                    THEN NULL
                ELSE (elem.value->>'due_kst_date')::date
            END,
            1,
            t.updated_at,
            CASE
                WHEN elem.value->>'status' = 'done' THEN t.updated_at
                ELSE NULL
            END,
            'migration:rob-878',
            'migration',
            NULL,
            CASE
                WHEN elem.value->>'status' = 'done' THEN
                    jsonb_build_object(
                        'schema_version', 1,
                        'kind', 'legacy_status',
                        'source', 'migration',
                        'reference', 'review.trade_retrospectives.next_actions',
                        'observed_at', t.updated_at,
                        'summary', 'historical done; exact completion time unavailable'
                    )
                ELSE NULL
            END,
            elem.value,
            t.created_at,
            t.updated_at
        FROM review.trade_retrospectives t,
             jsonb_array_elements(t.next_actions) WITH ORDINALITY AS elem(value, ordinality)
        WHERE t.next_actions IS NOT NULL
          AND jsonb_typeof(t.next_actions) = 'array'
          AND jsonb_array_length(t.next_actions) > 0
        """
    )


def _assert_parity() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            parent_count INTEGER;
            child_count INTEGER;
        BEGIN
            SELECT COALESCE(SUM(
                CASE
                    WHEN next_actions IS NOT NULL
                     AND jsonb_typeof(next_actions) = 'array'
                     AND jsonb_array_length(next_actions) > 0
                    THEN jsonb_array_length(next_actions)
                    ELSE 0
                END
            ), 0)
            INTO parent_count
            FROM review.trade_retrospectives;

            SELECT count(*) INTO child_count
            FROM review.trade_retrospective_actions;

            IF parent_count <> child_count THEN
                RAISE EXCEPTION
                    'ROB-878 parity mismatch: parent has % actions, child has %',
                    parent_count, child_count;
            END IF;

            RAISE NOTICE 'ROB-878 shadow backfill: % actions migrated', child_count;
        END;
        $$
        """
    )


def downgrade() -> None:
    op.execute(
        """
        DO $$
        DECLARE
            ctrl_mode TEXT;
            max_version INTEGER;
            non_migration_count INTEGER;
        BEGIN
            SELECT mode INTO ctrl_mode
            FROM review.trade_retrospective_action_control WHERE id = 1;

            IF ctrl_mode = 'canonical' THEN
                RAISE EXCEPTION
                    'cannot downgrade: control mode is canonical; '
                    'recovery is mutation-disable plus roll-forward';
            END IF;

            SELECT COALESCE(max(version), 1) INTO max_version
            FROM review.trade_retrospective_actions;

            IF max_version > 1 THEN
                RAISE EXCEPTION
                    'cannot downgrade: actions have version > 1 '
                    '(canonical writes exist)';
            END IF;

            SELECT count(*) INTO non_migration_count
            FROM review.trade_retrospective_actions
            WHERE status_source <> 'migration';

            IF non_migration_count > 0 THEN
                RAISE EXCEPTION
                    'cannot downgrade: % actions have non-migration source',
                    non_migration_count;
            END IF;
        END;
        $$
        """
    )

    op.execute(
        "DROP TRIGGER IF EXISTS trg_trade_retrospective_next_actions_fence "
        "ON review.trade_retrospectives"
    )
    op.execute(
        "DROP FUNCTION IF EXISTS "
        "review.guard_trade_retrospective_next_actions()"
    )
    op.drop_table(_CONTROL_TABLE, schema=_SCHEMA)
    op.drop_index(
        "ix_trade_retrospective_actions_status_updated",
        table_name=_ACTIONS_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        "ix_trade_retrospective_actions_issue_id",
        table_name=_ACTIONS_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        "uq_trade_retrospective_actions_creation_key",
        table_name=_ACTIONS_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        "ix_trade_retrospective_actions_due_active",
        table_name=_ACTIONS_TABLE,
        schema=_SCHEMA,
    )
    op.drop_index(
        "ix_trade_retrospective_actions_parent_position",
        table_name=_ACTIONS_TABLE,
        schema=_SCHEMA,
    )
    op.drop_table(_ACTIONS_TABLE, schema=_SCHEMA)
