"""Kick-priority classification for the fill-event handoff (task #825).

Pure functions only: a sanitized fill plus its ledger-derived position facts
map to a kick-eligibility verdict.  No database, clock, broker, or network
access lives here — callers supply every fact.

Kick classes (strategy-lab 2026-09-27 fill-to-session-handoff §2 item 3):
- ``sell_full_exit``          a sell whose post-fill position is <= 0
- ``buy_new_position``        a buy whose ledger-proven pre-fill position is 0
- ``partial_fill_ge_25pct``   a fill covering >= the configured fraction

Queue-only classes:
- ``parking_etf``             configured parking symbols (SGOV/BIL/…)
- ``small_dca_buy``           buy notional below the configured per-currency floor
- ``buy_add_below_25pct``     a covered buy add below the fraction
- ``sell_partial_below_25pct`` a partial take-profit/exit below the fraction
- ``position_unproven``       the ledger cannot prove the pre-fill position —
                              including a proven-negative balance, which is an
                              inconsistent ledger view rather than flat
- ``position_read_failed``    the position read itself failed
- ``fill_malformed``          missing/invalid side, quantity, or notional, or
                              degenerate magnitude that overflows arithmetic
- ``unsupported_market``      the fill's market/instrument is not a
                              session_context market (kr/us/crypto) — e.g.
                              forex — so no open_question can represent it
- ``classification_failed``   the classifier itself raised (service-level
                              catch-all — classification must never skip
                              the durable append)

A fill whose ledger key has never been seen (``rows_before == 0``) is
*unproven*, not flat — this module never treats absent data as a position of
zero.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

DEFAULT_PARKING_SYMBOLS = frozenset({"SGOV", "BIL", "459580", "357870"})
DEFAULT_SMALL_BUY_NOTIONAL = {"KRW": Decimal("5000"), "USD": Decimal("5")}
DEFAULT_MIN_POSITION_FRACTION = Decimal("0.25")
DEFAULT_KICK_DAILY_CAP = 2

KICK_CLASSES = frozenset(
    {"sell_full_exit", "buy_new_position", "partial_fill_ge_25pct"}
)
QUEUE_ONLY_CLASSES = frozenset(
    {
        "parking_etf",
        "small_dca_buy",
        "buy_add_below_25pct",
        "sell_partial_below_25pct",
        "position_unproven",
        "position_read_failed",
        "fill_malformed",
        "unsupported_market",
        "classification_failed",
    }
)

# Markets that have a session_context briefing surface — mirrors the schema
# ``MarketLiteral`` {kr, us, crypto}.  Anything else (forex, index, …) can
# never be queued as an open_question, let alone kick.
SUPPORTED_MARKETS = frozenset({"kr", "us", "crypto"})
# Instrument types that map onto supported markets via derive_market.
SUPPORTED_INSTRUMENT_TYPES = frozenset({"equity_kr", "equity_us", "crypto"})


@dataclass(frozen=True)
class FillPositionFacts:
    """Ledger-derived position facts around one fill.

    ``qty_before`` is the net signed quantity of every ledger row sharing the
    fill's exact match key ordered strictly before it by ``(filled_at, id)``,
    across all sources including opening-lot seeds.  ``rows_before`` counts
    those rows: zero means the ledger has never seen the key, so a flat
    reading is unproven rather than observed.
    """

    qty_before: Decimal
    rows_before: int


@dataclass(frozen=True)
class KickVerdict:
    """The filter verdict for one fill — its intrinsic kick eligibility."""

    eligible: bool
    reason: str
    position_before: Decimal | None = None
    position_after: Decimal | None = None


@dataclass(frozen=True)
class KickDecision:
    """The final per-fill disposition recorded in the run decisions log."""

    klass: Literal["kick", "queue_only", "capped"]
    reason: str
    flow_run_id: str | None = None


def _to_decimal(value: Any) -> Decimal | None:
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError):
        return None
    return parsed if parsed.is_finite() else None


def classify_without_position(
    fill: Mapping[str, Any],
    *,
    parking_symbols: frozenset[str] = DEFAULT_PARKING_SYMBOLS,
    small_buy_notional: Mapping[str, Decimal] = DEFAULT_SMALL_BUY_NOTIONAL,
) -> KickVerdict | None:
    """Return a verdict for classes that never need position facts, else None."""
    if (
        str(fill.get("market") or "").strip().lower() not in SUPPORTED_MARKETS
        or str(fill.get("instrument_type") or "").strip().lower()
        not in SUPPORTED_INSTRUMENT_TYPES
    ):
        return KickVerdict(False, "unsupported_market")
    symbol = str(fill.get("symbol") or "").strip().upper()
    if symbol in {str(item).upper() for item in parking_symbols}:
        return KickVerdict(False, "parking_etf")
    side = str(fill.get("side") or "").strip().lower()
    qty = _to_decimal(fill.get("filled_qty"))
    if side not in ("buy", "sell") or qty is None or qty <= 0:
        return KickVerdict(False, "fill_malformed")
    if side == "buy":
        notional = _to_decimal(fill.get("filled_notional"))
        if notional is None:
            # A buy whose notional cannot be checked against the DCA floor
            # must not reach position-based classification — fail closed.
            return KickVerdict(False, "fill_malformed")
        threshold = small_buy_notional.get(str(fill.get("currency") or "").upper())
        if threshold is not None and notional < threshold:
            return KickVerdict(False, "small_dca_buy")
    return None


def classify_fill_for_kick(
    fill: Mapping[str, Any],
    facts: FillPositionFacts | None,
    *,
    parking_symbols: frozenset[str] = DEFAULT_PARKING_SYMBOLS,
    small_buy_notional: Mapping[str, Decimal] = DEFAULT_SMALL_BUY_NOTIONAL,
    min_position_fraction: Decimal = DEFAULT_MIN_POSITION_FRACTION,
) -> KickVerdict:
    """Classify one fill for kick priority; missing facts fail closed."""
    early = classify_without_position(
        fill,
        parking_symbols=parking_symbols,
        small_buy_notional=small_buy_notional,
    )
    if early is not None:
        return early
    if facts is None:
        return KickVerdict(False, "position_read_failed")
    if facts.rows_before <= 0:
        return KickVerdict(False, "position_unproven")
    qty = _to_decimal(fill.get("filled_qty"))
    assert qty is not None  # classify_without_position already validated it
    side = str(fill.get("side") or "").strip().lower()
    try:
        return _classify_with_position(side, qty, facts, min_position_fraction)
    except ArithmeticError:
        # Degenerate magnitudes (e.g. ``1e9999999``) overflow Decimal context
        # arithmetic — fail closed so a malformed fill still reaches the
        # durable append instead of escaping ``run()``.
        return KickVerdict(False, "fill_malformed")


def _classify_with_position(
    side: str,
    qty: Decimal,
    facts: FillPositionFacts,
    min_position_fraction: Decimal,
) -> KickVerdict:
    """Position-dependent classification; callers catch ArithmeticError."""
    qty_before = facts.qty_before
    if side == "buy":
        position_after = qty_before + qty
        if qty_before == 0:
            return KickVerdict(
                True,
                "buy_new_position",
                position_before=qty_before,
                position_after=position_after,
            )
        if qty_before < 0:
            return KickVerdict(
                False,
                "position_unproven",
                position_before=qty_before,
                position_after=position_after,
            )
        if qty >= qty_before * min_position_fraction:
            return KickVerdict(
                True,
                "partial_fill_ge_25pct",
                position_before=qty_before,
                position_after=position_after,
            )
        return KickVerdict(
            False,
            "buy_add_below_25pct",
            position_before=qty_before,
            position_after=position_after,
        )
    position_after = qty_before - qty
    if qty_before <= 0:
        return KickVerdict(
            False,
            "position_unproven",
            position_before=qty_before,
            position_after=position_after,
        )
    if position_after <= 0:
        return KickVerdict(
            True,
            "sell_full_exit",
            position_before=qty_before,
            position_after=position_after,
        )
    if qty >= qty_before * min_position_fraction:
        return KickVerdict(
            True,
            "partial_fill_ge_25pct",
            position_before=qty_before,
            position_after=position_after,
        )
    return KickVerdict(
        False,
        "sell_partial_below_25pct",
        position_before=qty_before,
        position_after=position_after,
    )


__all__ = [
    "DEFAULT_KICK_DAILY_CAP",
    "DEFAULT_MIN_POSITION_FRACTION",
    "DEFAULT_PARKING_SYMBOLS",
    "DEFAULT_SMALL_BUY_NOTIONAL",
    "FillPositionFacts",
    "KICK_CLASSES",
    "KickDecision",
    "KickVerdict",
    "QUEUE_ONLY_CLASSES",
    "SUPPORTED_INSTRUMENT_TYPES",
    "SUPPORTED_MARKETS",
    "classify_fill_for_kick",
    "classify_without_position",
]
