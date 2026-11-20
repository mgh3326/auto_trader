"""hk 5441: the intent gateway learns the upbit_live account.

Design: ``docs/design/5438-go-execution-path.md`` §2-§4 and §8.2.

Additive. No existing row changes: every v1 (kis_mock) intent and child keeps
exactly the bounds it had, and every existing sleeve is on the ``card`` path.

New tables
    ``review.execution_account_control`` (account kill row, seeded frozen),
    ``review.execution_mandates`` and ``review.execution_mandate_revocations``
    (append-only), ``review.execution_operator_exits`` (append-only, S6),
    ``review.execution_exit_handoffs`` (S5), and
    ``review.execution_child_order_evidence``.

New columns
    ``review.execution_intents``: the v2 columns (mandate, sleeve, mode, decimal
    quantities, ...); the four whole-share columns become nullable because a v2
    row does not have them. ``review.execution_child_requests.quantity_decimal``.
    ``review.strategy_sleeves.execution_path`` (S7).
    ``review.order_proposals.exit_handoff_id`` (S5).

Guards (any role, before and after the #789 Stage 4 role split)
    * An intent is still immutable. A v2 intent must sit inside its mandate at
      INSERT (I1, I3), or inside a valid operator exit row (S6).
    * A child still moves only out of ``sending``. ``shadowed`` is reachable
      only for a shadow intent, and a shadow intent can never record a broker
      order id or reach ``accepted`` / ``uncertain`` (I6). ``not_sent`` needs a
      reason from the closed list and no broker order id (B29).
    * Mandates, revocations and operator exits are append-only. A mandate binds
      only to a ``go`` sleeve with no proposal and no intent, one at a time (S7).
    * A proposal that tags a ``go`` sleeve must be the card its exit handoff has
      claimed, and at COMMIT that card has exactly one sell rung no larger than
      the handoff's reserved quantity (S5 d). A later rung insert is checked too.
    * An exit handoff only moves forward; a claim cannot change under a card.

The app role gets no DELETE on any new table.

Downgrade refuses while anything written under this revision exists: a mandate,
a v2 intent, a shadowed or not_sent child, a ``go`` sleeve, a handoff card.
Dropping them would forget which keys were used and which positions are handed
off.

Revision ID: 20261120_hk5441_upbit_gw
Revises: 20261120_hk5637_sleeve_kiwoom
Create Date: 2026-11-20
"""

# The DDL below is raw SQL kept as module constants (the test schema bootstrap
# installs the same statements), so the expand-contract lint cannot analyse this
# file and it carries the file marker. What the upgrade does to existing tables:
# adds nullable columns, drops NOT NULL on four execution_intents columns, and
# replaces CHECKs with ones that hold the old bounds for every existing row.
# expand-contract: contract-step 5441
from __future__ import annotations

from collections.abc import Sequence

from alembic import op

revision: str = "20261120_hk5441_upbit_gw"
down_revision: str | Sequence[str] | None = "20261120_hk5637_sleeve_kiwoom"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Equal to ``upbit_decisions.NOT_SENT_REASONS``; a test pins the two.
NOT_SENT_REASONS: tuple[str, ...] = (
    "account_mismatch",
    "authorization_expired",
    "exit_handoff_active",
    "inventory_constraint_exceeded",
    "lower_guard_refused",
    "mandate_expired",
    "mandate_missing",
    "mandate_revoked",
    "mandate_scope_mismatch",
    "open_sell_intent_exists",
    "operator_exit_invalid",
    "price_normalization_mismatch",
    "sleeve_holdings_unmeasured",
    "sleeve_not_live",
    "sleeve_not_shadow",
    "sleeve_pnl_unmeasured",
    "sleeve_unreadable",
    "sleeve_used_unmeasured",
    "submissions_killed",
    "upbit_live_disabled",
)
_NOT_SENT_SQL = ", ".join(f"'{reason}'" for reason in NOT_SENT_REASONS)

INTENT_ACCOUNT_BY_VERSION_SQL = (
    "(schema_version = 1 AND account = 'kis_mock' AND venue = 'krx') "
    "OR (schema_version = 2 AND account = 'upbit_live' AND venue = 'upbit')"
)
INTENT_V1_SHAPE_SQL = (
    "schema_version <> 1 OR ("
    "max_quantity IS NOT NULL AND max_quantity BETWEEN 1 AND 1000000 "
    "AND max_notional_krw BETWEEN 1 AND 5000000 "
    "AND limit_price_krw BETWEEN 1 AND 10000000 "
    "AND position_at_authorization IS NOT NULL "
    "AND min_position_after IS NOT NULL AND max_position_after IS NOT NULL "
    "AND position_at_authorization >= 0 AND min_position_after >= 0 "
    "AND min_position_after <= max_position_after "
    "AND mandate_id IS NULL AND sleeve_id IS NULL AND execution_mode IS NULL "
    "AND strategy_code_sha IS NULL AND decision_ref IS NULL AND origin IS NULL "
    "AND operator_exit_id IS NULL AND max_quantity_decimal IS NULL "
    "AND position_at_authorization_decimal IS NULL "
    "AND min_position_after_decimal IS NULL "
    "AND max_position_after_decimal IS NULL)"
)
INTENT_V2_SHAPE_SQL = (
    "schema_version <> 2 OR ("
    "max_quantity IS NULL AND position_at_authorization IS NULL "
    "AND min_position_after IS NULL AND max_position_after IS NULL "
    "AND mandate_id IS NOT NULL AND sleeve_id IS NOT NULL "
    "AND execution_mode IS NOT NULL AND execution_mode IN ('shadow', 'live') "
    "AND strategy_code_sha IS NOT NULL AND strategy_code_sha ~ '^[0-9a-f]{40}$' "
    "AND decision_ref IS NOT NULL AND char_length(decision_ref) BETWEEN 1 AND 128 "
    "AND origin IS NOT NULL AND origin IN ('strategy', 'operator_exit') "
    "AND (origin = 'operator_exit') = (operator_exit_id IS NOT NULL) "
    "AND (origin <> 'operator_exit' OR side = 'sell') "
    "AND symbol ~ '^KRW-[A-Z0-9]{2,10}$' "
    "AND max_quantity_decimal IS NOT NULL AND max_quantity_decimal > 0 "
    "AND max_quantity_decimal <= 1000000 "
    "AND max_notional_krw BETWEEN 5000 AND 1000000000000 "
    "AND limit_price_krw BETWEEN 1 AND 10000000000 "
    "AND position_at_authorization_decimal IS NOT NULL "
    "AND min_position_after_decimal IS NOT NULL "
    "AND max_position_after_decimal IS NOT NULL "
    "AND position_at_authorization_decimal >= 0 "
    "AND min_position_after_decimal >= 0 "
    "AND min_position_after_decimal <= max_position_after_decimal)"
)
CHILD_ORDER_VALUES_SQL = (
    "(action = 'cancel' AND quantity IS NULL AND quantity_decimal IS NULL "
    "AND limit_price_krw IS NULL) "
    "OR (action <> 'cancel' AND limit_price_krw IS NOT NULL AND limit_price_krw > 0 "
    "AND ((quantity IS NOT NULL AND quantity > 0 AND quantity_decimal IS NULL) "
    "OR (quantity IS NULL AND quantity_decimal IS NOT NULL "
    "AND quantity_decimal > 0)))"
)

#: New tables. ``IF NOT EXISTS`` so the test bootstrap can run these after
#: ``create_all`` built the same tables from the models.
TABLE_DDL: tuple[str, ...] = (
    """
    CREATE TABLE IF NOT EXISTS review.execution_account_control (
        account TEXT NOT NULL,
        mode TEXT NOT NULL,
        reason TEXT NOT NULL,
        updated_by TEXT NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_execution_account_control PRIMARY KEY (account),
        CONSTRAINT ck_execution_account_control_account
            CHECK (account IN ('upbit_live')),
        CONSTRAINT ck_execution_account_control_mode
            CHECK (mode IN ('ACTIVE', 'ENTRY_HALT', 'GLOBAL_FREEZE')),
        CONSTRAINT ck_execution_account_control_audit_nonblank
            CHECK (char_length(btrim(reason)) BETWEEN 1 AND 500 AND char_length(btrim(updated_by)) BETWEEN 1 AND 128)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review.execution_mandates (
        id UUID NOT NULL,
        terms_version TEXT NOT NULL,
        strategy TEXT NOT NULL,
        strategy_code_sha TEXT NOT NULL,
        sleeve_id BIGINT NOT NULL,
        sleeve_version_id BIGINT NOT NULL,
        account TEXT NOT NULL,
        mode TEXT NOT NULL,
        allowed_symbols TEXT[] NOT NULL,
        per_intent_max_notional_krw NUMERIC(20, 0) NOT NULL,
        max_open_intents INTEGER NOT NULL,
        price_band_bps INTEGER NOT NULL,
        max_intent_ttl INTERVAL NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        loss_sell_mode TEXT NOT NULL,
        account_ref TEXT,
        approval_ref TEXT NOT NULL,
        content_sha256 TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_execution_mandates PRIMARY KEY (id),
        CONSTRAINT fk_execution_mandates_sleeve_id_strategy_sleeves
            FOREIGN KEY (sleeve_id) REFERENCES review.strategy_sleeves (id)
            ON DELETE RESTRICT,
        CONSTRAINT fk_execution_mandates_sleeve_version
            FOREIGN KEY (sleeve_version_id)
            REFERENCES review.strategy_sleeve_versions (id) ON DELETE RESTRICT,
        CONSTRAINT ck_execution_mandates_terms_version
            CHECK (terms_version = 'execution-mandate/min-v1'),
        CONSTRAINT ck_execution_mandates_account CHECK (account IN ('upbit_live')),
        CONSTRAINT ck_execution_mandates_mode CHECK (mode IN ('shadow', 'live')),
        CONSTRAINT ck_execution_mandates_code_sha
            CHECK (strategy_code_sha ~ '^[0-9a-f]{40}$'),
        CONSTRAINT ck_execution_mandates_content_sha256
            CHECK (content_sha256 ~ '^[0-9a-f]{64}$'),
        CONSTRAINT ck_execution_mandates_allowed_symbols
            CHECK (cardinality(allowed_symbols) BETWEEN 1 AND 32),
        CONSTRAINT ck_execution_mandates_per_intent_max_notional
            CHECK (per_intent_max_notional_krw >= 5000 AND per_intent_max_notional_krw <= 1000000000000),
        CONSTRAINT ck_execution_mandates_max_open_intents
            CHECK (max_open_intents BETWEEN 1 AND 64),
        CONSTRAINT ck_execution_mandates_price_band
            CHECK (price_band_bps BETWEEN 0 AND 500),
        CONSTRAINT ck_execution_mandates_max_intent_ttl
            CHECK (max_intent_ttl > interval '0' AND max_intent_ttl <= interval '24 hours'),
        CONSTRAINT ck_execution_mandates_expires_after_created
            CHECK (expires_at > created_at),
        CONSTRAINT ck_execution_mandates_loss_sell_mode
            CHECK (loss_sell_mode = 'human_card'),
        CONSTRAINT ck_execution_mandates_live_requires_account_ref
            CHECK (mode <> 'live' OR (account_ref IS NOT NULL AND char_length(account_ref) BETWEEN 1 AND 200)),
        CONSTRAINT ck_execution_mandates_references_nonblank
            CHECK (char_length(btrim(strategy)) BETWEEN 1 AND 128 AND char_length(btrim(approval_ref)) BETWEEN 1 AND 200 AND char_length(btrim(created_by)) BETWEEN 1 AND 128)
    )
    """,
    "CREATE INDEX IF NOT EXISTS ix_execution_mandates_sleeve "
    "ON review.execution_mandates (sleeve_id)",
    """
    CREATE TABLE IF NOT EXISTS review.execution_mandate_revocations (
        mandate_id UUID NOT NULL,
        revoked_by TEXT NOT NULL,
        reason TEXT NOT NULL,
        revoked_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        CONSTRAINT pk_execution_mandate_revocations PRIMARY KEY (mandate_id),
        CONSTRAINT fk_execution_mandate_revocations_mandate_id_execution_mandates
            FOREIGN KEY (mandate_id) REFERENCES review.execution_mandates (id)
            ON DELETE RESTRICT,
        CONSTRAINT ck_execution_mandate_revocations_audit_nonblank
            CHECK (char_length(btrim(revoked_by)) BETWEEN 1 AND 128 AND char_length(btrim(reason)) BETWEEN 1 AND 500)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review.execution_operator_exits (
        id UUID NOT NULL,
        mandate_id UUID NOT NULL,
        symbol TEXT NOT NULL,
        max_quantity NUMERIC(28, 8) NOT NULL,
        reason TEXT NOT NULL,
        created_by TEXT NOT NULL,
        created_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
        CONSTRAINT pk_execution_operator_exits PRIMARY KEY (id),
        CONSTRAINT fk_execution_operator_exits_mandate_id_execution_mandates
            FOREIGN KEY (mandate_id) REFERENCES review.execution_mandates (id)
            ON DELETE RESTRICT,
        CONSTRAINT ck_execution_operator_exits_symbol
            CHECK (symbol ~ '^KRW-[A-Z0-9]{2,10}$'),
        CONSTRAINT ck_execution_operator_exits_max_quantity
            CHECK (max_quantity > 0 AND max_quantity <= 1000000),
        CONSTRAINT ck_execution_operator_exits_expires_within_one_hour
            CHECK (expires_at > created_at AND expires_at <= created_at + interval '1 hour'),
        CONSTRAINT ck_execution_operator_exits_audit_nonblank
            CHECK (char_length(btrim(reason)) BETWEEN 1 AND 500 AND char_length(btrim(created_by)) BETWEEN 1 AND 128)
    )
    """,
    """
    CREATE TABLE IF NOT EXISTS review.execution_exit_handoffs (
        id BIGSERIAL NOT NULL,
        sleeve_id BIGINT NOT NULL,
        symbol TEXT NOT NULL,
        mandate_id UUID NOT NULL,
        state TEXT NOT NULL,
        open_reason TEXT NOT NULL,
        proposal_id UUID,
        reserved_quantity NUMERIC(28, 8),
        claim_count INTEGER DEFAULT 0 NOT NULL,
        send_attempts INTEGER DEFAULT 0 NOT NULL,
        send_attempts_proven_unsent INTEGER DEFAULT 0 NOT NULL,
        fills_unmeasured BOOLEAN DEFAULT false NOT NULL,
        opened_by TEXT NOT NULL,
        opened_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        updated_at TIMESTAMP WITH TIME ZONE DEFAULT now() NOT NULL,
        closed_at TIMESTAMP WITH TIME ZONE,
        CONSTRAINT pk_execution_exit_handoffs PRIMARY KEY (id),
        CONSTRAINT fk_execution_exit_handoffs_sleeve_id_strategy_sleeves
            FOREIGN KEY (sleeve_id) REFERENCES review.strategy_sleeves (id)
            ON DELETE RESTRICT,
        CONSTRAINT fk_execution_exit_handoffs_mandate_id_execution_mandates
            FOREIGN KEY (mandate_id) REFERENCES review.execution_mandates (id)
            ON DELETE RESTRICT,
        CONSTRAINT ck_execution_exit_handoffs_state
            CHECK (state IN ('open', 'ready_for_card', 'card_claimed', 'closed')),
        CONSTRAINT ck_execution_exit_handoffs_symbol
            CHECK (symbol ~ '^KRW-[A-Z0-9]{2,10}$'),
        CONSTRAINT ck_execution_exit_handoffs_claim_columns
            CHECK ((state = 'card_claimed') = (proposal_id IS NOT NULL) AND (state = 'card_claimed') = (reserved_quantity IS NOT NULL)),
        CONSTRAINT ck_execution_exit_handoffs_reserved_quantity_positive
            CHECK (reserved_quantity IS NULL OR reserved_quantity > 0),
        CONSTRAINT ck_execution_exit_handoffs_closed
            CHECK ((state = 'closed') = (closed_at IS NOT NULL)),
        CONSTRAINT ck_execution_exit_handoffs_counters
            CHECK (claim_count >= 0 AND send_attempts >= 0 AND send_attempts_proven_unsent >= 0 AND send_attempts_proven_unsent <= send_attempts),
        CONSTRAINT ck_execution_exit_handoffs_audit_nonblank
            CHECK (char_length(btrim(open_reason)) BETWEEN 1 AND 64 AND char_length(btrim(opened_by)) BETWEEN 1 AND 128)
    )
    """,
    "CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_exit_handoffs_open "
    "ON review.execution_exit_handoffs (sleeve_id, symbol) "
    "WHERE state <> 'closed'",
    """
    CREATE TABLE IF NOT EXISTS review.execution_child_order_evidence (
        child_id BIGINT NOT NULL,
        order_uuid TEXT NOT NULL,
        broker_state TEXT NOT NULL,
        executed_volume NUMERIC(28, 8) NOT NULL,
        remaining_volume NUMERIC(28, 8),
        observed_at TIMESTAMP WITH TIME ZONE NOT NULL,
        recorded_by TEXT NOT NULL,
        CONSTRAINT pk_execution_child_order_evidence PRIMARY KEY (child_id),
        CONSTRAINT ck_execution_child_order_evidence_broker_state
            CHECK (broker_state IN ('wait', 'watch', 'done', 'cancel')),
        CONSTRAINT ck_execution_child_order_evidence_volumes
            CHECK (executed_volume >= 0 AND (remaining_volume IS NULL OR remaining_volume >= 0)),
        CONSTRAINT ck_execution_child_order_evidence_audit_nonblank
            CHECK (char_length(btrim(order_uuid)) BETWEEN 1 AND 64 AND char_length(btrim(recorded_by)) BETWEEN 1 AND 128)
    )
    """,
)


def _swap_check(table: str, name: str, sql: str, *dropped: str) -> tuple[str, ...]:
    """Drop the old CHECKs and (re)create ``name``; safe to run more than once."""
    statements = [
        f"ALTER TABLE review.{table} DROP CONSTRAINT IF EXISTS {old}"
        for old in (*dropped, name)
    ]
    statements.append(f"ALTER TABLE review.{table} ADD CONSTRAINT {name} CHECK ({sql})")
    return tuple(statements)


def _add_fk(table: str, name: str, column: str, target: str) -> tuple[str, ...]:
    return (
        f"ALTER TABLE review.{table} DROP CONSTRAINT IF EXISTS {name}",
        f"ALTER TABLE review.{table} ADD CONSTRAINT {name} FOREIGN KEY ({column}) "
        f"REFERENCES review.{target} (id) ON DELETE RESTRICT",
    )


#: Changes to existing tables. Idempotent for the same reason as ``TABLE_DDL``.
ALTER_DDL: tuple[str, ...] = (
    # --- review.strategy_sleeves.execution_path (S7) ---
    "ALTER TABLE review.strategy_sleeves ADD COLUMN IF NOT EXISTS execution_path "
    "TEXT DEFAULT 'card' NOT NULL",
    *_swap_check(
        "strategy_sleeves",
        "ck_strategy_sleeves_execution_path",
        "execution_path IN ('card', 'go')",
    ),
    # --- review.order_proposals.exit_handoff_id (S5) ---
    "ALTER TABLE review.order_proposals ADD COLUMN IF NOT EXISTS exit_handoff_id BIGINT",
    *_add_fk(
        "order_proposals",
        "fk_order_proposals_exit_handoff_id_execution_exit_handoffs",
        "exit_handoff_id",
        "execution_exit_handoffs",
    ),
    *_swap_check(
        "order_proposals",
        "ck_order_proposals_order_proposals_exit_handoff_needs_sleeve",
        "exit_handoff_id IS NULL OR sleeve_id IS NOT NULL",
    ),
    "CREATE INDEX IF NOT EXISTS ix_order_proposals_exit_handoff_id "
    "ON review.order_proposals (exit_handoff_id)",
    # --- review.execution_intents: the v2 columns ---
    "ALTER TABLE review.execution_intents ALTER COLUMN max_quantity DROP NOT NULL",
    "ALTER TABLE review.execution_intents "
    "ALTER COLUMN position_at_authorization DROP NOT NULL",
    "ALTER TABLE review.execution_intents ALTER COLUMN min_position_after DROP NOT NULL",
    "ALTER TABLE review.execution_intents ALTER COLUMN max_position_after DROP NOT NULL",
    "ALTER TABLE review.execution_intents ADD COLUMN IF NOT EXISTS mandate_id UUID",
    "ALTER TABLE review.execution_intents ADD COLUMN IF NOT EXISTS sleeve_id BIGINT",
    "ALTER TABLE review.execution_intents ADD COLUMN IF NOT EXISTS execution_mode TEXT",
    "ALTER TABLE review.execution_intents "
    "ADD COLUMN IF NOT EXISTS strategy_code_sha TEXT",
    "ALTER TABLE review.execution_intents ADD COLUMN IF NOT EXISTS decision_ref TEXT",
    "ALTER TABLE review.execution_intents ADD COLUMN IF NOT EXISTS origin TEXT",
    "ALTER TABLE review.execution_intents "
    "ADD COLUMN IF NOT EXISTS operator_exit_id UUID",
    "ALTER TABLE review.execution_intents "
    "ADD COLUMN IF NOT EXISTS max_quantity_decimal NUMERIC(28, 8)",
    "ALTER TABLE review.execution_intents "
    "ADD COLUMN IF NOT EXISTS position_at_authorization_decimal NUMERIC(28, 8)",
    "ALTER TABLE review.execution_intents "
    "ADD COLUMN IF NOT EXISTS min_position_after_decimal NUMERIC(28, 8)",
    "ALTER TABLE review.execution_intents "
    "ADD COLUMN IF NOT EXISTS max_position_after_decimal NUMERIC(28, 8)",
    *_add_fk(
        "execution_intents",
        "fk_execution_intents_mandate_id_execution_mandates",
        "mandate_id",
        "execution_mandates",
    ),
    *_add_fk(
        "execution_intents",
        "fk_execution_intents_sleeve_id_strategy_sleeves",
        "sleeve_id",
        "strategy_sleeves",
    ),
    *_add_fk(
        "execution_intents",
        "fk_execution_intents_operator_exit_id_execution_operator_exits",
        "operator_exit_id",
        "execution_operator_exits",
    ),
    "CREATE INDEX IF NOT EXISTS ix_execution_intents_sleeve "
    "ON review.execution_intents (sleeve_id)",
    *_swap_check(
        "execution_intents",
        "ck_execution_intents_schema_version",
        "schema_version IN (1, 2)",
    ),
    *_swap_check(
        "execution_intents",
        "ck_execution_intents_account_by_version",
        INTENT_ACCOUNT_BY_VERSION_SQL,
        "ck_execution_intents_account_mock_only",
        "ck_execution_intents_venue",
    ),
    *_swap_check(
        "execution_intents",
        "ck_execution_intents_v1_shape",
        INTENT_V1_SHAPE_SQL,
        "ck_execution_intents_max_quantity",
        "ck_execution_intents_max_notional",
        "ck_execution_intents_limit_price",
        "ck_execution_intents_inventory",
    ),
    *_swap_check(
        "execution_intents", "ck_execution_intents_v2_shape", INTENT_V2_SHAPE_SQL
    ),
    # --- review.execution_child_requests: decimal quantity and two states ---
    "ALTER TABLE review.execution_child_requests "
    "ADD COLUMN IF NOT EXISTS quantity_decimal NUMERIC(28, 8)",
    *_swap_check(
        "execution_child_requests",
        "ck_execution_child_requests_state",
        "state IN ('sending', 'accepted', 'uncertain', 'refused', "
        "'shadowed', 'not_sent')",
    ),
    *_swap_check(
        "execution_child_requests",
        "ck_execution_child_requests_refusal",
        "(state IN ('refused', 'not_sent')) = (refusal_reason IS NOT NULL)",
    ),
    *_swap_check(
        "execution_child_requests",
        "ck_execution_child_requests_order_values",
        CHILD_ORDER_VALUES_SQL,
    ),
)

_RESTRICT = "USING ERRCODE = 'restrict_violation'"

#: One statement per entry. The test schema bootstrap installs these too, so the
#: DB tests exercise this exact DDL.
GUARD_DDL: tuple[str, ...] = (
    # ------------------------------------------------------------------ intents
    f"""
    CREATE OR REPLACE FUNCTION review.execution_intents_guard()
    RETURNS trigger AS $$
    DECLARE
        m review.execution_mandates%ROWTYPE;
        x review.execution_operator_exits%ROWTYPE;
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.expires_at <= now()
               OR NEW.expires_at > now() + interval '24 hours' THEN
                RAISE EXCEPTION 'review.execution_intents expiry is outside the allowed window'
                    {_RESTRICT};
            END IF;
            IF NEW.schema_version = 2 THEN
                SELECT * INTO m FROM review.execution_mandates
                 WHERE id = NEW.mandate_id;
                IF NOT FOUND
                   OR m.account IS DISTINCT FROM NEW.account
                   OR m.sleeve_id IS DISTINCT FROM NEW.sleeve_id
                   OR m.strategy IS DISTINCT FROM NEW.strategy
                   OR m.strategy_code_sha IS DISTINCT FROM NEW.strategy_code_sha
                   OR m.mode IS DISTINCT FROM NEW.execution_mode
                   OR NOT coalesce(NEW.symbol = ANY (m.allowed_symbols), false) THEN
                    RAISE EXCEPTION 'review.execution_intents intent is outside its mandate'
                        {_RESTRICT};
                END IF;
                IF NEW.origin = 'operator_exit' THEN
                    SELECT * INTO x FROM review.execution_operator_exits
                     WHERE id = NEW.operator_exit_id;
                    IF NOT FOUND
                       OR x.mandate_id IS DISTINCT FROM NEW.mandate_id
                       OR x.symbol IS DISTINCT FROM NEW.symbol
                       OR NEW.side IS DISTINCT FROM 'sell'
                       OR x.expires_at <= now()
                       OR NEW.expires_at > x.expires_at
                       OR NOT coalesce(NEW.max_quantity_decimal <= x.max_quantity, false) THEN
                        RAISE EXCEPTION 'review.execution_intents operator exit intent is outside its exit row'
                            {_RESTRICT};
                    END IF;
                    -- S6 sets aside the mandate's expiry and revocation only:
                    -- its price band and intent lifetime still bind. The
                    -- lifetime runs from created_at, the instant the row is
                    -- stored: every later check measures it from there too.
                    -- created_at defaults to the transaction's start; a
                    -- writer that waited for its locks may set it to the
                    -- clock it read after the wait, and it is believed only
                    -- inside this transaction's own time. An expiry already
                    -- past at the INSERT is refused by the same clock.
                    IF NEW.created_at < now()
                       OR NEW.created_at > clock_timestamp()
                       OR NEW.expires_at <= clock_timestamp()
                       OR NEW.expires_at > NEW.created_at + m.max_intent_ttl
                       OR NEW.max_slippage_bps > m.price_band_bps THEN
                        RAISE EXCEPTION 'review.execution_intents operator exit intent exceeds its mandate'
                            {_RESTRICT};
                    END IF;
                ELSE
                    IF EXISTS (
                           SELECT 1 FROM review.execution_mandate_revocations r
                            WHERE r.mandate_id = m.id
                       )
                       OR m.expires_at <= now()
                       OR NEW.expires_at > m.expires_at
                       OR NEW.expires_at > now() + m.max_intent_ttl
                       OR NEW.max_slippage_bps > m.price_band_bps
                       OR (NEW.side = 'buy'
                           AND NEW.max_notional_krw > m.per_intent_max_notional_krw) THEN
                        RAISE EXCEPTION 'review.execution_intents intent exceeds its mandate'
                            {_RESTRICT};
                    END IF;
                END IF;
            END IF;
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'review.execution_intents rows are immutable'
            {_RESTRICT};
    END;
    $$ LANGUAGE plpgsql
    """,
    # ----------------------------------------------------------------- children
    f"""
    CREATE OR REPLACE FUNCTION review.execution_child_requests_guard()
    RETURNS trigger AS $$
    DECLARE
        intent_mode text;
        intent_version smallint;
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'review.execution_child_requests rows cannot be deleted'
                {_RESTRICT};
        END IF;
        IF TG_OP = 'INSERT' THEN
            IF NEW.state NOT IN ('sending', 'refused')
               OR NEW.broker_order_id IS NOT NULL
               OR NEW.result IS NOT NULL THEN
                RAISE EXCEPTION 'review.execution_child_requests new rows cannot carry an outcome'
                    {_RESTRICT};
            END IF;
            RETURN NEW;
        END IF;
        IF OLD.state <> 'sending'
           OR NEW.state NOT IN ('accepted', 'uncertain', 'shadowed', 'not_sent') THEN
            RAISE EXCEPTION 'review.execution_child_requests state change % to % refused',
                OLD.state, NEW.state {_RESTRICT};
        END IF;
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.intent_id IS DISTINCT FROM OLD.intent_id
           OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
           OR NEW.request_digest IS DISTINCT FROM OLD.request_digest
           OR NEW.action IS DISTINCT FROM OLD.action
           OR NEW.target_idempotency_key IS DISTINCT FROM OLD.target_idempotency_key
           OR NEW.quantity IS DISTINCT FROM OLD.quantity
           OR NEW.quantity_decimal IS DISTINCT FROM OLD.quantity_decimal
           OR NEW.limit_price_krw IS DISTINCT FROM OLD.limit_price_krw
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'review.execution_child_requests request fields are immutable'
                {_RESTRICT};
        END IF;
        IF NEW.refusal_reason IS DISTINCT FROM OLD.refusal_reason
           AND NEW.state <> 'not_sent' THEN
            RAISE EXCEPTION 'review.execution_child_requests request fields are immutable'
                {_RESTRICT};
        END IF;
        SELECT i.execution_mode, i.schema_version INTO intent_mode, intent_version
          FROM review.execution_intents i WHERE i.id = NEW.intent_id;
        IF intent_mode = 'shadow' THEN
            IF NEW.state NOT IN ('shadowed', 'not_sent')
               OR NEW.broker_order_id IS NOT NULL THEN
                RAISE EXCEPTION 'review.execution_child_requests a shadow intent cannot record a broker outcome'
                    {_RESTRICT};
            END IF;
        ELSIF NEW.state = 'shadowed' THEN
            RAISE EXCEPTION 'review.execution_child_requests only a shadow intent can be shadowed'
                {_RESTRICT};
        END IF;
        IF NEW.state = 'not_sent' THEN
            IF intent_version IS DISTINCT FROM 2
               OR NEW.broker_order_id IS NOT NULL
               OR NEW.refusal_reason IS NULL
               OR NEW.refusal_reason NOT IN ({_NOT_SENT_SQL}) THEN
                RAISE EXCEPTION 'review.execution_child_requests not_sent needs a proven reason and no broker order'
                    {_RESTRICT};
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    # ---------------------------------------------------------------- mandates
    f"""
    CREATE OR REPLACE FUNCTION review.execution_mandates_guard()
    RETURNS trigger AS $$
    DECLARE
        s review.strategy_sleeves%ROWTYPE;
    BEGIN
        IF TG_OP <> 'INSERT' THEN
            RAISE EXCEPTION 'review.execution_mandates is append-only (% refused)', TG_OP
                {_RESTRICT};
        END IF;
        SELECT * INTO s FROM review.strategy_sleeves WHERE id = NEW.sleeve_id;
        IF NOT FOUND
           OR s.execution_path IS DISTINCT FROM 'go'
           OR s.account_mode IS DISTINCT FROM 'upbit'
           OR s.market IS DISTINCT FROM 'crypto' THEN
            RAISE EXCEPTION 'review.execution_mandates binds only to a go-path upbit sleeve'
                {_RESTRICT};
        END IF;
        IF NOT EXISTS (
            SELECT 1 FROM review.strategy_sleeve_versions v
             WHERE v.id = NEW.sleeve_version_id AND v.sleeve_id = NEW.sleeve_id
        ) THEN
            RAISE EXCEPTION 'review.execution_mandates names a version of another sleeve'
                {_RESTRICT};
        END IF;
        IF EXISTS (
               SELECT 1 FROM review.order_proposals p WHERE p.sleeve_id = NEW.sleeve_id
           )
           OR EXISTS (
               SELECT 1 FROM review.execution_intents i WHERE i.sleeve_id = NEW.sleeve_id
           ) THEN
            RAISE EXCEPTION 'review.execution_mandates binds only to a sleeve with no order history'
                {_RESTRICT};
        END IF;
        IF EXISTS (
            SELECT 1 FROM review.execution_mandates o
             WHERE o.sleeve_id = NEW.sleeve_id
               AND NOT EXISTS (
                   SELECT 1 FROM review.execution_mandate_revocations r
                    WHERE r.mandate_id = o.id
               )
        ) THEN
            RAISE EXCEPTION 'review.execution_mandates the sleeve already has a mandate that is not revoked'
                {_RESTRICT};
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_execution_mandates_guard ON review.execution_mandates",
    """
    CREATE TRIGGER trg_execution_mandates_guard
    BEFORE INSERT OR UPDATE OR DELETE ON review.execution_mandates
    FOR EACH ROW EXECUTE FUNCTION review.execution_mandates_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_execution_mandates_truncate "
    "ON review.execution_mandates",
    """
    CREATE TRIGGER trg_execution_mandates_truncate
    BEFORE TRUNCATE ON review.execution_mandates
    FOR EACH STATEMENT EXECUTE FUNCTION review.execution_mandates_guard()
    """,
    # --------------------------------------- revocations, operator exits, control
    f"""
    CREATE OR REPLACE FUNCTION review.execution_append_only_guard()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'review.% is append-only (% refused)', TG_TABLE_NAME, TG_OP
            {_RESTRICT};
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_execution_mandate_revocations_guard "
    "ON review.execution_mandate_revocations",
    """
    CREATE TRIGGER trg_execution_mandate_revocations_guard
    BEFORE UPDATE OR DELETE ON review.execution_mandate_revocations
    FOR EACH ROW EXECUTE FUNCTION review.execution_append_only_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_execution_mandate_revocations_truncate "
    "ON review.execution_mandate_revocations",
    """
    CREATE TRIGGER trg_execution_mandate_revocations_truncate
    BEFORE TRUNCATE ON review.execution_mandate_revocations
    FOR EACH STATEMENT EXECUTE FUNCTION review.execution_append_only_guard()
    """,
    f"""
    CREATE OR REPLACE FUNCTION review.execution_operator_exits_guard()
    RETURNS trigger AS $$
    BEGIN
        IF NOT EXISTS (
            SELECT 1 FROM review.execution_mandates m
             WHERE m.id = NEW.mandate_id AND NEW.symbol = ANY (m.allowed_symbols)
        ) THEN
            RAISE EXCEPTION 'review.execution_operator_exits symbol is outside the mandate'
                {_RESTRICT};
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_execution_operator_exits_insert "
    "ON review.execution_operator_exits",
    """
    CREATE TRIGGER trg_execution_operator_exits_insert
    BEFORE INSERT ON review.execution_operator_exits
    FOR EACH ROW EXECUTE FUNCTION review.execution_operator_exits_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_execution_operator_exits_guard "
    "ON review.execution_operator_exits",
    """
    CREATE TRIGGER trg_execution_operator_exits_guard
    BEFORE UPDATE OR DELETE ON review.execution_operator_exits
    FOR EACH ROW EXECUTE FUNCTION review.execution_append_only_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_execution_operator_exits_truncate "
    "ON review.execution_operator_exits",
    """
    CREATE TRIGGER trg_execution_operator_exits_truncate
    BEFORE TRUNCATE ON review.execution_operator_exits
    FOR EACH STATEMENT EXECUTE FUNCTION review.execution_append_only_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_execution_account_control_delete "
    "ON review.execution_account_control",
    """
    CREATE TRIGGER trg_execution_account_control_delete
    BEFORE DELETE ON review.execution_account_control
    FOR EACH ROW EXECUTE FUNCTION review.execution_append_only_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_execution_account_control_truncate "
    "ON review.execution_account_control",
    """
    CREATE TRIGGER trg_execution_account_control_truncate
    BEFORE TRUNCATE ON review.execution_account_control
    FOR EACH STATEMENT EXECUTE FUNCTION review.execution_append_only_guard()
    """,
    # ---------------------------------------------------------- exit handoffs
    f"""
    CREATE OR REPLACE FUNCTION review.execution_exit_handoffs_guard()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
            RAISE EXCEPTION 'review.execution_exit_handoffs rows cannot be removed'
                {_RESTRICT};
        END IF;
        IF TG_OP = 'INSERT' THEN
            IF NEW.state <> 'open'
               OR NEW.claim_count <> 0
               OR NEW.send_attempts <> 0
               OR NEW.send_attempts_proven_unsent <> 0
               OR NEW.fills_unmeasured THEN
                RAISE EXCEPTION 'review.execution_exit_handoffs a new handoff starts open'
                    {_RESTRICT};
            END IF;
            RETURN NEW;
        END IF;
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.sleeve_id IS DISTINCT FROM OLD.sleeve_id
           OR NEW.symbol IS DISTINCT FROM OLD.symbol
           OR NEW.mandate_id IS DISTINCT FROM OLD.mandate_id
           OR NEW.open_reason IS DISTINCT FROM OLD.open_reason
           OR NEW.opened_by IS DISTINCT FROM OLD.opened_by
           OR NEW.opened_at IS DISTINCT FROM OLD.opened_at THEN
            RAISE EXCEPTION 'review.execution_exit_handoffs identity is immutable'
                {_RESTRICT};
        END IF;
        IF NOT (
               (OLD.state = 'open' AND NEW.state IN ('ready_for_card', 'closed'))
            OR (OLD.state = 'ready_for_card' AND NEW.state IN ('card_claimed', 'closed'))
            OR (OLD.state = 'card_claimed'
                AND NEW.state IN ('card_claimed', 'ready_for_card', 'closed'))
        ) THEN
            RAISE EXCEPTION 'review.execution_exit_handoffs state change % to % refused',
                OLD.state, NEW.state {_RESTRICT};
        END IF;
        IF OLD.state = 'ready_for_card' AND NEW.state = 'card_claimed' THEN
            IF NEW.claim_count <> OLD.claim_count + 1
               OR NEW.send_attempts <> 0
               OR NEW.send_attempts_proven_unsent <> 0 THEN
                RAISE EXCEPTION 'review.execution_exit_handoffs a claim starts with no send attempt'
                    {_RESTRICT};
            END IF;
        ELSIF OLD.state = 'card_claimed' AND NEW.state = 'card_claimed' THEN
            IF NEW.proposal_id IS DISTINCT FROM OLD.proposal_id
               OR NEW.reserved_quantity IS DISTINCT FROM OLD.reserved_quantity
               OR NEW.claim_count <> OLD.claim_count
               OR NEW.send_attempts < OLD.send_attempts
               OR NEW.send_attempts_proven_unsent < OLD.send_attempts_proven_unsent
               OR NEW.send_attempts_proven_unsent > NEW.send_attempts THEN
                RAISE EXCEPTION 'review.execution_exit_handoffs a claim cannot change under its card'
                    {_RESTRICT};
            END IF;
            -- O1, O3, O4: one attempt at a time. A new attempt is counted only
            -- when every earlier one was proven unsent, and it marks the
            -- sleeve unmeasured in the same row change.
            IF NEW.send_attempts <> OLD.send_attempts
               AND (NEW.send_attempts <> OLD.send_attempts + 1
                    OR OLD.send_attempts <> OLD.send_attempts_proven_unsent
                    OR NOT NEW.fills_unmeasured) THEN
                RAISE EXCEPTION 'review.execution_exit_handoffs a send attempt needs every earlier one proven unsent'
                    {_RESTRICT};
            END IF;
        ELSIF NEW.claim_count <> OLD.claim_count THEN
            RAISE EXCEPTION 'review.execution_exit_handoffs claim_count changes only at a claim'
                {_RESTRICT};
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_execution_exit_handoffs_guard "
    "ON review.execution_exit_handoffs",
    """
    CREATE TRIGGER trg_execution_exit_handoffs_guard
    BEFORE INSERT OR UPDATE OR DELETE ON review.execution_exit_handoffs
    FOR EACH ROW EXECUTE FUNCTION review.execution_exit_handoffs_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_execution_exit_handoffs_truncate "
    "ON review.execution_exit_handoffs",
    """
    CREATE TRIGGER trg_execution_exit_handoffs_truncate
    BEFORE TRUNCATE ON review.execution_exit_handoffs
    FOR EACH STATEMENT EXECUTE FUNCTION review.execution_exit_handoffs_guard()
    """,
    # ---------------------------------------------------- child order evidence
    f"""
    CREATE OR REPLACE FUNCTION review.execution_child_order_evidence_guard()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP IN ('DELETE', 'TRUNCATE') THEN
            RAISE EXCEPTION 'review.execution_child_order_evidence rows cannot be removed'
                {_RESTRICT};
        END IF;
        IF OLD.broker_state IN ('done', 'cancel')
           OR NEW.child_id IS DISTINCT FROM OLD.child_id
           OR NEW.order_uuid IS DISTINCT FROM OLD.order_uuid
           OR NEW.executed_volume < OLD.executed_volume THEN
            RAISE EXCEPTION 'review.execution_child_order_evidence a final or older reading cannot be replaced'
                {_RESTRICT};
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_execution_child_order_evidence_guard "
    "ON review.execution_child_order_evidence",
    """
    CREATE TRIGGER trg_execution_child_order_evidence_guard
    BEFORE UPDATE OR DELETE ON review.execution_child_order_evidence
    FOR EACH ROW EXECUTE FUNCTION review.execution_child_order_evidence_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_execution_child_order_evidence_truncate "
    "ON review.execution_child_order_evidence",
    """
    CREATE TRIGGER trg_execution_child_order_evidence_truncate
    BEFORE TRUNCATE ON review.execution_child_order_evidence
    FOR EACH STATEMENT EXECUTE FUNCTION review.execution_child_order_evidence_guard()
    """,
    # ----------------------------------------- proposals on a go sleeve (S5, S7)
    f"""
    CREATE OR REPLACE FUNCTION review.order_proposals_sleeve_guard()
    RETURNS trigger AS $$
    DECLARE
        sleeve_path text;
        h review.execution_exit_handoffs%ROWTYPE;
    BEGIN
        IF TG_OP = 'UPDATE' THEN
            RAISE EXCEPTION 'review.order_proposals.sleeve_id cannot change once set'
                {_RESTRICT};
        END IF;
        SELECT s.execution_path INTO sleeve_path
          FROM review.strategy_sleeves s
         WHERE s.id = NEW.sleeve_id
           AND s.account_mode = NEW.account_mode
           AND s.market = NEW.market;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'review.order_proposals.sleeve_id names a sleeve of another account or market'
                {_RESTRICT};
        END IF;
        IF sleeve_path = 'go' THEN
            SELECT * INTO h FROM review.execution_exit_handoffs
             WHERE id = NEW.exit_handoff_id;
            IF NOT FOUND
               OR h.sleeve_id IS DISTINCT FROM NEW.sleeve_id
               OR h.state IS DISTINCT FROM 'card_claimed'
               OR h.proposal_id IS DISTINCT FROM NEW.proposal_id
               OR NEW.side IS DISTINCT FROM 'sell'
               OR coalesce(NEW.action, 'place') <> 'place'
               OR (h.symbol IS DISTINCT FROM NEW.symbol
                   AND h.symbol IS DISTINCT FROM 'KRW-' || NEW.symbol) THEN
                RAISE EXCEPTION 'review.order_proposals a go-path sleeve takes only its claimed exit handoff card'
                    {_RESTRICT};
            END IF;
        ELSIF NEW.exit_handoff_id IS NOT NULL THEN
            RAISE EXCEPTION 'review.order_proposals.exit_handoff_id needs a go-path sleeve'
                {_RESTRICT};
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """,
    f"""
    CREATE OR REPLACE FUNCTION review.order_proposals_exit_handoff_guard()
    RETURNS trigger AS $$
    BEGIN
        RAISE EXCEPTION 'review.order_proposals.exit_handoff_id cannot change once set'
            {_RESTRICT};
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_order_proposals_exit_handoff_update "
    "ON review.order_proposals",
    """
    CREATE TRIGGER trg_order_proposals_exit_handoff_update
    BEFORE UPDATE OF exit_handoff_id ON review.order_proposals
    FOR EACH ROW WHEN (OLD.exit_handoff_id IS DISTINCT FROM NEW.exit_handoff_id)
    EXECUTE FUNCTION review.order_proposals_exit_handoff_guard()
    """,
    f"""
    CREATE OR REPLACE FUNCTION review.exit_handoff_card_shape_guard()
    RETURNS trigger AS $$
    DECLARE
        card_pk bigint;
        card_public_id uuid;
        card_handoff_id bigint;
        h review.execution_exit_handoffs%ROWTYPE;
        rung_count integer;
        bad_count integer;
    BEGIN
        IF TG_TABLE_NAME = 'order_proposals' THEN
            card_pk := NEW.id;
        ELSE
            card_pk := NEW.proposal_pk;
        END IF;
        SELECT p.proposal_id, p.exit_handoff_id INTO card_public_id, card_handoff_id
          FROM review.order_proposals p WHERE p.id = card_pk;
        IF card_handoff_id IS NULL THEN
            RETURN NULL;
        END IF;
        SELECT * INTO h FROM review.execution_exit_handoffs
         WHERE id = card_handoff_id;
        IF NOT FOUND
           OR h.state IS DISTINCT FROM 'card_claimed'
           OR h.proposal_id IS DISTINCT FROM card_public_id THEN
            RAISE EXCEPTION 'exit handoff card rungs can change only while the card holds the claim'
                {_RESTRICT};
        END IF;
        SELECT count(*),
               count(*) FILTER (
                   WHERE r.side IS DISTINCT FROM 'sell'
                      OR NOT coalesce(r.quantity > 0, false)
                      OR NOT coalesce(r.quantity <= h.reserved_quantity, false)
               )
          INTO rung_count, bad_count
          FROM review.order_proposal_rungs r
         WHERE r.proposal_pk = card_pk;
        IF rung_count <> 1 OR bad_count <> 0 THEN
            RAISE EXCEPTION 'an exit handoff card has exactly one sell rung within the reserved quantity'
                {_RESTRICT};
        END IF;
        RETURN NULL;
    END;
    $$ LANGUAGE plpgsql
    """,
    "DROP TRIGGER IF EXISTS trg_exit_handoff_card_shape ON review.order_proposals",
    """
    CREATE CONSTRAINT TRIGGER trg_exit_handoff_card_shape
    AFTER INSERT ON review.order_proposals
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW WHEN (NEW.exit_handoff_id IS NOT NULL)
    EXECUTE FUNCTION review.exit_handoff_card_shape_guard()
    """,
    "DROP TRIGGER IF EXISTS trg_exit_handoff_card_rung_shape "
    "ON review.order_proposal_rungs",
    """
    CREATE CONSTRAINT TRIGGER trg_exit_handoff_card_rung_shape
    AFTER INSERT OR UPDATE OF quantity, side ON review.order_proposal_rungs
    DEFERRABLE INITIALLY DEFERRED
    FOR EACH ROW
    EXECUTE FUNCTION review.exit_handoff_card_shape_guard()
    """,
)

#: The account starts frozen: an operator arms it on purpose.
SEED_DDL: tuple[str, ...] = (
    "INSERT INTO review.execution_account_control (account, mode, reason, updated_by) "
    "VALUES ('upbit_live', 'GLOBAL_FREEZE', 'initial_control_row', 'migration-hk5441') "
    "ON CONFLICT (account) DO NOTHING",
)

#: The Stage 4 role may not exist in dev and CI databases (hard rule 18).
GRANT_DDL: str = (
    "DO $$ BEGIN IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'at_app') "
    "THEN GRANT USAGE ON SCHEMA review TO at_app; "
    "GRANT SELECT ON review.execution_account_control TO at_app; "
    "GRANT UPDATE (mode, reason, updated_by, updated_at) "
    "ON review.execution_account_control TO at_app; "
    "GRANT SELECT, INSERT ON review.execution_mandates TO at_app; "
    "GRANT UPDATE (id) ON review.execution_mandates TO at_app; "
    "GRANT SELECT, INSERT ON review.execution_mandate_revocations TO at_app; "
    "GRANT SELECT, INSERT ON review.execution_operator_exits TO at_app; "
    "GRANT SELECT, INSERT ON review.execution_exit_handoffs TO at_app; "
    "GRANT UPDATE (state, proposal_id, reserved_quantity, claim_count, "
    "send_attempts, send_attempts_proven_unsent, fills_unmeasured, updated_at, "
    "closed_at) ON review.execution_exit_handoffs TO at_app; "
    "GRANT USAGE, SELECT ON SEQUENCE review.execution_exit_handoffs_id_seq "
    "TO at_app; "
    "GRANT SELECT, INSERT ON review.execution_child_order_evidence TO at_app; "
    "GRANT UPDATE (broker_state, executed_volume, remaining_volume, observed_at, "
    "recorded_by) ON review.execution_child_order_evidence TO at_app; "
    "GRANT UPDATE (refusal_reason) ON review.execution_child_requests TO at_app; "
    "GRANT SELECT (exit_handoff_id), INSERT (exit_handoff_id) "
    "ON review.order_proposals TO at_app; "
    "END IF; END $$"
)

#: Losing these rows would forget used keys, open handoffs and who may trade.
DOWNGRADE_GUARD: str = """
    DO $$
    BEGIN
        LOCK TABLE review.execution_child_requests, review.execution_intents,
                   review.execution_mandates, review.execution_exit_handoffs,
                   review.strategy_sleeves
            IN ACCESS EXCLUSIVE MODE;
        IF EXISTS (SELECT 1 FROM review.execution_mandates)
           OR EXISTS (SELECT 1 FROM review.execution_mandate_revocations)
           OR EXISTS (SELECT 1 FROM review.execution_operator_exits)
           OR EXISTS (SELECT 1 FROM review.execution_exit_handoffs)
           OR EXISTS (SELECT 1 FROM review.execution_child_order_evidence)
           OR EXISTS (SELECT 1 FROM review.execution_intents WHERE schema_version <> 1)
           OR EXISTS (
               SELECT 1 FROM review.execution_child_requests
                WHERE state IN ('shadowed', 'not_sent') OR quantity_decimal IS NOT NULL
           )
           OR EXISTS (SELECT 1 FROM review.strategy_sleeves WHERE execution_path <> 'card')
           OR EXISTS (
               SELECT 1 FROM review.order_proposals WHERE exit_handoff_id IS NOT NULL
           ) THEN
            RAISE EXCEPTION 'upbit intent gateway rows exist; export and clear them before downgrading'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END;
    $$
"""

#: The three functions this revision replaced, as the earlier revisions wrote
#: them (20261101_rob1440_intent_gw and 20261102_rob1470_sleeves).
PREVIOUS_GUARD_DDL: tuple[str, ...] = (
    """
    CREATE OR REPLACE FUNCTION review.execution_intents_guard()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'INSERT' THEN
            IF NEW.expires_at <= now()
               OR NEW.expires_at > now() + interval '24 hours' THEN
                RAISE EXCEPTION 'review.execution_intents expiry is outside the allowed window'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END IF;
        RAISE EXCEPTION 'review.execution_intents rows are immutable'
            USING ERRCODE = 'restrict_violation';
    END;
    $$ LANGUAGE plpgsql
    """,
    """
    CREATE OR REPLACE FUNCTION review.execution_child_requests_guard()
    RETURNS trigger AS $$
    BEGIN
        IF TG_OP = 'DELETE' THEN
            RAISE EXCEPTION 'review.execution_child_requests rows cannot be deleted'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF TG_OP = 'INSERT' THEN
            IF NEW.state NOT IN ('sending', 'refused')
               OR NEW.broker_order_id IS NOT NULL
               OR NEW.result IS NOT NULL THEN
                RAISE EXCEPTION 'review.execution_child_requests new rows cannot carry an outcome'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END IF;
        IF OLD.state <> 'sending' OR NEW.state NOT IN ('accepted', 'uncertain') THEN
            RAISE EXCEPTION 'review.execution_child_requests state change % to % refused',
                OLD.state, NEW.state USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.id IS DISTINCT FROM OLD.id
           OR NEW.intent_id IS DISTINCT FROM OLD.intent_id
           OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
           OR NEW.request_digest IS DISTINCT FROM OLD.request_digest
           OR NEW.action IS DISTINCT FROM OLD.action
           OR NEW.target_idempotency_key IS DISTINCT FROM OLD.target_idempotency_key
           OR NEW.quantity IS DISTINCT FROM OLD.quantity
           OR NEW.limit_price_krw IS DISTINCT FROM OLD.limit_price_krw
           OR NEW.refusal_reason IS DISTINCT FROM OLD.refusal_reason
           OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
            RAISE EXCEPTION 'review.execution_child_requests request fields are immutable'
                USING ERRCODE = 'restrict_violation';
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
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
)

_DOWNGRADE_DDL: tuple[str, ...] = (
    "DROP TRIGGER IF EXISTS trg_exit_handoff_card_rung_shape "
    "ON review.order_proposal_rungs",
    "DROP TRIGGER IF EXISTS trg_exit_handoff_card_shape ON review.order_proposals",
    "DROP TRIGGER IF EXISTS trg_order_proposals_exit_handoff_update "
    "ON review.order_proposals",
    *PREVIOUS_GUARD_DDL,
    "DROP FUNCTION IF EXISTS review.exit_handoff_card_shape_guard()",
    "DROP FUNCTION IF EXISTS review.order_proposals_exit_handoff_guard()",
    "DROP INDEX IF EXISTS review.ix_order_proposals_exit_handoff_id",
    "ALTER TABLE review.order_proposals DROP CONSTRAINT IF EXISTS "
    "ck_order_proposals_order_proposals_exit_handoff_needs_sleeve",
    "ALTER TABLE review.order_proposals DROP CONSTRAINT IF EXISTS "
    "fk_order_proposals_exit_handoff_id_execution_exit_handoffs",
    "ALTER TABLE review.order_proposals DROP COLUMN IF EXISTS exit_handoff_id",
    # execution_child_requests back to its 20261101 shape
    "ALTER TABLE review.execution_child_requests "
    "DROP CONSTRAINT IF EXISTS ck_execution_child_requests_order_values",
    "ALTER TABLE review.execution_child_requests "
    "DROP CONSTRAINT IF EXISTS ck_execution_child_requests_refusal",
    "ALTER TABLE review.execution_child_requests "
    "DROP CONSTRAINT IF EXISTS ck_execution_child_requests_state",
    "ALTER TABLE review.execution_child_requests DROP COLUMN IF EXISTS quantity_decimal",
    "ALTER TABLE review.execution_child_requests ADD CONSTRAINT "
    "ck_execution_child_requests_state "
    "CHECK (state IN ('sending', 'accepted', 'uncertain', 'refused'))",
    "ALTER TABLE review.execution_child_requests ADD CONSTRAINT "
    "ck_execution_child_requests_refusal "
    "CHECK ((state = 'refused') = (refusal_reason IS NOT NULL))",
    "ALTER TABLE review.execution_child_requests ADD CONSTRAINT "
    "ck_execution_child_requests_order_values CHECK ("
    "(action = 'cancel' AND quantity IS NULL AND limit_price_krw IS NULL) "
    "OR (action <> 'cancel' AND quantity > 0 AND limit_price_krw > 0))",
    # execution_intents back to its 20261101 shape
    "ALTER TABLE review.execution_intents "
    "DROP CONSTRAINT IF EXISTS ck_execution_intents_v2_shape",
    "ALTER TABLE review.execution_intents "
    "DROP CONSTRAINT IF EXISTS ck_execution_intents_v1_shape",
    "ALTER TABLE review.execution_intents "
    "DROP CONSTRAINT IF EXISTS ck_execution_intents_account_by_version",
    "ALTER TABLE review.execution_intents "
    "DROP CONSTRAINT IF EXISTS ck_execution_intents_schema_version",
    "DROP INDEX IF EXISTS review.ix_execution_intents_sleeve",
    "ALTER TABLE review.execution_intents DROP COLUMN IF EXISTS mandate_id",
    "ALTER TABLE review.execution_intents DROP COLUMN IF EXISTS sleeve_id",
    "ALTER TABLE review.execution_intents DROP COLUMN IF EXISTS execution_mode",
    "ALTER TABLE review.execution_intents DROP COLUMN IF EXISTS strategy_code_sha",
    "ALTER TABLE review.execution_intents DROP COLUMN IF EXISTS decision_ref",
    "ALTER TABLE review.execution_intents DROP COLUMN IF EXISTS origin",
    "ALTER TABLE review.execution_intents DROP COLUMN IF EXISTS operator_exit_id",
    "ALTER TABLE review.execution_intents DROP COLUMN IF EXISTS max_quantity_decimal",
    "ALTER TABLE review.execution_intents "
    "DROP COLUMN IF EXISTS position_at_authorization_decimal",
    "ALTER TABLE review.execution_intents "
    "DROP COLUMN IF EXISTS min_position_after_decimal",
    "ALTER TABLE review.execution_intents "
    "DROP COLUMN IF EXISTS max_position_after_decimal",
    "ALTER TABLE review.execution_intents ALTER COLUMN max_quantity SET NOT NULL",
    "ALTER TABLE review.execution_intents "
    "ALTER COLUMN position_at_authorization SET NOT NULL",
    "ALTER TABLE review.execution_intents ALTER COLUMN min_position_after SET NOT NULL",
    "ALTER TABLE review.execution_intents ALTER COLUMN max_position_after SET NOT NULL",
    "ALTER TABLE review.execution_intents ADD CONSTRAINT "
    "ck_execution_intents_schema_version CHECK (schema_version = 1)",
    "ALTER TABLE review.execution_intents ADD CONSTRAINT "
    "ck_execution_intents_account_mock_only CHECK (account IN ('kis_mock'))",
    "ALTER TABLE review.execution_intents ADD CONSTRAINT "
    "ck_execution_intents_venue CHECK (venue IN ('krx'))",
    "ALTER TABLE review.execution_intents ADD CONSTRAINT "
    "ck_execution_intents_max_quantity CHECK (max_quantity BETWEEN 1 AND 1000000)",
    "ALTER TABLE review.execution_intents ADD CONSTRAINT "
    "ck_execution_intents_max_notional "
    "CHECK (max_notional_krw BETWEEN 1 AND 5000000)",
    "ALTER TABLE review.execution_intents ADD CONSTRAINT "
    "ck_execution_intents_limit_price "
    "CHECK (limit_price_krw BETWEEN 1 AND 10000000)",
    "ALTER TABLE review.execution_intents ADD CONSTRAINT "
    "ck_execution_intents_inventory CHECK ("
    "position_at_authorization >= 0 AND min_position_after >= 0 "
    "AND min_position_after <= max_position_after)",
    # sleeves
    "ALTER TABLE review.strategy_sleeves "
    "DROP CONSTRAINT IF EXISTS ck_strategy_sleeves_execution_path",
    "ALTER TABLE review.strategy_sleeves DROP COLUMN IF EXISTS execution_path",
    # new tables, children first
    "DROP TABLE IF EXISTS review.execution_child_order_evidence",
    "DROP TABLE IF EXISTS review.execution_exit_handoffs",
    "DROP TABLE IF EXISTS review.execution_operator_exits",
    "DROP TABLE IF EXISTS review.execution_mandate_revocations",
    "DROP TABLE IF EXISTS review.execution_mandates",
    "DROP TABLE IF EXISTS review.execution_account_control",
    "DROP FUNCTION IF EXISTS review.execution_child_order_evidence_guard()",
    "DROP FUNCTION IF EXISTS review.execution_exit_handoffs_guard()",
    "DROP FUNCTION IF EXISTS review.execution_operator_exits_guard()",
    "DROP FUNCTION IF EXISTS review.execution_append_only_guard()",
    "DROP FUNCTION IF EXISTS review.execution_mandates_guard()",
)


def upgrade() -> None:
    for statement in (*TABLE_DDL, *ALTER_DDL, *GUARD_DDL, *SEED_DDL):
        op.execute(statement)
    op.execute(GRANT_DDL)


def downgrade() -> None:
    op.execute(DOWNGRADE_GUARD)
    for statement in _DOWNGRADE_DDL:
        op.execute(statement)
