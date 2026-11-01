"""Daily portfolio simulator that speaks the evaluation contract.

The KR adapter of the contract wraps the D3 engine, which ranks its own signals
and carries no volume, so it can run neither a custom candidate list nor
``VolumeShare``. This simulator takes a policy's candidate list and emits the
same contract rows (``Decision`` .. ``Outcome``, ``FillModelResult``,
``EvaluationReport``), asking the contract's fill models for every fill through
the same daily ``FillContext`` the adapter builds, plus window volume.

Frame shared by every policy:

* entry orders go in pre-open at 08:30 KST of the session after ``t``, at most
  ``MAX_NEW_PER_SESSION`` new entries per session, one position per listing;
* a resting entry is a DAY child order per session, for ``rest_sessions``;
* exits are marketable closing-auction sells at 15:20 on the first valid bar at
  or after the exit session; volume share can leave a remainder for later;
* cash earns nothing and proceeds are usable next session (no T+2 model);
* a listing that ends is settled on the next calendar session at its last close
  (``terminal_writeoff`` zeroes the listed payout semantics for the stress run).
"""

from __future__ import annotations

from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

import numpy as np

from research.eval_contract.contract import (
    CostModel,
    Decision,
    DecisionAction,
    EvaluationReport,
    FillModelResult,
    FillRow,
    Intent,
    Market,
    OrderSpec,
    Outcome,
    Provenance,
    RegistryRef,
    Side,
    sha256_canonical,
)
from research.eval_contract.fill_models import (
    ConservativeThrough,
    EmpiricalFillModel,
    EmpiricalModelNotFitted,
    FillContext,
    FillModel,
    FillModelUnavailable,
    OptimisticTouch,
    VolumeShare,
    params_sha256,
)
from research.eval_contract.outcomes import buy_markouts
from research.kr_boosting.config import (
    CAPITAL,
    FEE_RATE,
    MAX_NEW_PER_SESSION,
    ORDER_NOTIONAL,
    PARTICIPATION,
    SELL_FLOOR,
)
from research.kr_boosting.panel import STATE_VALID, Panel
from research.kr_boosting.policies import Candidate, Plan
from research.kr_corpus.d3_engine.tick import TickTable

KST = ZoneInfo("Asia/Seoul")
PRE_OPEN = time(8, 30)
CLOSE_AUCTION = time(15, 20)
SESSION_CLOSE = time(15, 30)
BAR_PUBLISHED = time(16, 0)
VENUE = "krx"
_BELOW = Decimal("0.0001")
_MARKOUT_SPAN = 20
WRITE_OFF_SEMANTICS = frozenset({"residual_after_liquidation_trading", "ambiguous"})


def fill_models() -> tuple[FillModel, ...]:
    return (
        OptimisticTouch(),
        ConservativeThrough(),
        VolumeShare(participation=PARTICIPATION),
        EmpiricalFillModel(),
    )


def _at(session: date, moment: time) -> datetime:
    return datetime.combine(session, moment, tzinfo=KST)


def _dec(value: float) -> Decimal:
    return Decimal(int(value))


@dataclass(slots=True)
class _Working:
    cand: Candidate
    decision_id: str
    first_session: int
    last_session: int


@dataclass(slots=True)
class _Position:
    li: int
    qty: int
    exit_idx: int


@dataclass(slots=True)
class SimOutput:
    result: FillModelResult
    nav: np.ndarray  # one value per simulated session
    sessions: list[date]
    traded_gross: Decimal
    deployed: np.ndarray  # share of NAV in positions
    writeoffs: int = 0
    unresolved_at_end: int = 0
    notes: list[str] = field(default_factory=list)


def _tick(ticks: TickTable, side: Side, limit: Decimal) -> Decimal:
    """Same rule as the KR adapter: a buy goes through to the lower band's tick."""

    if side is Side.SELL:
        return ticks.band_for(limit).tick
    return ticks.band_for(limit - _BELOW).tick


def daily_context(
    side: Side,
    limit: Decimal,
    qty: int,
    tick: Decimal,
    *,
    open_price: float,
    extreme: float,
    volume: float,
) -> FillContext:
    """A DAY order resting from before the open: the adapter's daily context."""

    return FillContext(
        side=side,
        limit=limit,
        quantity=Decimal(qty),
        tick=tick,
        optimistic_extreme=_dec(extreme),
        conservative_extreme=_dec(extreme),
        first_price=_dec(open_price),
        optimistic_window_volume=Decimal(int(volume)),
    )


def closing_auction_context(
    limit: Decimal, qty: int, tick: Decimal, *, close: float, volume: float
) -> FillContext:
    """A marketable sell into the closing auction trades only at the close."""

    price = _dec(close)
    return FillContext(
        side=Side.SELL,
        limit=limit,
        quantity=Decimal(qty),
        tick=tick,
        optimistic_extreme=price,
        conservative_extreme=price,
        first_price=price,
        optimistic_window_volume=Decimal(int(volume)),
    )


def _params(model: FillModel) -> str:
    return params_sha256(model)


def simulate(
    panel: Panel,
    plan: Plan,
    model: FillModel,
    *,
    ticks: TickTable,
    hypothesis_id: str,
    first_session: int,
    last_session: int,
    fee_rate: Decimal = FEE_RATE,
    terminal_writeoff: Collection[str] = (),
    capital: Decimal = CAPITAL,
) -> SimOutput:
    """Run one policy under one fill model across ``first_session..last_session``.

    ``plan`` is keyed by decision session ``t``; its orders reach the market at
    ``t+1``. An unavailable fill model yields ``status="unavailable"``, no numbers.
    """

    if first_session < 1:
        raise ValueError("first_session needs a prior session to date its data")
    costs = CostModel("KRW", fee_rate, fee_rate)
    cal = panel.calendar
    n_sessions = last_session - first_session + 1

    def unavailable(exc: Exception) -> SimOutput:
        return SimOutput(
            FillModelResult(
                fill_model_id=model.model_id,
                params_sha256=_params(model),
                status="unavailable",
                unavailable_reason=f"{type(exc).__name__}: {exc}",
            ),
            np.array([]),
            [],
            Decimal(0),
            np.array([]),
        )

    if isinstance(model, EmpiricalFillModel):
        # Refuse up front: a plan with no orders would otherwise never ask it.
        return unavailable(EmpiricalModelNotFitted("no fitted empirical model in v1"))

    decisions: list[Decision] = []
    intents: list[Intent] = []
    orders: list[OrderSpec] = []
    fills: list[FillRow] = []
    outcomes: list[Outcome] = []

    cash = capital
    positions: dict[int, _Position] = {}
    working: dict[int, _Working] = {}
    traded = Decimal(0)
    marks: dict[int, Decimal] = {}
    nav = np.zeros(n_sessions)
    deployed = np.zeros(n_sessions)
    writeoffs = 0
    order_seq = 0

    def next_order_id(session: int, li: int, side: Side) -> str:
        nonlocal order_seq
        order_seq += 1
        return f"{hypothesis_id}:{cal[session]}:{panel.symbols[li]}:{side.value}:{order_seq}"

    def add_decision(
        decision_id: str,
        session: int,
        li: int,
        side: Side,
        moment: time,
        valid_until: datetime,
        notional: Decimal,
    ) -> None:
        action = DecisionAction.ENTER if side is Side.BUY else DecisionAction.EXIT
        decided = _at(cal[session], moment)
        decisions.append(
            Decision(
                decision_id=decision_id,
                hypothesis_id=hypothesis_id,
                market=Market.KR,
                symbol=panel.symbols[li],
                action=action,
                decided_at=decided,
                data_cutoff=_at(cal[session - 1], BAR_PUBLISHED),
            )
        )
        intents.append(
            Intent(
                intent_id=decision_id,
                decision_id=decision_id,
                side=side,
                max_notional=notional,
                valid_until=valid_until,
            )
        )

    def book_fill(
        order: OrderSpec,
        session: int,
        qty: Decimal,
        price: Decimal,
        li: int,
        seq: int = 0,
    ) -> None:
        nonlocal traded
        gross = qty * price
        rate = fee_rate
        fills.append(
            FillRow(
                venue=VENUE,
                instrument_type="equity_kr",
                symbol=panel.symbols[li],
                raw_symbol=panel.symbols[li],
                side=order.side,
                broker_order_id=order.order_id,
                filled_qty=qty,
                filled_price=price,
                filled_notional=gross,
                fee_amount=gross * rate,
                fee_currency="KRW",
                filled_at=_at(cal[session], SESSION_CLOSE),
                currency="KRW",
                correlation_id=order.intent_id,
                source=f"simulated:{model.model_id}",
                fill_seq=seq,
            )
        )
        traded += gross

    try:
        for offset, s in enumerate(range(first_session, last_session + 1)):
            session = cal[s]

            # Terminal settlement before the open: the listing has ended.
            for li in sorted(positions):
                pos = positions[li]
                if panel.terminal_idx[li] < 0 or s <= panel.terminal_idx[li]:
                    continue
                raw_close = panel.close[li, panel.terminal_idx[li]]
                # An invalid_ohlc terminal row has no close: settle at the last mark.
                last_close = marks[li] if np.isnan(raw_close) else _dec(raw_close)
                if panel.payout[li] in terminal_writeoff:
                    writeoffs += 1
                    marks.pop(li, None)
                    del positions[li]
                    continue
                decision_id = f"terminal:{session}:{panel.symbols[li]}"
                value = last_close * pos.qty
                expires = _at(session, SESSION_CLOSE)
                add_decision(decision_id, s, li, Side.SELL, PRE_OPEN, expires, value)
                order = OrderSpec(
                    order_id=next_order_id(s, li, Side.SELL),
                    intent_id=decision_id,
                    market=Market.KR,
                    symbol=panel.symbols[li],
                    side=Side.SELL,
                    limit_price=last_close,
                    quantity=Decimal(pos.qty),
                    submitted_at=_at(session, PRE_OPEN),
                    expires_at=expires,
                    origin="engine_time_trim",
                )
                orders.append(order)
                book_fill(order, s, Decimal(pos.qty), last_close, li)
                cash += costs.sell_proceeds(value)
                marks.pop(li, None)
                del positions[li]
            for li in [x for x in working if 0 <= panel.terminal_idx[x] < s]:
                del working[li]

            # New entries decided after the prior close.
            new_today = 0
            for cand in plan.get(s - 1, ()) if s > 0 else ():
                if new_today >= MAX_NEW_PER_SESSION:
                    break
                if cand.li in positions or cand.li in working:
                    continue
                if int(ORDER_NOTIONAL // cand.limit) < 1:
                    continue
                last = min(s + cand.rest_sessions - 1, last_session)
                decision_id = f"entry:{session}:{panel.symbols[cand.li]}"
                add_decision(
                    decision_id,
                    s,
                    cand.li,
                    Side.BUY,
                    PRE_OPEN,
                    _at(cal[last], SESSION_CLOSE),
                    ORDER_NOTIONAL,
                )
                working[cand.li] = _Working(cand, decision_id, s, last)
                new_today += 1

            # Morning: one DAY child per working entry while cash covers it.
            reserved = Decimal(0)
            todays: list[tuple[int, _Working, OrderSpec]] = []
            for li in sorted(working):
                w = working[li]
                qty = int(ORDER_NOTIONAL // w.cand.limit)
                need = costs.buy_cash(w.cand.limit * qty)
                if reserved + need > cash:
                    continue
                reserved += need
                order = OrderSpec(
                    order_id=next_order_id(s, li, Side.BUY),
                    intent_id=w.decision_id,
                    market=Market.KR,
                    symbol=panel.symbols[li],
                    side=Side.BUY,
                    limit_price=w.cand.limit,
                    quantity=Decimal(qty),
                    submitted_at=_at(session, PRE_OPEN),
                    expires_at=_at(session, SESSION_CLOSE),
                )
                orders.append(order)
                todays.append((li, w, order))

            for li, w, order in todays:
                filled = False
                if panel.state[li, s] == STATE_VALID:
                    ctx = daily_context(
                        Side.BUY,
                        order.limit_price,
                        int(order.quantity),
                        _tick(ticks, Side.BUY, order.limit_price),
                        open_price=panel.open[li, s],
                        extreme=panel.low[li, s],
                        volume=panel.volume[li, s],
                    )
                    outcome = model.fill(ctx)
                    if outcome.any_fill:
                        assert outcome.price is not None
                        book_fill(order, s, outcome.filled_qty, outcome.price, li)
                        cash -= costs.buy_cash(outcome.filled_qty * outcome.price)
                        exit_idx = (
                            s + w.cand.hold
                            if w.cand.hold_from_fill
                            else w.cand.t + w.cand.hold
                        )
                        positions[li] = _Position(li, int(outcome.filled_qty), exit_idx)
                        window = panel.close[li, s : s + _MARKOUT_SPAN + 1]
                        states = panel.state[li, s : s + _MARKOUT_SPAN + 1]
                        closes: list[Decimal | None] = [
                            _dec(c) if st == STATE_VALID else None
                            for c, st in zip(window, states, strict=True)
                        ]
                        outcomes.extend(
                            buy_markouts(
                                order_id=order.order_id,
                                entry_price=outcome.price,
                                entry_index=0,
                                closes=closes,
                                costs=costs,
                            )
                        )
                        filled = True
                if filled or s >= w.last_session:
                    del working[li]

            # Closing auction: exits due, on a valid bar, oldest first.
            for li in sorted(positions):
                pos = positions[li]
                if s < pos.exit_idx or panel.state[li, s] != STATE_VALID:
                    continue
                reference = marks.get(li, _dec(panel.close[li, s]))
                raw = max(reference * SELL_FLOOR, Decimal(1))
                limit = ticks.align_buy(raw)
                if limit <= 0:
                    limit = Decimal(1)
                decision_id = f"exit:{session}:{panel.symbols[li]}"
                expires = _at(session, SESSION_CLOSE)
                add_decision(
                    decision_id,
                    s,
                    li,
                    Side.SELL,
                    CLOSE_AUCTION,
                    expires,
                    limit * pos.qty,
                )
                order = OrderSpec(
                    order_id=next_order_id(s, li, Side.SELL),
                    intent_id=decision_id,
                    market=Market.KR,
                    symbol=panel.symbols[li],
                    side=Side.SELL,
                    limit_price=limit,
                    quantity=Decimal(pos.qty),
                    submitted_at=_at(session, CLOSE_AUCTION),
                    expires_at=expires,
                )
                orders.append(order)
                ctx = closing_auction_context(
                    limit,
                    pos.qty,
                    _tick(ticks, Side.SELL, limit),
                    close=panel.close[li, s],
                    volume=panel.volume[li, s],
                )
                outcome = model.fill(ctx)
                if outcome.any_fill:
                    assert outcome.price is not None
                    book_fill(order, s, outcome.filled_qty, outcome.price, li)
                    cash += costs.sell_proceeds(outcome.filled_qty * outcome.price)
                    pos.qty -= int(outcome.filled_qty)
                    if pos.qty == 0:
                        marks.pop(li, None)
                        del positions[li]

            # Mark at the last known close (a halted name keeps its reference).
            value = Decimal(0)
            for li, pos in positions.items():
                if not np.isnan(panel.close[li, s]):
                    marks[li] = _dec(panel.close[li, s])
                value += marks.get(li, Decimal(0)) * pos.qty
            nav_today = cash + value
            nav[offset] = float(nav_today)
            deployed[offset] = float(value / nav_today) if nav_today > 0 else 0.0
    except FillModelUnavailable as exc:
        return unavailable(exc)

    # Anything still open is valued at its mark after the sell fee.
    terminal_value = cash + sum(
        (
            costs.sell_proceeds(marks.get(li, Decimal(0)) * p.qty)
            for li, p in positions.items()
        ),
        Decimal(0),
    )
    result = FillModelResult(
        fill_model_id=model.model_id,
        params_sha256=_params(model),
        status="ok",
        decisions=tuple(decisions),
        intents=tuple(intents),
        orders=tuple(orders),
        fills=tuple(fills),
        outcomes=tuple(outcomes),
        capital=capital,
        terminal_value=terminal_value,
        cash_baseline=capital,
        notes=(f"terminal_writeoffs={writeoffs}", f"open_at_end={len(positions)}"),
    )
    return SimOutput(
        result=result,
        nav=nav,
        sessions=list(cal[first_session : last_session + 1]),
        traded_gross=traded,
        deployed=deployed,
        writeoffs=writeoffs,
        unresolved_at_end=len(positions),
    )


def evaluation_report(
    *,
    hypothesis_id: str,
    outputs: Mapping[str, SimOutput],
    registry: RegistryRef,
    code_ref: str,
    data_manifest_sha256: str,
    config: Mapping[str, object],
) -> EvaluationReport:
    """Wrap one policy's per-fill-model results in the contract's report."""

    return EvaluationReport(
        hypothesis_id=hypothesis_id,
        market=Market.KR,
        provenance=Provenance(
            code_ref=code_ref,
            data_manifest_sha256=data_manifest_sha256,
            config_sha256=sha256_canonical(dict(config)),
            registry=registry,
        ),
        results=tuple(o.result for o in outputs.values()),
    )


def session_slice(calendar: Sequence[date], start: date, end: date) -> tuple[int, int]:
    """First and last calendar positions inside ``start..end``."""

    positions = [i for i, d in enumerate(calendar) if start <= d <= end]
    if not positions:
        raise ValueError(f"no session in {start}..{end}")
    return positions[0], positions[-1]
