"""Durable mock-only NHPLUG dispatch state machine.

Revision ID: 20260926_task711_dispatch
Revises: 20260925_rob728_lot_protection
"""

from __future__ import annotations

from alembic import op

revision = "20260926_task711_dispatch"
down_revision = "20260925_rob728_lot_protection"
branch_labels = None
depends_on = None


def _execute_script(script: str) -> None:
    """Execute individual statements through asyncpg's prepared-statement path."""

    start = 0
    quoted = False
    dollar = False
    pos = 0
    while pos < len(script):
        if script[pos : pos + 2] == "$$" and not quoted:
            dollar = not dollar
            pos += 2
            continue
        if script[pos] == "'" and not dollar:
            if quoted and script[pos + 1 : pos + 2] == "'":
                pos += 2
                continue
            quoted = not quoted
        elif script[pos] == ";" and not quoted and not dollar:
            statement = script[start:pos].strip()
            if statement:
                op.execute(statement)
            start = pos + 1
        pos += 1
    tail = script[start:].strip()
    if tail:
        op.execute(tail)


def upgrade() -> None:
    op.execute("CREATE SCHEMA IF NOT EXISTS review")
    _execute_script("""
    DO $$ BEGIN
      IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'nhplug_operator') THEN
        CREATE ROLE nhplug_operator NOLOGIN;
      END IF;
    END $$;
    CREATE TABLE review.nhplug_mock_key_version (
      key_version smallint PRIMARY KEY CHECK (key_version > 0),
      key_id text NOT NULL UNIQUE CHECK (length(key_id) BETWEEN 1 AND 128),
      key_check text NOT NULL CHECK (key_check ~ '^[0-9a-f]{64}$'),
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE review.nhplug_mock_account_ref (
      account_ref uuid PRIMARY KEY,
      created_at timestamptz NOT NULL DEFAULT now()
    );
    CREATE TABLE review.nhplug_mock_account_binding (
      key_version smallint NOT NULL REFERENCES review.nhplug_mock_key_version(key_version),
      binding text NOT NULL CHECK (binding ~ '^[0-9a-f]{64}$'),
      account_ref uuid NOT NULL REFERENCES review.nhplug_mock_account_ref(account_ref),
      created_at timestamptz NOT NULL DEFAULT now(),
      PRIMARY KEY (key_version, binding),
      UNIQUE (account_ref, key_version)
    );
    CREATE TABLE review.nhplug_success_proof_code (
      path text NOT NULL, rsp_cd text NOT NULL, citation text NOT NULL CHECK (length(trim(citation)) > 0),
      approved_by text NOT NULL CHECK (length(trim(approved_by)) > 0),
      created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (path, rsp_cd)
    );
    CREATE TABLE review.nhplug_no_order_proof_code (
      path text NOT NULL, rsp_cd text NOT NULL, citation text NOT NULL CHECK (length(trim(citation)) > 0),
      approved_by text NOT NULL CHECK (length(trim(approved_by)) > 0),
      created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY (path, rsp_cd)
    );
    CREATE TABLE review.nhplug_mock_operator_authorization (
      id uuid PRIMARY KEY,
      kind text NOT NULL CHECK (kind IN ('second_order','bind_candidate','abandon')),
      target_row_id bigint NOT NULL,
      account_ref uuid NOT NULL,
      order_date date NOT NULL,
      body_digest text NOT NULL CHECK (body_digest ~ '^[0-9a-f]{64}$'),
      candidate_order_id text,
      evidence jsonb NOT NULL,
      dispatcher_gone_proof boolean NOT NULL DEFAULT false,
      grace_until timestamptz,
      operator_id text NOT NULL CHECK (length(trim(operator_id)) > 0),
      reason text NOT NULL CHECK (length(trim(reason)) > 0),
      created_at timestamptz NOT NULL DEFAULT now(),
      consumed_by_row_id bigint,
      consumed_at timestamptz,
      CHECK ((consumed_by_row_id IS NULL) = (consumed_at IS NULL)),
      CHECK (kind <> 'abandon' OR grace_until IS NOT NULL)
    );
    CREATE FUNCTION review.nhplug_body_field(tag text, v text) RETURNS text
      LANGUAGE sql IMMUTABLE PARALLEL SAFE
      RETURN tag || CASE WHEN v IS NULL THEN '=-;' ELSE '=' || octet_length(v)::text || ':' || v || ';' END;
    CREATE FUNCTION review.nhplug_body_digest_v1(op text, side text, sym text, qty bigint, px bigint,
       org text, scope text, acct text) RETURNS text LANGUAGE sql IMMUTABLE PARALLEL SAFE
      RETURN encode(sha256(convert_to('nhplug-body-v1|'
       || review.nhplug_body_field('op', op) || review.nhplug_body_field('side', side)
       || review.nhplug_body_field('sym', sym) || review.nhplug_body_field('qty', qty::text)
       || review.nhplug_body_field('px', px::text) || review.nhplug_body_field('org', org)
       || review.nhplug_body_field('scope', scope) || review.nhplug_body_field('acct', acct), 'UTF8')), 'hex');
    CREATE TABLE review.nhplug_mock_order_ledger (
      id bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
      client_request_id uuid NOT NULL UNIQUE,
      account_ref uuid NOT NULL REFERENCES review.nhplug_mock_account_ref(account_ref),
      idempotency_key text NOT NULL CHECK (idempotency_key ~ '^[A-Za-z0-9_-]{16,64}$'),
      attempt_no integer NOT NULL CHECK (attempt_no > 0),
      order_date date NOT NULL,
      operation_kind text NOT NULL CHECK (operation_kind IN ('place','modify','cancel')),
      side text NOT NULL CHECK (side IN ('buy','sell')),
      symbol text NOT NULL CHECK (symbol ~ '^[0-9]{6}$'),
      quantity bigint,
      price bigint,
      original_order_id text,
      amend_scope text,
      body_schema_version smallint NOT NULL DEFAULT 1 CHECK (body_schema_version = 1),
      body_digest text NOT NULL GENERATED ALWAYS AS (review.nhplug_body_digest_v1(operation_kind, side,
        symbol, quantity, price, original_order_id, amend_scope, account_ref::text)) STORED,
      duplicate_of bigint REFERENCES review.nhplug_mock_order_ledger(id),
      duplicate_ordinal integer NOT NULL DEFAULT 0 CHECK (duplicate_ordinal >= 0),
      second_order_authorization_id uuid UNIQUE REFERENCES review.nhplug_mock_operator_authorization(id),
      state text NOT NULL DEFAULT 'intent' CHECK (state IN ('intent','withdrawn','claimed','sending',
        'accepted','rejected','uncertain','open','partially_filled','filled','cancelled',
        'modified','confirmed','anomaly','abandoned')),
      claim_token uuid UNIQUE,
      claimed_at timestamptz,
      claim_deadline timestamptz,
      lease_machine_id text,
      lease_boot_id text,
      lease_pid_ns text,
      lease_pid integer,
      lease_process_start bigint,
      sending_at timestamptz,
      lease_expires_at timestamptz,
      lease_closed_at timestamptz,
      dispatcher_done_at timestamptz,
      withdraw_reason text,
      uncertain_reason text,
      broker_order_id text,
      ack_order_id text,
      ack_evidence_order_id text,
      late_result_at timestamptz,
      ack_source text,
      success_rsp_cd text,
      reject_rsp_cd text,
      resolution_authorization_id uuid UNIQUE REFERENCES review.nhplug_mock_operator_authorization(id),
      candidate_order_ids jsonb,
      reconcile_state text NOT NULL DEFAULT 'pending' CHECK (reconcile_state IN
        ('pending','verified','unknown','source_disagreement')),
      evidence jsonb,
      last_reconcile jsonb,
      requires_manual_review boolean NOT NULL DEFAULT false,
      manual_review_reason text,
      filled_qty bigint,
      open_qty bigint,
      cancelled_qty bigint,
      modified_qty bigint,
      avg_fill_price numeric(20,3),
      successor_order_id text,
      applied_qty bigint,
      created_at timestamptz NOT NULL DEFAULT now(),
      updated_at timestamptz NOT NULL DEFAULT now(),
      CHECK (price IS NULL OR price > 0),
      CHECK (quantity IS NULL OR quantity > 0),
      CHECK (original_order_id IS NULL OR original_order_id ~ '^[1-9][0-9]{0,9}$'),
      CHECK ((operation_kind = 'place' AND quantity IS NOT NULL AND price IS NOT NULL
              AND original_order_id IS NULL AND amend_scope IS NULL)
          OR (operation_kind = 'modify' AND quantity IS NOT NULL AND price IS NOT NULL
              AND original_order_id IS NOT NULL AND amend_scope IS NOT NULL
              AND amend_scope IN ('full','partial'))
          OR (operation_kind = 'cancel' AND price IS NULL AND original_order_id IS NOT NULL
              AND amend_scope IS NOT NULL
              AND ((amend_scope = 'full' AND quantity IS NULL)
                OR (amend_scope = 'partial' AND quantity IS NOT NULL)))),
      CHECK ((duplicate_ordinal = 0) = (duplicate_of IS NULL)),
      CHECK ((duplicate_ordinal = 0) = (second_order_authorization_id IS NULL)),
      CHECK ((claim_token IS NULL) = (claimed_at IS NULL)),
      CHECK (state <> 'intent' OR claim_token IS NULL),
      CHECK (state IN ('intent','withdrawn') OR claim_token IS NOT NULL),
      CHECK (sending_at IS NULL OR (claim_token IS NOT NULL AND lease_expires_at IS NOT NULL)),
      CHECK (state NOT IN ('sending','accepted','uncertain','abandoned','open','partially_filled',
         'filled','cancelled','modified','confirmed','anomaly','rejected') OR sending_at IS NOT NULL),
      CHECK (state <> 'withdrawn' OR sending_at IS NULL),
      CHECK (state IN ('intent','claimed','sending','withdrawn') OR lease_closed_at IS NOT NULL),
      CHECK (state <> 'sending' OR lease_closed_at IS NULL),
      CHECK (state NOT IN ('accepted','open','partially_filled','filled','cancelled','modified','confirmed')
        OR (broker_order_id IS NOT NULL AND ack_order_id IS NOT NULL AND ack_source IS NOT NULL
          AND broker_order_id ~ '^[1-9][0-9]{0,9}$' AND ack_order_id = broker_order_id)),
      CHECK (state NOT IN ('intent','claimed','sending','withdrawn','uncertain','abandoned','rejected')
        OR (broker_order_id IS NULL AND ack_order_id IS NULL AND ack_source IS NULL)),
      CHECK (ack_order_id IS NOT DISTINCT FROM broker_order_id),
      CHECK (ack_evidence_order_id IS NULL OR ack_evidence_order_id ~ '^[1-9][0-9]{0,9}$'),
      CHECK (ack_source IS NULL OR ack_source IN ('response','own_evidence','operator')),
      CHECK (ack_source IS DISTINCT FROM 'response' OR success_rsp_cd IS NOT NULL),
      CHECK (ack_source IS DISTINCT FROM 'operator' OR resolution_authorization_id IS NOT NULL),
      CHECK (state NOT IN ('open','partially_filled','filled','cancelled','modified') OR
        (operation_kind IN ('place','modify') AND quantity IS NOT NULL AND filled_qty IS NOT NULL
        AND open_qty IS NOT NULL AND cancelled_qty IS NOT NULL AND modified_qty IS NOT NULL
        AND filled_qty >= 0 AND open_qty >= 0 AND cancelled_qty >= 0 AND modified_qty >= 0
        AND filled_qty + open_qty + cancelled_qty + modified_qty = quantity
        AND evidence IS NOT NULL AND reconcile_state = 'verified')),
      CHECK (state <> 'open' OR (filled_qty = 0 AND open_qty > 0)),
      CHECK (state <> 'partially_filled' OR (filled_qty > 0 AND open_qty > 0)),
      CHECK (state <> 'filled' OR filled_qty = quantity),
      CHECK (state <> 'cancelled' OR (open_qty = 0 AND cancelled_qty > 0)),
      CHECK (state <> 'modified' OR (open_qty = 0 AND modified_qty > 0 AND successor_order_id IS NOT NULL)),
      CHECK (state <> 'confirmed' OR (operation_kind IN ('modify','cancel') AND applied_qty IS NOT NULL
         AND applied_qty > 0 AND evidence IS NOT NULL AND reconcile_state = 'verified'
         AND (quantity IS NULL OR applied_qty <= quantity))),
      CHECK (state NOT IN ('intent','claimed','sending','withdrawn','uncertain','accepted','rejected','abandoned')
        OR (filled_qty IS NULL AND open_qty IS NULL AND cancelled_qty IS NULL
          AND modified_qty IS NULL AND applied_qty IS NULL))
    );
    CREATE UNIQUE INDEX uq_nhplug_mock_order_number
      ON review.nhplug_mock_order_ledger(account_ref, order_date, broker_order_id)
      WHERE broker_order_id IS NOT NULL;
    CREATE UNIQUE INDEX uq_nhplug_mock_live_idempotency
      ON review.nhplug_mock_order_ledger(account_ref, idempotency_key) WHERE state <> 'withdrawn';
    CREATE UNIQUE INDEX uq_nhplug_mock_active_reservation
      ON review.nhplug_mock_order_ledger(account_ref, symbol, side)
      WHERE state IN ('intent','claimed','sending','uncertain');
    CREATE UNIQUE INDEX uq_nhplug_mock_same_body
      ON review.nhplug_mock_order_ledger(account_ref, order_date, body_digest, duplicate_ordinal)
      WHERE state <> 'withdrawn';
    """)
    _execute_script("""
    CREATE FUNCTION review.nhplug_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN RAISE EXCEPTION 'nhplug append-only table'; END $$;
    CREATE TRIGGER nhplug_account_ref_immutable BEFORE UPDATE OR DELETE
      ON review.nhplug_mock_account_ref FOR EACH ROW EXECUTE FUNCTION review.nhplug_append_only();
    CREATE TRIGGER nhplug_account_ref_no_truncate BEFORE TRUNCATE
      ON review.nhplug_mock_account_ref FOR EACH STATEMENT EXECUTE FUNCTION review.nhplug_append_only();
    CREATE TRIGGER nhplug_binding_immutable BEFORE UPDATE OR DELETE
      ON review.nhplug_mock_account_binding FOR EACH ROW EXECUTE FUNCTION review.nhplug_append_only();
    CREATE TRIGGER nhplug_binding_no_truncate BEFORE TRUNCATE
      ON review.nhplug_mock_account_binding FOR EACH STATEMENT EXECUTE FUNCTION review.nhplug_append_only();
    CREATE TRIGGER nhplug_key_immutable BEFORE UPDATE OR DELETE
      ON review.nhplug_mock_key_version FOR EACH ROW EXECUTE FUNCTION review.nhplug_append_only();
    CREATE TRIGGER nhplug_key_no_truncate BEFORE TRUNCATE
      ON review.nhplug_mock_key_version FOR EACH STATEMENT EXECUTE FUNCTION review.nhplug_append_only();
    CREATE TRIGGER nhplug_success_code_immutable BEFORE UPDATE OR DELETE
      ON review.nhplug_success_proof_code FOR EACH ROW EXECUTE FUNCTION review.nhplug_append_only();
    CREATE TRIGGER nhplug_success_code_no_truncate BEFORE TRUNCATE
      ON review.nhplug_success_proof_code FOR EACH STATEMENT EXECUTE FUNCTION review.nhplug_append_only();
    CREATE TRIGGER nhplug_no_order_code_immutable BEFORE UPDATE OR DELETE
      ON review.nhplug_no_order_proof_code FOR EACH ROW EXECUTE FUNCTION review.nhplug_append_only();
    CREATE TRIGGER nhplug_no_order_code_no_truncate BEFORE TRUNCATE
      ON review.nhplug_no_order_proof_code FOR EACH STATEMENT EXECUTE FUNCTION review.nhplug_append_only();
    CREATE FUNCTION review.nhplug_key_registry_insert() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN PERFORM pg_advisory_xact_lock(711); RETURN NEW; END $$;
    CREATE TRIGGER nhplug_key_registry_serial BEFORE INSERT ON review.nhplug_mock_key_version
      FOR EACH ROW EXECUTE FUNCTION review.nhplug_key_registry_insert();
    CREATE FUNCTION review.nhplug_auth_immutable() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'nhplug authorization delete forbidden'; END IF;
      IF NEW.id IS DISTINCT FROM OLD.id
        OR (to_jsonb(NEW) - 'consumed_by_row_id' - 'consumed_at')
           IS DISTINCT FROM (to_jsonb(OLD) - 'consumed_by_row_id' - 'consumed_at')
        OR OLD.consumed_at IS NOT NULL OR NEW.consumed_at IS NULL
        OR NEW.consumed_by_row_id IS NULL THEN
        RAISE EXCEPTION 'nhplug authorization is immutable or already consumed';
      END IF;
      RETURN NEW;
    END $$;
    CREATE TRIGGER nhplug_auth_guard BEFORE UPDATE OR DELETE
      ON review.nhplug_mock_operator_authorization FOR EACH ROW
      EXECUTE FUNCTION review.nhplug_auth_immutable();
    CREATE TRIGGER nhplug_auth_no_truncate BEFORE TRUNCATE
      ON review.nhplug_mock_operator_authorization FOR EACH STATEMENT
      EXECUTE FUNCTION review.nhplug_append_only();
    CREATE FUNCTION review.nhplug_consume_authorization(
      auth_id uuid, expected_kind text, target bigint, account uuid,
      trading_day date, digest text, candidate text, consumer bigint) RETURNS jsonb
      LANGUAGE plpgsql SECURITY DEFINER SET search_path = pg_catalog, review AS $$
    DECLARE a review.nhplug_mock_operator_authorization%ROWTYPE;
    BEGIN
      SELECT * INTO a FROM review.nhplug_mock_operator_authorization WHERE id = auth_id FOR UPDATE;
      IF NOT FOUND OR a.kind IS DISTINCT FROM expected_kind OR a.target_row_id IS DISTINCT FROM target
        OR a.account_ref IS DISTINCT FROM account OR a.order_date IS DISTINCT FROM trading_day
        OR a.body_digest IS DISTINCT FROM digest OR a.candidate_order_id IS DISTINCT FROM candidate
        OR a.consumed_at IS NOT NULL THEN
        RAISE EXCEPTION 'nhplug authorization mismatch or consumed';
      END IF;
      UPDATE review.nhplug_mock_operator_authorization SET consumed_at = now(),
        consumed_by_row_id = consumer WHERE id = auth_id;
      RETURN jsonb_build_object('evidence', a.evidence,
        'dispatcher_gone_proof', a.dispatcher_gone_proof, 'grace_until', a.grace_until);
    END $$;
    CREATE FUNCTION review.nhplug_order_guard() RETURNS trigger LANGUAGE plpgsql
      SECURITY DEFINER SET search_path = pg_catalog, review AS $$
    DECLARE allowed text[]; k text; v jsonb; old_json jsonb; new_json jsonb;
      root review.nhplug_mock_order_ledger%ROWTYPE; auth jsonb; route text;
      current_digest text;
    BEGIN
      IF TG_OP = 'DELETE' THEN RAISE EXCEPTION 'nhplug ledger delete forbidden'; END IF;
      IF TG_OP = 'INSERT' THEN
        IF NEW.state <> 'intent' OR NEW.claim_token IS NOT NULL OR NEW.claimed_at IS NOT NULL
           OR NEW.claim_deadline IS NOT NULL OR NEW.lease_machine_id IS NOT NULL
           OR NEW.lease_boot_id IS NOT NULL OR NEW.lease_pid_ns IS NOT NULL
           OR NEW.lease_pid IS NOT NULL OR NEW.lease_process_start IS NOT NULL
           OR NEW.sending_at IS NOT NULL OR NEW.lease_expires_at IS NOT NULL
           OR NEW.lease_closed_at IS NOT NULL OR NEW.dispatcher_done_at IS NOT NULL
           OR NEW.withdraw_reason IS NOT NULL OR NEW.uncertain_reason IS NOT NULL
           OR NEW.broker_order_id IS NOT NULL OR NEW.ack_order_id IS NOT NULL
           OR NEW.ack_evidence_order_id IS NOT NULL OR NEW.evidence IS NOT NULL
           OR NEW.late_result_at IS NOT NULL OR NEW.ack_source IS NOT NULL
           OR NEW.success_rsp_cd IS NOT NULL OR NEW.reject_rsp_cd IS NOT NULL
           OR NEW.resolution_authorization_id IS NOT NULL
           OR NEW.candidate_order_ids IS NOT NULL OR NEW.filled_qty IS NOT NULL
           OR NEW.open_qty IS NOT NULL OR NEW.cancelled_qty IS NOT NULL
           OR NEW.modified_qty IS NOT NULL OR NEW.applied_qty IS NOT NULL
           OR NEW.avg_fill_price IS NOT NULL OR NEW.successor_order_id IS NOT NULL
           OR NEW.last_reconcile IS NOT NULL OR NEW.manual_review_reason IS NOT NULL
           OR NEW.reconcile_state <> 'pending' OR NEW.requires_manual_review THEN
          RAISE EXCEPTION 'nhplug intent must be clean';
        END IF;
        IF NEW.duplicate_of IS NULL THEN
          IF NEW.duplicate_ordinal <> 0 OR NEW.second_order_authorization_id IS NOT NULL THEN
            RAISE EXCEPTION 'nhplug ordinary intent cannot claim second order';
          END IF;
        ELSE
          SELECT * INTO root FROM review.nhplug_mock_order_ledger WHERE id = NEW.duplicate_of FOR UPDATE;
          current_digest := review.nhplug_body_digest_v1(NEW.operation_kind, NEW.side, NEW.symbol,
            NEW.quantity, NEW.price, NEW.original_order_id, NEW.amend_scope, NEW.account_ref::text);
          IF NOT FOUND OR root.duplicate_ordinal <> 0 OR root.state = 'withdrawn'
            OR root.account_ref IS DISTINCT FROM NEW.account_ref OR root.order_date IS DISTINCT FROM NEW.order_date
            OR root.body_digest IS DISTINCT FROM current_digest OR NEW.second_order_authorization_id IS NULL THEN
            RAISE EXCEPTION 'nhplug second order root mismatch';
          END IF;
          auth := review.nhplug_consume_authorization(NEW.second_order_authorization_id,
            'second_order', root.id, NEW.account_ref, NEW.order_date, current_digest, NULL, NEW.id);
          SELECT coalesce(max(duplicate_ordinal),0)+1 INTO NEW.duplicate_ordinal
            FROM review.nhplug_mock_order_ledger WHERE duplicate_of = root.id;
        END IF;
        RETURN NEW;
      END IF;

      IF NEW.client_request_id IS DISTINCT FROM OLD.client_request_id
         OR NEW.account_ref IS DISTINCT FROM OLD.account_ref
         OR NEW.idempotency_key IS DISTINCT FROM OLD.idempotency_key
         OR NEW.attempt_no IS DISTINCT FROM OLD.attempt_no
         OR NEW.order_date IS DISTINCT FROM OLD.order_date
         OR NEW.operation_kind IS DISTINCT FROM OLD.operation_kind
         OR NEW.side IS DISTINCT FROM OLD.side OR NEW.symbol IS DISTINCT FROM OLD.symbol
         OR NEW.quantity IS DISTINCT FROM OLD.quantity OR NEW.price IS DISTINCT FROM OLD.price
         OR NEW.original_order_id IS DISTINCT FROM OLD.original_order_id
         OR NEW.amend_scope IS DISTINCT FROM OLD.amend_scope
         OR NEW.body_schema_version IS DISTINCT FROM OLD.body_schema_version
         OR NEW.duplicate_of IS DISTINCT FROM OLD.duplicate_of
         OR NEW.duplicate_ordinal IS DISTINCT FROM OLD.duplicate_ordinal
         OR NEW.second_order_authorization_id IS DISTINCT FROM OLD.second_order_authorization_id
         OR NEW.created_at IS DISTINCT FROM OLD.created_at THEN
         RAISE EXCEPTION 'nhplug immutable identity changed';
      END IF;
      IF (OLD.broker_order_id IS NOT NULL AND NEW.broker_order_id IS DISTINCT FROM OLD.broker_order_id)
         OR (OLD.ack_order_id IS NOT NULL AND NEW.ack_order_id IS DISTINCT FROM OLD.ack_order_id)
         OR (OLD.ack_evidence_order_id IS NOT NULL AND NEW.ack_evidence_order_id IS DISTINCT FROM OLD.ack_evidence_order_id)
         OR (OLD.dispatcher_done_at IS NOT NULL AND NEW.dispatcher_done_at IS DISTINCT FROM OLD.dispatcher_done_at)
         OR (OLD.claim_token IS NOT NULL AND NEW.claim_token IS DISTINCT FROM OLD.claim_token)
         OR (OLD.lease_closed_at IS NOT NULL AND NEW.lease_closed_at IS DISTINCT FROM OLD.lease_closed_at)
         OR (OLD.resolution_authorization_id IS NOT NULL AND NEW.resolution_authorization_id IS DISTINCT FROM OLD.resolution_authorization_id)
         THEN RAISE EXCEPTION 'nhplug write-once field changed'; END IF;

      route := CASE WHEN OLD.operation_kind = 'place' AND OLD.side = 'buy' THEN '/krstock/order/v1/cashBuy'
                    WHEN OLD.operation_kind = 'place' AND OLD.side = 'sell' THEN '/krstock/order/v1/cashSell'
                    WHEN OLD.operation_kind = 'modify' THEN '/krstock/order/v1/modify'
                    ELSE '/krstock/order/v1/cancel' END;
      IF OLD.state = 'intent' AND NEW.state = 'withdrawn' THEN
        allowed := ARRAY['state','withdraw_reason'];
        IF NEW.withdraw_reason IS NULL THEN RAISE EXCEPTION 'nhplug withdrawal reason absent'; END IF;
      ELSIF OLD.state = 'intent' AND NEW.state = 'claimed' THEN
        allowed := ARRAY['state','claim_token','claimed_at','claim_deadline','lease_machine_id',
          'lease_boot_id','lease_pid_ns','lease_pid','lease_process_start'];
        IF NEW.claim_token IS NULL OR NEW.claim_deadline IS NULL OR NEW.lease_machine_id IS NULL
          OR NEW.lease_boot_id IS NULL OR NEW.lease_pid_ns IS NULL OR NEW.lease_pid IS NULL
          OR NEW.lease_process_start IS NULL THEN RAISE EXCEPTION 'nhplug incomplete claim'; END IF;
      ELSIF OLD.state = 'claimed' AND NEW.state = 'withdrawn' THEN
        allowed := ARRAY['state','withdraw_reason'];
        IF NEW.withdraw_reason IS NULL THEN RAISE EXCEPTION 'nhplug withdrawal reason absent'; END IF;
      ELSIF OLD.state = 'claimed' AND NEW.state = 'sending' THEN
        allowed := ARRAY['state','sending_at','lease_expires_at'];
        IF OLD.claim_deadline <= now() OR NEW.sending_at IS NULL OR NEW.lease_expires_at IS NULL
          THEN RAISE EXCEPTION 'nhplug fence deadline failed'; END IF;
      ELSIF OLD.state = 'sending' AND NEW.state = 'accepted' THEN
        allowed := ARRAY['state','broker_order_id','ack_order_id','ack_source','success_rsp_cd',
          'lease_closed_at','dispatcher_done_at'];
        IF NEW.ack_source IS DISTINCT FROM 'response' OR NEW.dispatcher_done_at IS NULL
          THEN RAISE EXCEPTION 'nhplug response acceptance proof absent'; END IF;
      ELSIF OLD.state = 'sending' AND NEW.state = 'rejected' THEN
        allowed := ARRAY['state','reject_rsp_cd','lease_closed_at','dispatcher_done_at'];
        IF NEW.dispatcher_done_at IS NULL OR NEW.reject_rsp_cd IS NULL
          OR NOT EXISTS (SELECT 1 FROM review.nhplug_no_order_proof_code
             WHERE path = route AND rsp_cd = NEW.reject_rsp_cd) THEN
          RAISE EXCEPTION 'nhplug no-order proof code absent';
        END IF;
      ELSIF OLD.state = 'sending' AND NEW.state = 'uncertain' THEN
        IF NEW.dispatcher_done_at IS NULL THEN
          allowed := ARRAY['state','uncertain_reason','lease_closed_at'];
          IF OLD.lease_expires_at >= now() OR NEW.uncertain_reason <> 'lease_expired_without_result'
            THEN RAISE EXCEPTION 'nhplug recovery lease not expired'; END IF;
        ELSE
          allowed := ARRAY['state','uncertain_reason','ack_evidence_order_id',
            'lease_closed_at','dispatcher_done_at'];
        END IF;
        IF NEW.uncertain_reason IS NULL THEN
          RAISE EXCEPTION 'nhplug uncertainty reason absent'; END IF;
      ELSIF OLD.state = 'uncertain' AND NEW.state = 'accepted' AND NEW.ack_source = 'response' THEN
        allowed := ARRAY['state','broker_order_id','ack_order_id','ack_source','success_rsp_cd','dispatcher_done_at'];
        IF OLD.dispatcher_done_at IS NOT NULL OR NEW.dispatcher_done_at IS NULL OR
           (OLD.ack_evidence_order_id IS NOT NULL AND OLD.ack_evidence_order_id <> NEW.broker_order_id)
           THEN RAISE EXCEPTION 'nhplug late response conflict'; END IF;
      ELSIF OLD.state = 'uncertain' AND NEW.state = 'accepted' AND NEW.ack_source = 'own_evidence' THEN
        allowed := ARRAY['state','broker_order_id','ack_order_id','ack_source','reconcile_state','evidence','last_reconcile'];
        IF OLD.ack_evidence_order_id IS NULL OR NEW.broker_order_id IS DISTINCT FROM OLD.ack_evidence_order_id
          OR NEW.reconcile_state <> 'verified' OR NEW.evidence IS NULL
          OR NEW.evidence->>'listing_order_id' IS DISTINCT FROM OLD.ack_evidence_order_id
          OR NEW.evidence->>'listing_complete' IS DISTINCT FROM 'true'
          OR NEW.evidence->>'listing_scope' IS DISTINCT FROM 'all'
          OR NEW.evidence->>'account_ref' IS DISTINCT FROM OLD.account_ref::text
          OR NEW.evidence->>'order_date' IS DISTINCT FROM OLD.order_date::text
          OR NEW.evidence->>'attributes_match' IS DISTINCT FROM 'true'
          THEN RAISE EXCEPTION 'nhplug own-number positive listing proof absent'; END IF;
      ELSIF OLD.state = 'uncertain' AND NEW.state = 'accepted' AND NEW.ack_source = 'operator' THEN
        allowed := ARRAY['state','broker_order_id','ack_order_id','ack_source','resolution_authorization_id'];
        IF NEW.resolution_authorization_id IS NULL OR NEW.broker_order_id IS NULL
          OR OLD.candidate_order_ids IS NULL
          OR NOT (OLD.candidate_order_ids ? NEW.broker_order_id)
          OR (OLD.ack_evidence_order_id IS NOT NULL AND OLD.ack_evidence_order_id <> NEW.broker_order_id)
          THEN RAISE EXCEPTION 'nhplug candidate bind mismatch'; END IF;
        auth := review.nhplug_consume_authorization(NEW.resolution_authorization_id,
          'bind_candidate', OLD.id, OLD.account_ref, OLD.order_date, OLD.body_digest,
          NEW.broker_order_id, NEW.id);
        IF OLD.dispatcher_done_at IS NULL AND coalesce((auth->>'dispatcher_gone_proof')::boolean,false) = false
          THEN RAISE EXCEPTION 'nhplug dispatcher may still write'; END IF;
      ELSIF OLD.state = 'uncertain' AND NEW.state = 'abandoned' THEN
        allowed := ARRAY['state','resolution_authorization_id','manual_review_reason'];
        IF OLD.lease_closed_at IS NULL OR OLD.lease_expires_at IS NULL
           OR OLD.lease_expires_at >= now() OR NEW.resolution_authorization_id IS NULL
           THEN RAISE EXCEPTION 'nhplug abandon prerequisites absent'; END IF;
        auth := review.nhplug_consume_authorization(NEW.resolution_authorization_id,
          'abandon', OLD.id, OLD.account_ref, OLD.order_date, OLD.body_digest, NULL, NEW.id);
        IF coalesce((auth->>'grace_until')::timestamptz, 'infinity'::timestamptz) > now()
          OR coalesce((auth->>'grace_until')::timestamptz, '-infinity'::timestamptz) <= OLD.lease_expires_at
          OR coalesce((auth->'evidence'->>'process_gone')::boolean,false) = false
          OR coalesce((auth->'evidence'->>'listing_complete')::boolean,false) = false
          OR coalesce((auth->'evidence'->>'grace_elapsed')::boolean,false) = false
          THEN RAISE EXCEPTION 'nhplug abandon positive process proof absent'; END IF;
      ELSIF OLD.state IN ('accepted','open','partially_filled')
          AND NEW.state IN ('open','partially_filled','filled','cancelled','modified') THEN
        allowed := ARRAY['state','reconcile_state','last_reconcile','evidence','filled_qty','avg_fill_price',
          'open_qty','cancelled_qty','modified_qty','successor_order_id','requires_manual_review','manual_review_reason'];
        IF NEW.reconcile_state <> 'verified' OR NEW.evidence IS NULL
          OR NEW.last_reconcile->>'account_ref' IS DISTINCT FROM OLD.account_ref::text
          OR NEW.last_reconcile->>'order_date' IS DISTINCT FROM OLD.order_date::text
          THEN RAISE EXCEPTION 'nhplug reconcile evidence absent'; END IF;
      ELSIF OLD.state = 'accepted' AND NEW.state = 'confirmed' THEN
        allowed := ARRAY['state','reconcile_state','last_reconcile','evidence','applied_qty'];
        IF NEW.reconcile_state <> 'verified' OR NEW.evidence IS NULL
          OR NEW.last_reconcile->>'account_ref' IS DISTINCT FROM OLD.account_ref::text
          OR NEW.last_reconcile->>'order_date' IS DISTINCT FROM OLD.order_date::text
          THEN RAISE EXCEPTION 'nhplug reconcile evidence absent'; END IF;
      ELSIF OLD.state IN ('accepted','open','partially_filled','uncertain') AND NEW.state = 'anomaly' THEN
        allowed := ARRAY['state','requires_manual_review','manual_review_reason','evidence','last_reconcile'];
        IF NEW.requires_manual_review IS DISTINCT FROM true OR NEW.evidence IS NULL
          OR NEW.manual_review_reason IS NULL THEN
          RAISE EXCEPTION 'nhplug anomaly evidence absent'; END IF;
        IF OLD.state = 'uncertain' AND (OLD.ack_evidence_order_id IS NULL OR NEW.evidence IS NULL
          OR NEW.evidence->>'listing_order_id' IS DISTINCT FROM OLD.ack_evidence_order_id
          OR NEW.evidence->>'listing_complete' IS DISTINCT FROM 'true'
          OR NEW.evidence->>'listing_scope' IS DISTINCT FROM 'all'
          OR NEW.evidence->>'account_ref' IS DISTINCT FROM OLD.account_ref::text
          OR NEW.evidence->>'order_date' IS DISTINCT FROM OLD.order_date::text
          OR NEW.evidence->>'attributes_match' IS DISTINCT FROM 'false') THEN
          RAISE EXCEPTION 'nhplug uncertain anomaly positive listing proof absent';
        END IF;
      ELSIF OLD.state = 'uncertain' AND NEW.state = 'uncertain' THEN
        IF NEW.dispatcher_done_at IS DISTINCT FROM OLD.dispatcher_done_at
          OR NEW.ack_evidence_order_id IS DISTINCT FROM OLD.ack_evidence_order_id THEN
          allowed := ARRAY['ack_evidence_order_id','late_result_at','dispatcher_done_at'];
          IF OLD.dispatcher_done_at IS NOT NULL OR NEW.dispatcher_done_at IS NULL
            OR (OLD.ack_evidence_order_id IS NOT NULL AND NEW.ack_evidence_order_id IS DISTINCT FROM OLD.ack_evidence_order_id)
            THEN RAISE EXCEPTION 'nhplug late dispatcher result already recorded'; END IF;
        ELSE
          allowed := ARRAY['candidate_order_ids','requires_manual_review','manual_review_reason',
            'last_reconcile','reconcile_state'];
          IF NEW.reconcile_state = 'verified' THEN RAISE EXCEPTION 'nhplug candidate cannot verify'; END IF;
        END IF;
      ELSIF OLD.state = 'accepted' AND NEW.state = 'accepted' THEN
        allowed := ARRAY['reconcile_state','last_reconcile','requires_manual_review','manual_review_reason'];
      ELSIF OLD.state IN ('open','partially_filled') AND NEW.state = OLD.state THEN
        allowed := ARRAY['reconcile_state','last_reconcile','requires_manual_review','manual_review_reason',
          'evidence','filled_qty','avg_fill_price','open_qty','cancelled_qty','modified_qty','successor_order_id'];
        IF NEW.reconcile_state <> 'verified' AND (
          NEW.evidence IS DISTINCT FROM OLD.evidence OR NEW.filled_qty IS DISTINCT FROM OLD.filled_qty
          OR NEW.avg_fill_price IS DISTINCT FROM OLD.avg_fill_price
          OR NEW.open_qty IS DISTINCT FROM OLD.open_qty
          OR NEW.cancelled_qty IS DISTINCT FROM OLD.cancelled_qty
          OR NEW.modified_qty IS DISTINCT FROM OLD.modified_qty
          OR NEW.successor_order_id IS DISTINCT FROM OLD.successor_order_id) THEN
          RAISE EXCEPTION 'nhplug quantity update needs verified reconciliation'; END IF;
      ELSIF OLD.state = 'anomaly' AND NEW.state = 'anomaly' THEN
        allowed := ARRAY['manual_review_reason'];
      ELSE RAISE EXCEPTION 'nhplug transition forbidden: % to %', OLD.state, NEW.state;
      END IF;

      IF NEW.state = 'accepted' AND NEW.ack_source = 'response' THEN
        IF NEW.success_rsp_cd IS NULL OR NOT EXISTS (SELECT 1 FROM review.nhplug_success_proof_code
          WHERE path = route AND rsp_cd = NEW.success_rsp_cd) THEN
          RAISE EXCEPTION 'nhplug success proof code absent';
        END IF;
      END IF;
      IF OLD.state = 'sending' AND NEW.state <> 'sending' AND NEW.lease_closed_at IS NULL THEN
        RAISE EXCEPTION 'nhplug sending lease not closed';
      END IF;
      old_json := to_jsonb(OLD) - 'body_digest' - 'updated_at';
      new_json := to_jsonb(NEW) - 'body_digest' - 'updated_at';
      FOR k,v IN SELECT key,value FROM jsonb_each(new_json) LOOP
        IF v IS DISTINCT FROM old_json->k AND NOT (k = ANY(allowed)) THEN
          RAISE EXCEPTION 'nhplug column forbidden on transition: %', k;
        END IF;
      END LOOP;
      NEW.updated_at := now();
      RETURN NEW;
    END $$;
    CREATE TRIGGER nhplug_order_guard BEFORE INSERT OR UPDATE OR DELETE
      ON review.nhplug_mock_order_ledger FOR EACH ROW EXECUTE FUNCTION review.nhplug_order_guard();
    CREATE TRIGGER nhplug_order_no_truncate BEFORE TRUNCATE
      ON review.nhplug_mock_order_ledger FOR EACH STATEMENT EXECUTE FUNCTION review.nhplug_append_only();
    """)
    _execute_script("""
    DO $$ DECLARE app_role text := current_user; BEGIN
      EXECUTE format('GRANT SELECT ON review.nhplug_mock_key_version, review.nhplug_success_proof_code, review.nhplug_no_order_proof_code, review.nhplug_mock_operator_authorization TO %I', app_role);
    END $$;
    ALTER TABLE review.nhplug_mock_key_version OWNER TO nhplug_operator;
    ALTER TABLE review.nhplug_success_proof_code OWNER TO nhplug_operator;
    ALTER TABLE review.nhplug_no_order_proof_code OWNER TO nhplug_operator;
    ALTER TABLE review.nhplug_mock_operator_authorization OWNER TO nhplug_operator;
    ALTER FUNCTION review.nhplug_consume_authorization(uuid,text,bigint,uuid,date,text,text,bigint) OWNER TO nhplug_operator;
    ALTER FUNCTION review.nhplug_order_guard() OWNER TO nhplug_operator;
    GRANT SELECT, UPDATE ON review.nhplug_mock_order_ledger TO nhplug_operator;
    GRANT USAGE ON SCHEMA review TO nhplug_operator;
    """)


def downgrade() -> None:
    _execute_script("""
    DROP TABLE review.nhplug_mock_order_ledger;
    DROP TABLE review.nhplug_mock_operator_authorization;
    DROP TABLE review.nhplug_no_order_proof_code;
    DROP TABLE review.nhplug_success_proof_code;
    DROP TABLE review.nhplug_mock_account_binding;
    DROP TABLE review.nhplug_mock_account_ref;
    DROP TABLE review.nhplug_mock_key_version;
    DROP FUNCTION review.nhplug_body_digest_v1(text,text,text,bigint,bigint,text,text,text);
    DROP FUNCTION review.nhplug_body_field(text,text);
    DROP FUNCTION review.nhplug_order_guard();
    DROP FUNCTION review.nhplug_consume_authorization(uuid,text,bigint,uuid,date,text,text,bigint);
    DROP FUNCTION review.nhplug_auth_immutable();
    DROP FUNCTION review.nhplug_key_registry_insert();
    DROP FUNCTION review.nhplug_append_only();
    """)
