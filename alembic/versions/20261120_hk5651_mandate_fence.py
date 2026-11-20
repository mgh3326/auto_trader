"""hk 5651 (3795 T4c): fence-area coordination tables and the auto-fence profile guard.

Additive. Two coordination tables and one trigger, nothing else changes:

* ``review.mandate_basket_authority`` — one row per (realm, basket key), the lock
  target F2 of design §4.6. A writer of a clause, an activation profile, a sleeve
  version or a plan registration bumps ``authority_seq`` in its own transaction; a
  send-fence transaction takes the row ``FOR SHARE`` and reads the latest facts
  after it. The row carries no amount and no authority value.
* ``review.mandate_cap_mutex`` — one row per serialization key (a sleeve, a
  basket day, an account-mode/market day), the lock target F3s/F3/F4. It carries
  no amount: totals are always re-summed from their sources.
* ``trg_mandate_plan_events_guard_fence_profile`` (named so that it fires after
  ``trg_mandate_plan_events_guard``, which checks the row's own shape first) — the second defence of the §4.6 "자격의
  재확인": an automatic ``tranche_send_fenced`` must cite the latest activation
  profile of the plan's sleeve, and that profile, the clause it opens and the
  sleeve's current version must still allow it. The first defence is the fence
  transaction itself, which reads these facts after F2.

Every basket that exists when this migration runs gets its authority row here;
afterwards clause and plan registration create it in the same transaction. A
send fence on a basket without a row is refused (``mandate_unreadable``).

Hard rule 18: conditional ``at_app`` GRANT. The two coordination tables get
SELECT, INSERT and UPDATE (a ``FOR SHARE``/``FOR UPDATE`` lock needs the privilege)
but never DELETE; triggers refuse DELETE, TRUNCATE and any change but the
authority counter's ``+1`` for every role.

The trigger compares expiry with the database clock (``clock_timestamp()``) while the
fence transaction compares with the caller's ``now``; a difference at an expiry boundary can
only make the trigger refuse a fence the Python check allowed (the safe direction).

Downgrade drops the two tables and the trigger; they hold no authority value.

Revision ID: 20261120_hk5651_mandate_fence
Revises: 20261120_hk5441_upbit_gw
Create Date: 2026-11-20
"""

# expand-contract: contract-step 5651
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "20261120_hk5651_mandate_fence"
down_revision: str | Sequence[str] | None = "20261120_hk5441_upbit_gw"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES: tuple[str, ...] = ("mandate_basket_authority", "mandate_cap_mutex")
FUNCTIONS: tuple[str, ...] = (
    "review.mandate_fence_profile_guard()",
    "review.mandate_authority_guard()",
    "review.mandate_cap_mutex_guard()",
)

#: The mutex key scopes and the shape of each key (``mandate_cap_mutex.mutex_key``).
MUTEX_SCOPES: tuple[str, ...] = ("sleeve", "basket", "global")
REALMS: tuple[str, ...] = ("live", "mock")

GUARD_DDL: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION review.mandate_authority_guard()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'UPDATE' THEN
            IF NEW.realm IS DISTINCT FROM OLD.realm
               OR NEW.basket_key IS DISTINCT FROM OLD.basket_key
               OR NEW.created_at IS DISTINCT FROM OLD.created_at
               OR NEW.authority_seq IS DISTINCT FROM OLD.authority_seq + 1 THEN
                RAISE EXCEPTION 'mandate_basket_authority only counts up by one'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'mandate_basket_authority rows are never deleted (% refused)', TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_mandate_basket_authority_guard "
    "ON review.mandate_basket_authority",
    """
    CREATE TRIGGER trg_mandate_basket_authority_guard
    BEFORE UPDATE OR DELETE ON review.mandate_basket_authority
    FOR EACH ROW EXECUTE FUNCTION review.mandate_authority_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_mandate_basket_authority_truncate "
    "ON review.mandate_basket_authority",
    """
    CREATE TRIGGER trg_mandate_basket_authority_truncate
    BEFORE TRUNCATE ON review.mandate_basket_authority
    FOR EACH STATEMENT EXECUTE FUNCTION review.mandate_authority_guard()
    """,
    """
    CREATE OR REPLACE FUNCTION review.mandate_cap_mutex_guard()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'mandate_cap_mutex rows are lock targets only (% refused)', TG_OP
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_mandate_cap_mutex_guard ON review.mandate_cap_mutex",
    """
    CREATE TRIGGER trg_mandate_cap_mutex_guard
    BEFORE UPDATE OR DELETE ON review.mandate_cap_mutex
    FOR EACH ROW EXECUTE FUNCTION review.mandate_cap_mutex_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_mandate_cap_mutex_truncate ON review.mandate_cap_mutex",
    """
    CREATE TRIGGER trg_mandate_cap_mutex_truncate
    BEFORE TRUNCATE ON review.mandate_cap_mutex
    FOR EACH STATEMENT EXECUTE FUNCTION review.mandate_cap_mutex_guard()
    """,
    # The second defence of the automatic fence's qualification (§4.6). It runs
    # after the plan-event trigger has accepted the row's own shape.
    """
    CREATE OR REPLACE FUNCTION review.mandate_fence_profile_guard()
    RETURNS trigger AS $$
    DECLARE
        pl record;
        latest record;
        cur record;
        bound_terms bigint;
    BEGIN
        SELECT p.sleeve_id INTO pl FROM review.mandate_plans p WHERE p.id = NEW.plan_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'the fence names no plan' USING ERRCODE = 'restrict_violation';
        END IF;
        SELECT ap.id, ap.terms_id, ap.max_tranche_index, ap.effective_session, ap.expires_at
          INTO latest
          FROM review.mandate_activation_profiles ap
         WHERE ap.sleeve_id = pl.sleeve_id
         ORDER BY ap.profile_version DESC LIMIT 1;
        IF NOT FOUND OR latest.id <> NEW.activation_profile_id THEN
            RAISE EXCEPTION 'an automatic fence cites the latest profile of the plan sleeve only'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.tranche_index > latest.max_tranche_index
           OR latest.effective_session > NEW.kst_day
           OR latest.expires_at <= clock_timestamp() THEN
            RAISE EXCEPTION 'the latest profile does not open this tranche now'
                USING ERRCODE = 'restrict_violation';
        END IF;
        SELECT v.state, t.id AS terms_id, t.expires_at INTO cur
          FROM review.strategy_sleeve_versions v
          JOIN review.mandate_terms t ON t.sleeve_version_id = v.id
         WHERE v.sleeve_id = pl.sleeve_id
           AND v.version = (SELECT max(v2.version)
                              FROM review.strategy_sleeve_versions v2
                             WHERE v2.sleeve_id = pl.sleeve_id);
        IF NOT FOUND OR cur.state <> 'live'
           OR cur.terms_id <> latest.terms_id
           OR cur.expires_at <= clock_timestamp() THEN
            RAISE EXCEPTION 'the profile clause is not the current live clause of the sleeve'
                USING ERRCODE = 'restrict_violation';
        END IF;
        SELECT e.terms_id INTO bound_terms
          FROM review.mandate_plan_events e
         WHERE e.plan_id = NEW.plan_id AND e.event IN ('approved', 'terms_rebound')
         ORDER BY e.seq DESC LIMIT 1;
        IF bound_terms IS DISTINCT FROM latest.terms_id THEN
            RAISE EXCEPTION 'the plan is bound to a clause the profile does not open'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_mandate_plan_events_guard_fence_profile "
    "ON review.mandate_plan_events",
    """
    CREATE TRIGGER trg_mandate_plan_events_guard_fence_profile
    BEFORE INSERT ON review.mandate_plan_events
    FOR EACH ROW
    WHEN (NEW.event = 'tranche_send_fenced' AND NEW.approval_mode = 'auto')
    EXECUTE FUNCTION review.mandate_fence_profile_guard()
    """,
)

#: Every basket that exists when the migration runs gets its authority row.
BACKFILL_AUTHORITY = """
    INSERT INTO review.mandate_basket_authority (realm, basket_key)
    SELECT DISTINCT review.mandate_realm(s.account_mode), t.basket_key
      FROM review.mandate_terms t
      JOIN review.strategy_sleeve_versions v ON v.id = t.sleeve_version_id
      JOIN review.strategy_sleeves s ON s.id = v.sleeve_id
     WHERE review.mandate_realm(s.account_mode) IS NOT NULL
    ON CONFLICT DO NOTHING
"""


def _ts() -> sa.Column:
    return sa.Column(
        "created_at",
        sa.TIMESTAMP(timezone=True),
        nullable=False,
        server_default=sa.text("now()"),
    )


def _create_tables() -> None:
    op.create_table(
        "mandate_basket_authority",
        sa.Column("realm", sa.Text(), nullable=False),
        sa.Column("basket_key", sa.Text(), nullable=False),
        sa.Column("authority_seq", sa.BigInteger(), nullable=False, server_default="0"),
        _ts(),
        sa.PrimaryKeyConstraint(
            "realm", "basket_key", name="pk_mandate_basket_authority"
        ),
        sa.CheckConstraint(
            "realm IN ('live','mock')", name=op.f("ck_mandate_basket_authority_realm")
        ),
        sa.CheckConstraint(
            "char_length(btrim(basket_key)) BETWEEN 1 AND 120",
            name=op.f("ck_mandate_basket_authority_basket_key"),
        ),
        sa.CheckConstraint(
            "authority_seq >= 0", name=op.f("ck_mandate_basket_authority_seq")
        ),
        schema="review",
    )
    op.create_table(
        "mandate_cap_mutex",
        sa.Column("mutex_key", sa.Text(), nullable=False),
        sa.Column("scope", sa.Text(), nullable=False),
        _ts(),
        sa.PrimaryKeyConstraint("mutex_key", name="pk_mandate_cap_mutex"),
        sa.CheckConstraint(
            "scope IN ('sleeve','basket','global')",
            name=op.f("ck_mandate_cap_mutex_scope"),
        ),
        sa.CheckConstraint(
            "char_length(mutex_key) BETWEEN 1 AND 400",
            name=op.f("ck_mandate_cap_mutex_key"),
        ),
        schema="review",
    )


def upgrade() -> None:
    _create_tables()
    for statement in GUARD_DDL:
        op.execute(statement)
    op.execute(BACKFILL_AUTHORITY)
    grants = "".join(
        [
            "GRANT USAGE ON SCHEMA review TO at_app; ",
            *(
                f"GRANT SELECT, INSERT, UPDATE ON review.{t} TO at_app; "
                for t in TABLES
            ),
        ]
    )
    # The Stage 4 role may not exist in dev and CI databases (hard rule 18).
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        f"THEN {grants}END IF; END $$"
    )


def downgrade() -> None:
    op.execute(
        "DROP TRIGGER IF EXISTS trg_mandate_plan_events_guard_fence_profile "
        "ON review.mandate_plan_events"
    )
    for table in reversed(TABLES):
        op.drop_table(table, schema="review")
    for function in FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
