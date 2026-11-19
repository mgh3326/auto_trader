"""Closed vocabularies of the mandate model (hk 3795, ticket T1).

Pure: standard library only. The plan event vocabulary, the authority transition
table and the per-event field rules live here once. ``alembic/versions/
20261120_hk5528_mandate_model.py`` carries its own literal copy (a migration must not
change when this module does) and ``tests/services/mandate/test_migration_shape.py``
pins the two together, so the Python fold and the DB event trigger cannot drift.

Design source: ``docs/design/3795-mandate.md`` §3.2 (events), §3.3 (invariants),
§3.4 (DB guards), §3.7 (cleanup), §4.6 (locks).
"""

from __future__ import annotations

from enum import StrEnum

TERMS_VERSION = "mandate-terms/v1"
LOSS_SELL_MODES: tuple[str, ...] = ("human_card",)
MAX_TRANCHES = 3

# --- realms and baskets -----------------------------------------------------
# A basket's identity is (realm, basket_key). The realm comes from the account
# mode of the sleeve: real accounts are ``live``, the Kiwoom mock is ``mock``.
# Anything else (Upbit, KIS mock, simulated accounts) has no mandate realm.
REALMS: tuple[str, ...] = ("live", "mock")
REALM_BY_ACCOUNT_MODE: dict[str, str] = {
    "kis_live": "live",
    "toss_live": "live",
    "kiwoom_mock": "mock",
}
PLAN_MARKETS: tuple[str, ...] = ("equity_kr", "equity_us")


def realm_of(account_mode: str) -> str:
    """The realm of an account mode; unsupported modes are refused, not guessed."""
    try:
        return REALM_BY_ACCOUNT_MODE[account_mode]
    except KeyError as exc:
        raise ValueError(f"account mode {account_mode!r} has no mandate realm") from exc


# --- approval records -------------------------------------------------------
APPROVAL_SUBJECT_KINDS: tuple[str, ...] = ("terms", "activation_profile")
APPROVAL_DECISIONS: tuple[str, ...] = ("approved", "denied")

# --- plan events --------------------------------------------------------------
PLAN_EVENTS: tuple[str, ...] = (
    "proposed",
    "approved",
    "rejected",
    "first_tranche_settled",
    "suspended",
    "resumed",
    "terms_rebound",
    "exhausted",
    "thesis_broken",
    "thesis_evidence_refreshed",
    "closed",
    "retired",
    "orders_settled",
    "holdings_disposed",
    "released",
)
TRANCHE_EVENTS: tuple[str, ...] = (
    "tranche_armed",
    "tranche_proposed",
    "tranche_send_fenced",
    "tranche_send_released",
    "tranche_filled",
    "tranche_partially_filled",
    "tranche_unfilled",
    "tranche_closed_partial",
)
ALL_EVENTS: tuple[str, ...] = PLAN_EVENTS + TRANCHE_EVENTS

AUTH_STATES: tuple[str, ...] = (
    "proposed",
    "approved",
    "active",
    "suspended",
    "exhausted",
    "thesis_broken",
    "closed",
    "rejected",
    "retired",
)

#: event -> authority states it may be appended from. ``proposed`` opens a stream.
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
#: event -> authority state after it (``resumed`` takes its ``resume_target``;
#: events not listed leave the authority state unchanged).
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
RESUME_TARGETS: tuple[str, ...] = ("approved", "active")

#: A fence (and a new tranche proposal) needs the plan to hold this authority.
SEND_AUTHORITY: tuple[str, ...] = ("approved", "active")
#: Cleanup events (§3.7) apply once the plan no longer holds sending authority.
CLEANUP_AUTH: tuple[str, ...] = (
    "rejected",
    "retired",
    "closed",
    "exhausted",
    "thesis_broken",
)
CLEANUP_EVENTS: tuple[str, ...] = ("orders_settled", "holdings_disposed", "released")
#: Events that close a proposal of a tranche (evidence from the ledger).
TRANCHE_TERMINAL_EVENTS: tuple[str, ...] = (
    "tranche_filled",
    "tranche_partially_filled",
    "tranche_unfilled",
)
#: Events that complete a tranche (§3.2 "회차 완료").
TRANCHE_COMPLETION_EVENTS: tuple[str, ...] = (
    "tranche_filled",
    "tranche_closed_partial",
)
#: Operator-side tranche events may run while the plan is paused.
CLOSED_PARTIAL_AUTH: tuple[str, ...] = ("approved", "active", "suspended")

#: Morning batch cards (§5.1): what a card asks and which plan events answer it.
BATCH_KINDS: tuple[str, ...] = ("new_plan", "reapproval")
BATCH_ANSWER_EVENTS: dict[str, tuple[str, str]] = {
    "new_plan": ("approved", "rejected"),
    "reapproval": ("terms_rebound", "retired"),
}

APPROVAL_MODES: tuple[str, ...] = ("auto", "human")
HOLDINGS_DISPOSITIONS: tuple[str, ...] = (
    "zero_holdings",
    "kept_outside_basket",
    "never_adopted",
)
#: Closed ``evidence`` keys of a plan event (§3.2). ``holdings_account_refs`` is the
#: set of account references the holdings check read: I19 compares it with the
#: ``scope_account_refs`` fixed at registration.
EVIDENCE_KEYS: tuple[str, ...] = (
    "ledger_row_ids",
    "trade_date",
    "reason_code",
    "retrospective_id",
    "approval_issue_id",
    "open_order_count",
    "unknown_order_count",
    "holdings_quantity",
    "holdings_checked_at",
    "holdings_account_refs",
    "holdings_disposition",
)

#: Column group -> (required events, allowed events). Outside ``allowed`` the
#: columns must be NULL; inside ``required`` they must be set.
_S = frozenset
FIELD_RULES: dict[str, tuple[frozenset[str], frozenset[str]]] = {
    "tranche_index": (_S(TRANCHE_EVENTS), _S(TRANCHE_EVENTS)),
    "terms": (
        _S({"approved", "terms_rebound"}),
        _S({"approved", "terms_rebound", "rejected", "retired"}),
    ),
    "effective_session": (
        _S({"approved", "terms_rebound", "resumed"}),
        _S({"approved", "terms_rebound", "resumed"}),
    ),
    "resume_target": (_S({"resumed"}), _S({"resumed"})),
    "batch_member_id": (
        _S(),
        _S({"approved", "rejected", "retired", "terms_rebound"}),
    ),
    "proposal_id": (
        _S(
            {
                "tranche_proposed",
                "tranche_send_fenced",
                "tranche_filled",
                "tranche_partially_filled",
                "tranche_unfilled",
            }
        ),
        _S(
            {
                "tranche_proposed",
                "tranche_send_fenced",
                "tranche_filled",
                "tranche_partially_filled",
                "tranche_unfilled",
            }
        ),
    ),
    "fence": (_S({"tranche_send_fenced"}), _S({"tranche_send_fenced"})),
    "fenced_seq": (_S({"tranche_send_released"}), _S({"tranche_send_released"})),
    "thesis_seq": (
        _S({"thesis_evidence_refreshed"}),
        _S({"thesis_evidence_refreshed"}),
    ),
    "fill": (
        _S({"tranche_filled", "tranche_partially_filled"}),
        _S({"tranche_filled", "tranche_partially_filled"}),
    ),
}
#: Columns that make up each group of ``FIELD_RULES``.
FIELD_COLUMNS: dict[str, tuple[str, ...]] = {
    "tranche_index": ("tranche_index",),
    "terms": ("terms_id", "terms_sha256"),
    "effective_session": ("effective_session",),
    "resume_target": ("resume_target",),
    "batch_member_id": ("batch_member_id",),
    "proposal_id": ("proposal_id",),
    "fence": ("notional", "kst_day", "approval_mode", "account_ref", "attempt_id"),
    "fenced_seq": ("fenced_seq",),
    "thesis_seq": ("thesis_seq",),
    "fill": ("fill_amount", "fill_quantity"),
}


class MandateRefusal(StrEnum):
    """Closed refusal codes of the T1 writer (§4.7 vocabulary, registration side)."""

    BUDGET_EXCEEDED = "mandate_budget_exceeded"
    SECTOR_CAP = "mandate_sector_cap"
    OPEN_LOSS_CUT = "mandate_open_loss_cut"
    TRANCHE_BELOW_ONE_SHARE = "mandate_tranche_below_one_share"
    SYMBOL_NOT_ALLOWED = "mandate_symbol_not_allowed"
    PARKING_SYMBOL = "mandate_parking_symbol"
    SYMBOL_RESERVED = "mandate_symbol_reserved"
    BINDING_MISMATCH = "mandate_binding_mismatch"
    ACCOUNT_MISMATCH = "mandate_account_mismatch"
    TERMS_NOT_CURRENT = "mandate_terms_not_current"
    TERMS_EXPIRED = "mandate_expired"
    BINDING_BROKEN = "mandate_binding_broken"
    REQUEST_STALE = "mandate_request_stale"
    APPROVER_NOT_ALLOWED = "mandate_approver_not_allowed"
    INVALID_TRANSITION = "mandate_invalid_transition"
    INVALID_FIELDS = "mandate_invalid_fields"
    PLAN_NOT_ACTIVE = "mandate_plan_not_active"
    TRANCHE_DUPLICATE = "mandate_tranche_duplicate"
    TRANCHE_UNCERTAIN = "mandate_tranche_uncertain"
    SCOPE_UNKNOWN = "mandate_scope_unknown"
    UNSUPPORTED = "mandate_unsupported"
    BATCH_OPEN = "mandate_batch_open"
    BATCH_EXPIRED = "mandate_batch_expired"
    BATCH_ANSWERED = "mandate_batch_answered"
    CARD_SUPERSEDED = "mandate_card_superseded"
    NONCE_MISMATCH = "mandate_nonce_mismatch"
    CALENDAR_UNKNOWN = "mandate_calendar_unknown"
