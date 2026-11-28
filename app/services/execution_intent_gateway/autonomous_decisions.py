"""hk 7118 (6939 N3) — the autonomous-order decision rules as one pure module.

Design ``docs/design/6939-autonomous-strategy-orders.md`` §2 (broker × market ×
session table, §2.2 arming) and §3 invariants I1, I1a, I1b, I2, I7–I11, I14.

Every input is a plain value the caller has already read (intent, plan row,
child, arming fold, counts, FX snapshot, session class, book, flags). There is
no DB, broker, network, clock or environment read here; ``now`` is an input.
Every unknown or missing input refuses (fail closed). Nothing in this module is
wired into ``decisions.decide*`` or a forwarder yet (N4/N6/N10+ do that), so the
existing gateway behaviour is unchanged.

The locked constants below are the single allowed values. They have no env,
CLI or policy override; a test pins them against independent literals
(ROB-993 ``LOCKED_LIMITS`` idiom). The spread-gate values are the one exception
by design: they are operator policy keys (``SPREAD_POLICY_KEYS``) passed in as
:class:`SpreadLimits`; while the keys are absent the decision refuses.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Any, Final

# --------------------------------------------------------------------------
# Locked constants (design §2.2, §3; operator decision 2026-11-28 item 4).
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class AutonomousLimits:
    per_order_cap_krw: Decimal = Decimal("2000000")
    half_cap_krw: Decimal = Decimal("1000000")
    one_share_ceiling_krw: Decimal = Decimal("10000000")
    max_working_orders_per_account: int = 4
    book_max_age: timedelta = timedelta(seconds=5)


LOCKED_LIMITS: Final[AutonomousLimits] = AutonomousLimits()
PER_ORDER_CAP_KRW: Final = LOCKED_LIMITS.per_order_cap_krw
HALF_CAP_KRW: Final = LOCKED_LIMITS.half_cap_krw
ONE_SHARE_CEILING_KRW: Final = LOCKED_LIMITS.one_share_ceiling_krw
MAX_WORKING_ORDERS_PER_ACCOUNT: Final = LOCKED_LIMITS.max_working_orders_per_account
BOOK_MAX_AGE: Final = LOCKED_LIMITS.book_max_age


class LimitsNotLocked(RuntimeError):
    """Supplied limits deviate from :data:`LOCKED_LIMITS`."""


def assert_limits_locked(limits: object) -> None:
    if not isinstance(limits, AutonomousLimits) or limits != LOCKED_LIMITS:
        raise LimitsNotLocked(
            "autonomous limits must equal LOCKED_LIMITS — no operator-tunable caps"
        )


#: Policy keys (dotted path in ``config/trading_policy.yaml``) for the spread
#: gate (operator Q3: 30 bp regular / 15 bp extended / 20 bp Upbit). This module
#: does not read the file; the values arrive through the operator PR.
SPREAD_POLICY_KEYS: Final[Mapping[str, str]] = {
    "regular": "autonomous_orders.spread_gate_bp.regular",
    "extended": "autonomous_orders.spread_gate_bp.extended",
    "upbit": "autonomous_orders.spread_gate_bp.upbit",
}


# --------------------------------------------------------------------------
# Closed vocabularies.
# --------------------------------------------------------------------------


class AutonomousRefusal(StrEnum):
    # I1 / I1a / I1b
    ORDER_CAP_EXCEEDED = "order_cap_exceeded"
    ONE_SHARE_EXCEPTION_INVALID = "one_share_exception_invalid"
    ARMING_HALF_CAP = "arming_half_cap"
    ORDER_CAP_UNVERIFIABLE = "order_cap_unverifiable"
    FX_SNAPSHOT_MISSING = "fx_snapshot_missing"
    ARMING_NOT_LIVE = "arming_not_live"
    # I2
    WORKING_ORDERS_LIMIT = "working_orders_limit"
    WORKING_ORDERS_UNKNOWN = "working_orders_unknown"
    # I7
    PLAN_VERSION_STALE = "plan_version_stale"
    PLAN_GUARD_UNAVAILABLE = "plan_guard_unavailable"
    # I8
    EXIT_EXCEEDS_CONFIRMED_QTY = "exit_exceeds_confirmed_qty"
    EXIT_QUANTITY_UNVERIFIED = "exit_quantity_unverified"
    # I9
    BUDGET_EXCEEDED = "budget_exceeded"
    BUDGET_UNVERIFIED = "budget_unverified"
    # I10
    HALT_SUSPECT = "halt_suspect"
    TRADING_FLAG_BLOCKED = "trading_flag_blocked"
    EXPECTED_PRICE = "expected_price"
    QUOTE_STALE = "quote_stale"
    HALT_STATE_UNKNOWN = "halt_state_unknown"
    # I11
    CELL_UNSUPPORTED = "cell_unsupported"
    ORDER_FORM_NOT_ACCEPTED = "order_form_not_accepted"
    ENTRY_NO_FILL_WINDOW = "entry_no_fill_window"
    BOOK_INCOMPLETE = "book_incomplete"
    SPREAD_TOO_WIDE = "spread_too_wide"
    SPREAD_LIMIT_UNAVAILABLE = "spread_limit_unavailable"
    # I14
    PRODUCT_NOT_ALLOWED = "product_not_allowed"
    # shape
    CHILD_INVALID = "child_invalid"


class SessionClass(StrEnum):
    KRX_REGULAR = "krx_regular"
    NXT_PRE = "nxt_pre"
    NXT_AFTER = "nxt_after"
    KRX_AFTER = "krx_after"
    US_REGULAR = "us_regular"
    US_PRE = "us_pre"
    US_POST = "us_post"
    US_DAY = "us_day"
    CRYPTO_24H = "crypto_24h"


class Arming(StrEnum):
    FROZEN = "frozen"
    MOCK_PROVING = "mock_proving"
    LIVE_HALF = "live_half"
    LIVE_FULL = "live_full"


class ArmingKind(StrEnum):
    MOCK_FIRST = "mock_first"
    LIVE_HALF = "live_half"


@dataclass(frozen=True, slots=True)
class CellSpec:
    """One supported row of design §2 for a live account."""

    currency: str  # "KRW" | "USD"
    order_types: frozenset[str]  # "limit" / "market"
    integer_qty_only: bool
    amount_allowed: bool
    spread_class: str  # key of SPREAD_POLICY_KEYS
    arming_kind: ArmingKind
    #: I1a applies to shares only; crypto is divisible (Upbit is False).
    share_instrument: bool = True
    #: Session name that ``toss_live_us_sessions`` must list for this cell.
    requires_toss_us_session: str | None = None


_LIMIT = frozenset({"limit"})

#: Design §2, live accounts only. Absent cells (KIS US pre/post — operator Q5,
#: Toss US day market, KIS KR amount orders) are unsupported. NXT/after are LH
#: cells (Q4). Toss ``orderAmount`` orders are not accepted in any cell: the
#: broker chooses the quantity, so neither the intent quantity cap nor the
#: position bound can be proven. Market orders are not accepted in any cell: no broker request
#: carries an enforceable price bound, so the locked cap could not hold.
CELLS: Final[Mapping[tuple[str, SessionClass], CellSpec]] = {
    ("kis_live", SessionClass.KRX_REGULAR): CellSpec(
        "KRW", _LIMIT, True, False, "regular", ArmingKind.MOCK_FIRST
    ),
    ("kis_live", SessionClass.NXT_PRE): CellSpec(
        "KRW", _LIMIT, True, False, "extended", ArmingKind.LIVE_HALF
    ),
    ("kis_live", SessionClass.NXT_AFTER): CellSpec(
        "KRW", _LIMIT, True, False, "extended", ArmingKind.LIVE_HALF
    ),
    ("kis_live", SessionClass.KRX_AFTER): CellSpec(
        "KRW", _LIMIT, True, False, "extended", ArmingKind.LIVE_HALF
    ),
    ("kis_live", SessionClass.US_REGULAR): CellSpec(
        "USD", _LIMIT, True, False, "regular", ArmingKind.MOCK_FIRST
    ),
    ("toss_live", SessionClass.KRX_REGULAR): CellSpec(
        "KRW", _LIMIT, True, False, "regular", ArmingKind.LIVE_HALF
    ),
    # Toss KR NXT: qty limit only until a live amount order is recorded.
    ("toss_live", SessionClass.NXT_PRE): CellSpec(
        "KRW", _LIMIT, True, False, "extended", ArmingKind.LIVE_HALF
    ),
    ("toss_live", SessionClass.NXT_AFTER): CellSpec(
        "KRW", _LIMIT, True, False, "extended", ArmingKind.LIVE_HALF
    ),
    ("toss_live", SessionClass.KRX_AFTER): CellSpec(
        "KRW", _LIMIT, True, False, "extended", ArmingKind.LIVE_HALF
    ),
    ("toss_live", SessionClass.US_REGULAR): CellSpec(
        "USD", _LIMIT, False, False, "regular", ArmingKind.LIVE_HALF
    ),
    # Toss US pre/post: limit, integer qty, no orderAmount (this rule only).
    ("toss_live", SessionClass.US_PRE): CellSpec(
        "USD",
        _LIMIT,
        True,
        False,
        "extended",
        ArmingKind.LIVE_HALF,
        requires_toss_us_session="pre",
    ),
    ("toss_live", SessionClass.US_POST): CellSpec(
        "USD",
        _LIMIT,
        True,
        False,
        "extended",
        ArmingKind.LIVE_HALF,
        requires_toss_us_session="post",
    ),
    ("upbit_live", SessionClass.CRYPTO_24H): CellSpec(
        "KRW",
        _LIMIT,
        False,
        False,
        "upbit",
        ArmingKind.LIVE_HALF,
        share_instrument=False,
    ),
}

_KINDS: Final = frozenset({"submit", "amend", "cancel"})
_SIDES: Final = frozenset({"buy", "sell"})
#: ``kr_quote_price`` kinds a quote may be used on; every other value refuses.
TRUSTED_PRICE_KINDS: Final = frozenset({"last_trade", "mid"})


# --------------------------------------------------------------------------
# Inputs.
# --------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class SpreadLimits:
    regular_bp: Decimal
    extended_bp: Decimal
    upbit_bp: Decimal

    def for_class(self, spread_class: str) -> Decimal | None:
        """The class limit, or None unless it is a finite positive Decimal."""
        value = {
            "regular": self.regular_bp,
            "extended": self.extended_bp,
            "upbit": self.upbit_bp,
        }.get(spread_class)
        return value if _positive(value) else None


def spread_limits_from_policy(policy: Mapping[str, Any] | None) -> SpreadLimits | None:
    """Extract the three spread keys from a parsed policy mapping.

    Returns None (the decision then refuses) when any key is absent, not a
    finite positive number, or the mapping is missing.
    """
    if not isinstance(policy, Mapping):
        return None
    values: dict[str, Decimal] = {}
    for name, path in SPREAD_POLICY_KEYS.items():
        node: Any = policy
        for part in path.split("."):
            if not isinstance(node, Mapping) or part not in node:
                return None
            node = node[part]
        if isinstance(node, bool) or not isinstance(node, (int, float, str, Decimal)):
            return None
        try:
            value = Decimal(str(node))
        except InvalidOperation:
            return None
        if not value.is_finite() or value <= 0:
            return None
        values[name] = value
    return SpreadLimits(values["regular"], values["extended"], values["upbit"])


@dataclass(frozen=True, slots=True)
class Book:
    best_bid: Decimal | None
    best_ask: Decimal | None
    bid_observed_at: datetime | None
    ask_observed_at: datetime | None


@dataclass(frozen=True, slots=True)
class ChildOrder:
    kind: str  # submit | amend | cancel
    side: str  # buy | sell
    order_type: str  # only "limit" is accepted
    quantity: Decimal | None
    limit_price: Decimal | None  # quote currency
    amount: Decimal | None = None  # Toss orderAmount (quote currency), buys only


@dataclass(frozen=True, slots=True)
class DecisionInputs:
    account: str
    session: SessionClass | str | None
    product: str | None  # must be "cash"
    child: ChildOrder
    arming: Arming | str | None
    #: KRW per one unit of the cell currency, snapshotted in the intent at
    #: publish (operator Q1). Required for USD cells; ignored for KRW.
    fx_krw_per_unit: Decimal | None
    working_orders: int | None  # account-wide, without terminal evidence
    plan_version: int | None
    latest_plan_change_seq: int | None
    confirmed_net_quantity: Decimal | None  # for sells (I8)
    #: Quantity of this account×symbol's sells still working or uncertain
    #: (journal rows without terminal evidence), including an amend's target.
    #: Scope: the same intent lineage as ``confirmed_net_quantity`` (not the
    #: whole account × symbol), so independent lineages do not block each other.
    working_sell_quantity: Decimal | None
    #: Approved − confirmed fills − working (I9), quantity form for qty buys.
    budget_remaining_quantity: Decimal | None
    #: Same remainder in KRW (approved notional − fills − working), I9.
    budget_remaining_notional_krw: Decimal | None
    #: For an amend: the target order's reserved (high-water) quantity, already
    #: counted in ``working_sell_quantity`` / the budget remainder. None on an
    #: amend refuses; ignored on a submit.
    amend_target_quantity: Decimal | None
    #: For an amend: the target's high-water notional in KRW, already counted in
    #: ``budget_remaining_notional_krw``. None on a buy amend refuses.
    amend_target_notional_krw: Decimal | None
    halt_suspect: bool | None
    trading_flag_blocked: bool | None
    price_kind: str | None  # "last_trade" | "mid" | "expected" | ...
    book: Book | None
    in_fill_window: bool | None
    spread_limits: SpreadLimits | None
    #: Policy ``order_proposals.approval_window.toss_live_us_sessions``.
    toss_us_sessions: frozenset[str] | None
    now: datetime


@dataclass(frozen=True, slots=True)
class AutonomousDecision:
    refusal: AutonomousRefusal | None
    notional_krw: Decimal | None = None
    cap_in_force_krw: Decimal | None = None

    @property
    def allowed(self) -> bool:
        return self.refusal is None


def _refuse(code: AutonomousRefusal) -> AutonomousDecision:
    return AutonomousDecision(refusal=code)


def _positive(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite() and value > 0


# --------------------------------------------------------------------------
# Rules. Each returns a refusal or None; ``decide`` runs them in order.
# --------------------------------------------------------------------------


def check_product(inputs: DecisionInputs) -> AutonomousRefusal | None:
    """I14 — live cash buy/sell only."""
    if inputs.product != "cash" or inputs.child.side not in _SIDES:
        return AutonomousRefusal.PRODUCT_NOT_ALLOWED
    return None


def resolve_cell(inputs: DecisionInputs) -> CellSpec | None:
    try:
        session = SessionClass(inputs.session) if inputs.session is not None else None
    except ValueError:
        return None
    if session is None:
        return None
    return CELLS.get((inputs.account, session))


def check_plan_version(inputs: DecisionInputs) -> AutonomousRefusal | None:
    """I7 — a superseded plan version never sends (cancels exempt)."""
    if inputs.plan_version is None or inputs.latest_plan_change_seq is None:
        return AutonomousRefusal.PLAN_GUARD_UNAVAILABLE
    if inputs.latest_plan_change_seq > inputs.plan_version:
        return AutonomousRefusal.PLAN_VERSION_STALE
    return None


def check_order_form(
    cell: CellSpec, inputs: DecisionInputs
) -> AutonomousRefusal | None:
    """I11 — per-cell order shape and the Toss US session allowlist."""
    child = inputs.child
    if cell.requires_toss_us_session is not None:
        allowed = inputs.toss_us_sessions
        if not isinstance(allowed, frozenset) or (
            cell.requires_toss_us_session not in allowed
        ):
            return AutonomousRefusal.CELL_UNSUPPORTED
    if child.order_type not in cell.order_types:
        return AutonomousRefusal.ORDER_FORM_NOT_ACCEPTED
    if child.amount is not None:
        if (
            not cell.amount_allowed
            or child.quantity is not None
            or child.side != "buy"
            or child.kind != "submit"
        ):
            return AutonomousRefusal.ORDER_FORM_NOT_ACCEPTED
        if not _positive(child.amount):
            return AutonomousRefusal.CHILD_INVALID
    else:
        if not _positive(child.quantity):
            return AutonomousRefusal.CHILD_INVALID
        assert child.quantity is not None
        if (
            cell.integer_qty_only
            and child.quantity != child.quantity.to_integral_value()
        ):
            return AutonomousRefusal.ORDER_FORM_NOT_ACCEPTED
    if child.order_type == "limit" and not _positive(child.limit_price):
        return AutonomousRefusal.CHILD_INVALID
    return None


def check_fill_window(inputs: DecisionInputs) -> AutonomousRefusal | None:
    """§2 rule — no new entry inside acceptance but outside fill."""
    if inputs.in_fill_window is None:
        return AutonomousRefusal.ENTRY_NO_FILL_WINDOW
    if not inputs.in_fill_window and inputs.child.side == "buy":
        return AutonomousRefusal.ENTRY_NO_FILL_WINDOW
    return None


def check_halt_and_quote(inputs: DecisionInputs) -> AutonomousRefusal | None:
    """I10 — halt / trading flag / expected price."""
    if inputs.halt_suspect is None or inputs.trading_flag_blocked is None:
        return AutonomousRefusal.HALT_STATE_UNKNOWN
    if inputs.halt_suspect:
        return AutonomousRefusal.HALT_SUSPECT
    if inputs.trading_flag_blocked:
        return AutonomousRefusal.TRADING_FLAG_BLOCKED
    if inputs.price_kind == "expected":
        return AutonomousRefusal.EXPECTED_PRICE
    if inputs.price_kind not in TRUSTED_PRICE_KINDS:
        return AutonomousRefusal.QUOTE_STALE
    return None


def _aware(value: object) -> bool:
    return isinstance(value, datetime) and value.utcoffset() is not None


def _fresh(observed_at: datetime | None, now: datetime) -> bool:
    if not (_aware(observed_at) and _aware(now)):
        return False
    assert observed_at is not None
    return timedelta(0) <= now - observed_at <= BOOK_MAX_AGE


def spread_bp(bid: Decimal, ask: Decimal) -> Decimal:
    mid = (bid + ask) / 2
    return (ask - bid) / mid * Decimal(10000)


def check_book(cell: CellSpec, inputs: DecisionInputs) -> AutonomousRefusal | None:
    """I10 quote age + I11 both sides fresh and spread within the class limit."""
    book = inputs.book
    if book is None:
        return AutonomousRefusal.BOOK_INCOMPLETE
    if not (_positive(book.best_bid) and _positive(book.best_ask)):
        return AutonomousRefusal.BOOK_INCOMPLETE
    assert book.best_bid is not None and book.best_ask is not None
    if book.best_bid >= book.best_ask:
        return AutonomousRefusal.BOOK_INCOMPLETE
    if not (
        _fresh(book.bid_observed_at, inputs.now)
        and _fresh(book.ask_observed_at, inputs.now)
    ):
        return AutonomousRefusal.QUOTE_STALE
    limit = (
        inputs.spread_limits.for_class(cell.spread_class)
        if inputs.spread_limits
        else None
    )
    if limit is None:
        return AutonomousRefusal.SPREAD_LIMIT_UNAVAILABLE
    if spread_bp(book.best_bid, book.best_ask) > limit:
        return AutonomousRefusal.SPREAD_TOO_WIDE
    return None


def cap_in_force(arming: Arming | str | None) -> Decimal | None:
    """I1/I1b — per-order cap for the arming state; None = not live."""
    try:
        state = Arming(arming) if arming is not None else None
    except ValueError:
        return None
    if state is Arming.LIVE_FULL:
        return PER_ORDER_CAP_KRW
    if state is Arming.LIVE_HALF:
        return HALF_CAP_KRW
    return None


def check_caps(cell: CellSpec, inputs: DecisionInputs) -> AutonomousDecision:
    """I1, I1a, I1b."""
    cap = cap_in_force(inputs.arming)
    if cap is None:
        return _refuse(AutonomousRefusal.ARMING_NOT_LIVE)
    if cell.currency == "KRW":
        fx = Decimal(1)
    else:
        if not _positive(inputs.fx_krw_per_unit):
            return _refuse(AutonomousRefusal.FX_SNAPSHOT_MISSING)
        assert inputs.fx_krw_per_unit is not None
        fx = inputs.fx_krw_per_unit
    child = inputs.child
    if child.amount is not None:
        notional = child.amount * fx
        unit_krw = None
    else:
        price = child.limit_price if child.order_type == "limit" else None
        if not _positive(price) or child.quantity is None:
            return _refuse(AutonomousRefusal.ORDER_CAP_UNVERIFIABLE)
        assert price is not None
        unit_krw = price * fx
        notional = child.quantity * unit_krw
    result = AutonomousDecision(
        refusal=None, notional_krw=notional, cap_in_force_krw=cap
    )
    if notional <= cap:
        return result
    if cell.share_instrument and child.quantity == 1 and unit_krw is not None:
        # I1a: one share costs more than the cap in force.
        if unit_krw <= ONE_SHARE_CEILING_KRW:
            return result
        return AutonomousDecision(
            AutonomousRefusal.ONE_SHARE_EXCEPTION_INVALID, notional, cap
        )
    if notional > PER_ORDER_CAP_KRW:
        return AutonomousDecision(AutonomousRefusal.ORDER_CAP_EXCEEDED, notional, cap)
    return AutonomousDecision(AutonomousRefusal.ARMING_HALF_CAP, notional, cap)


def check_working_orders(inputs: DecisionInputs) -> AutonomousRefusal | None:
    """I2 — at most four working orders per account (new submits only)."""
    count = inputs.working_orders
    if (
        count is None
        or isinstance(count, bool)
        or not isinstance(count, int)
        or count < 0
    ):
        return AutonomousRefusal.WORKING_ORDERS_UNKNOWN
    if count >= MAX_WORKING_ORDERS_PER_ACCOUNT:
        return AutonomousRefusal.WORKING_ORDERS_LIMIT
    return None


def _finite(value: object) -> bool:
    return isinstance(value, Decimal) and value.is_finite()


def _amend_add_back(inputs: DecisionInputs) -> Decimal | None:
    """The target's reservation an amend may reuse; 0 on a submit."""
    if inputs.child.kind != "amend":
        return Decimal(0)
    target = inputs.amend_target_quantity
    if not _finite(target) or target < 0:
        return None
    return target


def check_exit_quantity(inputs: DecisionInputs) -> AutonomousRefusal | None:
    """I8 — sells working plus this one never exceed confirmed net quantity."""
    confirmed = inputs.confirmed_net_quantity
    working = inputs.working_sell_quantity
    qty = inputs.child.quantity
    add_back = _amend_add_back(inputs)
    if (
        not _finite(confirmed)
        or not _finite(working)
        or working < 0
        or qty is None
        or add_back is None
        or add_back > working
    ):
        return AutonomousRefusal.EXIT_QUANTITY_UNVERIFIED
    if qty + working - add_back > confirmed:
        return AutonomousRefusal.EXIT_EXCEEDS_CONFIRMED_QTY
    return None


def check_budget(
    inputs: DecisionInputs, notional_krw: Decimal | None
) -> AutonomousRefusal | None:
    """I9 — a buy stays within approved − confirmed fills − working, in both
    quantity and KRW notional. An amend reuses only its target's reservation.
    """
    remaining = inputs.budget_remaining_quantity
    remaining_krw = inputs.budget_remaining_notional_krw
    qty = inputs.child.quantity
    add_back = _amend_add_back(inputs)
    if inputs.child.kind == "amend":
        target_krw = inputs.amend_target_notional_krw
        add_back_krw = target_krw if _finite(target_krw) and target_krw >= 0 else None
    else:
        add_back_krw = Decimal(0)
    if (
        not _finite(remaining)
        or not _finite(remaining_krw)
        or qty is None
        or notional_krw is None
        or add_back is None
        or add_back_krw is None
    ):
        return AutonomousRefusal.BUDGET_UNVERIFIED
    if qty - add_back > remaining:
        return AutonomousRefusal.BUDGET_EXCEEDED
    if notional_krw - add_back_krw > remaining_krw:
        return AutonomousRefusal.BUDGET_EXCEEDED
    return None


def decide(inputs: DecisionInputs) -> AutonomousDecision:
    """Return allow or the first refusal for one autonomous child request.

    A cancel is always allowed here (ROB-1440 operator contract: cancel ignores
    kill, caps and plan supersession; the account fingerprint is checked by the
    forwarder). Submit and amend run every rule.
    """
    child = inputs.child
    if child.kind not in _KINDS:
        return _refuse(AutonomousRefusal.CHILD_INVALID)
    if child.kind == "cancel":
        return AutonomousDecision(refusal=None)
    for refusal in (check_product(inputs),):
        if refusal is not None:
            return _refuse(refusal)
    cell = resolve_cell(inputs)
    if cell is None:
        return _refuse(AutonomousRefusal.CELL_UNSUPPORTED)
    for rule in (check_plan_version, check_halt_and_quote, check_fill_window):
        refusal = rule(inputs)
        if refusal is not None:
            return _refuse(refusal)
    refusal = check_order_form(cell, inputs)
    if refusal is not None:
        return _refuse(refusal)
    refusal = check_book(cell, inputs)
    if refusal is not None:
        return _refuse(refusal)
    caps = check_caps(cell, inputs)
    if not caps.allowed:
        return caps
    if child.kind == "submit":
        refusal = check_working_orders(inputs)
        if refusal is not None:
            return AutonomousDecision(refusal, caps.notional_krw, caps.cap_in_force_krw)
    if child.side == "sell":
        refusal = check_exit_quantity(inputs)
    else:
        refusal = check_budget(inputs, caps.notional_krw)
    if refusal is not None:
        return AutonomousDecision(refusal, caps.notional_krw, caps.cap_in_force_krw)
    return caps
