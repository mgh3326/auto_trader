"""Closed vocabulary shared by daily-plan validation and persistence."""

import re

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
MARKETS = ("kr", "us", "crypto")
PLAN_MODES = ("plan_only", "executable")
ACCOUNT_MODES = (
    "kis_live",
    "toss_live",
    "upbit_live",
    # Mock/demo accounts named in decision/2026-11-27/at-autonomous-strategy-orders.
    "kis_mock",
    "kiwoom_mock",
    "kiwoom_mock_us",
    "alpaca_paper",
    "binance_spot_demo",
    "binance_futures_demo",
    "nh_mock",
    "kis_futures_mock",
)
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
    # A row downgraded for an unusable input carries the bundle's own reason.
    "input_invalid",
    "observed_after_cutoff",
    "direction_unclassified",
    "price_missing",
    "price_stale",
    "halted_suspect",
    "position_unreadable",
    "no_exit_plan",
    "open_sell_unknown",
    "protected_unknown",
)

VENUE_SESSIONS = {
    "KRX": frozenset({"regular", "aftermarket"}),
    "NXT": frozenset({"premarket", "regular", "aftermarket"}),
    "NASD": frozenset({"us_pre", "us_regular", "us_post"}),
    "NYSE": frozenset({"us_pre", "us_regular", "us_post"}),
    "AMEX": frozenset({"us_pre", "us_regular", "us_post"}),
    "UPBIT": frozenset({"crypto_24h"}),
}
VENUE_MARKETS = {
    "KRX": "kr",
    "NXT": "kr",
    "NASD": "us",
    "NYSE": "us",
    "AMEX": "us",
    "UPBIT": "crypto",
}

# hk 6790 plan-only ranking and budget comparison. Stored in version metadata.
ALLOCATION_METADATA_KEY = "allocation_comparison"
ALLOCATION_SCHEMA_VERSION = "daily-plan-allocation/v1"
# A is the design §4.2 control without investor flow; B adds only the rest.
RANKING_A_INPUTS = ("dist_to_support_pct", "rsi_14", "daily_turnover", "pbr")
RANKING_B_ADDED_INPUTS = ("foreign_net", "news", "disclosures")
LIST_INPUTS = ("news", "disclosures")
NEWS_DIRECTIONS = ("positive", "neutral", "negative")
INPUT_UNKNOWN_REASONS = (
    "input_missing",
    "input_invalid",
    "observed_time_unproven",
    "observed_after_cutoff",
    "input_stale",
    "direction_unclassified",
)
# Closed reasons of an {"unavailable": reason} bundle value (design §7.1, §7.2).
UNAVAILABLE_REASONS = INPUT_UNKNOWN_REASONS + (
    "price_missing",
    "price_stale",
    "halted_suspect",
    "position_unreadable",
    "no_exit_plan",
    "open_sell_unknown",
    "protected_unknown",
)
ENTRY_STATES = ("met", "not_met", "unknown")
BUDGET_POLICIES = ("W", "N")
# Reasons that keep a row out of both policies in both rankings.
INELIGIBLE_REASONS = (
    "expired",
    "valid_until_unknown",
    "disposition_dropped",
    "no_buy_intent",
    "not_executable",
    "account_unassigned",
    "account_facts_missing",
    "amount_unknown",
)
ALLOCATION_STATUSES = (
    "funded",
    "partially_funded",
    "reserved_waiting",
    "skipped_waiting",
    "not_funded",
)
ALLOCATION_REASONS = (
    "unranked",
    "entry_not_met",
    "entry_unknown",
    "max_adopt_reached",
    "cash_unknown",
    "budget_exhausted",
)
PARKING_UNUSABLE_REASONS = (
    "not_parking_instrument",
    "duplicate_parking_symbol",
    "input_invalid",
    "observed_time_unproven",
    "observed_after_cutoff",
    "input_stale",
    "input_missing",
)
PARKING_SALE_REASONS = ("cash_short",)
# Recorded text only; nothing in this service enforces or executes it.
PARKING_PRECONDITION = "buy_must_not_proceed_until_broker_orderable_cash_has_increased"

# KR short code: exactly six digits or upper-case ASCII letters (hk 6769, hk 6959).
KR_SHORT_CODE = re.compile(r"[0-9A-Z]{6}")
