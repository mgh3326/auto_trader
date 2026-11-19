"""hk 3795 T1: mandate clauses, approvals, activation profiles and split plans.

Additive: nine new ``review`` tables and two nullable columns on
``review.order_proposals``. Existing rows keep both columns NULL and behave
exactly as before (the two proposal triggers carry a WHEN clause with no function
call, so an untagged proposal write never runs trigger code).

Triggers hold the rules for any role, before and after the #789 Stage 4 role
split:

* all nine tables are append-only: no UPDATE, DELETE or TRUNCATE (I13);
* an approval must carry the content hash of its request; a clause (terms) row
  and an activation profile row need an ``approved`` approval of the same kind,
  sleeve and hash (I21), and a clause binds to a live sleeve version;
* plans bind to a clause of their sleeve with the same ``account_ref`` (I15);
* ``mandate_plan_events``: ``seq`` is the last seq + 1, the authority transition
  table and the tranche rules of the design hold (I7, I9), a fence is refused
  once the plan's authority is not ``approved``/``active`` (I16, I24), and
  ``released`` needs settled orders, a holdings disposal and no open fence (I19);
* ``mandate_symbol_reservations``: the trigger takes the symbol lock itself and
  refuses a second unreleased reservation of the same realm/market/symbol (I1);
  a release row exists only with the plan's ``released`` event, written by an
  AFTER trigger in the same transaction;
* ``review.order_proposals.mandate_plan_id`` is set once at INSERT and must name
  a plan of the proposal's sleeve, account mode, market and symbol, buy/limit
  place only (I15).

The app role gets SELECT and INSERT only (hard rule 18).

Downgrade refuses while any mandate row exists or any proposal is bound to a
plan.

Revision ID: 20261120_hk5528_mandate
Revises: 20261116_hk4902_drop_unused
Create Date: 2026-11-20
"""

# expand-contract: contract-step 5528
from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "20261120_hk5528_mandate"
down_revision: str | Sequence[str] | None = "20261116_hk4902_drop_unused"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

# --- literal copy of app.services.mandate.vocabulary (pinned by a test) -------
REALM_BY_ACCOUNT_MODE: dict[str, str] = {
    "kis_live": "live",
    "toss_live": "live",
    "kiwoom_mock": "mock",
}
AUTH_FROM: dict[str, tuple[str, ...]] = {
    "approved": ("proposed",),
    "rejected": ("proposed",),
    "first_tranche_settled": ("approved",),
    "suspended": ("approved", "active"),
    "resumed": ("suspended",),
    "terms_rebound": ("approved", "active"),
    "exhausted": ("active",),
    "thesis_broken": ("approved", "active", "suspended", "exhausted"),
    "closed": ("thesis_broken",),
    "retired": (
        "proposed",
        "approved",
        "active",
        "suspended",
        "exhausted",
        "thesis_broken",
        "closed",
        "rejected",
    ),
}
AUTH_AFTER: dict[str, str] = {
    "proposed": "proposed",
    "approved": "approved",
    "rejected": "rejected",
    "first_tranche_settled": "active",
    "suspended": "suspended",
    "exhausted": "exhausted",
    "thesis_broken": "thesis_broken",
    "closed": "closed",
    "retired": "retired",
}
SEND_AUTHORITY: tuple[str, ...] = ("approved", "active")
CLEANUP_AUTH: tuple[str, ...] = (
    "rejected",
    "retired",
    "closed",
    "exhausted",
    "thesis_broken",
)
CLOSED_PARTIAL_AUTH: tuple[str, ...] = ("approved", "active", "suspended")
TRANCHE_TERMINAL_EVENTS: tuple[str, ...] = (
    "tranche_filled",
    "tranche_partially_filled",
    "tranche_unfilled",
)
HOLDINGS_DISPOSITIONS: tuple[str, ...] = (
    "zero_holdings",
    "kept_outside_basket",
    "never_adopted",
)

# (name, sql) of every CHECK on mandate_plan_events, rendered from
# app.models.mandate.event_check_sql().
EVENT_CHECKS: tuple[tuple[str, str], ...] = (
    (
        "event_vocabulary",
        "event IN ('approved','closed','exhausted','first_tranche_settled','holdings_disposed','orders_settled','proposed','rejected','released','resumed','retired','suspended','terms_rebound','thesis_broken','thesis_evidence_refreshed','tranche_armed','tranche_closed_partial','tranche_filled','tranche_partially_filled','tranche_proposed','tranche_send_fenced','tranche_send_released','tranche_unfilled')",
    ),
    ("seq_positive", "seq >= 1"),
    (
        "tranche_index_fields",
        "(tranche_index IS NOT NULL) = (event IN ('tranche_armed','tranche_closed_partial','tranche_filled','tranche_partially_filled','tranche_proposed','tranche_send_fenced','tranche_send_released','tranche_unfilled'))",
    ),
    ("terms_together", "(terms_sha256 IS NULL) = (terms_id IS NULL)"),
    (
        "terms_allowed",
        "terms_id IS NULL OR event IN ('approved','rejected','retired','terms_rebound')",
    ),
    (
        "terms_required",
        "NOT (event IN ('approved','terms_rebound')) OR terms_id IS NOT NULL",
    ),
    (
        "effective_session_fields",
        "(effective_session IS NOT NULL) = (event IN ('approved','resumed','terms_rebound'))",
    ),
    ("resume_target_fields", "(resume_target IS NOT NULL) = (event IN ('resumed'))"),
    (
        "batch_member_id_allowed",
        "batch_member_id IS NULL OR event IN ('approved','rejected','retired','terms_rebound')",
    ),
    (
        "proposal_id_fields",
        "(proposal_id IS NOT NULL) = (event IN ('tranche_filled','tranche_partially_filled','tranche_proposed','tranche_send_fenced','tranche_unfilled'))",
    ),
    (
        "fence_together",
        "(kst_day IS NULL) = (notional IS NULL) AND (approval_mode IS NULL) = (notional IS NULL) AND (account_ref IS NULL) = (notional IS NULL) AND (attempt_id IS NULL) = (notional IS NULL)",
    ),
    ("fence_fields", "(notional IS NOT NULL) = (event IN ('tranche_send_fenced'))"),
    (
        "fenced_seq_fields",
        "(fenced_seq IS NOT NULL) = (event IN ('tranche_send_released'))",
    ),
    (
        "thesis_seq_fields",
        "(thesis_seq IS NOT NULL) = (event IN ('thesis_evidence_refreshed'))",
    ),
    ("fill_together", "(fill_quantity IS NULL) = (fill_amount IS NULL)"),
    (
        "fill_fields",
        "(fill_amount IS NOT NULL) = (event IN ('tranche_filled','tranche_partially_filled'))",
    ),
    ("tranche_index_range", "tranche_index IS NULL OR tranche_index BETWEEN 1 AND 3"),
    (
        "resume_target_value",
        "resume_target IS NULL OR resume_target IN ('approved','active')",
    ),
    (
        "approval_mode_value",
        "approval_mode IS NULL OR approval_mode IN ('auto','human')",
    ),
    (
        "profile_with_auto_fence",
        "(event = 'tranche_send_fenced' AND approval_mode = 'auto') = (activation_profile_id IS NOT NULL)",
    ),
    (
        "first_fill_fields",
        "((first_fill_amount IS NULL) = (first_fill_quantity IS NULL)) AND ((first_fill_amount IS NOT NULL) = (event IN ('tranche_closed_partial','tranche_filled') AND tranche_index = 1))",
    ),
    (
        "amounts_positive",
        "(notional IS NULL OR notional > 0) AND (fill_amount IS NULL OR fill_amount > 0) AND (fill_quantity IS NULL OR fill_quantity > 0) AND (first_fill_amount IS NULL OR first_fill_amount > 0) AND (first_fill_quantity IS NULL OR first_fill_quantity > 0)",
    ),
    ("fenced_seq_positive", "fenced_seq IS NULL OR fenced_seq >= 1"),
    ("thesis_seq_positive", "thesis_seq IS NULL OR thesis_seq >= 1"),
    ("evidence_object", "jsonb_typeof(evidence) = 'object'"),
    (
        "evidence_keys",
        "(evidence - ARRAY['ledger_row_ids','trade_date','reason_code','retrospective_id','approval_issue_id','open_order_count','unknown_order_count','holdings_quantity','holdings_checked_at','holdings_account_refs','holdings_disposition']) = '{}'::jsonb",
    ),
    ("actor_nonblank", "char_length(btrim(actor)) BETWEEN 1 AND 128"),
)

TABLES_NEWEST_FIRST: tuple[str, ...] = (
    "mandate_symbol_reservation_releases",
    "mandate_symbol_reservations",
    "mandate_plan_events",
    "mandate_plan_tranches",
    "mandate_plans",
    "mandate_activation_profiles",
    "mandate_terms",
    "mandate_approvals",
    "mandate_approval_requests",
)
SEQUENCE_TABLES: tuple[str, ...] = tuple(
    t for t in TABLES_NEWEST_FIRST if t != "mandate_plan_tranches"
)


def _list(values: Sequence[str]) -> str:
    return "ARRAY[" + ",".join(f"'{x}'" for x in values) + "]::text[]"


def _in(values: Sequence[str]) -> str:
    return ",".join(f"'{x}'" for x in values)


def _auth_after_case() -> str:
    arms = " ".join(f"WHEN '{e}' THEN '{s}'" for e, s in AUTH_AFTER.items())
    return f"CASE rec.event {arms} WHEN 'resumed' THEN rec.resume_target ELSE st END"


def _auth_from_case() -> str:
    arms = " ".join(f"WHEN '{e}' THEN {_list(a)}" for e, a in AUTH_FROM.items())
    return f"CASE NEW.event {arms} END"


def _realm_case() -> str:
    arms = " ".join(f"WHEN '{m}' THEN '{r}'" for m, r in REALM_BY_ACCOUNT_MODE.items())
    return f"CASE p_account_mode {arms} END"


_TERMINAL_IN = _in(TRANCHE_TERMINAL_EVENTS)
_EVENTS = "review.mandate_plan_events"

# Reusable SQL fragments over the plan's own events. ``E`` is the event being
# inserted; ``k`` the PL/pgSQL variable holding its tranche index.
_COMPLETED = (
    "EXISTS (SELECT 1 FROM {ev} e WHERE e.plan_id = NEW.plan_id "
    "AND e.tranche_index = {k} "
    "AND e.event IN ('tranche_filled','tranche_closed_partial'))"
)
_OPEN_PROPOSAL = (
    "EXISTS (SELECT 1 FROM {ev} p WHERE p.plan_id = NEW.plan_id "
    "AND p.event = 'tranche_proposed' AND p.tranche_index = k "
    "AND NOT EXISTS (SELECT 1 FROM {ev} t WHERE t.plan_id = p.plan_id "
    f"AND t.event IN ({_TERMINAL_IN}) AND t.proposal_id = p.proposal_id))"
)
_UNCLOSED_FENCE = (
    "EXISTS (SELECT 1 FROM {ev} f WHERE f.plan_id = NEW.plan_id "
    "AND f.event = 'tranche_send_fenced' AND f.tranche_index = k "
    "AND NOT EXISTS (SELECT 1 FROM {ev} r WHERE r.plan_id = f.plan_id "
    "AND r.event = 'tranche_send_released' AND r.fenced_seq = f.seq) "
    "AND NOT EXISTS (SELECT 1 FROM {ev} t WHERE t.plan_id = f.plan_id "
    f"AND t.event IN ({_TERMINAL_IN}) AND t.proposal_id = f.proposal_id))"
)
_ANY_UNCLOSED_FENCE = (
    "EXISTS (SELECT 1 FROM {ev} f WHERE f.plan_id = NEW.plan_id "
    "AND f.event = 'tranche_send_fenced' "
    "AND NOT EXISTS (SELECT 1 FROM {ev} r WHERE r.plan_id = f.plan_id "
    "AND r.event = 'tranche_send_released' AND r.fenced_seq = f.seq) "
    "AND NOT EXISTS (SELECT 1 FROM {ev} t WHERE t.plan_id = f.plan_id "
    f"AND t.event IN ({_TERMINAL_IN}) AND t.proposal_id = f.proposal_id))"
)
_ANY_OPEN_PROPOSAL = (
    "EXISTS (SELECT 1 FROM {ev} p WHERE p.plan_id = NEW.plan_id "
    "AND p.event = 'tranche_proposed' "
    "AND NOT EXISTS (SELECT 1 FROM {ev} t WHERE t.plan_id = p.plan_id "
    f"AND t.event IN ({_TERMINAL_IN}) AND t.proposal_id = p.proposal_id))"
)
_ARMED = (
    "coalesce((SELECT max(a.seq) FROM {ev} a WHERE a.plan_id = NEW.plan_id "
    "AND a.event = 'tranche_armed' AND a.tranche_index = k), 0) > "
    "coalesce((SELECT max(c.seq) FROM {ev} c WHERE c.plan_id = NEW.plan_id "
    "AND c.event IN ('tranche_proposed','tranche_closed_partial') "
    "AND c.tranche_index = k), 0)"
)


def _frag(template: str) -> str:
    return template.format(ev=_EVENTS, k="k")


def _events_guard_ddl() -> str:
    completed_k = _frag(_COMPLETED)
    completed_prev = _COMPLETED.format(ev=_EVENTS, k="k - 1")
    completed_1 = _COMPLETED.format(ev=_EVENTS, k="1")
    completed_3 = _COMPLETED.format(ev=_EVENTS, k="3")
    return f"""
    CREATE OR REPLACE FUNCTION review.mandate_plan_events_guard()
    RETURNS trigger AS $$
    DECLARE
        last_seq integer;
        st text;
        allowed text[];
        pl record;
        tm record;
        k smallint := NEW.tranche_index;
        eff date;
        prior_amount numeric;
        prior_quantity numeric;
    BEGIN
        SELECT coalesce(max(seq), 0) INTO last_seq
          FROM {_EVENTS} WHERE plan_id = NEW.plan_id;
        IF NEW.seq <> last_seq + 1 THEN
            RAISE EXCEPTION 'mandate_plan_events seq must follow the last seq'
                USING ERRCODE = 'restrict_violation';
        END IF;
        SELECT p.account_ref, p.sleeve_id, p.scope_account_refs INTO pl
          FROM review.mandate_plans p WHERE p.id = NEW.plan_id;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'mandate_plan_events names no plan'
                USING ERRCODE = 'restrict_violation';
        END IF;
        st := review.mandate_plan_auth_state(NEW.plan_id);
        IF st IS NULL THEN
            IF NEW.event <> 'proposed' THEN
                RAISE EXCEPTION 'the first event of a plan is proposed'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END IF;
        IF NEW.event = 'proposed' THEN
            RAISE EXCEPTION 'proposed opens a stream once'
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF EXISTS (SELECT 1 FROM {_EVENTS} x
                    WHERE x.plan_id = NEW.plan_id AND x.event = 'released') THEN
            RAISE EXCEPTION 'a released plan takes no more events'
                USING ERRCODE = 'restrict_violation';
        END IF;

        -- The clause an event cites must be a clause of this plan's sleeve and
        -- hash to the recorded value; a binding event keeps the plan's account.
        IF NEW.terms_id IS NOT NULL THEN
            SELECT t.account_ref, t.content_sha256, v.sleeve_id INTO tm
              FROM review.mandate_terms t
              JOIN review.strategy_sleeve_versions v ON v.id = t.sleeve_version_id
             WHERE t.id = NEW.terms_id;
            IF NOT FOUND OR tm.sleeve_id <> pl.sleeve_id
               OR tm.content_sha256 <> NEW.terms_sha256
               OR (NEW.event IN ('approved','terms_rebound')
                   AND tm.account_ref <> pl.account_ref) THEN
                RAISE EXCEPTION 'the cited terms do not bind to this plan'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END IF;

        IF NEW.event IN ({_in(list(AUTH_FROM))}) THEN
            allowed := {_auth_from_case()};
            IF NOT (st = ANY (allowed)) THEN
                RAISE EXCEPTION 'event % cannot follow authority %', NEW.event, st
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.event = 'first_tranche_settled' AND NOT EXISTS (
                SELECT 1 FROM {_EVENTS} e WHERE e.plan_id = NEW.plan_id
                   AND e.tranche_index = 1 AND e.first_fill_amount IS NOT NULL
                   AND e.event IN ('tranche_filled','tranche_closed_partial')) THEN
                RAISE EXCEPTION 'first_tranche_settled needs a completed first tranche'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.event = 'exhausted' AND NOT {completed_3} THEN
                RAISE EXCEPTION 'exhausted needs a completed third tranche'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.event = 'resumed' AND NEW.resume_target IS DISTINCT FROM
               (CASE WHEN {completed_1} THEN 'active' ELSE 'approved' END) THEN
                RAISE EXCEPTION 'resume_target does not match the plan'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END IF;

        IF NEW.event = 'thesis_evidence_refreshed' THEN
            IF NOT EXISTS (SELECT 1 FROM {_EVENTS} x WHERE x.plan_id = NEW.plan_id
                              AND x.event = 'thesis_broken' AND x.seq = NEW.thesis_seq) THEN
                RAISE EXCEPTION 'thesis_seq must name a thesis_broken event'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END IF;

        IF NEW.event IN ('orders_settled','holdings_disposed','released') THEN
            IF NOT (st = ANY ({_list(CLEANUP_AUTH)})) THEN
                RAISE EXCEPTION 'cleanup needs a plan without sending authority'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF {_ANY_UNCLOSED_FENCE.format(ev=_EVENTS)}
               AND NEW.event <> 'holdings_disposed' THEN
                RAISE EXCEPTION 'a send fence is not closed'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF {_ANY_OPEN_PROPOSAL.format(ev=_EVENTS)}
               AND NEW.event <> 'holdings_disposed' THEN
                RAISE EXCEPTION 'a tranche proposal is still open'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.event = 'orders_settled' AND EXISTS (
                SELECT 1 FROM {_EVENTS} x
                 WHERE x.plan_id = NEW.plan_id AND x.event = 'orders_settled') THEN
                RAISE EXCEPTION 'orders_settled is written once'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.event = 'holdings_disposed' THEN
                IF NOT EXISTS (SELECT 1 FROM {_EVENTS} x WHERE x.plan_id = NEW.plan_id
                                  AND x.event = 'orders_settled')
                   OR EXISTS (SELECT 1 FROM {_EVENTS} x WHERE x.plan_id = NEW.plan_id
                                 AND x.event = 'holdings_disposed') THEN
                    RAISE EXCEPTION 'holdings_disposed follows orders_settled, once'
                        USING ERRCODE = 'restrict_violation';
                END IF;
                IF NEW.evidence ->> 'holdings_disposition' NOT IN ({_in(HOLDINGS_DISPOSITIONS)})
                   OR NEW.evidence ->> 'holdings_disposition' IS NULL THEN
                    RAISE EXCEPTION 'holdings_disposed needs a closed disposition kind'
                        USING ERRCODE = 'restrict_violation';
                END IF;
                IF NEW.evidence ->> 'holdings_disposition' = 'never_adopted'
                   AND EXISTS (SELECT 1 FROM {_EVENTS} x WHERE x.plan_id = NEW.plan_id
                                  AND x.event = 'approved') THEN
                    RAISE EXCEPTION 'never_adopted is for a plan that was never approved'
                        USING ERRCODE = 'restrict_violation';
                END IF;
                IF NEW.evidence ->> 'holdings_disposition' = 'kept_outside_basket'
                   AND NEW.actor LIKE 'system:%' THEN
                    RAISE EXCEPTION 'kept_outside_basket is an operator record'
                        USING ERRCODE = 'restrict_violation';
                END IF;
                IF NEW.evidence ->> 'holdings_disposition' = 'zero_holdings' THEN
                    IF NEW.evidence -> 'holdings_account_refs' IS NULL
                       OR jsonb_typeof(NEW.evidence -> 'holdings_account_refs') <> 'array'
                       OR (SELECT array_agg(x ORDER BY x)
                             FROM jsonb_array_elements_text(
                                  NEW.evidence -> 'holdings_account_refs') AS x)
                          IS DISTINCT FROM
                          (SELECT array_agg(y ORDER BY y)
                             FROM unnest(pl.scope_account_refs) AS y)
                       OR (NEW.evidence ->> 'holdings_quantity') IS NULL
                       OR (NEW.evidence ->> 'holdings_quantity')::numeric <> 0 THEN
                        RAISE EXCEPTION 'zero_holdings needs a zero read of every scope account'
                            USING ERRCODE = 'restrict_violation';
                    END IF;
                END IF;
            END IF;
            IF NEW.event = 'released' AND NOT (
                EXISTS (SELECT 1 FROM {_EVENTS} x WHERE x.plan_id = NEW.plan_id
                           AND x.event = 'holdings_disposed')) THEN
                RAISE EXCEPTION 'released needs orders settled and holdings disposed'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END IF;

        -- Tranche events.
        IF NEW.event IN ('tranche_armed','tranche_proposed','tranche_send_fenced')
           AND NOT (st = ANY ({_list(SEND_AUTHORITY)})) THEN
            RAISE EXCEPTION 'the plan does not hold sending authority (%)', st
                USING ERRCODE = 'restrict_violation';
        END IF;
        IF NEW.event = 'tranche_armed' THEN
            IF {completed_k} OR {_frag(_ARMED)} OR {_frag(_OPEN_PROPOSAL)}
               OR {_frag(_UNCLOSED_FENCE)}
               OR (k > 1 AND NOT {completed_prev}) THEN
                RAISE EXCEPTION 'tranche cannot be armed now'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        ELSIF NEW.event = 'tranche_proposed' THEN
            IF NOT {_frag(_ARMED)} OR {completed_k} OR {_frag(_OPEN_PROPOSAL)}
               OR {_frag(_UNCLOSED_FENCE)} THEN
                RAISE EXCEPTION 'tranche_proposed needs an armed tranche with nothing open'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        ELSIF NEW.event = 'tranche_send_fenced' THEN
            SELECT e.effective_session INTO eff FROM {_EVENTS} e
             WHERE e.plan_id = NEW.plan_id
               AND e.event IN ('approved','terms_rebound','resumed')
             ORDER BY e.seq DESC LIMIT 1;
            IF eff IS NULL OR NEW.kst_day < eff THEN
                RAISE EXCEPTION 'the fence day is before the effective session'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.account_ref <> pl.account_ref THEN
                RAISE EXCEPTION 'the fence account is not the plan account'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.activation_profile_id IS NOT NULL AND NOT EXISTS (
                SELECT 1 FROM review.mandate_activation_profiles ap
                 WHERE ap.id = NEW.activation_profile_id
                   AND ap.sleeve_id = pl.sleeve_id) THEN
                RAISE EXCEPTION 'the fence cites another sleeve activation profile'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NOT EXISTS (
                SELECT 1 FROM {_EVENTS} p WHERE p.plan_id = NEW.plan_id
                   AND p.event = 'tranche_proposed' AND p.tranche_index = k
                   AND p.proposal_id = NEW.proposal_id
                   AND NOT EXISTS (SELECT 1 FROM {_EVENTS} t
                                    WHERE t.plan_id = p.plan_id
                                      AND t.event IN ({_TERMINAL_IN})
                                      AND t.proposal_id = p.proposal_id)) THEN
                RAISE EXCEPTION 'the fence needs the tranche open proposal'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF {_frag(_UNCLOSED_FENCE)} THEN
                RAISE EXCEPTION 'the tranche has a send fence that is not closed'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        ELSIF NEW.event = 'tranche_send_released' THEN
            IF NOT EXISTS (
                SELECT 1 FROM {_EVENTS} f WHERE f.plan_id = NEW.plan_id
                   AND f.event = 'tranche_send_fenced' AND f.seq = NEW.fenced_seq
                   AND f.tranche_index = k
                   AND NOT EXISTS (SELECT 1 FROM {_EVENTS} t
                                    WHERE t.plan_id = f.plan_id
                                      AND t.event IN ({_TERMINAL_IN})
                                      AND t.proposal_id = f.proposal_id)) THEN
                RAISE EXCEPTION 'fenced_seq must name an open fence of this tranche'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        ELSIF NEW.event IN ({_TERMINAL_IN}) THEN
            IF NOT EXISTS (
                SELECT 1 FROM {_EVENTS} p WHERE p.plan_id = NEW.plan_id
                   AND p.event = 'tranche_proposed' AND p.tranche_index = k
                   AND p.proposal_id = NEW.proposal_id) THEN
                RAISE EXCEPTION 'a terminal event needs a proposal of this tranche'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.event = 'tranche_filled' AND k = 1 THEN
                SELECT coalesce(sum(e.fill_amount), 0), coalesce(sum(e.fill_quantity), 0)
                  INTO prior_amount, prior_quantity
                  FROM {_EVENTS} e WHERE e.plan_id = NEW.plan_id AND e.tranche_index = 1;
                IF NEW.first_fill_amount IS DISTINCT FROM prior_amount + NEW.fill_amount
                   OR NEW.first_fill_quantity IS DISTINCT FROM prior_quantity + NEW.fill_quantity THEN
                    RAISE EXCEPTION 'first_fill must equal the first tranche cumulative fills'
                        USING ERRCODE = 'restrict_violation';
                END IF;
            END IF;
        ELSIF NEW.event = 'tranche_closed_partial' THEN
            IF NOT (st = ANY ({_list(CLOSED_PARTIAL_AUTH)})) THEN
                RAISE EXCEPTION 'tranche_closed_partial needs a live plan'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            SELECT coalesce(sum(e.fill_amount), 0), coalesce(sum(e.fill_quantity), 0)
              INTO prior_amount, prior_quantity
              FROM {_EVENTS} e WHERE e.plan_id = NEW.plan_id AND e.tranche_index = k;
            IF {completed_k} OR {_frag(_OPEN_PROPOSAL)} OR {_frag(_UNCLOSED_FENCE)}
               OR prior_amount <= 0 THEN
                RAISE EXCEPTION 'a tranche with no fill, an open proposal or fence cannot be closed'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF k = 1 AND (NEW.first_fill_amount IS DISTINCT FROM prior_amount
                          OR NEW.first_fill_quantity IS DISTINCT FROM prior_quantity) THEN
                RAISE EXCEPTION 'first_fill must equal the first tranche cumulative fills'
                    USING ERRCODE = 'restrict_violation';
            END IF;
        END IF;
        RETURN NEW;
    END;
    $$ LANGUAGE plpgsql
    """


def _guard_ddl() -> tuple[str, ...]:
    immutable_tables = TABLES_NEWEST_FIRST
    ddl: list[str] = [
        """
        CREATE OR REPLACE FUNCTION review.mandate_rows_immutable()
        RETURNS trigger AS $$
        BEGIN
            RAISE EXCEPTION '% rows are append-only (% refused)', TG_TABLE_NAME, TG_OP
                USING ERRCODE = 'restrict_violation';
        END;
        $$ LANGUAGE plpgsql
        """,
    ]
    for table in immutable_tables:
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
        f"""
        CREATE OR REPLACE FUNCTION review.mandate_realm(p_account_mode text)
        RETURNS text AS $$
            SELECT {_realm_case()}
        $$ LANGUAGE sql IMMUTABLE
        """,
        f"""
        CREATE OR REPLACE FUNCTION review.mandate_plan_auth_state(p_plan_id bigint)
        RETURNS text AS $$
        DECLARE
            rec record;
            st text := NULL;
        BEGIN
            FOR rec IN
                SELECT event, resume_target FROM {_EVENTS}
                 WHERE plan_id = p_plan_id ORDER BY seq
            LOOP
                st := {_auth_after_case()};
            END LOOP;
            RETURN st;
        END;
        $$ LANGUAGE plpgsql
        """,
        # --- approvals -----------------------------------------------------
        """
        CREATE OR REPLACE FUNCTION review.mandate_approvals_guard()
        RETURNS trigger AS $$
        DECLARE
            req_sha text;
        BEGIN
            SELECT content_sha256 INTO req_sha
              FROM review.mandate_approval_requests WHERE id = NEW.request_id;
            IF req_sha IS NULL OR NEW.subject_sha256 IS DISTINCT FROM req_sha THEN
                RAISE EXCEPTION 'an approval must carry its request content hash'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_mandate_approvals_guard ON review.mandate_approvals",
        """
        CREATE TRIGGER trg_mandate_approvals_guard
        BEFORE INSERT ON review.mandate_approvals
        FOR EACH ROW EXECUTE FUNCTION review.mandate_approvals_guard()
        """,
        # --- terms ---------------------------------------------------------
        """
        CREATE OR REPLACE FUNCTION review.mandate_terms_guard()
        RETURNS trigger AS $$
        DECLARE
            ver record;
            apv record;
            realm text;
        BEGIN
            SELECT v.sleeve_id, v.state, s.account_mode, s.market, s.currency INTO ver
              FROM review.strategy_sleeve_versions v
              JOIN review.strategy_sleeves s ON s.id = v.sleeve_id
             WHERE v.id = NEW.sleeve_version_id;
            IF NOT FOUND OR ver.state <> 'live' THEN
                RAISE EXCEPTION 'mandate terms bind to a live sleeve version'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            realm := review.mandate_realm(ver.account_mode);
            IF realm IS NULL OR ver.market NOT IN ('equity_kr','equity_us') THEN
                RAISE EXCEPTION 'this sleeve has no mandate realm or market'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            SELECT ap.decision, ap.subject_sha256, rq.subject_kind, rq.sleeve_id INTO apv
              FROM review.mandate_approvals ap
              JOIN review.mandate_approval_requests rq ON rq.id = ap.request_id
             WHERE ap.id = NEW.approval_id;
            IF NOT FOUND OR apv.decision <> 'approved' OR apv.subject_kind <> 'terms'
               OR apv.sleeve_id <> ver.sleeve_id
               OR apv.subject_sha256 <> NEW.content_sha256 THEN
                RAISE EXCEPTION 'terms need an approved approval of the same content'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF EXISTS (
                SELECT 1 FROM review.mandate_terms t
                  JOIN review.strategy_sleeve_versions tv ON tv.id = t.sleeve_version_id
                  JOIN review.strategy_sleeves ts ON ts.id = tv.sleeve_id
                 WHERE t.basket_key = NEW.basket_key
                   AND review.mandate_realm(ts.account_mode) = realm
                   AND (ts.market <> ver.market OR ts.currency <> ver.currency)
            ) THEN
                RAISE EXCEPTION 'a basket holds one market and one currency'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_mandate_terms_guard ON review.mandate_terms",
        """
        CREATE TRIGGER trg_mandate_terms_guard
        BEFORE INSERT ON review.mandate_terms
        FOR EACH ROW EXECUTE FUNCTION review.mandate_terms_guard()
        """,
        # --- activation profiles -------------------------------------------
        """
        CREATE OR REPLACE FUNCTION review.mandate_activation_profiles_guard()
        RETURNS trigger AS $$
        DECLARE
            last_version integer;
            terms_sleeve bigint;
            apv record;
        BEGIN
            SELECT max(profile_version) INTO last_version
              FROM review.mandate_activation_profiles WHERE sleeve_id = NEW.sleeve_id;
            IF NEW.profile_version <> coalesce(last_version, 0) + 1 THEN
                RAISE EXCEPTION 'profile_version must follow the last version'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            SELECT v.sleeve_id INTO terms_sleeve
              FROM review.mandate_terms t
              JOIN review.strategy_sleeve_versions v ON v.id = t.sleeve_version_id
             WHERE t.id = NEW.terms_id;
            IF terms_sleeve IS DISTINCT FROM NEW.sleeve_id THEN
                RAISE EXCEPTION 'a profile opens a clause of its own sleeve'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            SELECT ap.decision, ap.subject_sha256, rq.subject_kind, rq.sleeve_id INTO apv
              FROM review.mandate_approvals ap
              JOIN review.mandate_approval_requests rq ON rq.id = ap.request_id
             WHERE ap.id = NEW.approval_id;
            IF NOT FOUND OR apv.decision <> 'approved'
               OR apv.subject_kind <> 'activation_profile'
               OR apv.sleeve_id <> NEW.sleeve_id
               OR apv.subject_sha256 <> NEW.content_sha256 THEN
                RAISE EXCEPTION 'a profile needs an approved approval of the same content'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_mandate_activation_profiles_guard "
        "ON review.mandate_activation_profiles",
        """
        CREATE TRIGGER trg_mandate_activation_profiles_guard
        BEFORE INSERT ON review.mandate_activation_profiles
        FOR EACH ROW EXECUTE FUNCTION review.mandate_activation_profiles_guard()
        """,
        # --- plans ----------------------------------------------------------
        """
        CREATE OR REPLACE FUNCTION review.mandate_plans_guard()
        RETURNS trigger AS $$
        DECLARE
            tm record;
            sl record;
        BEGIN
            SELECT t.account_ref, v.sleeve_id INTO tm
              FROM review.mandate_terms t
              JOIN review.strategy_sleeve_versions v ON v.id = t.sleeve_version_id
             WHERE t.id = NEW.approved_terms_id;
            IF NOT FOUND OR tm.sleeve_id <> NEW.sleeve_id
               OR tm.account_ref <> NEW.account_ref THEN
                RAISE EXCEPTION 'the plan does not bind to a clause of its sleeve and account'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            SELECT review.mandate_realm(account_mode) AS realm, market INTO sl
              FROM review.strategy_sleeves WHERE id = NEW.sleeve_id;
            IF NOT FOUND OR sl.realm IS NULL
               OR sl.market NOT IN ('equity_kr','equity_us') THEN
                RAISE EXCEPTION 'this sleeve cannot hold a mandate plan'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF cardinality(NEW.scope_account_refs) <>
                   (CASE sl.realm WHEN 'live' THEN 2 ELSE 1 END)
               OR (SELECT count(DISTINCT x) FROM unnest(NEW.scope_account_refs) AS x)
                   <> cardinality(NEW.scope_account_refs) THEN
                RAISE EXCEPTION 'a live plan scopes both real accounts, a mock plan its one account'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_mandate_plans_guard ON review.mandate_plans",
        """
        CREATE TRIGGER trg_mandate_plans_guard
        BEFORE INSERT ON review.mandate_plans
        FOR EACH ROW EXECUTE FUNCTION review.mandate_plans_guard()
        """,
        # --- events ---------------------------------------------------------
        _events_guard_ddl(),
        "DROP TRIGGER IF EXISTS trg_mandate_plan_events_guard "
        "ON review.mandate_plan_events",
        """
        CREATE TRIGGER trg_mandate_plan_events_guard
        BEFORE INSERT ON review.mandate_plan_events
        FOR EACH ROW EXECUTE FUNCTION review.mandate_plan_events_guard()
        """,
        # --- reservations ---------------------------------------------------
        """
        CREATE OR REPLACE FUNCTION review.mandate_symbol_reservations_guard()
        RETURNS trigger AS $$
        DECLARE
            pl record;
        BEGIN
            IF current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'mandate symbol reservations need READ COMMITTED'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'mandate_symbol:' || NEW.realm || ':' || NEW.market || ':' || NEW.symbol, 0));
            SELECT p.symbol, review.mandate_realm(s.account_mode) AS realm, s.market INTO pl
              FROM review.mandate_plans p
              JOIN review.strategy_sleeves s ON s.id = p.sleeve_id
             WHERE p.id = NEW.plan_id;
            IF NOT FOUND OR pl.symbol <> NEW.symbol
               OR pl.realm IS DISTINCT FROM NEW.realm OR pl.market <> NEW.market THEN
                RAISE EXCEPTION 'the reservation does not match its plan'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF EXISTS (SELECT 1 FROM review.mandate_plan_events e
                        WHERE e.plan_id = NEW.plan_id AND e.event = 'released') THEN
                RAISE EXCEPTION 'a released plan cannot take a reservation'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF EXISTS (
                SELECT 1 FROM review.mandate_symbol_reservations r
                 WHERE r.realm = NEW.realm AND r.market = NEW.market
                   AND r.symbol = NEW.symbol
                   AND NOT EXISTS (
                       SELECT 1 FROM review.mandate_symbol_reservation_releases x
                        WHERE x.reservation_id = r.id)
            ) THEN
                RAISE EXCEPTION 'this symbol already has an unreleased plan in this realm'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_mandate_symbol_reservations_guard "
        "ON review.mandate_symbol_reservations",
        """
        CREATE TRIGGER trg_mandate_symbol_reservations_guard
        BEFORE INSERT ON review.mandate_symbol_reservations
        FOR EACH ROW EXECUTE FUNCTION review.mandate_symbol_reservations_guard()
        """,
        """
        CREATE OR REPLACE FUNCTION review.mandate_symbol_reservation_releases_guard()
        RETURNS trigger AS $$
        DECLARE
            res record;
        BEGIN
            IF current_setting('transaction_isolation') <> 'read committed' THEN
                RAISE EXCEPTION 'mandate symbol releases need READ COMMITTED'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            SELECT realm, market, symbol, plan_id INTO res
              FROM review.mandate_symbol_reservations WHERE id = NEW.reservation_id;
            IF NOT FOUND THEN
                RAISE EXCEPTION 'the release names no reservation'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            PERFORM pg_advisory_xact_lock(hashtextextended(
                'mandate_symbol:' || res.realm || ':' || res.market || ':' || res.symbol, 0));
            IF NOT EXISTS (SELECT 1 FROM review.mandate_plan_events e
                            WHERE e.plan_id = res.plan_id AND e.event = 'released') THEN
                RAISE EXCEPTION 'a reservation is released only with the plan released event'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_mandate_symbol_reservation_releases_guard "
        "ON review.mandate_symbol_reservation_releases",
        """
        CREATE TRIGGER trg_mandate_symbol_reservation_releases_guard
        BEFORE INSERT ON review.mandate_symbol_reservation_releases
        FOR EACH ROW EXECUTE FUNCTION
            review.mandate_symbol_reservation_releases_guard()
        """,
        # The release row is written by the ``released`` event itself, so the two
        # always commit together.
        """
        CREATE OR REPLACE FUNCTION review.mandate_plan_released_writes_release()
        RETURNS trigger AS $$
        BEGIN
            INSERT INTO review.mandate_symbol_reservation_releases (reservation_id)
            SELECT r.id FROM review.mandate_symbol_reservations r
             WHERE r.plan_id = NEW.plan_id;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_mandate_plan_released_release "
        "ON review.mandate_plan_events",
        """
        CREATE TRIGGER trg_mandate_plan_released_release
        AFTER INSERT ON review.mandate_plan_events
        FOR EACH ROW WHEN (NEW.event = 'released')
        EXECUTE FUNCTION review.mandate_plan_released_writes_release()
        """,
        # --- order_proposals ------------------------------------------------
        """
        CREATE OR REPLACE FUNCTION review.order_proposals_mandate_guard()
        RETURNS trigger AS $$
        DECLARE
            pl record;
        BEGIN
            IF TG_OP = 'UPDATE' THEN
                RAISE EXCEPTION 'review.order_proposals mandate binding cannot change once set'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.mandate_plan_id IS NULL OR NEW.mandate_tranche_index IS NULL
               OR NEW.mandate_tranche_index NOT BETWEEN 1 AND 3 THEN
                RAISE EXCEPTION 'a mandate binding needs a plan and a tranche 1..3'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            SELECT p.symbol, p.sleeve_id, s.account_mode, s.market INTO pl
              FROM review.mandate_plans p
              JOIN review.strategy_sleeves s ON s.id = p.sleeve_id
             WHERE p.id = NEW.mandate_plan_id;
            IF NOT FOUND OR NEW.sleeve_id IS DISTINCT FROM pl.sleeve_id
               OR NEW.account_mode <> pl.account_mode OR NEW.market <> pl.market
               OR NEW.symbol <> pl.symbol THEN
                RAISE EXCEPTION 'the proposal does not match its mandate plan'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            IF NEW.side <> 'buy' OR NEW.order_type <> 'limit'
               OR NEW.exit_intent IS NOT NULL
               OR coalesce(NEW.action, 'place') <> 'place' THEN
                RAISE EXCEPTION 'a mandate binding is for a limit buy place only'
                    USING ERRCODE = 'restrict_violation';
            END IF;
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """,
        "DROP TRIGGER IF EXISTS trg_order_proposals_mandate_insert "
        "ON review.order_proposals",
        """
        CREATE TRIGGER trg_order_proposals_mandate_insert
        BEFORE INSERT ON review.order_proposals
        FOR EACH ROW WHEN (NEW.mandate_plan_id IS NOT NULL
                           OR NEW.mandate_tranche_index IS NOT NULL)
        EXECUTE FUNCTION review.order_proposals_mandate_guard()
        """,
        "DROP TRIGGER IF EXISTS trg_order_proposals_mandate_update "
        "ON review.order_proposals",
        """
        CREATE TRIGGER trg_order_proposals_mandate_update
        BEFORE UPDATE OF mandate_plan_id, mandate_tranche_index ON review.order_proposals
        FOR EACH ROW WHEN (OLD.mandate_plan_id IS DISTINCT FROM NEW.mandate_plan_id
                           OR OLD.mandate_tranche_index IS DISTINCT FROM NEW.mandate_tranche_index)
        EXECUTE FUNCTION review.order_proposals_mandate_guard()
        """,
    ]
    return tuple(ddl)


#: One statement per entry. The test schema bootstrap installs these too, so the
#: DB tests exercise this exact DDL.
GUARD_DDL: tuple[str, ...] = _guard_ddl()

DOWNGRADE_GUARD = """
    DO $$
    BEGIN
        LOCK TABLE review.mandate_approval_requests, review.mandate_approvals,
                   review.mandate_terms, review.mandate_activation_profiles,
                   review.mandate_plans, review.mandate_plan_tranches,
                   review.mandate_plan_events, review.mandate_symbol_reservations,
                   review.mandate_symbol_reservation_releases
            IN ACCESS EXCLUSIVE MODE;
        IF EXISTS (SELECT 1 FROM review.mandate_approval_requests)
           OR EXISTS (SELECT 1 FROM review.mandate_approvals)
           OR EXISTS (SELECT 1 FROM review.mandate_terms)
           OR EXISTS (SELECT 1 FROM review.mandate_activation_profiles)
           OR EXISTS (SELECT 1 FROM review.mandate_plans)
           OR EXISTS (SELECT 1 FROM review.mandate_plan_tranches)
           OR EXISTS (SELECT 1 FROM review.mandate_plan_events)
           OR EXISTS (SELECT 1 FROM review.mandate_symbol_reservations)
           OR EXISTS (SELECT 1 FROM review.mandate_symbol_reservation_releases)
           OR EXISTS (
               SELECT 1 FROM review.order_proposals WHERE mandate_plan_id IS NOT NULL
           ) THEN
            RAISE EXCEPTION 'mandate tables hold rows or bind proposals; export them before downgrading'
                USING ERRCODE = 'restrict_violation';
        END IF;
    END;
    $$
"""

_FUNCTIONS = (
    "review.mandate_rows_immutable()",
    "review.mandate_realm(text)",
    "review.mandate_plan_auth_state(bigint)",
    "review.mandate_approvals_guard()",
    "review.mandate_terms_guard()",
    "review.mandate_activation_profiles_guard()",
    "review.mandate_plans_guard()",
    "review.mandate_plan_events_guard()",
    "review.mandate_symbol_reservations_guard()",
    "review.mandate_symbol_reservation_releases_guard()",
    "review.mandate_plan_released_writes_release()",
    "review.order_proposals_mandate_guard()",
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


def _sha_check(table: str, column: str) -> sa.CheckConstraint:
    return _ck(table, f"{column}_format", f"{column} ~ '^[0-9a-f]{{64}}$'")


def _create_tables() -> None:
    op.create_table(
        "mandate_approval_requests",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("subject_kind", sa.Text(), nullable=False),
        sa.Column("sleeve_id", sa.BigInteger(), nullable=False),
        sa.Column("base_sleeve_version", sa.Integer(), nullable=False),
        sa.Column("content", postgresql.JSONB(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        sa.Column("approval_ref", sa.Text(), nullable=False),
        sa.Column("requested_by", sa.Text(), nullable=False),
        _ts(),
        _ck(
            "mandate_approval_requests",
            "subject_kind",
            "subject_kind IN ('activation_profile','terms')",
        ),
        _ck(
            "mandate_approval_requests",
            "base_version_positive",
            "base_sleeve_version >= 1",
        ),
        _ck(
            "mandate_approval_requests",
            "content_object",
            "jsonb_typeof(content) = 'object'",
        ),
        _sha_check("mandate_approval_requests", "content_sha256"),
        _ck(
            "mandate_approval_requests",
            "approval_ref_nonblank",
            "char_length(btrim(approval_ref)) BETWEEN 1 AND 300",
        ),
        _ck(
            "mandate_approval_requests",
            "requested_by_nonblank",
            "char_length(btrim(requested_by)) BETWEEN 1 AND 128",
        ),
        sa.ForeignKeyConstraint(
            ["sleeve_id"], ["review.strategy_sleeves.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mandate_approval_requests"),
        schema="review",
    )
    op.create_index(
        "ix_mandate_approval_requests_sleeve_id",
        "mandate_approval_requests",
        ["sleeve_id"],
        schema="review",
    )
    op.create_table(
        "mandate_approvals",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("request_id", sa.BigInteger(), nullable=False),
        sa.Column("decision", sa.Text(), nullable=False),
        sa.Column("subject_sha256", sa.Text(), nullable=False),
        sa.Column("principal", sa.Text(), nullable=False),
        sa.Column("nonce_digest", sa.Text(), nullable=False),
        _ts("decided_at"),
        _ck("mandate_approvals", "decision", "decision IN ('approved','denied')"),
        _sha_check("mandate_approvals", "subject_sha256"),
        _ck(
            "mandate_approvals",
            "principal_format",
            "principal ~ '^[1-9][0-9]{0,15}$' AND "
            "(char_length(principal) < 16 OR principal <= '4503599627370495')",
        ),
        _sha_check("mandate_approvals", "nonce_digest"),
        sa.ForeignKeyConstraint(
            ["request_id"], ["review.mandate_approval_requests.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mandate_approvals"),
        sa.UniqueConstraint("request_id", name="uq_mandate_approvals_request_id"),
        schema="review",
    )
    op.create_table(
        "mandate_terms",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("sleeve_version_id", sa.BigInteger(), nullable=False),
        sa.Column("terms_version", sa.Text(), nullable=False),
        sa.Column("basket_key", sa.Text(), nullable=False),
        sa.Column("account_ref", sa.Text(), nullable=False),
        sa.Column("universe_rule", sa.Text(), nullable=False),
        sa.Column("allowed_symbols", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("entry_max_premium_bps", sa.Numeric(10, 4), nullable=False),
        sa.Column("per_order_cap", sa.Numeric(20, 4), nullable=False),
        sa.Column("daily_cap", sa.Numeric(20, 4), nullable=False),
        sa.Column("sector_cap_pct", sa.Numeric(10, 4), nullable=False),
        sa.Column("basket_drawdown_freeze_pct", sa.Numeric(10, 4), nullable=False),
        sa.Column("account_drawdown_freeze_pct", sa.Numeric(10, 4), nullable=False),
        sa.Column("tranche_rule", postgresql.JSONB(), nullable=False),
        sa.Column("loss_sell_mode", sa.Text(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("approval_id", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        _ts(),
        _ck("mandate_terms", "terms_version", "terms_version = 'mandate-terms/v1'"),
        _ck(
            "mandate_terms",
            "basket_key_format",
            "basket_key ~ '^[a-z0-9][a-z0-9._-]{0,63}$'",
        ),
        _ck(
            "mandate_terms",
            "account_ref_nonblank",
            "char_length(btrim(account_ref)) BETWEEN 1 AND 200",
        ),
        _ck(
            "mandate_terms",
            "universe_rule_nonblank",
            "char_length(btrim(universe_rule)) BETWEEN 1 AND 500",
        ),
        _ck("mandate_terms", "premium_positive", "entry_max_premium_bps > 0"),
        _ck("mandate_terms", "per_order_cap_positive", "per_order_cap > 0"),
        _ck("mandate_terms", "daily_cap_positive", "daily_cap > 0"),
        _ck(
            "mandate_terms",
            "sector_cap_range",
            "sector_cap_pct > 0 AND sector_cap_pct <= 100",
        ),
        _ck(
            "mandate_terms",
            "basket_drawdown_range",
            "basket_drawdown_freeze_pct > 0 AND basket_drawdown_freeze_pct <= 100",
        ),
        _ck(
            "mandate_terms",
            "account_drawdown_range",
            "account_drawdown_freeze_pct > 0 AND account_drawdown_freeze_pct <= 100",
        ),
        _ck(
            "mandate_terms",
            "tranche_rule_closed",
            "jsonb_typeof(tranche_rule) = 'object' AND "
            "(tranche_rule - ARRAY['weights','triggers_pct_from_first','min_gap_sessions']) "
            "= '{}'::jsonb AND "
            "tranche_rule ?& ARRAY['weights','triggers_pct_from_first','min_gap_sessions']",
        ),
        _ck("mandate_terms", "loss_sell_mode", "loss_sell_mode IN ('human_card')"),
        _sha_check("mandate_terms", "content_sha256"),
        sa.ForeignKeyConstraint(
            ["sleeve_version_id"],
            ["review.strategy_sleeve_versions.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"], ["review.mandate_approvals.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mandate_terms"),
        sa.UniqueConstraint(
            "sleeve_version_id", name="uq_mandate_terms_sleeve_version_id"
        ),
        sa.UniqueConstraint("approval_id", name="uq_mandate_terms_approval_id"),
        schema="review",
    )
    op.create_index(
        "ix_mandate_terms_basket_key", "mandate_terms", ["basket_key"], schema="review"
    )
    op.create_table(
        "mandate_activation_profiles",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("sleeve_id", sa.BigInteger(), nullable=False),
        sa.Column("profile_version", sa.Integer(), nullable=False),
        sa.Column("terms_id", sa.BigInteger(), nullable=False),
        sa.Column(
            "max_tranche_index", sa.SmallInteger(), server_default="1", nullable=False
        ),
        sa.Column("effective_session", sa.Date(), nullable=False),
        sa.Column("expires_at", postgresql.TIMESTAMP(timezone=True), nullable=False),
        sa.Column("approval_id", sa.BigInteger(), nullable=False),
        sa.Column("content_sha256", sa.Text(), nullable=False),
        _ts(),
        _ck("mandate_activation_profiles", "version_positive", "profile_version >= 1"),
        _ck(
            "mandate_activation_profiles",
            "max_tranche_range",
            "max_tranche_index BETWEEN 0 AND 3",
        ),
        _sha_check("mandate_activation_profiles", "content_sha256"),
        sa.ForeignKeyConstraint(
            ["sleeve_id"], ["review.strategy_sleeves.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["terms_id"], ["review.mandate_terms.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["approval_id"], ["review.mandate_approvals.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mandate_activation_profiles"),
        sa.UniqueConstraint(
            "sleeve_id",
            "profile_version",
            name="uq_mandate_activation_profiles_version",
        ),
        sa.UniqueConstraint(
            "approval_id", name="uq_mandate_activation_profiles_approval_id"
        ),
        schema="review",
    )
    op.create_table(
        "mandate_plans",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("sleeve_id", sa.BigInteger(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("approved_terms_id", sa.BigInteger(), nullable=False),
        sa.Column("account_ref", sa.Text(), nullable=False),
        sa.Column("scope_account_refs", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("planned_amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("adopted_amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("adopted_evidence", postgresql.JSONB(), nullable=False),
        sa.Column("thesis", sa.Text(), nullable=False),
        sa.Column(
            "invalidation_conditions", postgresql.ARRAY(sa.Text()), nullable=False
        ),
        sa.Column("sector_key", sa.Text(), nullable=False),
        sa.Column("created_by", sa.Text(), nullable=False),
        _ts(),
        _ck(
            "mandate_plans",
            "symbol_nonblank",
            "char_length(btrim(symbol)) BETWEEN 1 AND 40",
        ),
        _ck(
            "mandate_plans",
            "account_ref_nonblank",
            "char_length(btrim(account_ref)) BETWEEN 1 AND 200",
        ),
        _ck(
            "mandate_plans",
            "scope_refs_size",
            "cardinality(scope_account_refs) BETWEEN 1 AND 2",
        ),
        _ck(
            "mandate_plans",
            "account_in_scope",
            "account_ref = ANY (scope_account_refs)",
        ),
        _ck("mandate_plans", "planned_positive", "planned_amount > 0"),
        _ck(
            "mandate_plans",
            "adopted_within_planned",
            "adopted_amount >= 0 AND adopted_amount <= planned_amount",
        ),
        _ck(
            "mandate_plans",
            "adopted_evidence_object",
            "jsonb_typeof(adopted_evidence) = 'object'",
        ),
        _ck(
            "mandate_plans",
            "thesis_length",
            "char_length(btrim(thesis)) BETWEEN 1 AND 1000",
        ),
        _ck(
            "mandate_plans",
            "invalidation_size",
            "cardinality(invalidation_conditions) <= 3",
        ),
        _ck(
            "mandate_plans",
            "sector_key_nonblank",
            "char_length(btrim(sector_key)) BETWEEN 1 AND 100",
        ),
        _ck(
            "mandate_plans",
            "created_by_nonblank",
            "char_length(btrim(created_by)) BETWEEN 1 AND 128",
        ),
        sa.ForeignKeyConstraint(
            ["sleeve_id"], ["review.strategy_sleeves.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["approved_terms_id"], ["review.mandate_terms.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mandate_plans"),
        schema="review",
    )
    op.create_index(
        "ix_mandate_plans_sleeve_id", "mandate_plans", ["sleeve_id"], schema="review"
    )
    op.create_table(
        "mandate_plan_tranches",
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("tranche_index", sa.SmallInteger(), nullable=False),
        sa.Column("weight", sa.Numeric(7, 6), nullable=False),
        sa.Column("amount", sa.Numeric(20, 4), nullable=False),
        sa.Column("trigger_pct_from_first", sa.Numeric(10, 4), nullable=False),
        sa.Column("min_gap_sessions", sa.Integer(), nullable=False),
        _ck(
            "mandate_plan_tranches",
            "tranche_index_range",
            "tranche_index BETWEEN 1 AND 3",
        ),
        _ck("mandate_plan_tranches", "weight_range", "weight > 0 AND weight <= 1"),
        _ck("mandate_plan_tranches", "amount_nonnegative", "amount >= 0"),
        _ck(
            "mandate_plan_tranches",
            "trigger_not_positive",
            "trigger_pct_from_first <= 0",
        ),
        _ck("mandate_plan_tranches", "min_gap_nonnegative", "min_gap_sessions >= 0"),
        _ck(
            "mandate_plan_tranches",
            "first_tranche_unconditional",
            "tranche_index <> 1 OR (trigger_pct_from_first = 0 AND min_gap_sessions = 0)",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["review.mandate_plans.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint(
            "plan_id", "tranche_index", name="pk_mandate_plan_tranches"
        ),
        schema="review",
    )
    op.create_table(
        "mandate_plan_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        sa.Column("seq", sa.Integer(), nullable=False),
        sa.Column("event", sa.Text(), nullable=False),
        sa.Column("tranche_index", sa.SmallInteger(), nullable=True),
        sa.Column("first_fill_amount", sa.Numeric(28, 12), nullable=True),
        sa.Column("first_fill_quantity", sa.Numeric(28, 12), nullable=True),
        sa.Column("fill_amount", sa.Numeric(28, 12), nullable=True),
        sa.Column("fill_quantity", sa.Numeric(28, 12), nullable=True),
        sa.Column("terms_id", sa.BigInteger(), nullable=True),
        sa.Column("terms_sha256", sa.Text(), nullable=True),
        sa.Column("effective_session", sa.Date(), nullable=True),
        sa.Column("resume_target", sa.Text(), nullable=True),
        sa.Column("batch_member_id", sa.BigInteger(), nullable=True),
        sa.Column("proposal_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("notional", sa.Numeric(28, 12), nullable=True),
        sa.Column("kst_day", sa.Date(), nullable=True),
        sa.Column("approval_mode", sa.Text(), nullable=True),
        sa.Column("account_ref", sa.Text(), nullable=True),
        sa.Column("attempt_id", postgresql.UUID(as_uuid=True), nullable=True),
        sa.Column("activation_profile_id", sa.BigInteger(), nullable=True),
        sa.Column("fenced_seq", sa.Integer(), nullable=True),
        sa.Column("thesis_seq", sa.Integer(), nullable=True),
        sa.Column(
            "evidence",
            postgresql.JSONB(),
            server_default=sa.text("'{}'::jsonb"),
            nullable=False,
        ),
        sa.Column("actor", sa.Text(), nullable=False),
        _ts(),
        *(_ck("mandate_plan_events", name, sql) for name, sql in EVENT_CHECKS),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["review.mandate_plans.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["terms_id"], ["review.mandate_terms.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["activation_profile_id"],
            ["review.mandate_activation_profiles.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mandate_plan_events"),
        sa.UniqueConstraint("plan_id", "seq", name="uq_mandate_plan_events_plan_seq"),
        schema="review",
    )
    for name, column, where in (
        (
            "uq_mandate_plan_events_terminal_proposal",
            "proposal_id",
            "event IN ('tranche_filled','tranche_partially_filled','tranche_unfilled')",
        ),
        (
            "uq_mandate_plan_events_proposed",
            "proposal_id",
            "event = 'tranche_proposed'",
        ),
        (
            "uq_mandate_plan_events_fence_attempt",
            "attempt_id",
            "event = 'tranche_send_fenced'",
        ),
        (
            "uq_mandate_plan_events_fence_release",
            "fenced_seq",
            "event = 'tranche_send_released'",
        ),
    ):
        op.create_index(
            name,
            "mandate_plan_events",
            ["plan_id", column],
            unique=True,
            schema="review",
            postgresql_where=sa.text(where),
        )
    op.create_index(
        "ix_mandate_plan_events_event",
        "mandate_plan_events",
        ["plan_id", "event"],
        schema="review",
    )
    op.create_table(
        "mandate_symbol_reservations",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("realm", sa.Text(), nullable=False),
        sa.Column("market", sa.Text(), nullable=False),
        sa.Column("symbol", sa.Text(), nullable=False),
        sa.Column("plan_id", sa.BigInteger(), nullable=False),
        _ts(),
        _ck("mandate_symbol_reservations", "realm", "realm IN ('live','mock')"),
        _ck(
            "mandate_symbol_reservations",
            "market",
            "market IN ('equity_kr','equity_us')",
        ),
        _ck(
            "mandate_symbol_reservations",
            "symbol_nonblank",
            "char_length(btrim(symbol)) BETWEEN 1 AND 40",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"], ["review.mandate_plans.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mandate_symbol_reservations"),
        sa.UniqueConstraint("plan_id", name="uq_mandate_symbol_reservations_plan_id"),
        schema="review",
    )
    op.create_index(
        "ix_mandate_symbol_reservations_key",
        "mandate_symbol_reservations",
        ["realm", "market", "symbol"],
        schema="review",
    )
    op.create_table(
        "mandate_symbol_reservation_releases",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("reservation_id", sa.BigInteger(), nullable=False),
        _ts(),
        sa.ForeignKeyConstraint(
            ["reservation_id"],
            ["review.mandate_symbol_reservations.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_mandate_symbol_reservation_releases"),
        sa.UniqueConstraint(
            "reservation_id",
            name="uq_mandate_symbol_reservation_releases_reservation_id",
        ),
        schema="review",
    )


def upgrade() -> None:
    _create_tables()
    op.add_column(
        "order_proposals",
        sa.Column("mandate_plan_id", sa.BigInteger(), nullable=True),
        schema="review",
    )
    op.add_column(
        "order_proposals",
        sa.Column("mandate_tranche_index", sa.SmallInteger(), nullable=True),
        schema="review",
    )
    op.create_foreign_key(
        "fk_order_proposals_mandate_plan_id_mandate_plans",
        "order_proposals",
        "mandate_plans",
        ["mandate_plan_id"],
        ["id"],
        source_schema="review",
        referent_schema="review",
        ondelete="RESTRICT",
    )
    op.create_index(
        "ix_order_proposals_mandate_plan_id",
        "order_proposals",
        ["mandate_plan_id"],
        schema="review",
    )
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
                for t in SEQUENCE_TABLES
            ),
            "GRANT EXECUTE ON FUNCTION review.mandate_realm(text) TO at_app; ",
            "GRANT EXECUTE ON FUNCTION review.mandate_plan_auth_state(bigint) TO at_app; ",
            "GRANT SELECT (mandate_plan_id, mandate_tranche_index), "
            "INSERT (mandate_plan_id, mandate_tranche_index) "
            "ON review.order_proposals TO at_app; ",
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
        "DROP TRIGGER IF EXISTS trg_order_proposals_mandate_update "
        "ON review.order_proposals"
    )
    op.execute(
        "DROP TRIGGER IF EXISTS trg_order_proposals_mandate_insert "
        "ON review.order_proposals"
    )
    op.drop_index(
        "ix_order_proposals_mandate_plan_id",
        table_name="order_proposals",
        schema="review",
    )
    op.drop_constraint(
        "fk_order_proposals_mandate_plan_id_mandate_plans",
        "order_proposals",
        type_="foreignkey",
        schema="review",
    )
    op.drop_column("order_proposals", "mandate_tranche_index", schema="review")
    op.drop_column("order_proposals", "mandate_plan_id", schema="review")
    for table in TABLES_NEWEST_FIRST:
        op.drop_table(table, schema="review")
    for function in reversed(_FUNCTIONS):
        op.execute(f"DROP FUNCTION IF EXISTS {function}")
