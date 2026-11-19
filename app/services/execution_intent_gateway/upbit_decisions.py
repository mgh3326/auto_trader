"""hk 5441 — refusal rules for an ``upbit_live`` intent and its children. Pure.

No DB, no network, no clock. The store loads :class:`UpbitFacts` while it holds
the sleeve lock, the mandate row lock and the intent row lock, in that order, so
the facts are complete for the sleeve when a rule reads them.

The rows of ``docs/design/5438-go-execution-path.md`` §2 that this module
enforces are named in the comments (I1..I10, B.., S..). Three entry points share
the same checks:

* :func:`check_storable` — ``store_intent``;
* :func:`decide_upbit_child` — reserving a child;
* :func:`check_before_send` — the pre-send recheck, on facts read again.

An operator exit (S6) is judged by :func:`evaluate_operator_exit` from all three.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Final
from uuid import UUID

from app.schemas.execution_intent import (
    MAX_UPBIT_IDEMPOTENCY_KEY_LENGTH,
    UPBIT_LIVE_ACCOUNT,
    AmendChild,
    AnyChild,
    CancelChild,
    PriceOffTick,
    PriceTickUnsupported,
    UpbitSubmitChild,
    check_upbit_limit_price,
)
from app.services.execution_intent_gateway.decisions import (
    NEVER_SENT_STATES,
    ChildSnapshot,
    IntentSnapshot,
    Refusal,
)
from app.services.kis_mock_runner.control import KillMode
from app.services.strategy_sleeves.book import SleeveBook
from app.services.strategy_sleeves.gate import (
    SleeveVersionView,
    evaluate_sleeve_gate,
)
from app.services.strategy_sleeves.vocabulary import SleeveState

UPBIT_ACCOUNTS: Final[frozenset[str]] = frozenset({UPBIT_LIVE_ACCOUNT})

#: One account, three names (design §1.3). Every surface reads from here.
SLEEVE_ACCOUNT_MODE: Final = "upbit"
SLEEVE_MARKET: Final = "crypto"
LEDGER_BROKER: Final = "upbit"
LEDGER_ACCOUNT_MODE: Final = "live"
ORDER_LEDGER_ACCOUNT_SCOPE: Final = "upbit_live"
CURRENCY: Final = "KRW"

#: The existing loss-sell guard's floor (``order_validation``): a sell below
#: average buy price x 1.01 is a loss sell. The guard is not changed here; this
#: is the same number read first, so that such a sell is never forwarded (I10).
LOSS_SELL_FLOOR_FACTOR: Final = Decimal("1.01")

#: Why a reserved child was proven never to have reached the broker (B29). The
#: journal trigger accepts ``sending -> not_sent`` only with one of these.
NOT_SENT_REASONS: Final[frozenset[str]] = frozenset(
    {
        Refusal.PRICE_NORMALIZATION_MISMATCH.value,
        Refusal.MANDATE_REVOKED.value,
        Refusal.MANDATE_EXPIRED.value,
        Refusal.MANDATE_MISSING.value,
        Refusal.MANDATE_SCOPE_MISMATCH.value,
        Refusal.ACCOUNT_MISMATCH.value,
        Refusal.SUBMISSIONS_KILLED.value,
        Refusal.AUTHORIZATION_EXPIRED.value,
        Refusal.UPBIT_LIVE_DISABLED.value,
        Refusal.OPERATOR_EXIT_INVALID.value,
        Refusal.EXIT_HANDOFF_ACTIVE.value,
        Refusal.SLEEVE_NOT_LIVE.value,
        Refusal.SLEEVE_UNREADABLE.value,
        Refusal.LOWER_GUARD_REFUSED.value,
    }
)


@dataclass(frozen=True, slots=True)
class MandateView:
    """One ``review.execution_mandates`` row and whether it is revoked."""

    mandate_id: UUID
    strategy: str
    strategy_code_sha: str
    sleeve_id: int
    account: str
    mode: str
    allowed_symbols: frozenset[str]
    per_intent_max_notional_krw: Decimal
    max_open_intents: int
    price_band_bps: int
    max_intent_ttl: timedelta
    expires_at: datetime
    account_ref: str | None
    revoked: bool


@dataclass(frozen=True, slots=True)
class OperatorExitView:
    """One ``review.execution_operator_exits`` row (S6)."""

    exit_id: UUID
    mandate_id: UUID
    symbol: str
    max_quantity: Decimal
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class AccountReads:
    """What the gateway read outside the database before it reserved.

    ``kill_mode`` is ``None`` only for a cancel, which never reads it.
    ``avg_buy_price`` is the account's average buy price for the symbol, or
    ``None`` when it is missing, not positive, or the read failed (B9).
    ``cooldown_active`` is read for a buy only.
    """

    kill_mode: KillMode | None
    live_enabled: bool
    avg_buy_price: Decimal | None = None
    cooldown_active: bool = False


@dataclass(frozen=True, slots=True)
class SleeveView:
    """The sleeve as measured under its lock.

    ``book`` comes from ledger-attributed fills for a live intent and from the
    journal's shadowed children for a shadow intent (B17). ``reserved_buy_notional``
    is the room other unexpired buy intents of the sleeve still hold: budget that
    is promised but not yet at the broker.
    """

    version: SleeveVersionView | None
    book: SleeveBook | None
    reserved_buy_notional: Decimal = Decimal("0")


@dataclass(frozen=True, slots=True)
class SymbolView:
    """The intent's symbol inside its sleeve.

    ``holdings`` is the proven own position, or ``None`` when it cannot be
    proven (an unresolved order, an unreadable book). Unknown is never zero.
    """

    handoff_active: bool
    unresolved_prior_order: bool
    open_sell_intents: frozenset[UUID]
    holdings: Decimal | None


@dataclass(frozen=True, slots=True)
class UpbitFacts:
    mandate: MandateView | None
    operator_exit: OperatorExitView | None
    sleeve: SleeveView
    symbol: SymbolView
    #: Unexpired strategy buy intents of the sleeve, the judged one excluded.
    open_buy_intents: int
    now: datetime


@dataclass(frozen=True, slots=True)
class UpbitDecision:
    """``refusal is None`` allows the forward.

    ``journal`` is False when nothing was decided about the intent's money (a
    child of the wrong shape, the live switch off): no row is written, like the
    env gate. ``open_handoff`` asks the store to open the S5 exit handoff in the
    same transaction as the refused row. ``stop_sleeve`` asks it to append the
    I8 ``stopped`` version.
    """

    refusal: Refusal | None = None
    target_broker_order_id: str | None = None
    journal: bool = True
    open_handoff: bool = False
    stop_sleeve: SleeveVersionView | None = None


def _no_row(refusal: Refusal) -> UpbitDecision:
    return UpbitDecision(refusal=refusal, journal=False)


def loss_sell_floor(avg_buy_price: Decimal) -> Decimal:
    return avg_buy_price * LOSS_SELL_FLOOR_FACTOR


def child_notional(quantity: Decimal, limit_price_krw: int) -> Decimal:
    """Exact, never rounded (design §3.3)."""
    return quantity * Decimal(limit_price_krw)


def _price_within_bound(intent: IntentSnapshot, price: int) -> bool:
    """A buy may pay up to the bound and a sell may give up to it, never further."""
    scaled = price * 10_000
    if intent.side == "buy":
        return scaled <= intent.limit_price_krw * (10_000 + intent.max_slippage_bps)
    if intent.side == "sell":
        return scaled >= intent.limit_price_krw * (10_000 - intent.max_slippage_bps)
    return False


def sent_exposure(children: Sequence[ChildSnapshot]) -> tuple[Decimal, Decimal]:
    """Quantity and notional every order of the intent may have reached.

    The E1 rule ("How caps count") with exact decimals: every submit that was
    not refused and not proven unsent counts in full, cancelled ones included,
    because a cancel is not fill evidence. Upbit has no amend, so each submit is
    its own order.
    """
    quantity = Decimal("0")
    notional = Decimal("0")
    for child in children:
        if (
            child.state in NEVER_SENT_STATES
            or child.action != "submit"
            or child.quantity_decimal is None
            or child.limit_price_krw is None
        ):
            continue
        quantity += child.quantity_decimal
        notional += child_notional(child.quantity_decimal, child.limit_price_krw)
    return quantity, notional


def mandate_refusal(
    intent: IntentSnapshot, mandate: MandateView | None, *, now: datetime
) -> Refusal | None:
    """I1, I3, B11: the intent must sit inside a valid mandate.

    Called when the intent is stored and again each time a child is reserved,
    so a mandate revoked or expired in between stops the next child.
    """
    if mandate is None or mandate.mandate_id != intent.mandate_id:
        return Refusal.MANDATE_MISSING
    if mandate.revoked:
        return Refusal.MANDATE_REVOKED
    if now >= mandate.expires_at:
        return Refusal.MANDATE_EXPIRED
    if _identity_mismatch(intent, mandate):
        return Refusal.MANDATE_SCOPE_MISMATCH
    # I3: the intent's caps are inside the mandate's.
    if intent.expires_at > mandate.expires_at:
        return Refusal.MANDATE_SCOPE_MISMATCH
    if intent.created_at is None:
        return Refusal.MANDATE_SCOPE_MISMATCH
    if intent.expires_at > intent.created_at + mandate.max_intent_ttl:
        return Refusal.MANDATE_SCOPE_MISMATCH
    if intent.max_slippage_bps > mandate.price_band_bps:
        return Refusal.MANDATE_SCOPE_MISMATCH
    # B7: the per-intent notional cap binds a buy only.
    if (
        intent.side == "buy"
        and Decimal(intent.max_notional_krw) > mandate.per_intent_max_notional_krw
    ):
        return Refusal.MANDATE_SCOPE_MISMATCH
    return None


def _identity_mismatch(intent: IntentSnapshot, mandate: MandateView) -> bool:
    return (
        mandate.account != intent.account
        or mandate.sleeve_id != intent.sleeve_id
        or mandate.strategy != intent.strategy
        or mandate.strategy_code_sha != intent.strategy_code_sha
        or mandate.mode != intent.execution_mode
        or intent.symbol not in mandate.allowed_symbols
    )


def evaluate_operator_exit(
    intent: IntentSnapshot,
    *,
    quantity: Decimal,
    facts: UpbitFacts,
    reads: AccountReads,
) -> Refusal | None:
    """S6: the one judgement of an operator exit, wherever it is asked.

    ``store_intent``, the child reservation and the pre-send hook all call this,
    with the quantity each is about to authorize. It replaces the sleeve state
    check and the mandate's expiry and revocation: a paused or stopped sleeve,
    or a mandate that has ended, must not strand a position. Everything that
    protects the account still applies.
    """
    exit_row = facts.operator_exit
    mandate = facts.mandate
    if (
        intent.origin != "operator_exit"
        or exit_row is None
        or exit_row.exit_id != intent.operator_exit_id
        or exit_row.mandate_id != intent.mandate_id
        or exit_row.symbol != intent.symbol
        or intent.side != "sell"
        or facts.now >= exit_row.expires_at
        or intent.expires_at > exit_row.expires_at
        or intent.max_quantity_decimal is None
        or intent.max_quantity_decimal > exit_row.max_quantity
        or quantity > exit_row.max_quantity
    ):
        return Refusal.OPERATOR_EXIT_INVALID
    # The mandate still names the account, sleeve and mode; only its time
    # limits and its revocation are set aside.
    if mandate is None or mandate.mandate_id != intent.mandate_id:
        return Refusal.MANDATE_MISSING
    if _identity_mismatch(intent, mandate):
        return Refusal.MANDATE_SCOPE_MISMATCH
    if reads.kill_mode is not KillMode.ACTIVE:
        return Refusal.SUBMISSIONS_KILLED
    if intent.execution_mode == "live" and not reads.live_enabled:
        return Refusal.UPBIT_LIVE_DISABLED
    if facts.symbol.handoff_active:
        return Refusal.EXIT_HANDOFF_ACTIVE
    return _sell_position_refusal(intent, quantity=quantity, facts=facts)


def _sell_position_refusal(
    intent: IntentSnapshot, *, quantity: Decimal, facts: UpbitFacts
) -> Refusal | None:
    """S2 and I9: one live sell per sleeve and symbol, never above proven holdings."""
    others = facts.symbol.open_sell_intents - {intent.intent_id}
    if others:
        return Refusal.OPEN_SELL_INTENT_EXISTS
    holdings = facts.symbol.holdings
    if holdings is None:
        return Refusal.SLEEVE_HOLDINGS_UNMEASURED
    if quantity > holdings:
        return Refusal.INVENTORY_CONSTRAINT_EXCEEDED
    return None


@dataclass(frozen=True, slots=True)
class _SleeveVerdict:
    refusal: Refusal | None
    stop_sleeve: SleeveVersionView | None = None


def evaluate_shadow_sleeve(
    *,
    version: SleeveVersionView | None,
    book: SleeveBook | None,
    order_buy_notional: Decimal | None,
    places_buy: bool,
) -> Refusal | None:
    """B17: budget, loss and state of a shadow sleeve, from its shadow book.

    The same order of checks as ``strategy_sleeves.gate.evaluate_sleeve_gate``,
    but the sleeve must be in ``shadow`` and nothing is ever written. The answer
    only ever leads to a ``shadowed`` row: the forwarder is chosen by the
    intent's ``execution_mode``, not by this verdict.
    """
    if version is None:
        return Refusal.SLEEVE_UNREADABLE
    if version.state != SleeveState.SHADOW:
        return Refusal.SLEEVE_NOT_SHADOW
    if book is None:
        return Refusal.SLEEVE_UNREADABLE
    loss = book.loss
    if loss is None:
        return Refusal.SLEEVE_PNL_UNMEASURED
    if loss >= version.loss_limit:
        return Refusal.SLEEVE_LOSS_LIMIT_REACHED
    if not places_buy:
        return None
    if book.used is None or order_buy_notional is None:
        return Refusal.SLEEVE_USED_UNMEASURED
    if book.used + order_buy_notional > version.budget_cap:
        return Refusal.SLEEVE_BUDGET_EXCEEDED
    return None


def _sleeve_state_refusal(intent: IntentSnapshot, sleeve: SleeveView) -> Refusal | None:
    """S4: a live intent trades only on a live sleeve, a shadow one on a shadow sleeve."""
    version = sleeve.version
    if version is None:
        return Refusal.SLEEVE_UNREADABLE
    if intent.execution_mode == "live":
        return None if version.state == SleeveState.LIVE else Refusal.SLEEVE_NOT_LIVE
    if intent.execution_mode == "shadow":
        return (
            None if version.state == SleeveState.SHADOW else Refusal.SLEEVE_NOT_SHADOW
        )
    return Refusal.SLEEVE_UNREADABLE


def _buy_sleeve_verdict(
    intent: IntentSnapshot, *, notional: Decimal, sleeve: SleeveView
) -> _SleeveVerdict:
    """I4 and I8 for a buy: budget, loss limit, and unknown is a refusal."""
    order_notional = notional + sleeve.reserved_buy_notional
    if intent.execution_mode == "shadow":
        return _SleeveVerdict(
            evaluate_shadow_sleeve(
                version=sleeve.version,
                book=sleeve.book,
                order_buy_notional=order_notional,
                places_buy=True,
            )
        )
    verdict = evaluate_sleeve_gate(
        version=sleeve.version,
        book=sleeve.book,
        order_buy_notional=order_notional,
        places_buy=True,
    )
    if verdict.allowed:
        return _SleeveVerdict(None)
    reason = Refusal.SLEEVE_UNREADABLE
    if verdict.reason is not None:
        reason = Refusal(verdict.reason.value)
    stop = sleeve.version if verdict.stop_sleeve else None
    return _SleeveVerdict(reason, stop)


def check_storable(
    intent: IntentSnapshot, *, facts: UpbitFacts, reads: AccountReads
) -> UpbitDecision:
    """Whether ``store_intent`` may store this v2 intent.

    ``intent.created_at`` must be the time it is being stored.
    """
    if intent.max_quantity_decimal is None:
        return UpbitDecision(refusal=Refusal.CHILD_SHAPE_INVALID_FOR_ACCOUNT)
    quantity = intent.max_quantity_decimal
    if intent.origin == "operator_exit":
        return UpbitDecision(
            refusal=evaluate_operator_exit(
                intent, quantity=quantity, facts=facts, reads=reads
            )
        )
    # S5 (b): no new intent, buy or sell, while the symbol is handed off.
    if facts.symbol.handoff_active:
        return UpbitDecision(refusal=Refusal.EXIT_HANDOFF_ACTIVE)
    refusal = mandate_refusal(intent, facts.mandate, now=facts.now)
    if refusal is not None:
        return UpbitDecision(refusal=refusal)
    # B5: an order whose end is not proven keeps the symbol reserved.
    if facts.symbol.unresolved_prior_order:
        return UpbitDecision(refusal=Refusal.UNRESOLVED_PRIOR_ORDER)
    refusal = _sleeve_state_refusal(intent, facts.sleeve)
    if refusal is not None:
        return UpbitDecision(refusal=refusal)
    if intent.side == "sell":
        return UpbitDecision(
            refusal=_sell_position_refusal(intent, quantity=quantity, facts=facts)
        )
    mandate = facts.mandate
    if mandate is None or facts.open_buy_intents + 1 > mandate.max_open_intents:
        return UpbitDecision(refusal=Refusal.MAX_OPEN_INTENTS_EXCEEDED)
    verdict = _buy_sleeve_verdict(
        intent, notional=Decimal(intent.max_notional_krw), sleeve=facts.sleeve
    )
    return UpbitDecision(refusal=verdict.refusal, stop_sleeve=verdict.stop_sleeve)


def _order_may_have_moved(
    target: ChildSnapshot, children: Sequence[ChildSnapshot]
) -> bool:
    return any(
        row.target_idempotency_key == target.idempotency_key
        and row.state not in NEVER_SENT_STATES
        for row in children
    )


def _decide_cancel(
    request: CancelChild, children: Sequence[ChildSnapshot]
) -> UpbitDecision:
    """The E1 cancel rules. A cancel reads no expiry, kill, cap, mandate or
    handoff: it only ever reduces exposure (I7, S5 b).
    """
    by_key = {child.idempotency_key: child for child in children}
    target = by_key.get(request.target_idempotency_key)
    if target is None or target.action != "submit":
        return UpbitDecision(refusal=Refusal.TARGET_NOT_FOUND)
    if target.state in NEVER_SENT_STATES or target.broker_order_id is None:
        return UpbitDecision(refusal=Refusal.TARGET_NOT_WORKING)
    if _order_may_have_moved(target, children):
        return UpbitDecision(refusal=Refusal.TARGET_SUPERSEDED)
    return UpbitDecision(target_broker_order_id=target.broker_order_id)


def shape_refusal(request: AnyChild) -> Refusal | None:
    """Design §3.3: the account decides the field set."""
    if isinstance(request, AmendChild):
        return Refusal.AMEND_UNSUPPORTED_FOR_ACCOUNT
    if not isinstance(request, UpbitSubmitChild | CancelChild):
        return Refusal.CHILD_SHAPE_INVALID_FOR_ACCOUNT
    if len(request.idempotency_key) > MAX_UPBIT_IDEMPOTENCY_KEY_LENGTH:
        # The key is the Upbit identifier and cannot be shortened or hashed.
        return Refusal.CHILD_SHAPE_INVALID_FOR_ACCOUNT
    return None


def _tick_refusal(limit_price_krw: int) -> Refusal | None:
    try:
        check_upbit_limit_price(limit_price_krw)
    except PriceTickUnsupported:
        return Refusal.PRICE_TICK_UNSUPPORTED
    except PriceOffTick:
        return Refusal.PRICE_OFF_TICK
    return None


def _caps_refusal(
    intent: IntentSnapshot,
    *,
    total_quantity: Decimal,
    total_notional: Decimal,
) -> Refusal | None:
    """I2 with exact decimals; B7 leaves a sell with a quantity cap only."""
    if (
        intent.max_quantity_decimal is None
        or intent.position_at_authorization_decimal is None
        or intent.min_position_after_decimal is None
        or intent.max_position_after_decimal is None
    ):
        return Refusal.QUANTITY_CAP_EXCEEDED
    if total_quantity > intent.max_quantity_decimal:
        return Refusal.QUANTITY_CAP_EXCEEDED
    if intent.side == "buy" and total_notional > Decimal(intent.max_notional_krw):
        return Refusal.NOTIONAL_CAP_EXCEEDED
    start = intent.position_at_authorization_decimal
    projected = (
        start + total_quantity if intent.side == "buy" else start - total_quantity
    )
    if not (
        intent.min_position_after_decimal
        <= projected
        <= intent.max_position_after_decimal
    ):
        return Refusal.INVENTORY_CONSTRAINT_EXCEEDED
    return None


def _loss_sell_decision(
    intent: IntentSnapshot, *, limit_price_krw: int, reads: AccountReads
) -> UpbitDecision | None:
    """I10, B9: a sell the loss-sell guard would refuse is never forwarded.

    A live refusal opens the S5 handoff, so the exit becomes a card. A shadow
    refusal is only recorded (B21): nothing in shadow may lead to a card that
    would sell the account's real coins.
    """
    avg = reads.avg_buy_price
    if avg is None or not avg.is_finite() or avg <= 0:
        return UpbitDecision(refusal=Refusal.AVG_PRICE_UNPROVEN)
    if Decimal(limit_price_krw) < loss_sell_floor(avg):
        return UpbitDecision(
            refusal=Refusal.LOSS_SELL_REQUIRES_CARD,
            open_handoff=intent.execution_mode == "live",
        )
    return None


def decide_upbit_child(
    request: AnyChild,
    *,
    intent: IntentSnapshot,
    children: Sequence[ChildSnapshot],
    facts: UpbitFacts,
    reads: AccountReads,
) -> UpbitDecision:
    """Refuse or allow one new child of an ``upbit_live`` intent."""
    if intent.account not in UPBIT_ACCOUNTS or intent.schema_version != 2:
        return _no_row(Refusal.CHILD_SHAPE_INVALID_FOR_ACCOUNT)
    shape = shape_refusal(request)
    if shape is not None:
        return _no_row(shape)
    # §8.2-4: with the live switch off this process calls no Upbit order
    # function at all, a cancel included. Like the env gate it leaves no row.
    if intent.execution_mode == "live" and not reads.live_enabled:
        return _no_row(Refusal.UPBIT_LIVE_DISABLED)
    if isinstance(request, CancelChild):
        return _decide_cancel(request, children)
    if not isinstance(request, UpbitSubmitChild):  # narrowed by shape_refusal
        return _no_row(Refusal.CHILD_SHAPE_INVALID_FOR_ACCOUNT)

    quantity = Decimal(request.quantity_decimal)
    price = request.limit_price_krw
    if facts.now >= intent.expires_at:
        return UpbitDecision(refusal=Refusal.AUTHORIZATION_EXPIRED)
    if reads.kill_mode is not KillMode.ACTIVE:
        return UpbitDecision(refusal=Refusal.SUBMISSIONS_KILLED)
    tick = _tick_refusal(price)
    if tick is not None:
        return UpbitDecision(refusal=tick)
    if not _price_within_bound(intent, price):
        return UpbitDecision(refusal=Refusal.PRICE_OUTSIDE_BOUND)

    sent_quantity, sent_notional = sent_exposure(children)
    total_quantity = sent_quantity + quantity
    total_notional = sent_notional + child_notional(quantity, price)

    if intent.origin == "operator_exit":
        refusal = evaluate_operator_exit(
            intent, quantity=total_quantity, facts=facts, reads=reads
        )
        if refusal is not None:
            return UpbitDecision(refusal=refusal)
    else:
        if facts.symbol.handoff_active:
            return UpbitDecision(refusal=Refusal.EXIT_HANDOFF_ACTIVE)
        refusal = mandate_refusal(intent, facts.mandate, now=facts.now)
        if refusal is not None:
            return UpbitDecision(refusal=refusal)
        refusal = _sleeve_state_refusal(intent, facts.sleeve)
        if refusal is not None:
            return UpbitDecision(refusal=refusal)
        if facts.symbol.unresolved_prior_order:
            return UpbitDecision(refusal=Refusal.UNRESOLVED_PRIOR_ORDER)

    refusal = _caps_refusal(
        intent, total_quantity=total_quantity, total_notional=total_notional
    )
    if refusal is not None:
        return UpbitDecision(refusal=refusal)

    if intent.side == "buy":
        if reads.cooldown_active:
            return UpbitDecision(refusal=Refusal.COOLDOWN_ACTIVE)
        verdict = _buy_sleeve_verdict(
            intent, notional=child_notional(quantity, price), sleeve=facts.sleeve
        )
        return UpbitDecision(refusal=verdict.refusal, stop_sleeve=verdict.stop_sleeve)

    if intent.origin != "operator_exit":
        refusal = _sell_position_refusal(intent, quantity=total_quantity, facts=facts)
        if refusal is not None:
            return UpbitDecision(refusal=refusal)
    loss_sell = _loss_sell_decision(intent, limit_price_krw=price, reads=reads)
    return loss_sell if loss_sell is not None else UpbitDecision()


def check_before_send(
    intent: IntentSnapshot,
    *,
    quantity: Decimal,
    facts: UpbitFacts,
    reads: AccountReads,
    configured_account_ref: str | None,
) -> Refusal | None:
    """Design §4.4, B12: the last look, immediately before the POST.

    ``facts`` and ``reads`` are read again at that moment. A refusal here is a
    proven non-send (B29): the hook raised before the request was written.
    """
    if not reads.live_enabled or intent.execution_mode != "live":
        return Refusal.UPBIT_LIVE_DISABLED
    if reads.kill_mode is not KillMode.ACTIVE:
        return Refusal.SUBMISSIONS_KILLED
    if facts.now >= intent.expires_at:
        return Refusal.AUTHORIZATION_EXPIRED
    mandate = facts.mandate
    if intent.origin == "operator_exit":
        refusal = evaluate_operator_exit(
            intent, quantity=quantity, facts=facts, reads=reads
        )
    else:
        refusal = mandate_refusal(intent, mandate, now=facts.now)
        if refusal is None and facts.symbol.handoff_active:
            refusal = Refusal.EXIT_HANDOFF_ACTIVE
        if refusal is None:
            refusal = _sleeve_state_refusal(intent, facts.sleeve)
    if refusal is not None:
        return refusal
    # B12: the order goes to the account the mandate was approved for, or nowhere.
    if (
        mandate is None
        or not mandate.account_ref
        or not configured_account_ref
        or mandate.account_ref != configured_account_ref
    ):
        return Refusal.ACCOUNT_MISMATCH
    return None
