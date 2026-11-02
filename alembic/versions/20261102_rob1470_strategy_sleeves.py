"""ROB-1470: strategy sleeves inside the real accounts.

Additive: two new tables and one nullable column on ``review.order_proposals``.
Existing rows keep ``sleeve_id`` NULL and behave exactly as before.

Triggers hold the rules for any role, before and after the #789 Stage 4 role
split:

* ``review.strategy_sleeves`` is immutable identity: no UPDATE, DELETE or
  TRUNCATE.
* ``review.strategy_sleeve_versions`` is append-only history: no UPDATE, DELETE
  or TRUNCATE, and a new row's ``version`` must be exactly one more than the
  sleeve's last version, so history cannot be rewritten or skipped.
* The pre-registration (metric, minimum effect, horizon H, maximum duration;
  ROB-1468) must be complete on any ``live`` version (CHECK), and once a sleeve
  has had a ``live`` version its pre-registration can no longer change
  (trigger): a changed pre-registration is a new sleeve, not an edit.
* ``review.order_proposals.sleeve_id`` is set once at INSERT. A tagged INSERT
  must name a sleeve of the same account mode and market; any later change of
  the column is refused. Both triggers use a WHEN clause with no function call,
  so an untagged proposal write never runs trigger code.

The app role gets SELECT and INSERT only on the new tables.

Downgrade refuses while any sleeve exists or any proposal carries a sleeve,
because dropping the column would silently move those orders back into the
untagged book.

Revision ID: 20261102_rob1470_sleeves
Revises: 20261101_rob1440_intent_gw
Create Date: 2026-11-02
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261102_rob1470_sleeves"
down_revision: str | Sequence[str] | None = "20261101_rob1440_intent_gw"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_ACCOUNT_MARKET_SQL = (
    "(account_mode = 'kis_live' AND market = 'equity_kr') OR "
    "(account_mode = 'kis_live' AND market = 'equity_us') OR "
    "(account_mode = 'toss_live' AND market = 'equity_kr') OR "
    "(account_mode = 'toss_live' AND market = 'equity_us') OR "
    "(account_mode = 'upbit' AND market = 'crypto')"
)
_CURRENCY_SQL = (
    "(market = 'equity_kr' AND currency = 'KRW') OR "
    "(market = 'equity_us' AND currency = 'USD') OR "
    "(market = 'crypto' AND currency = 'KRW')"
)

#: One statement per entry. The test schema bootstrap installs these too, so the
#: DB tests exercise this exact DDL.
GUARD_DDL: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION review.strategy_sleeves_guard()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'review.strategy_sleeves rows are immutable (% refused)', TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_strategy_sleeves_guard ON review.strategy_sleeves",
    """
    CREATE TRIGGER trg_strategy_sleeves_guard
    BEFORE UPDATE OR DELETE ON review.strategy_sleeves
    FOR EACH ROW EXECUTE FUNCTION review.strategy_sleeves_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_strategy_sleeves_truncate ON review.strategy_sleeves",
    """
    CREATE TRIGGER trg_strategy_sleeves_truncate
    BEFORE TRUNCATE ON review.strategy_sleeves
    FOR EACH STATEMENT EXECUTE FUNCTION review.strategy_sleeves_guard()
    """,
    """
    CREATE OR REPLACE FUNCTION review.strategy_sleeve_versions_guard()
    RETURNS trigger AS $$
    DECLARE
        last_version integer;
    BEGIN
        IF TG_OP = 'INSERT' THEN
            SELECT max(version) INTO last_version
              FROM review.strategy_sleeve_versions
             WHERE sleeve_id = NEW.sleeve_id;
            IF NEW.version <> coalesce(last_version, 0) + 1 THEN
                RAISE EXCEPTION 'review.strategy_sleeve_versions version must follow the last version'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF EXISTS (
                SELECT 1 FROM review.strategy_sleeve_versions v
                 WHERE v.sleeve_id = NEW.sleeve_id AND v.state = 'live'
            ) AND EXISTS (
                SELECT 1 FROM review.strategy_sleeve_versions v
                 WHERE v.sleeve_id = NEW.sleeve_id AND v.version = last_version
                   AND (v.prereg_metric IS DISTINCT FROM NEW.prereg_metric
                        OR v.prereg_min_effect_pct IS DISTINCT FROM NEW.prereg_min_effect_pct
                        OR v.prereg_horizon_sessions IS DISTINCT FROM NEW.prereg_horizon_sessions
                        OR v.prereg_max_duration_days IS DISTINCT FROM NEW.prereg_max_duration_days)
            ) THEN
                RAISE EXCEPTION 'review.strategy_sleeve_versions pre-registration is frozen once the sleeve has been live'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'review.strategy_sleeve_versions is append-only (% refused)', TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_strategy_sleeve_versions_guard "
    "ON review.strategy_sleeve_versions",
    """
    CREATE TRIGGER trg_strategy_sleeve_versions_guard
    BEFORE INSERT OR UPDATE OR DELETE ON review.strategy_sleeve_versions
    FOR EACH ROW EXECUTE FUNCTION review.strategy_sleeve_versions_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_strategy_sleeve_versions_truncate "
    "ON review.strategy_sleeve_versions",
    """
    CREATE TRIGGER trg_strategy_sleeve_versions_truncate
    BEFORE TRUNCATE ON review.strategy_sleeve_versions
    FOR EACH STATEMENT EXECUTE FUNCTION review.strategy_sleeve_versions_guard()
    """,
    """
    CREATE OR REPLACE FUNCTION review.order_proposals_sleeve_guard()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION 'review.order_proposals.sleeve_id cannot change once set'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM review.strategy_sleeves s
             WHERE s.id = NEW.sleeve_id
               AND s.account_mode = NEW.account_mode
               AND s.market = NEW.market
        ) THEN
            RAISE EXCEPTION 'review.order_proposals.sleeve_id names a sleeve of another account or market'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_order_proposals_sleeve_insert ON review.order_proposals",
    """
    CREATE TRIGGER trg_order_proposals_sleeve_insert
    BEFORE INSERT ON review.order_proposals
    FOR EACH ROW WHEN (NEW.sleeve_id IS NOT NULL)
    EXECUTE FUNCTION review.order_proposals_sleeve_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_order_proposals_sleeve_update ON review.order_proposals",
    """
    CREATE TRIGGER trg_order_proposals_sleeve_update
    BEFORE UPDATE OF sleeve_id ON review.order_proposals
    FOR EACH ROW WHEN (OLD.sleeve_id IS DISTINCT FROM NEW.sleeve_id)
    EXECUTE FUNCTION review.order_proposals_sleeve_guard()
    """,
)

DOWNGRADE_GUARD = """
    DO $$
    BEGIN
        LOCK TABLE review.strategy_sleeves, review.strategy_sleeve_versions
            IN ACCESS EXCLUSIVE MODE;
        IF EXISTS (SELECT 1 FROM review.strategy_sleeves)
           OR EXISTS (
               SELECT 1 FROM review.order_proposals WHERE sleeve_id IS NOT NULL
           ) THEN
            RAISE EXCEPTION 'strategy sleeves hold rows or tag proposals; export them before downgrading'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END;
    $$
"""


def upgrade() -> None:
    op.create_table(
        "strategy_sleeves",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("sleeve_key", sa.Text(), nullable=False),
        sa.Column("rule_id", sa.Text(), nullable=False),
        sa.Column("account_mode", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("currency", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            _ACCOUNT_MARKET_SQL, name=op.f("ck_strategy_sleeves_account_market")
        ),
        sa.CheckConstraint(_CURRENCY_SQL, name=op.f("ck_strategy_sleeves_currency")),
        sa.CheckConstraint(
            "sleeve_key ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
            name=op.f("ck_strategy_sleeves_sleeve_key_format"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(rule_id)) BETWEEN 1 AND 128",
            name=op.f("ck_strategy_sleeves_rule_id_nonblank"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(created_by)) BETWEEN 1 AND 128",
            name=op.f("ck_strategy_sleeves_created_by_nonblank"),
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_sleeves"),
        sa.UniqueConstraint("sleeve_key", name="uq_strategy_sleeves_sleeve_key"),
        schema="review",
    )
    op.create_table(
        "strategy_sleeve_versions",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("sleeve_id", sa.BigInteger(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("state", sa.Text(), nullable=False),
        sa.Column("budget_cap", sa.Numeric(20, 4), nullable=False),
        sa.Column("loss_limit", sa.Numeric(20, 4), nullable=False),
        sa.Column("prereg_metric", sa.Text(), nullable=True),
        sa.Column("prereg_min_effect_pct", sa.Numeric(10, 4), nullable=True),
        sa.Column("prereg_horizon_sessions", sa.Integer(), nullable=True),
        sa.Column("prereg_max_duration_days", sa.Integer(), nullable=True),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("set_by", sa.Text(), nullable=False),
        sa.Column(
            "set_at",
            postgresql.TIMESTAMP(timezone=True),
            server_default=sa.text("now()"),
            nullable=False,
        ),
        sa.CheckConstraint(
            "version >= 1", name=op.f("ck_strategy_sleeve_versions_version_positive")
        ),
        sa.CheckConstraint(
            "state IN ('shadow','live','paused','stopped')",
            name=op.f("ck_strategy_sleeve_versions_state"),
        ),
        sa.CheckConstraint(
            "budget_cap >= 0",
            name=op.f("ck_strategy_sleeve_versions_budget_cap_nonnegative"),
        ),
        sa.CheckConstraint(
            "loss_limit > 0",
            name=op.f("ck_strategy_sleeve_versions_loss_limit_positive"),
        ),
        sa.CheckConstraint(
            "prereg_metric IS NULL OR char_length(btrim(prereg_metric)) BETWEEN 1 AND 300",
            name=op.f("ck_strategy_sleeve_versions_prereg_metric_nonblank"),
        ),
        sa.CheckConstraint(
            "prereg_min_effect_pct IS NULL OR prereg_min_effect_pct > 0",
            name=op.f("ck_strategy_sleeve_versions_prereg_min_effect_positive"),
        ),
        sa.CheckConstraint(
            "prereg_horizon_sessions IS NULL OR prereg_horizon_sessions >= 1",
            name=op.f("ck_strategy_sleeve_versions_prereg_horizon_positive"),
        ),
        sa.CheckConstraint(
            "prereg_max_duration_days IS NULL OR prereg_max_duration_days >= 1",
            name=op.f("ck_strategy_sleeve_versions_prereg_max_duration_positive"),
        ),
        sa.CheckConstraint(
            "state <> 'live' OR (prereg_metric IS NOT NULL "
            "AND prereg_min_effect_pct IS NOT NULL "
            "AND prereg_horizon_sessions IS NOT NULL "
            "AND prereg_max_duration_days IS NOT NULL)",
            name=op.f("ck_strategy_sleeve_versions_live_requires_prereg"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(reason)) BETWEEN 1 AND 500",
            name=op.f("ck_strategy_sleeve_versions_reason_nonblank"),
        ),
        sa.CheckConstraint(
            "char_length(btrim(set_by)) BETWEEN 1 AND 128",
            name=op.f("ck_strategy_sleeve_versions_set_by_nonblank"),
        ),
        sa.ForeignKeyConstraint(
            ["sleeve_id"],
            ["review.strategy_sleeves.id"],
            name="fk_strategy_sleeve_versions_sleeve_id_strategy_sleeves",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_strategy_sleeve_versions"),
        sa.UniqueConstraint(
            "sleeve_id",
            "version",
            name="uq_strategy_sleeve_versions_sleeve_version",
        ),
        schema="review",
    )
    op.add_column(
        "order_proposals",
        sa.Column("sleeve_id", sa.BigInteger(), nullable=True),
        schema="review",
    )
    op.create_foreign_key(
        "fk_order_proposals_sleeve_id_strategy_sleeves",
        "order_proposals",
        "strategy_sleeves",
        ["sleeve_id"],
        ["id"],
        source_schema="review",
        referent_schema="review",
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_order_proposals_sleeve_id",
        "order_proposals",
        ["sleeve_id"],
        schema="review",
    )
    for statement in GUARD_DDL:
        op.execute(statement)
    # The Stage 4 role may not exist in dev and CI databases.
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        "THEN GRANT USAGE ON SCHEMA review TO at_app; "
        "GRANT SELECT, INSERT ON review.strategy_sleeves TO at_app; "
        "GRANT SELECT, INSERT ON review.strategy_sleeve_versions TO at_app; "
        "GRANT USAGE, SELECT ON SEQUENCE review.strategy_sleeves_id_seq TO at_app; "
        "GRANT USAGE, SELECT ON SEQUENCE review.strategy_sleeve_versions_id_seq "
        "TO at_app; "
        "GRANT SELECT (sleeve_id), INSERT (sleeve_id) ON review.order_proposals "
        "TO at_app; "
        "END IF; END $$"
    )


def downgrade() -> None:
    op.execute(DOWNGRADE_GUARD)
    op.execute(
        "DROP TRIGGER IF EXISTS trg_order_proposals_sleeve_update "
        "ON review.order_proposals"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_order_proposals_sleeve_insert "
        "ON review.order_proposals"
    )
    op.drop_index(
        "ix_order_proposals_sleeve_id", table_name="order_proposals", schema="review"
    )
    op.drop_constraint(
        "fk_order_proposals_sleeve_id_strategy_sleeves",
        "order_proposals",
        type_="foreignkey",
        schema="review",
    )
    op.drop_column("order_proposals", "sleeve_id", schema="review")
    op.drop_table("strategy_sleeve_versions", schema="review")
    op.drop_table("strategy_sleeves", schema="review")
    op.execute("DROP FUNCTION IF EXISTS review.order_proposals_sleeve_guard()")
    op.execute("DROP FUNCTION IF EXISTS review.strategy_sleeve_versions_guard()")
    op.execute("DROP FUNCTION IF EXISTS review.strategy_sleeves_guard()")
