"""Pure rules for exit plans and the mechanical loss-cut trigger (ROB-1460).

No database, broker, network, clock or settings. Every price is ``Decimal``.

The trigger answers one question from recorded facts: is this lot's price
actually falling through the stop that was recorded for it? A lot that is below
average cost but above its stop is not cut, and neither is a lot below its stop
whose close is rising. A missing or stale price never triggers.
"""

from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import date, datetime
from decimal import ROUND_CEILING, ROUND_FLOOR, Decimal, InvalidOperation
from typing import Any, Literal, NamedTuple

from app.core.symbol import to_db_symbol
from app.schemas.trading_policy import (
    MechanicalLossCutDefaultStop,
    MechanicalLossCutMarket,
)
from app.services.halt_detection import HaltBar, classify_bars

StopBasis = Literal["support", "pct_floor", "volatility"]
PlanEvent = Literal["entry", "add", "backfill"]

STOP_BASES: frozenset[str] = frozenset({"support", "pct_floor", "volatility"})
PLAN_EVENTS: frozenset[str] = frozenset({"entry", "add", "backfill"})
MAX_STOP_REASON_CHARS = 200
MAX_LADDER_LEVELS = 10

#: Closed vocabulary. Every lot that is not cut ends in exactly one of these.
SKIP_REASONS: frozenset[str] = frozenset(
    {
        "parking_instrument",
        "no_exit_plan",
        "price_missing",
        "price_stale",
        "halted_suspect",
        "position_unreadable",
        "not_underwater",
        "above_planned_stop",
        "floor_not_reached",
        "rising",
        "not_n_day_low",
        "trailing_drop_not_reached",
        "open_sell_unknown",
        "protected_unknown",
        "covered_by_open_sell",
        "below_one_unit",
        "already_cut_this_run",
        "cooldown",
    }
)

#: Closed verdicts for a buy measured against the plan that already exists.
ADD_VERDICTS: frozenset[str] = frozenset(
    {
        "on_ladder",
        "no_ladder",
        "above_next_level",
        "at_or_below_stop",
        "max_adds_reached",
        "size_exceeds_level",
        "same_proposal_tranche",
    }
)

_ONE_HUNDRED = Decimal("100")
_PCT_STEP = Decimal("0.0001")
_CRYPTO_QUANTITY_STEP = Decimal("0.00000001")
_US_PRICE_TICK = Decimal("0.01")
# Same bands as quote_parity_shadow.krx_tick_size, which imports a broker package
# the shadow path must not load. A test pins them together.
_KRX_PRICE_UNITS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("500000"), Decimal("1000")),
    (Decimal("200000"), Decimal("500")),
    (Decimal("50000"), Decimal("100")),
    (Decimal("20000"), Decimal("50")),
    (Decimal("5000"), Decimal("10")),
    (Decimal("2000"), Decimal("5")),
    (Decimal("0"), Decimal("1")),
)
# Same bands as brokers.upbit.orders.adjust_price_to_upbit_unit, which rounds to
# nearest and sits in the same kind of package. A test pins them together.
_UPBIT_PRICE_UNITS: tuple[tuple[Decimal, Decimal], ...] = (
    (Decimal("2000000"), Decimal("1000")),
    (Decimal("1000000"), Decimal("500")),
    (Decimal("500000"), Decimal("100")),
    (Decimal("100000"), Decimal("50")),
    (Decimal("10000"), Decimal("10")),
    (Decimal("1000"), Decimal("5")),
    (Decimal("100"), Decimal("1")),
    (Decimal("10"), Decimal("0.1")),
    (Decimal("1"), Decimal("0.01")),
    (Decimal("0.1"), Decimal("0.001")),
    (Decimal("0.01"), Decimal("0.0001")),
    (Decimal("0"), Decimal("0.00001")),
)


class ExitPlanInputError(ValueError):
    """A buyer-supplied exit plan broke the contract. Carries a closed code."""


class LotKey(NamedTuple):
    account_mode: str
    market: str  # equity_kr | equity_us | crypto
    symbol: str


@dataclass(frozen=True, slots=True)
class Bar:
    """One completed daily bar, oldest first in any sequence."""

    session: date
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal | None = None
    open: Decimal | None = None


@dataclass(frozen=True, slots=True)
class HeldLot:
    key: LotKey
    quantity: Decimal
    average_cost: Decimal
    currency: str
    position_source: str = "execution_ledger_fifo"


@dataclass(frozen=True, slots=True)
class AddLevel:
    price: Decimal
    quantity: Decimal


@dataclass(frozen=True, slots=True)
class ExitPlanInput:
    """A stop and optional ladder stated by whoever placed the buy."""

    stop_price: Decimal
    stop_basis: StopBasis
    stop_reason: str | None = None
    add_ladder: tuple[AddLevel, ...] = ()
    max_adds: int = 0


@dataclass(frozen=True, slots=True)
class PlanDraft:
    """One version of a plan before it is written."""

    key: LotKey
    event: PlanEvent
    stop_price: Decimal
    stop_basis: StopBasis
    stop_reason: str | None
    reference_price: Decimal
    average_cost: Decimal
    quantity: Decimal
    add_ladder: tuple[AddLevel, ...]
    max_adds: int
    adds_used: int
    add_verdict: str | None
    backfilled: bool
    proposal_id: Any = None
    rung_index: int | None = None


@dataclass(frozen=True, slots=True)
class ExitPlan(PlanDraft):
    """A written plan version."""

    id: int = 0
    version: int = 0
    created_at: datetime | None = None


@dataclass(frozen=True, slots=True)
class CutDecision:
    """A would-sell. Written as a row; never a broker instruction by itself."""

    key: LotKey
    plan_id: int
    plan_version: int
    backfilled: bool
    trigger_family: str
    trigger_key: str
    session: date
    close: Decimal
    stop_price: Decimal
    average_cost: Decimal
    unrealized_pct: Decimal
    held_quantity: Decimal
    open_sell_quantity: Decimal
    protected_quantity: Decimal
    intended_quantity: Decimal
    intended_limit_price: Decimal
    intended_notional: Decimal
    currency: str
    episode_start: date | None
    details: dict[str, str]


@dataclass(frozen=True, slots=True)
class PriorDecision:
    """The lot's latest recorded decision, and how many share its plan and stop."""

    session: date
    plan_version: int
    stop_price: Decimal
    cuts_on_this_stop: int


@dataclass(frozen=True, slots=True)
class Skip:
    reason: str
    details: dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.reason not in SKIP_REASONS:
            raise ValueError("unknown mechanical loss-cut skip reason")


def text(value: Decimal) -> str:
    rendered = format(value.normalize(), "f")
    return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered


def is_parking_instrument(symbol: Any, market: Any) -> bool:
    """Parking instruments are cash, on any account that holds them.

    Deliberately wider than ``parking_scope``: one of these held on an account
    outside its authorization tuple is still a cash equivalent, not a lot to cut.
    """
    # Imported here: importing the order_proposals package from this module's
    # top level would cycle through the classifier that reads this module.
    from app.services.order_proposals.parking_allowlist import (
        PARKING_ALLOWLIST_SCOPES,
    )

    if not isinstance(symbol, str) or not symbol.strip().isascii():
        return False
    canonical = to_db_symbol(symbol.strip().upper())
    return any(
        scope.symbol == canonical and scope.market == market
        for scope in PARKING_ALLOWLIST_SCOPES
    )


def _positive_decimal(value: Any, code: str) -> Decimal:
    if type(value) not in (str, int, Decimal):
        raise ExitPlanInputError(code)
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ExitPlanInputError(code) from exc
    if not parsed.is_finite() or parsed <= 0:
        raise ExitPlanInputError(code)
    return parsed


def parse_exit_plan_input(raw: Any) -> ExitPlanInput:
    """Parse the ``exit_plan`` a buyer sends, failing on anything unexpected."""
    if type(raw) is not dict:
        raise ExitPlanInputError("exit_plan_not_a_mapping")
    allowed = {"stop_price", "stop_basis", "stop_reason", "add_ladder", "max_adds"}
    if set(raw) - allowed:
        raise ExitPlanInputError("exit_plan_unknown_field")
    stop_price = _positive_decimal(raw.get("stop_price"), "exit_plan_stop_invalid")
    stop_basis = raw.get("stop_basis")
    if stop_basis not in STOP_BASES:
        raise ExitPlanInputError("exit_plan_stop_basis_invalid")
    stop_reason = raw.get("stop_reason")
    if stop_reason is not None and (
        type(stop_reason) is not str
        or not stop_reason.strip()
        or len(stop_reason) > MAX_STOP_REASON_CHARS
    ):
        raise ExitPlanInputError("exit_plan_stop_reason_invalid")

    raw_ladder = raw.get("add_ladder", [])
    if type(raw_ladder) is not list or len(raw_ladder) > MAX_LADDER_LEVELS:
        raise ExitPlanInputError("exit_plan_ladder_invalid")
    levels: list[AddLevel] = []
    for item in raw_ladder:
        if type(item) is not dict or set(item) != {"price", "quantity"}:
            raise ExitPlanInputError("exit_plan_ladder_invalid")
        levels.append(
            AddLevel(
                price=_positive_decimal(item["price"], "exit_plan_ladder_invalid"),
                quantity=_positive_decimal(
                    item["quantity"], "exit_plan_ladder_invalid"
                ),
            )
        )
    prices = [level.price for level in levels]
    if prices != sorted(prices, reverse=True) or len(set(prices)) != len(prices):
        raise ExitPlanInputError("exit_plan_ladder_not_descending")
    if any(price <= stop_price for price in prices):
        raise ExitPlanInputError("exit_plan_ladder_not_above_stop")

    max_adds = raw.get("max_adds", len(levels))
    if type(max_adds) is not int or not 0 <= max_adds <= len(levels):
        raise ExitPlanInputError("exit_plan_max_adds_invalid")
    return ExitPlanInput(
        stop_price=stop_price,
        stop_basis=stop_basis,
        stop_reason=stop_reason,
        add_ladder=tuple(levels),
        max_adds=max_adds,
    )


def _average_true_range(bars: list[Bar], sessions: int) -> Decimal | None:
    if len(bars) < sessions + 1:
        return None
    window = bars[-(sessions + 1) :]
    ranges = [
        max(
            current.high - current.low,
            abs(current.high - previous.close),
            abs(current.low - previous.close),
        )
        for previous, current in zip(window, window[1:], strict=False)
    ]
    return sum(ranges, Decimal("0")) / Decimal(len(ranges))


def default_stop(
    params: MechanicalLossCutDefaultStop,
    *,
    reference_price: Decimal,
    bars: list[Bar],
) -> tuple[Decimal, StopBasis, str]:
    """Compute a stop from policy defaults.

    A support or volatility stop that cannot be computed, or that would sit at
    or above the reference, falls back to the percent floor. So does one wider
    than the floor: ``floor_pct`` is the widest stop this function can return.
    """
    floor = reference_price * (
        Decimal("1") - Decimal(str(params.floor_pct)) / _ONE_HUNDRED
    )
    floor_reason = f"-{params.floor_pct}% from reference"
    if params.basis == "support":
        window = bars[-params.support_lookback_sessions :]
        if window:
            support = min(bar.low for bar in window)
            candidate = support * (
                Decimal("1") - Decimal(str(params.support_buffer_pct)) / _ONE_HUNDRED
            )
            if floor < candidate < reference_price:
                return (
                    candidate,
                    "support",
                    f"{params.support_lookback_sessions}-session low less "
                    f"{params.support_buffer_pct}%",
                )
    elif params.basis == "volatility":
        atr = _average_true_range(bars, params.atr_sessions)
        if atr is not None:
            candidate = reference_price - atr * Decimal(str(params.atr_multiple))
            if floor < candidate < reference_price:
                return (
                    candidate,
                    "volatility",
                    f"{params.atr_multiple} x {params.atr_sessions}-session ATR",
                )
    return floor, "pct_floor", floor_reason


def evaluate_add_against_plan(
    plan: ExitPlan,
    *,
    price: Decimal,
    quantity: Decimal,
    proposal_id: Any = None,
) -> str:
    """Measure a buy against the plan that already exists. Advisory verdict."""
    if proposal_id is not None and proposal_id == plan.proposal_id:
        return "same_proposal_tranche"
    if price <= plan.stop_price:
        return "at_or_below_stop"
    if not plan.add_ladder:
        return "no_ladder"
    if plan.adds_used >= plan.max_adds or plan.adds_used >= len(plan.add_ladder):
        return "max_adds_reached"
    level = plan.add_ladder[plan.adds_used]
    if price > level.price:
        return "above_next_level"
    if quantity > level.quantity:
        return "size_exceeds_level"
    return "on_ladder"


def entry_plan(
    *,
    key: LotKey,
    params: MechanicalLossCutMarket,
    price: Decimal,
    quantity: Decimal,
    bars: list[Bar],
    stated: ExitPlanInput | None,
    proposal_id: Any = None,
    rung_index: int | None = None,
) -> PlanDraft:
    if stated is not None:
        stop, basis, reason = stated.stop_price, stated.stop_basis, stated.stop_reason
    else:
        stop, basis, reason = default_stop(
            params.default_stop, reference_price=price, bars=bars
        )
    return PlanDraft(
        key=key,
        event="entry",
        stop_price=stop,
        stop_basis=basis,
        stop_reason=reason,
        reference_price=price,
        average_cost=price,
        quantity=quantity,
        add_ladder=stated.add_ladder if stated is not None else (),
        max_adds=stated.max_adds if stated is not None else 0,
        adds_used=0,
        add_verdict=None,
        backfilled=False,
        proposal_id=proposal_id,
        rung_index=rung_index,
    )


def add_plan(
    *,
    previous: ExitPlan,
    params: MechanicalLossCutMarket,
    held_quantity: Decimal | None,
    held_average_cost: Decimal | None,
    price: Decimal,
    quantity: Decimal,
    stated: ExitPlanInput | None,
    proposal_id: Any = None,
    rung_index: int | None = None,
) -> PlanDraft:
    """The next plan version after an add.

    Average cost is the planned value at acceptance: the fill may differ, and
    the evaluator always measures against the position it reads at that time.
    """
    verdict = evaluate_add_against_plan(
        previous, price=price, quantity=quantity, proposal_id=proposal_id
    )
    base_quantity = held_quantity if held_quantity is not None else previous.quantity
    base_cost = (
        held_average_cost if held_average_cost is not None else previous.average_cost
    )
    total = base_quantity + quantity
    average = (base_quantity * base_cost + quantity * price) / total
    adds_used = previous.adds_used + (0 if verdict == "same_proposal_tranche" else 1)
    if stated is not None:
        stop, basis, reason = stated.stop_price, stated.stop_basis, stated.stop_reason
        ladder, max_adds = stated.add_ladder, stated.max_adds
        adds_used = 0
    else:
        ladder, max_adds = previous.add_ladder, previous.max_adds
        if params.stop_after_add == "recompute_from_average":
            stop, basis, reason = default_stop(
                params.default_stop, reference_price=average, bars=[]
            )
        else:
            stop, basis, reason = (
                previous.stop_price,
                previous.stop_basis,
                previous.stop_reason,
            )
    return PlanDraft(
        key=previous.key,
        event="add",
        stop_price=stop,
        stop_basis=basis,
        stop_reason=reason,
        reference_price=price,
        average_cost=average,
        quantity=total,
        add_ladder=ladder,
        max_adds=max_adds,
        adds_used=adds_used,
        add_verdict=verdict,
        backfilled=False,
        proposal_id=proposal_id,
        rung_index=rung_index,
    )


def backfill_plan(
    *,
    lot: HeldLot,
    params: MechanicalLossCutMarket,
    bars: list[Bar],
) -> PlanDraft:
    """A plan for a lot that was bought before plans existed.

    Measured from today's average cost, so an old loser can already be below
    this stop. That is why ``backfilled`` travels with every decision: these are
    reported apart, and the live path refuses them unless policy says otherwise.
    """
    stop, basis, reason = default_stop(
        params.default_stop, reference_price=lot.average_cost, bars=bars
    )
    return PlanDraft(
        key=lot.key,
        event="backfill",
        stop_price=stop,
        stop_basis=basis,
        stop_reason=reason,
        reference_price=lot.average_cost,
        average_cost=lot.average_cost,
        quantity=lot.quantity,
        add_ladder=(),
        max_adds=0,
        adds_used=0,
        add_verdict=None,
        backfilled=True,
    )


def _tick_for(market: str, price: Decimal) -> Decimal:
    if market == "equity_kr":
        return next(unit for floor, unit in _KRX_PRICE_UNITS if price >= floor)
    if market == "equity_us":
        return _US_PRICE_TICK
    return next(unit for floor, unit in _UPBIT_PRICE_UNITS if price >= floor)


def bounded_limit_price(
    market: str, *, price: Decimal, slippage_pct: Decimal
) -> Decimal:
    """The lowest valid limit inside the slippage band below ``price``."""
    raw = price * (Decimal("1") - slippage_pct / _ONE_HUNDRED)
    tick = _tick_for(market, raw)
    return (raw / tick).to_integral_value(rounding=ROUND_CEILING) * tick


def _floor_quantity(market: str, quantity: Decimal) -> Decimal:
    if market == "crypto":
        return quantity.quantize(_CRYPTO_QUANTITY_STEP, rounding=ROUND_FLOOR)
    return quantity.to_integral_value(rounding=ROUND_FLOOR)


def episode_start(bars: list[Bar], stop: Decimal) -> date | None:
    """The first session of the unbroken run of closes below ``stop``.

    ``None`` when every bar read is below the stop: the run started before this
    window, so the oldest bar is not its start. Using that bar would change the
    trigger key each day as the window rolls forward and fire the trigger again.
    """
    start = None
    for bar in reversed(bars):
        if bar.close >= stop:
            return start
        start = bar.session
    return None


def trigger_key(plan: ExitPlan, run_number: int) -> str:
    """One lot gets one cut per plan version, stop level and below-stop run.

    A run number, not a date. Any date read from the bar window would change once
    that date rolled off the window, and the same run would then fire again.
    """
    return f"v{plan.version}:{text(plan.stop_price)}:r{run_number}"


def evaluate_lot(
    *,
    lot: HeldLot,
    plan: ExitPlan | None,
    bars: list[Bar],
    params: MechanicalLossCutMarket,
    price_fresh: bool,
    open_sell_quantity: Decimal | None,
    protected_quantity: Decimal | None,
    prior_decision: PriorDecision | None,
) -> CutDecision | Skip:
    """Decide one lot. Every path that lacks evidence ends in a ``Skip``."""
    market = lot.key.market
    if is_parking_instrument(lot.key.symbol, market):
        return Skip("parking_instrument")
    if plan is None:
        return Skip("no_exit_plan")
    if lot.quantity <= 0 or lot.average_cost <= 0:
        return Skip("position_unreadable")
    if not bars:
        return Skip("price_missing")
    if not price_fresh:
        return Skip("price_stale", {"last_session": bars[-1].session.isoformat()})
    halt = classify_bars(
        [
            HaltBar(
                close=bar.close,
                high=bar.high,
                low=bar.low,
                volume=bar.volume,
                open=bar.open,
            )
            for bar in bars
        ]
    )
    if halt.suspected:
        return Skip("halted_suspect", {"frozen_sessions": str(halt.frozen_sessions)})

    last = bars[-1]
    close = last.close
    trigger = params.trigger
    unrealized = (
        (close - lot.average_cost) / lot.average_cost * _ONE_HUNDRED
    ).quantize(_PCT_STEP)
    facts = {
        "close": text(close),
        "stop": text(plan.stop_price),
        "unrealized_pct": text(unrealized),
    }
    if close >= lot.average_cost:
        return Skip("not_underwater", facts)
    if close >= plan.stop_price:
        return Skip("above_planned_stop", facts)
    if unrealized > Decimal(str(trigger.unrealized_floor_pct)):
        return Skip("floor_not_reached", facts)

    lookback = trigger.rising_lookback_sessions
    if len(bars) <= lookback:
        # A rising check that cannot be made is not a pass.
        return Skip("price_missing", {"bars": str(len(bars))})
    if close > bars[-1 - lookback].close:
        return Skip(
            "rising", {**facts, "compared_close": text(bars[-1 - lookback].close)}
        )

    family_details: dict[str, str] = {}
    if trigger.family == "plan_stop_and_n_day_low":
        prior = bars[-trigger.n_day_low_sessions - 1 : -1]
        if len(prior) < trigger.n_day_low_sessions:
            return Skip("price_missing", {"bars": str(len(bars))})
        prior_low = min(bar.close for bar in prior)
        if close > prior_low:
            return Skip("not_n_day_low", {**facts, "prior_low": text(prior_low)})
        family_details["prior_low_close"] = text(prior_low)
    elif trigger.family == "plan_stop_and_trailing_drop":
        window = bars[-trigger.trailing_lookback_sessions :]
        high = max(bar.high for bar in window)
        drop = ((high - close) / high * _ONE_HUNDRED).quantize(_PCT_STEP)
        if drop < Decimal(str(trigger.trailing_drop_pct)):
            return Skip("trailing_drop_not_reached", {**facts, "drop_pct": text(drop)})
        family_details.update(recent_high=text(high), drop_pct=text(drop))

    if open_sell_quantity is None or open_sell_quantity < 0:
        return Skip("open_sell_unknown")
    if protected_quantity is None or protected_quantity < 0:
        return Skip("protected_unknown")
    sellable = lot.quantity - open_sell_quantity - protected_quantity
    if sellable <= 0:
        return Skip(
            "covered_by_open_sell",
            {
                "held": text(lot.quantity),
                "open_sell": text(open_sell_quantity),
                "protected": text(protected_quantity),
            },
        )
    capped = lot.quantity * Decimal(str(params.order.max_lot_fraction))
    intended = _floor_quantity(market, min(sellable, capped))
    if intended <= 0:
        return Skip("below_one_unit", {"capped": text(capped)})

    run_number = 1
    if prior_decision is not None:
        since = [bar for bar in bars if bar.session > prior_decision.session]
        if (
            prior_decision.plan_version == plan.version
            and prior_decision.stop_price == plan.stop_price
        ):
            # Only a close back at or above the stop ends a run. With no such
            # close in the bars read, this is still the run already cut.
            if not any(bar.close >= plan.stop_price for bar in since):
                return Skip(
                    "already_cut_this_run",
                    {"last_decision": prior_decision.session.isoformat()},
                )
            run_number = prior_decision.cuts_on_this_stop + 1
        if len(since) < params.cooldown_sessions:
            return Skip("cooldown", {"sessions_since_last": str(len(since))})

    limit = bounded_limit_price(
        market,
        price=close,
        slippage_pct=Decimal(str(params.order.max_slippage_pct)),
    )
    return CutDecision(
        key=lot.key,
        plan_id=plan.id,
        plan_version=plan.version,
        backfilled=plan.backfilled,
        trigger_family=trigger.family,
        trigger_key=trigger_key(plan, run_number),
        session=last.session,
        close=close,
        stop_price=plan.stop_price,
        average_cost=lot.average_cost,
        unrealized_pct=unrealized,
        held_quantity=lot.quantity,
        open_sell_quantity=open_sell_quantity,
        protected_quantity=protected_quantity,
        intended_quantity=intended,
        intended_limit_price=limit,
        intended_notional=limit * intended,
        currency=lot.currency,
        episode_start=episode_start(bars, plan.stop_price),
        details={**facts, **family_details, "position_source": lot.position_source},
    )


def with_fresh_limit(
    decision: CutDecision, *, price: Decimal, params: MechanicalLossCutMarket
) -> CutDecision:
    """Re-price a decision from a fresh quote for the live path."""
    limit = bounded_limit_price(
        decision.key.market,
        price=price,
        slippage_pct=Decimal(str(params.order.max_slippage_pct)),
    )
    return replace(
        decision,
        intended_limit_price=limit,
        intended_notional=limit * decision.intended_quantity,
    )
