"""ROB-1059 H1 (preregistration §6/§9) — PIT eligible-universe (U_t) evaluation.

Named ``pit_universe_alpaca`` (not ``pit_universe``) to avoid any import
collision with the existing sibling ``research/nautilus_scalping/pit_universe.py``
— this is an independent, ROB-1059-specific eligibility contract, not an edit
of that module.

§6 rules applied in EXACT order (no reordering, no early-exit shortcut that
would change which reason is recorded):

    1. Alpaca Assets API: active AND tradable AND USD pair
    2. exclude USDC/USD, USDG/USD, USDT/USD, PAXG/USD
    3. Binance direct stable-quoted spot pair exists (quote_mode != NO_MAPPING)
    4. minimum 180-day PIT history
    5. every valid daily bar inside the lookback window
    6. no gap in the last 60 minutes
    7. N_t >= 18

Alpaca's historical listing date is not retrievable via the current API, so
``alpaca_first_daily`` (the first daily bar Alpaca ever returned) is used as a
PIT proxy for rule 4 — ``ListingProxy.source`` makes this explicit and is
never conflated with an actual listing date (§9/AC16).
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Literal, get_args

import quote_mode as qm

MIN_UNIVERSE_SIZE = 18
MIN_PIT_HISTORY_DAYS = 180
DAY_MS = 86_400_000
CONSECUTIVE_WEEKLY_OUTAGE_THRESHOLD = 2
CONSECUTIVE_DAILY_OUTAGE_THRESHOLD = 7

# The PIT-proxy provenance tag AC16 requires — never persisted as if it were
# an actual, API-retrievable listing date.
ALPACA_FIRST_DAILY_PROXY = "alpaca_first_daily_proxy"

EXCLUDED_STABLE_AND_PAXG_BASES: frozenset[str] = frozenset(
    {"USDC", "USDG", "USDT", "PAXG"}
)

# S2 remediation: reconcile the quote_mode vocabulary into ONE source of truth
# (``quote_mode.QuoteModeLiteral``) instead of a bare, unvalidated ``str`` deny-
# list. The old rule 3 checked ``candidate.binance_quote_mode in ("NO_MAPPING",
# "EXCLUDED")`` against a free-form string with no validation anywhere -- a
# typo or vocabulary-drift value (e.g. "no_mapping", "", "GARBAGE") silently
# fell THROUGH the deny-list and was admitted as eligible, exactly the failure
# rule 3 exists to prevent. ``SymbolCandidate.__post_init__`` below now rejects
# any ``binance_quote_mode`` outside this allow-list at construction time (fail
# closed before rule 3 ever runs), and rule 3 itself only has to distinguish
# the single "no usable Binance pair" value, "NO_MAPPING". Note "EXCLUDED" is
# NOT part of ``quote_mode.QuoteModeLiteral`` and never was a real value
# produced anywhere -- it is dropped rather than reconciled in.
VALID_BINANCE_QUOTE_MODES: frozenset[str] = frozenset(get_args(qm.QuoteModeLiteral))

# Known Binance/Alpaca base-symbol renames. Listed for documentation/testing
# ONLY -- this module never uses this mapping to merge/stitch two symbols'
# history into one series (§9/AC17): MATIC and POL (etc.) are always
# evaluated as fully independent SymbolCandidate entries.
KNOWN_MIGRATIONS: dict[str, str] = {
    "MATIC": "POL",
    "RNDR": "RENDER",
    "MKR": "SKY",
}

FailReason = Literal[
    "alpaca_not_active_tradable_usd",
    "stable_or_paxg_excluded",
    "no_binance_stable_pair",
    "insufficient_pit_history",
    "invalid_daily_bar_in_lookback",
    "gap_in_last_60min",
]

UniverseStateLiteral = Literal["normal", "restricted_exits_only", "universe_outage"]

__all__ = [
    "ALPACA_FIRST_DAILY_PROXY",
    "CONSECUTIVE_DAILY_OUTAGE_THRESHOLD",
    "CONSECUTIVE_WEEKLY_OUTAGE_THRESHOLD",
    "EXCLUDED_STABLE_AND_PAXG_BASES",
    "KNOWN_MIGRATIONS",
    "MIN_PIT_HISTORY_DAYS",
    "MIN_UNIVERSE_SIZE",
    "VALID_BINANCE_QUOTE_MODES",
    "SymbolCandidate",
    "SymbolEligibility",
    "UniverseSnapshot",
    "canonical_snapshot_sequence",
    "evaluate_universe",
    "is_universe_outage_daily",
    "is_universe_outage_weekly",
    "universe_state",
]


def _int(value: object, name: str) -> int:
    if type(value) is not int:
        raise TypeError(f"{name} must be built-in int")
    return value


@dataclass(frozen=True)
class SymbolCandidate:
    """Raw, independently-sourced per-symbol facts for one §6 evaluation.

    Every field is this ONE symbol's own data — no cross-symbol reference, so
    two candidates (e.g. MATIC and POL) can never influence each other's
    eligibility (§9/AC17: no migration stitching).
    """

    symbol: str
    base: str
    alpaca_active: bool
    alpaca_tradable: bool
    is_usd_pair: bool
    binance_quote_mode: (
        str  # from quote_mode_pipeline.resolve_and_validate_candidate_quote_mode(...)
    )
    alpaca_first_daily_ms: (
        int | None
    )  # PIT-proxy listing date (ALPACA_FIRST_DAILY_PROXY)
    all_valid_daily_bars_in_lookback: bool
    no_gap_in_last_60min: bool

    def __post_init__(self) -> None:
        # CodeRabbit fix: these five §6-rule inputs were accepted unvalidated
        # (any truthy value, e.g. the STRING "false" or an int `1`, silently
        # passed rules 1/5/6) -- the same fail-open shape S2 remediated for
        # `binance_quote_mode` below. `daily_bars.DailyBar` already enforces
        # `type(...) is bool` for its own boolean fields; match that here.
        for name in (
            "alpaca_active",
            "alpaca_tradable",
            "is_usd_pair",
            "all_valid_daily_bars_in_lookback",
            "no_gap_in_last_60min",
        ):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f"{self.symbol}: {name} must be built-in bool")
        if self.alpaca_first_daily_ms is not None:
            _int(self.alpaca_first_daily_ms, "alpaca_first_daily_ms")
        if self.binance_quote_mode not in VALID_BINANCE_QUOTE_MODES:
            raise ValueError(
                f"{self.symbol}: binance_quote_mode {self.binance_quote_mode!r} is "
                f"not a recognized quote_mode.QuoteModeLiteral value "
                f"({sorted(VALID_BINANCE_QUOTE_MODES)}) -- rule 3 must never fail "
                f"open on a typo/vocabulary-drift value"
            )


@dataclass(frozen=True)
class SymbolEligibility:
    symbol: str
    eligible: bool
    fail_reason: FailReason | None
    pit_history_days: int | None
    listing_proxy_source: str | None  # ALPACA_FIRST_DAILY_PROXY, or None if unknown

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "eligible": self.eligible,
            "fail_reason": self.fail_reason,
            "pit_history_days": self.pit_history_days,
            "listing_proxy_source": self.listing_proxy_source,
        }


def _evaluate_one(
    candidate: SymbolCandidate, decision_ts_ms: int, *, min_pit_history_days: int
) -> SymbolEligibility:
    listing_proxy_source = (
        ALPACA_FIRST_DAILY_PROXY
        if candidate.alpaca_first_daily_ms is not None
        else None
    )
    pit_history_days: int | None = None
    if candidate.alpaca_first_daily_ms is not None:
        pit_history_days = (decision_ts_ms - candidate.alpaca_first_daily_ms) // DAY_MS

    def result(eligible: bool, reason: FailReason | None) -> SymbolEligibility:
        return SymbolEligibility(
            symbol=candidate.symbol,
            eligible=eligible,
            fail_reason=reason,
            pit_history_days=pit_history_days,
            listing_proxy_source=listing_proxy_source,
        )

    # Rule 1
    if not (
        candidate.alpaca_active and candidate.alpaca_tradable and candidate.is_usd_pair
    ):
        return result(False, "alpaca_not_active_tradable_usd")
    # Rule 2
    if candidate.base in EXCLUDED_STABLE_AND_PAXG_BASES:
        return result(False, "stable_or_paxg_excluded")
    # Rule 3 -- candidate.binance_quote_mode is already guaranteed to be one of
    # VALID_BINANCE_QUOTE_MODES (validated fail-closed in __post_init__), so
    # this only needs to check the single "no usable Binance pair" value.
    if candidate.binance_quote_mode == "NO_MAPPING":
        return result(False, "no_binance_stable_pair")
    # Rule 4
    if pit_history_days is None or pit_history_days < min_pit_history_days:
        return result(False, "insufficient_pit_history")
    # Rule 5
    if not candidate.all_valid_daily_bars_in_lookback:
        return result(False, "invalid_daily_bar_in_lookback")
    # Rule 6
    if not candidate.no_gap_in_last_60min:
        return result(False, "gap_in_last_60min")
    # Rule 7 (N_t >= 18) is evaluated at the universe level, not per-symbol.
    return result(True, None)


@dataclass(frozen=True)
class UniverseSnapshot:
    decision_ts_ms: int
    eligible_symbols: tuple[str, ...]  # canonical lexicographic order
    per_symbol: tuple[SymbolEligibility, ...]  # canonical lexicographic order
    n_t: int
    meets_min_universe_size: bool  # rule 7

    def __post_init__(self) -> None:
        _int(self.decision_ts_ms, "decision_ts_ms")
        _int(self.n_t, "n_t")
        if self.eligible_symbols != tuple(sorted(self.eligible_symbols)):
            raise ValueError("eligible_symbols must be canonical lexicographic order")
        if tuple(e.symbol for e in self.per_symbol) != tuple(
            sorted(e.symbol for e in self.per_symbol)
        ):
            raise ValueError("per_symbol must be canonical lexicographic order")
        if self.n_t != len(self.eligible_symbols):
            raise ValueError("n_t must equal len(eligible_symbols)")


def evaluate_universe(
    decision_ts_ms: int,
    candidates: Sequence[SymbolCandidate],
    *,
    min_universe_size: int = MIN_UNIVERSE_SIZE,
    min_pit_history_days: int = MIN_PIT_HISTORY_DAYS,
) -> UniverseSnapshot:
    """Apply §6 rules 1-7, in order, to every candidate. Deterministic
    canonical order: symbols sorted lexicographically (AC19)."""
    _int(decision_ts_ms, "decision_ts_ms")
    symbols = [c.symbol for c in candidates]
    if len(symbols) != len(set(symbols)):
        raise ValueError("duplicate symbol in candidates")
    ordered = sorted(candidates, key=lambda c: c.symbol)
    per_symbol = tuple(
        _evaluate_one(c, decision_ts_ms, min_pit_history_days=min_pit_history_days)
        for c in ordered
    )
    eligible_symbols = tuple(e.symbol for e in per_symbol if e.eligible)
    n_t = len(eligible_symbols)
    return UniverseSnapshot(
        decision_ts_ms=decision_ts_ms,
        eligible_symbols=eligible_symbols,
        per_symbol=per_symbol,
        n_t=n_t,
        meets_min_universe_size=n_t >= min_universe_size,
    )


def universe_state(
    n_t: int,
    *,
    recent_daily_n_lt_min: Sequence[bool] = (),
    recent_weekly_n_lt_min: Sequence[bool] = (),
    min_universe_size: int = MIN_UNIVERSE_SIZE,
) -> UniverseStateLiteral:
    """ "normal" / "restricted_exits_only" (N_t < min, new entries stopped, exits
    only) / "universe_outage" (2 consecutive weekly evals, or 7 consecutive
    days, of N_t < min)."""
    if is_universe_outage_weekly(recent_weekly_n_lt_min) or is_universe_outage_daily(
        recent_daily_n_lt_min
    ):
        return "universe_outage"
    if n_t < min_universe_size:
        return "restricted_exits_only"
    return "normal"


def is_universe_outage_weekly(recent_weekly_n_lt_min: Sequence[bool]) -> bool:
    return len(recent_weekly_n_lt_min) >= CONSECUTIVE_WEEKLY_OUTAGE_THRESHOLD and all(
        recent_weekly_n_lt_min[-CONSECUTIVE_WEEKLY_OUTAGE_THRESHOLD:]
    )


def is_universe_outage_daily(recent_daily_n_lt_min: Sequence[bool]) -> bool:
    return len(recent_daily_n_lt_min) >= CONSECUTIVE_DAILY_OUTAGE_THRESHOLD and all(
        recent_daily_n_lt_min[-CONSECUTIVE_DAILY_OUTAGE_THRESHOLD:]
    )


def canonical_snapshot_sequence(
    snapshots: Sequence[UniverseSnapshot],
) -> tuple[UniverseSnapshot, ...]:
    """AC19: the canonical output order across MULTIPLE decision times is
    ``(decision_ts, symbol)`` — each ``UniverseSnapshot.per_symbol`` is already
    lexicographic-by-symbol (enforced in ``__post_init__``), so this only needs
    to enforce the outer, strictly-increasing ``decision_ts_ms`` dimension.
    Fail-closed (never silently re-sorts) on an out-of-order or duplicate
    decision timestamp — a caller producing snapshots out of order has a bug
    upstream that must surface, not be masked.
    """
    for earlier, later in zip(snapshots, snapshots[1:], strict=False):
        if later.decision_ts_ms <= earlier.decision_ts_ms:
            raise ValueError(
                f"snapshots must be strictly increasing by decision_ts_ms: "
                f"{later.decision_ts_ms} does not follow {earlier.decision_ts_ms}"
            )
    return tuple(snapshots)
