"""Closed vocabulary shared by daily-plan validation and persistence."""

ACTIONS = ("new", "add", "trim", "risk_sell", "hold", "watch")
ACTION_BASES = ("rule", "scored")
ROW_KINDS = ("holding", "candidate")
VERSION_KINDS = ("premarket", "fill", "event", "session", "close")
LINK_STATES = (
    "pending",
    "confirmed",
    "link_no_decision",
    "link_ambiguous",
    "link_mismatch",
    "link_unresolved",
)
TRIGGER_KINDS = ("price_at_or_below", "price_at_or_above", "event")
ACCOUNT_MODES = ("kis_live", "toss_live")
ACCOUNT_SCOPES = ("routable", "read_only")
ACCOUNT_REF_KINDS = ("derived", "logical")
LOT_AVAILABILITIES = ("fifo_lots", "absent")
DISPOSITIONS = ("adopted", "watch", "dropped")
REASON_CODES = (
    "adopted_top_rank",
    "adopted_event",
    "watch_wait_support",
    "watch_wait_event",
    "dropped_rule_blocked",
    "dropped_low_grade",
    "dropped_liquidity",
    "dropped_thesis_unclear",
    "dropped_concentration",
    "dropped_already_held_no_add",
    "dropped_outranked_by_cap",
    "dropped_data_missing",
    "input_missing",
    "input_stale",
    "observed_time_unproven",
    "calendar_unknown",
    "account_not_routable",
    "position_unknown",
    "expired",
)

VENUE_SESSIONS = {
    "KRX": frozenset({"regular", "aftermarket"}),
    "NXT": frozenset({"premarket", "regular", "aftermarket"}),
}
