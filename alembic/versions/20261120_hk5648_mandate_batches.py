"""hk 3795 T3a: morning approval batches of the mandate (``mpb`` card).

Additive: two new ``review`` tables, one index and three triggers on the T1
``review.mandate_plan_events`` table. Nothing existing is altered.

* ``review.mandate_plan_batches`` — one batch per (realm, basket, target session),
  with the approval deadline (open of the target session) and the expiry (its close).
* ``review.mandate_plan_batch_members`` — one row per card issued for a plan of a
  batch. Card 1 is written in the batch's own transaction; a re-send is the next
  card with a new nonce digest and the same snapshot (clause id, clause hash, plan
  hash, kind).

Both tables are append-only for any role (no UPDATE, DELETE or TRUNCATE, I13).

Triggers hold the rules for any writer. Expiry and the approval deadline are read from
the wall clock (``clock_timestamp()``), never from ``now()``: a transaction that began
before the batch closed must not be able to answer after it closed:

* batch insert (I22): takes the basket lock (L2, the same key the store uses), and
  refuses a new batch while an earlier batch of the basket is still open — that is,
  not expired and with a plan that has no answer yet;
* member insert: the plan, clause and batch agree (realm, market, basket, sleeve,
  account reference, clause hash); the plan holds the authority state the card kind
  needs; card 1 only comes with its batch; later cards follow ``card_no`` and repeat the
  snapshot, and only while the batch is open and the plan is unanswered;
* plan event insert carrying ``batch_member_id`` (answers): under the sleeve lock and
  the per-plan card lock (the card insert takes the same one), the card is the newest
  card of its plan, the batch is not expired, the plan has no other answer, the event
  matches the card kind, the card's clause is the sleeve's current one (for all four
  answer events), and ``effective_session`` follows the approval deadline.

The app role gets SELECT and INSERT only (hard rule 18).

Downgrade refuses while any batch row exists.

Revision ID: 20261120_hk5648_mandate_batches
Revises: 20261120_hk5637_sleeve_kiwoom
Create Date: 2026-11-20
"""

# expand-contract: contract-step 5648
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261120_hk5648_mandate_batches"
down_revision: str | Sequence[str] | None = "20261120_hk5637_sleeve_kiwoom"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

TABLES_NEWEST_FIRST: tuple[str, ...] = (
    "mandate_plan_batch_members",
    "mandate_plan_batches",
)

_ANSWERED = """
    EXISTS (
        SELECT 1
          FROM review.mandate_plan_batch_members x
          JOIN review.mandate_plan_events e ON e.batch_member_id = x.id
         WHERE x.batch_id = {batch} AND x.plan_id = {plan}
    )
"""


def _guard_ddl() -> tuple[str, ...]:
    ddl: list[str] = []
    for table in TABLES_NEWEST_FIRST:
        ddl += [
            f"DROP TRIGGER IF EXISTS trg_{table}_immutable ON review.{table}",
            f"""
            CREATE TRIGGER trg_{table}_immutable
            BEFORE UPDATE OR DELETE ON review.{table}
            FOR EACH ROW EXECUTE FUNCTION review.mandate_rows_immutable()
            """,
            f"DROP TRIGGER IF EXISTS trg_{table}_truncate ON review.{table}",
            f"""
            CREATE TRIGGER trg_{table}_truncate
            BEFORE TRUNCATE ON review.{table}
            FOR EACH STATEMENT EXECUTE FUNCTION review.mandate_rows_immutable()
            """,
        ]
    ddl += [
        # The answer lookup by card id is on the batch-closing path.
        "CREATE INDEX IF NOT EXISTS ix_mandate_plan_events_batch_member "
        "ON review.mandate_plan_events (batch_member_id) "
        "WHERE batch_member_id IS NOT NULL",
        # --- batches (I22) ---------------------------------------------------
        """
        CREATE OR REPLACE FUNCTION review.mandate_plan_batches_guard()
        RETURNS trigger AS $$
        BEGIN
            IF current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'mandate batches need READ COMMITTED'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'mandate_basket:' || NEW.realm || ':' || NEW.basket_key, 0));
            IF EXISTS (
                SELECT 1
                  FROM review.mandate_plan_batches b
                 WHERE b.realm = NEW.realm
                   AND b.basket_key = NEW.basket_key
                   AND b.expires_at > clock_timestamp()
                   AND EXISTS (
                       SELECT 1
                         FROM review.mandate_plan_batch_members m
                        WHERE m.batch_id = b.id AND m.card_no = 1
                          AND NOT """
        + _ANSWERED.format(batch="m.batch_id", plan="m.plan_id")
        + """
                   )
            ) THEN
                RAISE EXCEPTION 'the basket has an open batch; a new one is made after it closes'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_mandate_plan_batches_guard "
        "ON review.mandate_plan_batches",
        """
        CREATE TRIGGER trg_mandate_plan_batches_guard
        BEFORE INSERT ON review.mandate_plan_batches
        FOR EACH ROW EXECUTE FUNCTION review.mandate_plan_batches_guard()
        """,
        # --- members ---------------------------------------------------------
        """
        CREATE OR REPLACE FUNCTION review.mandate_plan_batch_members_guard()
        RETURNS trigger AS $$
        DECLARE
            b record;
            pl record;
            tm record;
            first_card record;
            last_no integer;
            st text;
        BEGIN
            IF current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'mandate batch cards need READ COMMITTED'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            SELECT realm, basket_key, market, expires_at, created_at INTO b
              FROM review.mandate_plan_batches WHERE id = NEW.batch_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'the card names no batch'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'mandate_basket:' || b.realm || ':' || b.basket_key, 0));
            -- the answer trigger takes this same lock (always after the basket and
            -- sleeve locks), so a re-send and an answer of one plan cannot both pass
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'mandate_batch_member:' || NEW.batch_id || ':' || NEW.plan_id, 0));
            SELECT p.sleeve_id, p.account_ref,
                   review.mandate_realm(s.account_mode) AS realm, s.market
              INTO pl
              FROM review.mandate_plans p
              JOIN review.strategy_sleeves s ON s.id = p.sleeve_id
             WHERE p.id = NEW.plan_id;
            IF NOT FOUND OR pl.realm IS DISTINCT FROM b.realm
               OR pl.market <> b.market THEN
                RAISE EXCEPTION 'the plan is not of this batch realm and market'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            SELECT t.content_sha256, t.basket_key, t.account_ref, v.sleeve_id INTO tm
              FROM review.mandate_terms t
              JOIN review.strategy_sleeve_versions v ON v.id = t.sleeve_version_id
             WHERE t.id = NEW.terms_id;
            IF NOT FOUND OR tm.sleeve_id <> pl.sleeve_id
               OR tm.content_sha256 <> NEW.terms_sha256
               OR tm.basket_key <> b.basket_key
               OR tm.account_ref <> pl.account_ref THEN
                RAISE EXCEPTION 'the card clause is not the plan sleeve clause of this basket'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            SELECT max(card_no) INTO last_no
              FROM review.mandate_plan_batch_members
             WHERE batch_id = NEW.batch_id AND plan_id = NEW.plan_id;
            IF last_no IS NULL THEN
                IF NEW.card_no <> 1 THEN
                    RAISE EXCEPTION 'the first card of a plan is card 1'
                        USING ERRCODE = 'restrict_violation';
                END IF;
                IF b.created_at IS DISTINCT FROM transaction_timestamp() THEN
                    RAISE EXCEPTION 'a plan joins a batch only in the batch own transaction'
                        USING ERRCODE = 'restrict_violation';
                END IF;
                IF EXISTS (
                    SELECT 1
                      FROM review.mandate_plan_batches o
                     WHERE o.realm = b.realm
                       AND o.basket_key = b.basket_key
                       AND o.id <> NEW.batch_id
                       AND o.expires_at > clock_timestamp()
                       AND EXISTS (
                           SELECT 1
                             FROM review.mandate_plan_batch_members m
                            WHERE m.batch_id = o.id AND m.card_no = 1
                              AND NOT EXISTS (
                                  SELECT 1
                                    FROM review.mandate_plan_batch_members x
                                    JOIN review.mandate_plan_events e
                                      ON e.batch_member_id = x.id
                                   WHERE x.batch_id = m.batch_id
                                     AND x.plan_id = m.plan_id
                              )
                       )
                ) THEN
                    RAISE EXCEPTION 'the basket has an open batch; a plan joins only one open batch'
                        USING ERRCODE = 'restrict_violation';
                END IF;
                st := review.mandate_plan_auth_state(NEW.plan_id);
                IF (NEW.kind = 'new_plan' AND st IS DISTINCT FROM 'proposed')
                   OR (NEW.kind = 'reapproval'
                       AND st NOT IN ('approved', 'active')) THEN
                    RAISE EXCEPTION 'the plan authority does not fit the card kind'
                        USING ERRCODE = 'restrict_violation';
                END IF;
            ELSE
                IF NEW.card_no <> last_no + 1 THEN
                    RAISE EXCEPTION 'cards of a plan are numbered in sequence'
                        USING ERRCODE = 'restrict_violation';
                END IF;
                SELECT kind, terms_id, terms_sha256, plan_sha256 INTO first_card
                  FROM review.mandate_plan_batch_members
                 WHERE batch_id = NEW.batch_id AND plan_id = NEW.plan_id
                   AND card_no = 1;
                IF first_card.kind <> NEW.kind
                   OR first_card.terms_id <> NEW.terms_id
                   OR first_card.terms_sha256 <> NEW.terms_sha256
                   OR first_card.plan_sha256 <> NEW.plan_sha256 THEN
                    RAISE EXCEPTION 'a re-sent card keeps the snapshot of card 1'
                        USING ERRCODE = 'restrict_violation';
                END IF;
                IF clock_timestamp() >= b.expires_at THEN
                    RAISE EXCEPTION 'an expired batch takes no new card'
                        USING ERRCODE = 'restrict_violation';
                END IF;
                IF """
        + _ANSWERED.format(batch="NEW.batch_id", plan="NEW.plan_id")
        + """ THEN
                    RAISE EXCEPTION 'the plan of this batch is already answered'
                        USING ERRCODE = 'restrict_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_mandate_plan_batch_members_guard "
        "ON review.mandate_plan_batch_members",
        """
        CREATE TRIGGER trg_mandate_plan_batch_members_guard
        BEFORE INSERT ON review.mandate_plan_batch_members
        FOR EACH ROW EXECUTE FUNCTION review.mandate_plan_batch_members_guard()
        """,
        # --- answers: plan events that carry a card id ----------------------
        """
        CREATE OR REPLACE FUNCTION review.mandate_plan_events_batch_guard()
        RETURNS trigger AS $$
        DECLARE
            mem record;
            cur_terms bigint;
        BEGIN
            IF current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'mandate batch answers need READ COMMITTED'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            SELECT m.batch_id, m.plan_id, m.card_no, m.kind, m.terms_id,
                   m.terms_sha256, b.target_session, b.approval_deadline_at,
                   b.expires_at, v.sleeve_id
              INTO mem
              FROM review.mandate_plan_batch_members m
              JOIN review.mandate_plan_batches b ON b.id = m.batch_id
              JOIN review.mandate_terms t ON t.id = m.terms_id
              JOIN review.strategy_sleeve_versions v ON v.id = t.sleeve_version_id
             WHERE m.id = NEW.batch_member_id;
            IF NOT FOUND OR mem.plan_id <> NEW.plan_id THEN
                RAISE EXCEPTION 'the answer names no card of this plan'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            -- the sleeve lock is the one every sleeve-version writer takes
            -- (review.strategy_sleeves:<id>); the clause is read after it, so a
            -- version change cannot slip in between the check and this insert
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'review.strategy_sleeves:' || mem.sleeve_id, 0));
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'mandate_batch_member:' || mem.batch_id || ':' || mem.plan_id, 0));
            IF clock_timestamp() >= mem.expires_at THEN
                RAISE EXCEPTION 'the batch has expired'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF EXISTS (
                SELECT 1 FROM review.mandate_plan_batch_members x
                 WHERE x.batch_id = mem.batch_id AND x.plan_id = mem.plan_id
                   AND x.card_no > mem.card_no
            ) THEN
                RAISE EXCEPTION 'a newer card replaced this one'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF """
        + _ANSWERED.format(batch="mem.batch_id", plan="mem.plan_id")
        + """ THEN
                RAISE EXCEPTION 'the plan of this batch is already answered'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NOT ((mem.kind = 'new_plan' AND NEW.event IN ('approved', 'rejected'))
                 OR (mem.kind = 'reapproval'
                     AND NEW.event IN ('terms_rebound', 'retired'))) THEN
                RAISE EXCEPTION 'the event does not answer this kind of card'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.terms_id IS NOT NULL
               AND (NEW.terms_id <> mem.terms_id
                    OR NEW.terms_sha256 IS DISTINCT FROM mem.terms_sha256) THEN
                RAISE EXCEPTION 'the event clause is not the card clause'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            SELECT t.id INTO cur_terms
              FROM review.strategy_sleeve_versions v
              JOIN review.mandate_terms t ON t.sleeve_version_id = v.id
             WHERE v.id = (
                   SELECT x.id FROM review.strategy_sleeve_versions x
                    WHERE x.sleeve_id = mem.sleeve_id
                    ORDER BY x.version DESC LIMIT 1);
            IF cur_terms IS DISTINCT FROM mem.terms_id THEN
                RAISE EXCEPTION 'the card clause is no longer the live clause'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.event IN ('approved', 'terms_rebound') THEN
                IF clock_timestamp() < mem.approval_deadline_at THEN
                    IF NEW.effective_session <> mem.target_session THEN
                        RAISE EXCEPTION 'before the deadline an approval takes effect on the target session'
                            USING ERRCODE = 'restrict_violation';
                    END IF;
                ELSIF NEW.effective_session <= mem.target_session THEN
                    RAISE EXCEPTION 'after the deadline an approval takes effect after the target session'
                        USING ERRCODE = 'restrict_violation';
                END IF;
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_mandate_plan_events_batch_guard "
        "ON review.mandate_plan_events",
        # No function call in WHEN (#789 Stage 4: an event without a card id never
        # runs trigger code).
        """
        CREATE TRIGGER trg_mandate_plan_events_batch_guard
        BEFORE INSERT ON review.mandate_plan_events
        FOR EACH ROW WHEN (NEW.batch_member_id IS NOT NULL)
        EXECUTE FUNCTION review.mandate_plan_events_batch_guard()
        """,
    ]
    return tuple(ddl)


#: One statement per entry. The test schema bootstrap installs these too, so the DB
#: tests exercise this exact DDL.
GUARD_DDL: tuple[str, ...] = _guard_ddl()

DOWNGRADE_GUARD = """
    DO $$
    BEGIN
        LOCK TABLE review.mandate_plan_batches, review.mandate_plan_batch_members
            IN ACCESS EXCLUSIVE MODE;
        IF EXISTS (SELECT 1 FROM review.mandate_plan_batches)
           OR EXISTS (SELECT 1 FROM review.mandate_plan_batch_members)
           OR EXISTS (
               SELECT 1 FROM review.mandate_plan_events
                WHERE batch_member_id IS NOT NULL
           ) THEN
            RAISE EXCEPTION 'mandate batch tables hold rows; export them before downgrading'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END;
    $$
"""

_FUNCTIONS = (
    "review.mandate_plan_events_batch_guard()",
    "review.mandate_plan_batch_members_guard()",
    "review.mandate_plan_batches_guard()",
)


def _ck(table: str, name: str, sql: str) -> sa.CheckConstraint:
    return sa.CheckConstraint(sql, name=op.f(f"ck_{table}_{name}"))


def _ts(name: str = "created_at") -> sa.Column:
    return sa.Column(
        name,
        postgresql.TIMESTAMP(timezone=True),
        server_default=sa.text("now()"),
        nullable=False,
    )


def _create_tables() -> None:
    op.create_table(
        "mandate_plan_batches",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("realm", sa.Text(), nullable=False),
        sa.Column("basket_key", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("target_session", sa.Date(), nullable=False),
        sa.Column(
            "approval_deadline_at", postgresql.TIMESTAMP(timezone=True), nullable=False
        ),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        _ts(),
        _ck("mandate_plan_batches", "realm", "realm IN ('live','mock')"),
        _ck(
            "mandate_plan_batches",
            "market",
            "market IN ('equity_kr','equity_us')",
        ),
        _ck(
            "mandate_plan_batches",
            "basket_key_nonblank",
            "char_length(btrim(basket_key)) BETWEEN 1 AND 100",
        ),
        _ck(
            "mandate_plan_batches",
            "deadlines_ordered",
            "approval_deadline_at < expires_at",
        ),
        _ck(
            "mandate_plan_batches",
            "created_by_nonblank",
            "char_length(btrim(created_by)) BETWEEN 1 AND 128",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mandate_plan_batches"),
        sa.UniqueConstraint(
            "realm",
            "basket_key",
            "target_session",
            name="uq_mandate_plan_batches_basket_session",
        ),
        schema="review",
    )
    op.create_table(
        "mandate_plan_batch_members",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("batch_id", sa.BigInteger(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("card_no", sa.Integer(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("terms_id", sa.BigInteger(), nullable=False),
        sa.Column("terms_sha256", sa.Text(), nullable=False),
        sa.Column("plan_sha256", sa.Text(), nullable=False),
        sa.Column("nonce_digest", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        _ts(),
        _ck("mandate_plan_batch_members", "card_no_positive", "card_no >= 1"),
        _ck(
            "mandate_plan_batch_members",
            "kind",
            "kind IN ('new_plan','reapproval')",
        ),
        _ck(
            "mandate_plan_batch_members",
            "terms_sha256_format",
            "terms_sha256 ~ '^[0-9a-f]{64}$'",
        ),
        _ck(
            "mandate_plan_batch_members",
            "plan_sha256_format",
            "plan_sha256 ~ '^[0-9a-f]{64}$'",
        ),
        _ck(
            "mandate_plan_batch_members",
            "nonce_digest_format",
            "nonce_digest ~ '^[0-9a-f]{64}$'",
        ),
        _ck(
            "mandate_plan_batch_members",
            "created_by_nonblank",
            "char_length(btrim(created_by)) BETWEEN 1 AND 128",
        ),
        sa.ForeignKeyConstraint(
            ["batch_id"], ["review.mandate_plan_batches.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["review.mandate_plans.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["terms_id"], ["review.mandate_terms.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mandate_plan_batch_members"),
        sa.UniqueConstraint(
            "batch_id", "plan_id", "card_no", name="uq_mandate_plan_batch_members_card"
        ),
        sa.UniqueConstraint("nonce_digest", name="uq_mandate_plan_batch_members_nonce"),
        schema="review",
    )
    op.create_index(
        "ix_mandate_plan_batch_members_plan",
        "mandate_plan_batch_members",
        ["plan_id"],
        schema="review",
    )


def upgrade() -> None:
    _create_tables()
    for statement in GUARD_DDL:
        op.execute(statement)
    grants = "".join(
        [
            "GRANT USAGE ON SCHEMA review TO at_app; ",
            *(
                f"GRANT SELECT, INSERT ON review.{t} TO at_app; "
                for t in TABLES_NEWEST_FIRST
            ),
            *(
                f"GRANT USAGE, SELECT ON SEQUENCE review.{t}_id_seq TO at_app; "
                for t in TABLES_NEWEST_FIRST
            ),
        ]
    )
    # The Stage 4 role may not exist in dev and CI databases (hard rule 18).
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
        f"THEN {grants}END IF; END $$"
    )


def downgrade() -> None:
    op.execute(DOWNGRADE_GUARD)
    op.execute(
        "DROP TRIGGER IF EXISTS trg_mandate_plan_events_batch_guard "
        "ON review.mandate_plan_events"
    )
    op.execute("DROP INDEX IF EXISTS review.ix_mandate_plan_events_batch_member")
    op.drop_index(
        "ix_mandate_plan_batch_members_plan",
        table_name="mandate_plan_batch_members",
        schema="review",
    )
    for table in TABLES_NEWEST_FIRST:
        op.drop_table(table, schema="review")
    for function in _FUNCTIONS:
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
