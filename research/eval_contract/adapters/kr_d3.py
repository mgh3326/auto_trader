"""KR adapter: wraps the existing D3 portfolio engine, does not rewrite it.

``research.kr_corpus.d3_engine.PortfolioEngine`` stays the harness. The adapter

* runs it unchanged for the legacy result (``LEGACY_MODEL_ID``);
* runs it once per contract fill model through a subclass that overrides only
  the two fill-price hooks (``_buy_fill_price`` / ``_sell_fill_price``) -- the
  engine still sizes, ranks, reserves cash, expires DAY orders and books fees;
* translates the engine's events and fills into contract rows.

On daily bars a DAY order rests from before the open through the close, so the
whole bar is both the overlapping window and the wholly-inside window and the
open is the first reachable price. ``OptimisticTouch`` on that context is the
engine's own rule (open if open <= limit, else limit if low <= limit), which a
test proves by equality with the legacy run.

Limits of v1 (reported, not hidden):

* The engine sizes orders itself and asks a fill hook only "at what price, if
  any", so only full-or-nothing models run here. ``VolumeShare`` is unavailable
  (D3 bars carry no volume); ``EmpiricalFillModel`` is unavailable (unfitted).
* C3 time trims are executed by the engine at the open, outside the fill
  hooks, so they fill at the open under every model. They appear as sell
  orders with ``origin="engine_time_trim"``.
* Net vs cash is defined at equal capital, so only
  ``CashflowView.NO_CONTRIBUTION`` runs are evaluated, and the cash leg is a
  zero rate: D3's NAV earns nothing on idle or reserved cash, so crediting the
  cash baseline with interest would bias the comparison against the strategy.
* Markouts are computed for buy fills on the market-session calendar of the
  run; a horizon past the last session or on a session without a bar for the
  symbol is censored.
* A buy goes through one tick *below* the limit, so at a band boundary the
  step is the lower band's tick.
"""

from __future__ import annotations

import bisect
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date, datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

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
    DEFAULT_MODELS,
    FillContext,
    FillModel,
    FillModelUnavailable,
    params_sha256,
)
from research.eval_contract.outcomes import buy_markouts
from research.kr_corpus.d3_engine.engine import PortfolioEngine
from research.kr_corpus.d3_engine.models import (
    Bar,
    CashflowView,
    EngineResult,
    OrderClass,
    OrderSide,
    PortfolioRunInput,
)
from research.kr_corpus.d3_engine.tick import TickTable

KST = ZoneInfo("Asia/Seoul")
DECISION_TIME = time(8, 30)  # orders are issued pre-open from prior closes
SESSION_CLOSE = time(15, 30)
LEGACY_MODEL_ID = "legacy_d3_engine"
VENUE = "krx"  # as the KIS KR ledger normalizer writes it
_PRICE_ONLY_QTY = Decimal(1)  # the engine owns quantity; hooks answer price only
_BELOW = Decimal("0.0001")  # below every KRX tick, to find the band under a limit


class HarnessUnsupported(FillModelUnavailable):
    """The wrapped harness cannot express what the fill model needs."""


def _at(session: date, at: time) -> datetime:
    return datetime.combine(session, at, tzinfo=KST)


def _side(side: OrderSide) -> Side:
    return Side.BUY if side is OrderSide.BUY else Side.SELL


def _daily_context(side: Side, limit: Decimal, bar: Bar, tick: Decimal) -> FillContext:
    extreme = bar.low if side is Side.BUY else bar.high
    return FillContext(
        side=side,
        limit=limit,
        quantity=_PRICE_ONLY_QTY,
        tick=tick,
        optimistic_extreme=extreme,
        conservative_extreme=extreme,
        first_price=bar.open,
        optimistic_window_volume=None,
    )


class _FillModelEngine(PortfolioEngine):
    """The legacy engine with its two fill-price hooks routed to a model."""

    def __init__(self, tick_table: TickTable, fill_model: FillModel) -> None:
        super().__init__(tick_table)
        self._fill_model = fill_model

    def _tick(self, side: Side, limit: Decimal) -> Decimal:
        """Distance to the next valid price beyond the limit.

        A buy goes through downwards, so at a band boundary (e.g. 5000) the
        step is the lower band's tick (4995), not the limit's band (4990).
        """

        if side is Side.SELL:
            return self._ticks.band_for(limit).tick
        lower = self._ticks.band_for(limit - _BELOW)
        return lower.tick

    def _price(self, side: Side, limit: Decimal, bar: Bar) -> Decimal | None:
        tick = self._tick(side, limit)
        outcome = self._fill_model.fill(_daily_context(side, limit, bar, tick))
        if outcome.any_fill and outcome.filled_qty != _PRICE_ONLY_QTY:
            raise HarnessUnsupported("d3 engine supports full-or-nothing fills only")
        return outcome.price

    def _buy_fill_price(self, limit: Decimal, bar: Bar) -> Decimal | None:  # type: ignore[override]
        return self._price(Side.BUY, limit, bar)

    def _sell_fill_price(self, limit: Decimal, bar: Bar) -> Decimal | None:  # type: ignore[override]
        return self._price(Side.SELL, limit, bar)


def run_engine(
    run_input: PortfolioRunInput, ticks: TickTable, fill_model: FillModel | None
) -> EngineResult:
    """``fill_model=None`` runs the legacy engine untouched."""

    engine = (
        PortfolioEngine(ticks)
        if fill_model is None
        else _FillModelEngine(ticks, fill_model)
    )
    return engine.run(run_input)


# ---------------------------------------------------------------- translation


@dataclass(frozen=True, slots=True)
class KrD3Rows:
    decisions: tuple[Decision, ...]
    intents: tuple[Intent, ...]
    orders: tuple[OrderSpec, ...]
    fills: tuple[FillRow, ...]
    outcomes: tuple[Outcome, ...]


def _sessions(run_input: PortfolioRunInput) -> list[date]:
    return sorted(set(run_input.market_sessions) | {b.session for b in run_input.bars})


def _decision_id(session: date, symbol: str, side: Side) -> str:
    return f"kr:{session.isoformat()}:{symbol}:{side.value}"


def cost_model(run_input: PortfolioRunInput) -> CostModel:
    """D3 charges one neutral scalar per side (taxes folded into it)."""

    rate = run_input.config.fee_rate
    return CostModel(currency="KRW", buy_fee_rate=rate, sell_fee_rate=rate)


def _order_specs(
    result: EngineResult,
) -> dict[tuple[date, str, Side], list[OrderSpec]]:
    """Order specs per (session, symbol, side) decision.

    Submitted limit orders come from ``order_submitted`` events. C3 time trims
    are executed by the engine at the session open with no submitted event
    (they bypass the fill hooks under every model); they become a sell limit
    at that open, marked ``origin="engine_time_trim"``.
    """

    grouped: dict[tuple[date, str, Side], list[OrderSpec]] = defaultdict(list)
    for e in result.events:
        if e["event"] != "order_submitted":
            continue
        session, symbol = e["session"], str(e["symbol"])
        side = _side(e["side"])  # type: ignore[arg-type]
        grouped[(session, symbol, side)].append(  # type: ignore[index]
            OrderSpec(
                order_id=str(e["order_id"]),
                intent_id=_decision_id(session, symbol, side),  # type: ignore[arg-type]
                market=Market.KR,
                symbol=symbol,
                side=side,
                limit_price=e["limit"],  # type: ignore[arg-type]
                quantity=Decimal(e["quantity"]),  # type: ignore[arg-type]
                submitted_at=_at(session, DECISION_TIME),  # type: ignore[arg-type]
                expires_at=_at(session, SESSION_CLOSE),  # type: ignore[arg-type]
            )
        )
    known = {o.order_id for specs in grouped.values() for o in specs}
    for f in result.fills:
        if f.order_id in known:
            continue
        if f.order_class is not OrderClass.TIME_TRIM:
            raise ValueError(f"fill {f.order_id!r} has no submitted order")
        side = _side(f.side)
        grouped[(f.session, f.symbol, side)].append(
            OrderSpec(
                order_id=f.order_id,
                intent_id=_decision_id(f.session, f.symbol, side),
                market=Market.KR,
                symbol=f.symbol,
                side=side,
                limit_price=f.price,
                quantity=Decimal(f.quantity),
                submitted_at=_at(f.session, DECISION_TIME),
                expires_at=_at(f.session, SESSION_CLOSE),
                origin="engine_time_trim",
            )
        )
    return grouped


def translate(
    run_input: PortfolioRunInput,
    result: EngineResult,
    *,
    hypothesis_id: str,
    fill_model_id: str,
) -> KrD3Rows:
    sessions = _sessions(run_input)
    decisions: list[Decision] = []
    intents: list[Intent] = []
    orders: list[OrderSpec] = []
    for (session, symbol, side), specs in sorted(
        _order_specs(result).items(),
        key=lambda kv: (kv[0][0], kv[0][1], kv[0][2].value),
    ):
        pos = bisect.bisect_left(sessions, session)
        if pos == 0:
            raise ValueError(f"no session before {session}: cannot date the data")
        decision_id = _decision_id(session, symbol, side)
        decisions.append(
            Decision(
                decision_id=decision_id,
                hypothesis_id=hypothesis_id,
                market=Market.KR,
                symbol=symbol,
                action=DecisionAction.ENTER
                if side is Side.BUY
                else DecisionAction.EXIT,
                decided_at=_at(session, DECISION_TIME),
                data_cutoff=_at(sessions[pos - 1], SESSION_CLOSE),
            )
        )
        intents.append(
            Intent(
                intent_id=decision_id,
                decision_id=decision_id,
                side=side,
                max_notional=sum((s.notional for s in specs), Decimal(0)),
                valid_until=_at(session, SESSION_CLOSE),
            )
        )
        orders.extend(sorted(specs, key=lambda o: o.order_id))

    by_order = {o.order_id: o for o in orders}
    fills = tuple(
        FillRow(
            venue=VENUE,
            instrument_type="equity_kr",
            symbol=f.symbol,
            raw_symbol=f.symbol,
            side=_side(f.side),
            broker_order_id=f.order_id,
            filled_qty=Decimal(f.quantity),
            filled_price=f.price,
            filled_notional=f.gross,
            fee_amount=f.fee,
            fee_currency="KRW",
            # Daily bars cannot place the print inside the session; the fill
            # is only known by the close.
            filled_at=_at(f.session, SESSION_CLOSE),
            currency="KRW",
            correlation_id=by_order[f.order_id].intent_id,
            source=f"simulated:{fill_model_id}",
        )
        for f in result.fills
    )

    # Closes on the market-session calendar: a session without a bar for the
    # symbol (halt, missing data) is None, so R_h counts sessions, not bars,
    # and a horizon landing on a gap is censored.
    by_symbol: dict[str, dict[date, Decimal]] = defaultdict(dict)
    for bar in run_input.bars:
        by_symbol[bar.symbol][bar.session] = bar.close
    costs = cost_model(run_input)
    outcomes: list[Outcome] = []
    for f in result.fills:
        if f.side is not OrderSide.BUY:
            continue
        closes = [by_symbol[f.symbol].get(d) for d in sessions]
        outcomes.extend(
            buy_markouts(
                order_id=f.order_id,
                entry_price=f.price,
                entry_index=sessions.index(f.session),
                closes=closes,
                costs=costs,
            )
        )
    return KrD3Rows(
        tuple(decisions), tuple(intents), tuple(orders), fills, tuple(outcomes)
    )


# ----------------------------------------------------------------- evaluation


def data_manifest_sha256(run_input: PortfolioRunInput) -> str:
    return sha256_canonical(
        {
            "bars": sorted(run_input.bars, key=lambda b: (b.session, b.symbol)),
            "market_sessions": sorted(run_input.market_sessions),
            "index_closes": sorted(run_input.index_closes),
            "corporate_actions": sorted(
                run_input.corporate_actions, key=lambda c: (c.session, c.symbol)
            ),
        }
    )


def config_sha256(run_input: PortfolioRunInput) -> str:
    return sha256_canonical(
        {
            "harness": "research.kr_corpus.d3_engine.PortfolioEngine",
            "arm": run_input.arm,
            "cashflow_view": run_input.cashflow_view,
            "data_view": run_input.data_view,
            "decision_start": run_input.decision_start,
            "config": run_input.config,
            "cash_rate_per_session": Decimal(0),
        }
    )


def evaluate_model(
    run_input: PortfolioRunInput,
    ticks: TickTable,
    fill_model: FillModel | None,
    *,
    hypothesis_id: str,
) -> FillModelResult:
    model_id = LEGACY_MODEL_ID if fill_model is None else fill_model.model_id
    params = (
        sha256_canonical({"model_id": LEGACY_MODEL_ID, "rule": "engine_builtin"})
        if fill_model is None
        else params_sha256(fill_model)
    )
    try:
        result = run_engine(run_input, ticks, fill_model)
    except FillModelUnavailable as exc:
        return FillModelResult(
            fill_model_id=model_id,
            params_sha256=params,
            status="unavailable",
            unavailable_reason=f"{type(exc).__name__}: {exc}",
        )
    if result.status != "OK":
        raise ValueError(f"engine status {result.status!r}")
    rows = translate(
        run_input, result, hypothesis_id=hypothesis_id, fill_model_id=model_id
    )
    capital = run_input.config.initial_cash
    return FillModelResult(
        fill_model_id=model_id,
        params_sha256=params,
        status="ok",
        decisions=rows.decisions,
        intents=rows.intents,
        orders=rows.orders,
        fills=rows.fills,
        outcomes=rows.outcomes,
        capital=capital,
        terminal_value=result.metrics["terminal_nav"],  # type: ignore[arg-type]
        # D3 NAV earns nothing on idle or reserved cash, so the only unbiased
        # equal-capital cash leg for this harness is a zero rate.
        cash_baseline=capital,
    )


def evaluate(
    run_input: PortfolioRunInput,
    ticks: TickTable,
    *,
    registry: RegistryRef | None,
    code_ref: str,
    fill_models: Sequence[FillModel] = DEFAULT_MODELS,
    include_legacy: bool = True,
) -> EvaluationReport:
    if run_input.cashflow_view is not CashflowView.NO_CONTRIBUTION:
        raise ValueError("net vs cash is at equal capital: use NO_CONTRIBUTION")
    # Provenance refuses a missing registry before any engine run.
    provenance = Provenance(
        code_ref=code_ref,
        data_manifest_sha256=data_manifest_sha256(run_input),
        config_sha256=config_sha256(run_input),
        registry=registry,
    )
    assert provenance.registry is not None
    hypothesis_id = provenance.registry.hypothesis_id
    models: list[FillModel | None] = [None] if include_legacy else []
    models.extend(fill_models)
    results = tuple(
        evaluate_model(run_input, ticks, model, hypothesis_id=hypothesis_id)
        for model in models
    )
    return EvaluationReport(
        hypothesis_id=hypothesis_id,
        market=Market.KR,
        provenance=provenance,
        results=results,
    )
