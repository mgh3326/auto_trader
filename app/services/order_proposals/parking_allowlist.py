"""Cash-parking allowlist for auto-approval (§163차, §170차).

The operator authorized two US Treasury ETFs (``SGOV``/``BIL``) and two KR
CD-rate ETFs (``459580``/``357870``) as parking instruments, each only in its explicitly
paired ``kis_live`` market. Two §40차/§156차 gates change -- and 🔴 exactly one
of them stops applying. The other keeps applying at a different value:

1. **marketability** -- a parking buy may price at the market instead of
   resting below it (§156차 ② kept "매수 marketable 은 수동 유지"; §163차 makes
   these two tickers its only exception), and
2. **the per-order cap value** -- 🔴 this is a **raise, not a removal**. The
   check is never skipped for a parking rung.
   §106차 defined ``per_order_cap`` as *the maximum loss boundary of one
   automation error*, and that boundary keeps its shape here: the per-order
   check still runs on every parking rung, against
   the scope's native-currency raised cap instead of the ordinary
   ``per_order_cap``. One automation error remains bounded by the selected
   tuple's per-order cap, which is what makes residual measurement gaps
   survivable.

   A **second** native-currency boundary sits behind it on the buy side,
   measured against the broker balance **plus** the durable same-day record of
   already-auto-approved parking buys -- the balance alone cannot see an
   accepted-but-unfilled order and would re-meter the next proposal from zero.
   The cumulative cap is the second line, never the only one.

Nothing else changes, and the per-order cap above has not stopped applying --
only its value moved. The daily cap is excluded only for an explicitly enabled
parking authorization tuple in ``expanded`` mode; the loss-cut and exit-intent gates, the
``policy_deviation`` tag scan, the veto-capable account/market allowlist, the
Toss auto-submission freeze, the sell-side break-even band and round-trip-cost
profit proof, and the mandatory veto thesis all apply to a parking rung exactly
as they apply to every other rung. ``off`` mode is untouched entirely.

This module is deliberately pure -- stdlib plus ``app.core.symbol`` -- and as
written reads no settings, environment variable, database, policy document or
broker. Every constant below is a module-level ``frozenset``/``Decimal``
literal: there is no setter, no loader, and no configuration key, so a runtime
session cannot widen the allowlist or raise the cap. Changing either is an
operator PR that edits this file.

🔴 **Guarantee strength: accidental prevention + static detection, not
structural impossibility** (the BL-4 / NHPLUG framing). The accompanying AST
test asserts the import surface and rejects direct references to ``settings``,
``os``/``getenv``/``environ``, ``open``, the policy loader and the DB session
factory, and it catches the plain ``importlib``/``getattr`` spellings. It does
**not** defeat a determined obfuscation -- string-assembled ``__import__``
defeats it, and enumerating spellings is a losing game. What the guard actually
buys is that re-introducing configurability *the obvious way* turns red in CI.
The real boundary is that this file is operator-PR-only, like the policy
document.

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

# 🔴 TWO boundaries, deliberately separate constants so that changing one can
# never silently move the other. Both are USD -- the same currency as
# ``order_proposals.auto_approve.per_order_cap.us`` / ``daily_cap.us`` and as
# KIS's ``ovrs_stck_evlu_amt`` on a ``currency_code="USD"`` overseas balance --
# so no FX conversion enters either comparison.
#
# FIRST line: the per-order cap, RAISED for parking, never removed. The
# §106차 "maximum loss of one automation error" boundary keeps its shape; only
# its value changes for these two tickers. A single parking order above this
# is rejected exactly as any other over-cap order is.
PARKING_PER_ORDER_CAP_USD: Decimal = Decimal("10000")

# SECOND line: cumulative parking exposure across orders, buy side. This is a
# backstop behind the per-order cap, not a substitute for it -- its
# measurement has known residual gaps (BL-37 account labelling, BL-38
# pre-submit reservation, BL-39 cross-day window; see the runbook §8.7), and
# the per-order cap above is what keeps each of those bounded per order.
PARKING_CUMULATIVE_CAP_USD: Decimal = Decimal("10000")

# The KR values are deliberately separate from USD values. No FX conversion
# enters either comparison: a KR proposal is compared only with a KRW cap and
# a domestic KIS balance value, while a US proposal remains USD-only.
PARKING_PER_ORDER_CAP_KRW: Decimal = Decimal("10000000")
PARKING_CUMULATIVE_CAP_KRW: Decimal = Decimal("15000000")

# §S170: independently closed activation constants. These are deliberately
# per-market rather than per-symbol so a future operator narrowing is a
# one-line edit without changing a ticker's cap or exposure tuple.
PARKING_DAILY_CAP_EXEMPT_US = True
PARKING_DAILY_CAP_EXEMPT_KR = True


@dataclass(frozen=True)
class ParkingScope:
    """One inseparable parking authorization tuple.

    Symbol, account/market, native currency, cap pair, and the KIS balance
    fields used to measure that same market live in one immutable record. This
    is intentionally *not* two independent symbol/account sets: adding either
    half alone cannot create an authorization through a cross product.
    """

    symbol: str
    account_mode: str
    market: str
    currency: str
    per_order_cap: Decimal
    cumulative_cap: Decimal
    balance_symbol_field: str
    balance_evaluation_field: str
    # §S170: a closed, per-scope operator switch. It is not a policy/env/DB
    # setting. The eligibility predicate below is still responsible for the
    # `expanded`-mode boundary.
    daily_cap_exempt: bool


# Closed operator allowlist. Each entry is a full authorization tuple, not
# independently extensible symbol and market collections. ``toss_live`` is
# intentionally absent: a KIS balance must never meter a Toss order.
PARKING_ALLOWLIST_SCOPES: frozenset[ParkingScope] = frozenset(
    {
        ParkingScope(
            symbol="BIL",
            account_mode="kis_live",
            market="equity_us",
            currency="USD",
            per_order_cap=PARKING_PER_ORDER_CAP_USD,
            cumulative_cap=PARKING_CUMULATIVE_CAP_USD,
            balance_symbol_field="ovrs_pdno",
            balance_evaluation_field="ovrs_stck_evlu_amt",
            daily_cap_exempt=PARKING_DAILY_CAP_EXEMPT_US,
        ),
        ParkingScope(
            symbol="SGOV",
            account_mode="kis_live",
            market="equity_us",
            currency="USD",
            per_order_cap=PARKING_PER_ORDER_CAP_USD,
            cumulative_cap=PARKING_CUMULATIVE_CAP_USD,
            balance_symbol_field="ovrs_pdno",
            balance_evaluation_field="ovrs_stck_evlu_amt",
            daily_cap_exempt=PARKING_DAILY_CAP_EXEMPT_US,
        ),
        ParkingScope(
            symbol="459580",
            account_mode="kis_live",
            market="equity_kr",
            currency="KRW",
            per_order_cap=PARKING_PER_ORDER_CAP_KRW,
            cumulative_cap=PARKING_CUMULATIVE_CAP_KRW,
            balance_symbol_field="pdno",
            balance_evaluation_field="evlu_amt",
            daily_cap_exempt=PARKING_DAILY_CAP_EXEMPT_KR,
        ),
        ParkingScope(
            symbol="357870",
            account_mode="kis_live",
            market="equity_kr",
            currency="KRW",
            per_order_cap=PARKING_PER_ORDER_CAP_KRW,
            cumulative_cap=PARKING_CUMULATIVE_CAP_KRW,
            balance_symbol_field="pdno",
            balance_evaluation_field="evlu_amt",
            daily_cap_exempt=PARKING_DAILY_CAP_EXEMPT_KR,
        ),
    }
)

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
        "currency_not_usd",
        "currency_not_krw",
        "scope_not_allowlisted",
        # §163차 재작업 1 — the durable half. Its absence or unreadability is
        # not a degraded measurement, it is a broken cap: without it an
        # accepted-but-unfilled parking buy is invisible and the next proposal
        # re-meters from zero.
        "durable_reader_missing",
        "durable_read_failed",
        "durable_notional_invalid",
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
    proposal gets no parking treatment", so it falls back to the ordinary
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


def parking_scope(
    *, symbol: Any, account_mode: Any, market: Any
) -> ParkingScope | None:
    """Return the one closed authorization tuple, or ``None`` fail-closed."""
    canonical = canonical_eligibility_symbol(symbol)
    if canonical is None or type(account_mode) is not str or type(market) is not str:
        return None
    return next(
        (
            scope
            for scope in PARKING_ALLOWLIST_SCOPES
            if (
                scope.symbol == canonical
                and scope.account_mode == account_mode
                and scope.market == market
            )
        ),
        None,
    )


def is_parking_exposure_symbol(value: Any, *, account_mode: Any, market: Any) -> bool:
    """Lenient membership for anything that CONTRIBUTES to measured exposure.

    Used by both halves of the §163차 measurement -- broker balance rows and
    the durable record of already-auto-approved parking buys -- so the two
    cannot disagree about what counts. Biased toward inclusion for the reason
    given on ``canonical_exposure_symbol``: counting one thing too many can
    only reject a buy, counting one too few lets a buy through.
    """
    canonical = canonical_exposure_symbol(value)
    if canonical is None or type(account_mode) is not str or type(market) is not str:
        return False
    return any(
        scope.symbol == canonical
        and scope.account_mode == account_mode
        and scope.market == market
        for scope in PARKING_ALLOWLIST_SCOPES
    )


def is_parking_allowlisted(*, symbol: Any, account_mode: Any, market: Any) -> bool:
    """Exact-element membership. Never a prefix, suffix or substring test.

    ``SGOVX``, ``BILS``, ``BILL``, ``SGOV.U`` and ``BIL.TO`` are different
    instruments and are rejected here because membership is tested against a
    ``frozenset`` of whole strings.
    """
    return (
        parking_scope(symbol=symbol, account_mode=account_mode, market=market)
        is not None
    )


def is_parking_daily_cap_exempt(
    *, symbol: Any, account_mode: Any, market: Any, mode: Any
) -> bool:
    """Return whether this rung may be excluded from the shared daily cap.

    This deliberately delegates tuple membership to :func:`parking_scope` --
    the same strict predicate that grants the parking treatment. A separate
    ticker check here would let the two authorization decisions drift. The
    only additional condition is the already-existing ``expanded`` mode: an
    ``off``-mode or otherwise ordinary parking-symbol rung retains the daily
    cap.
    """
    scope = parking_scope(
        symbol=symbol,
        account_mode=account_mode,
        market=market,
    )
    return (
        scope is not None
        and scope.daily_cap_exempt
        and type(mode) is str
        and mode == "expanded"
    )


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
    "PARKING_ALLOWLIST_SCOPES",
    "PARKING_CUMULATIVE_CAP_USD",
    "PARKING_CUMULATIVE_CAP_KRW",
    "PARKING_DAILY_CAP_EXEMPT_US",
    "PARKING_DAILY_CAP_EXEMPT_KR",
    "PARKING_PER_ORDER_CAP_USD",
    "PARKING_PER_ORDER_CAP_KRW",
    "PARKING_EXPOSURE_UNAVAILABLE_REASONS",
    "ParkingScope",
    "ParkingExposure",
    "canonical_eligibility_symbol",
    "canonical_exposure_symbol",
    "is_parking_allowlisted",
    "is_parking_daily_cap_exempt",
    "is_parking_exposure_symbol",
    "parking_scope",
]
