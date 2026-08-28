"""Cash-parking ticker allowlist for auto-approval (§163차).

The operator authorized exactly two ultra-short US Treasury ETFs -- ``SGOV``
and ``BIL`` -- as *cash parking* instruments. For those two tickers only, and
only on the one account/market pair whose parking exposure this repo can read
back from the broker, two §40차/§156차 gates are lifted:

1. **marketability** -- a parking buy may price at the market instead of
   resting below it (§156차 ② kept "매수 marketable 은 수동 유지"; §163차 makes
   these two tickers its only exception), and
2. **the per-order cap** -- §106차 defined ``per_order_cap`` as *the maximum
   loss boundary of one automation error*. §163차 does not delete that
   boundary; it **replaces** it for parking with a cumulative one:
   ``PARKING_CUMULATIVE_CAP_USD``, measured against broker-reported parking
   exposure, on the buy side.

Nothing else is lifted. The daily cap, the loss-cut and exit-intent gates, the
``policy_deviation`` tag scan, the veto-capable account/market allowlist, the
Toss auto-submission freeze, the sell-side break-even band and round-trip-cost
profit proof, and the mandatory veto thesis all apply to a parking rung exactly
as they apply to every other rung. ``off`` mode is untouched entirely.

This module is deliberately pure -- stdlib plus ``app.core.symbol`` -- and
reads no settings, environment variable, database, policy document or broker.
Every constant below is a module-level ``frozenset``/``Decimal`` literal: there
is no setter, no loader, and no configuration key, so a runtime session cannot
widen the allowlist or raise the cap. Changing either is an operator PR that
edits this file.

Two deliberately asymmetric symbol rules live here, and the asymmetry is the
safety property -- see ``canonical_eligibility_symbol`` (strict; over-strictness
falls back to the ordinary gates) versus ``canonical_exposure_symbol``
(lenient; over-inclusion raises measured exposure and can only *reject* a buy).
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Any

from app.core.symbol import to_db_symbol

# The closed allowlist. Two tickers, hardcoded, immutable at runtime.
PARKING_ALLOWLIST_SYMBOLS: frozenset[str] = frozenset({"BIL", "SGOV"})

# SGOV and BIL are US-listed ETFs, so the allowlist is market-scoped: the same
# four characters arriving on a KR or crypto proposal are NOT parking tickers
# and get no exemption.
#
# It is also account-scoped, and that is the stricter of the two decisions.
# The cumulative cap is only meaningful if this repo can read the *same
# account's* parking exposure back from the broker. ``kis_live`` is the one
# equity_us account mode for which it can (``fetch_my_us_stocks`` ->
# ``ovrs_stck_evlu_amt``). ``toss_live`` equity_us is veto-capable in principle
# (behind ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED) but its parking exposure
# would have to come from a different broker surface; measuring a KIS balance
# to authorize a Toss order would be a wrong-account cap, which is worse than
# no exemption. Toss parking orders therefore keep the ordinary gates.
PARKING_ALLOWLIST_ACCOUNT_MARKETS: frozenset[tuple[str, str]] = frozenset(
    {("kis_live", "equity_us")}
)

# §163차 operator decision: USD 10,000 of cumulative parking exposure. Same
# currency as ``order_proposals.auto_approve.per_order_cap.us`` / ``daily_cap.us``
# (USD) and as KIS's ``ovrs_stck_evlu_amt`` on a ``currency_code="USD"``
# overseas balance, so no FX conversion enters this comparison.
PARKING_CUMULATIVE_CAP_USD: Decimal = Decimal("10000")

# Closed reason vocabulary. A parking rejection renders one of these and never
# a broker- or proposer-supplied string.
PARKING_EXPOSURE_UNAVAILABLE_REASONS: frozenset[str] = frozenset(
    {
        "not_requested",
        "fetch_failed",
        "payload_not_a_list",
        "row_not_a_mapping",
        "symbol_unreadable",
        "evaluation_amount_missing",
        "evaluation_amount_invalid",
        "evaluation_amount_negative",
    }
)


def canonical_eligibility_symbol(value: Any) -> str | None:
    """Canonicalize a *proposal* symbol for allowlist membership -- strictly.

    Returns ``None`` for anything that is not already the exact canonical
    ticker: the value must be an exact built-in ``str`` (not a subclass, whose
    ``strip``/``upper`` could lie), pure ASCII, and already uppercase with no
    surrounding whitespace and no separator rewriting.

    The strictness is deliberate and is safe in one direction only, which is
    why it is used only here. A rejected spelling (``"sgov"``, ``" SGOV "``)
    does not become "some other instrument is allowlisted" -- it becomes "this
    proposal gets no exemption", so it falls back to the ordinary §40차/§156차
    gates and, at worst, costs the operator a Telegram tap.

    The ASCII gate is not decoration. ``str.upper()`` maps several non-ASCII
    code points onto ASCII letters -- U+017F LATIN SMALL LETTER LONG S upper-
    cases to ``"S"``, so ``"ſGOV".upper() == "SGOV"``. Rejecting non-ASCII
    input before any case operation closes that class of confusable outright,
    together with Cyrillic/Greek look-alikes and fullwidth forms.
    """
    if type(value) is not str:
        return None
    if not value or not value.isascii():
        return None
    # No strip, no upper, no separator rewrite: the value must already BE the
    # canonical form. ``to_db_symbol`` is applied only to prove the input
    # carries no separator that would have been rewritten into it.
    if to_db_symbol(value) != value:
        return None
    if value != value.upper():
        return None
    return value


def canonical_exposure_symbol(value: Any) -> str | None:
    """Canonicalize a *broker holdings row* symbol -- leniently.

    The opposite bias to ``canonical_eligibility_symbol``, for the opposite
    reason. This function decides whether a held position *counts against* the
    cumulative cap, so over-inclusion can only raise measured exposure and
    reject a buy. Under-inclusion would silently understate exposure and let a
    buy through, which is the one direction that must not happen. Broker
    balance rows are fixed-width in places and can arrive padded or in a
    different separator convention, so whitespace and case are normalized here.

    Non-ASCII input is still rejected: a look-alike row cannot be *made* to
    count, and admitting one would let an unrelated holding inflate parking
    exposure.
    """
    if not isinstance(value, str):
        return None
    stripped = value.strip()
    if not stripped or not stripped.isascii():
        return None
    return to_db_symbol(stripped.upper())


def is_parking_allowlisted(*, symbol: Any, account_mode: Any, market: Any) -> bool:
    """Exact-element membership. Never a prefix, suffix or substring test.

    ``SGOVX``, ``BILS``, ``BILL``, ``SGOV.U`` and ``BIL.TO`` are different
    instruments and are rejected here because membership is tested against a
    ``frozenset`` of whole strings.
    """
    canonical = canonical_eligibility_symbol(symbol)
    if canonical is None or canonical not in PARKING_ALLOWLIST_SYMBOLS:
        return False
    if type(account_mode) is not str or type(market) is not str:
        return False
    return (account_mode, market) in PARKING_ALLOWLIST_ACCOUNT_MARKETS


@dataclass(frozen=True)
class ParkingExposure:
    """Broker-observed cumulative market value of the allowlisted tickers.

    ``exposure`` is the USD sum of ``ovrs_stck_evlu_amt`` over every allowlisted
    row in one fresh overseas balance read. It is *not* derivable from anything
    a proposal, session or preview can write: the proposal contributes only the
    symbol (which the very same order is submitted against), never an amount.

    ``available=False`` is the fail-closed state and carries a closed
    ``unavailable_reason``; ``exposure`` is then ``None``.
    """

    available: bool
    exposure: Decimal | None = None
    unavailable_reason: str | None = None

    @classmethod
    def unavailable(cls, reason: str) -> ParkingExposure:
        return cls(
            available=False,
            exposure=None,
            unavailable_reason=(
                reason
                if reason in PARKING_EXPOSURE_UNAVAILABLE_REASONS
                else "fetch_failed"
            ),
        )

    @classmethod
    def observed(cls, exposure: Decimal) -> ParkingExposure:
        return cls(available=True, exposure=exposure, unavailable_reason=None)


__all__ = [
    "PARKING_ALLOWLIST_ACCOUNT_MARKETS",
    "PARKING_ALLOWLIST_SYMBOLS",
    "PARKING_CUMULATIVE_CAP_USD",
    "PARKING_EXPOSURE_UNAVAILABLE_REASONS",
    "ParkingExposure",
    "canonical_eligibility_symbol",
    "canonical_exposure_symbol",
    "is_parking_allowlisted",
]
