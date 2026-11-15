"""One row per recorded decision (``review.decision_evidence``) in a KST window.

hk 4190, design #4154 §8.2 (plan row L9). Every value comes from recorded rows
or the L7/L8 helpers (``links``, ``trade_evidence``, ``metrics``); anything not
derivable is null with a closed reason — never zero and never invented.

All reads are SELECT inside the reader's own snapshot (``read_snapshot``);
each source group runs in a SAVEPOINT via ``guarded`` so a failed or capped
read is an ``unknowns`` entry, not a wrong number. The decision tables have no
ORM model (their only writer uses SQL text), so the reader describes the
columns it reads as lightweight Core tables, the same rule as ``links``.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Final, Literal

from sqlalchemy import Text, column, select, table
from sqlalchemy.dialects.postgresql import UUID as PG_UUID
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.mechanical_loss_cut import LotExitPlan
from app.models.order_proposals import OrderProposal, OrderProposalRung
from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
    TradeRetrospective,
)
from app.models.screener_pick_log import ScreenerPickLog
from app.models.strategy_sleeves import StrategySleeveVersion
from app.services.execution_ledger.supersede import (
    AUTHORITATIVE_SOURCES,
    order_identity,
    order_trading_day,
    supersede_provisional,
)
from app.services.retro_v1 import daily_bars
from app.services.retro_v1.chain import (
    _INFERENCE_MARKER,
    _one_venue,
    _order_identity,
    fills_stmt,
    is_opening_seed,
)
from app.services.retro_v1.envelope import (
    DEFAULT_LIMIT,
    MAX_SOURCE_ROWS,
    Unknowns,
    build_response,
    check_limit,
    check_span,
    check_symbol,
    dec,
    decode_cursor,
    guarded,
    iso,
    kst_window,
    make_ref,
    params_digest,
    parse_date,
    read_snapshot,
    resolve_from,
    safe_code,
)
from app.services.retro_v1.links import (
    DECISION_LINKS,
    LINK_BASES,
    OrderLinkEvidence,
    decision_ref,
    kst_day,
    load_order_links,
    order_send_key,
)
from app.services.retro_v1.market import (
    MARKETS,
    MarketInfo,
    infer_market,
    normalize_symbol,
)
from app.services.retro_v1.metrics import (
    DailyBar,
    Excursion,
    RiskRung,
    calculate_mfe_mae,
    calculate_risk_amount,
)
from app.services.retro_v1.trade_evidence import (
    _order_entry_notional,
    costs_body,
    trade_costs,
    trade_time_basis,
)

#: ``decision_evidence.market`` vocabulary → the retro/v1 market key.
_MARKET_BY_INSTRUMENT: Final = {info.instrument_type: info for info in MARKETS.values()}

DECISIONS: Final = table(
    "decision_evidence",
    column("id"),
    column("decision_id", PG_UUID(as_uuid=True)),
    column("decision_kind", Text),
    column("decided_at"),
    column("decided_by", Text),
    column("market", Text),
    column("account_mode", Text),
    column("symbol", Text),
    column("side", Text),
    column("strategy", Text),
    column("sleeve_id"),
    column("candidate_call_id", PG_UUID(as_uuid=True)),
    column("candidate_symbol", Text),
    column("candidate_link_basis", Text),
    column("currency", Text),
    column("stop_price"),
    column("stop_basis", Text),
    column("stop_source", Text),
    column("target_price"),
    column("horizon_sessions"),
    column("forecast_ids"),
    column("report_item_uuid", PG_UUID(as_uuid=True)),
    column("policy_version", Text),
    column("policy_content_hash", Text),
    schema="review",
)
DECISION_RUNGS: Final = table(
    "decision_evidence_rungs",
    column("decision_id", PG_UUID(as_uuid=True)),
    column("rung_index"),
    column("side", Text),
    column("planned_quantity"),
    column("planned_notional"),
    column("limit_price"),
    schema="review",
)
REFERENCE_PRICES: Final = table(
    "decision_reference_prices",
    column("id"),
    column("decision_id", PG_UUID(as_uuid=True)),
    column("rung_index"),
    column("kind", Text),
    column("price"),
    column("asof"),
    schema="review",
)

#: §5.1 rung reference-price priority: first kind that exists wins; every kind
#: but the candidate's price is ``recorded`` evidence of the decision time.
_REFERENCE_KIND_ORDER: Final = (
    "limit",
    "approval_price",
    "quote_at_decision",
    "candidate_price",
)
_INFERENCE_REFERENCE_KINDS: Final = frozenset({"candidate_price"})

_AUTHORITY_PAD: Final = timedelta(days=2)
_BAR_LOOKAHEAD: Final = timedelta(days=60)
_HORIZONS: Final = (1, 5, 20, "to_exit")
_TERMINAL_STATUSES: Final = frozenset({"filled", "rejected", "expired", "cancelled"})
_OPEN_STATUSES: Final = frozenset({"accepted", "pending", "partial"})
_ORDER_STATES: Final = frozenset(
    {
        "filled",
        "partially_filled",
        "working",
        "cancelled",
        "expired",
        "rejected",
        "unresolved",
    }
)


@dataclass(slots=True)
class DecisionData:
    """Everything the row builders need; failed/capped reads are ``incomplete``."""

    decisions: list[Any] = field(default_factory=list)
    rungs: list[Any] = field(default_factory=list)
    ref_prices: list[Any] = field(default_factory=list)
    proposals: list[Any] = field(default_factory=list)
    proposal_rungs: list[Any] = field(default_factory=list)
    attempts: list[Any] = field(default_factory=list)
    order_links: OrderLinkEvidence = field(default_factory=OrderLinkEvidence.empty)
    orders: list[tuple[str, Any]] = field(default_factory=list)
    sleeve_versions: dict[Any, Decimal] = field(default_factory=dict)
    exit_plans: list[Any] = field(default_factory=list)
    picks: list[Any] = field(default_factory=list)
    reviews: list[Any] = field(default_factory=list)
    fills: dict[str, list[Any]] = field(default_factory=dict)
    raw_fills: dict[str, list[Any]] = field(default_factory=dict)
    bars: dict[str, list[daily_bars.Bar] | None] = field(default_factory=dict)
    incomplete: set[str] = field(default_factory=set)


def _track(
    data: DecisionData,
    unknowns: Unknowns,
    name: str,
    rows: Sequence[Any],
    ok: bool,
    cap: int,
) -> None:
    """A failed read (unknown already recorded) or a capped one is incomplete."""
    if not ok:
        data.incomplete.add(name)
    elif len(rows) >= cap:
        data.incomplete.add(name)
        unknowns.note(f"source.{name}", "incomplete_evidence")


async def _load_decision_data(
    session: AsyncSession,
    unknowns: Unknowns,
    *,
    lo: datetime,
    hi: datetime,
    market: MarketInfo | None,
    symbol: str | None,
    as_of: datetime,
) -> DecisionData:
    """Every source of the decision rows, each in its own SAVEPOINT."""
    data = DecisionData()
    markets = [market] if market is not None else list(MARKETS.values())
    instrument_types = [m.instrument_type for m in markets]

    async def read_decisions() -> list[Any]:
        stmt = select(DECISIONS).where(
            DECISIONS.c.market.in_(instrument_types),
            DECISIONS.c.decided_at >= lo,
            DECISIONS.c.decided_at < hi,
        )
        if symbol is not None:
            stmt = stmt.where(DECISIONS.c.symbol == symbol)
        stmt = stmt.order_by(DECISIONS.c.decided_at, DECISIONS.c.id).limit(
            MAX_SOURCE_ROWS
        )
        return list((await session.execute(stmt)).all())

    decisions, ok = await guarded(
        session, unknowns, "source.decision_evidence", read_decisions, []
    )
    data.decisions = decisions
    _track(data, unknowns, "decision_evidence", decisions, ok, MAX_SOURCE_ROWS)
    if not decisions:
        return data
    ids = sorted({d.decision_id for d in decisions}, key=str)

    async def read_rungs() -> list[Any]:
        stmt = (
            select(DECISION_RUNGS)
            .where(DECISION_RUNGS.c.decision_id.in_(ids))
            .order_by(DECISION_RUNGS.c.decision_id, DECISION_RUNGS.c.rung_index)
            .limit(MAX_SOURCE_ROWS * 4)
        )
        return list((await session.execute(stmt)).all())

    rungs, rung_ok = await guarded(
        session, unknowns, "source.decision_rungs", read_rungs, []
    )
    data.rungs = [r for r in rungs if r.decision_id in set(ids)]
    _track(data, unknowns, "decision_rungs", rungs, rung_ok, MAX_SOURCE_ROWS * 4)

    async def read_prices() -> list[Any]:
        stmt = (
            select(REFERENCE_PRICES)
            .where(REFERENCE_PRICES.c.decision_id.in_(ids))
            .order_by(REFERENCE_PRICES.c.decision_id, REFERENCE_PRICES.c.id)
            .limit(MAX_SOURCE_ROWS * 4)
        )
        return list((await session.execute(stmt)).all())

    # The unique index is on (decision_id, COALESCE(rung_index,-1), kind), so
    # id is not selected by the ORDER BY above — keep it deterministic anyway.
    prices, prices_ok = await guarded(
        session, unknowns, "source.reference_prices", read_prices, []
    )
    data.ref_prices = [r for r in prices if r.decision_id in set(ids)]
    _track(
        data,
        unknowns,
        "reference_prices",
        prices,
        prices_ok,
        MAX_SOURCE_ROWS * 4,
    )

    async def read_proposals() -> list[Any]:
        stmt = (
            select(OrderProposal)
            .where(OrderProposal.decision_id.in_(ids))
            .order_by(OrderProposal.id)
            .limit(MAX_SOURCE_ROWS)
        )
        return list((await session.execute(stmt)).scalars().all())

    proposals, proposal_ok = await guarded(
        session, unknowns, "source.proposals", read_proposals, []
    )
    data.proposals = [p for p in proposals if p.decision_id in set(ids)]
    _track(data, unknowns, "proposals", proposals, proposal_ok, MAX_SOURCE_ROWS)

    proposal_pks = [p.id for p in data.proposals]
    if proposal_ok and proposal_pks:

        async def read_proposal_rungs() -> list[Any]:
            stmt = (
                select(OrderProposalRung)
                .where(OrderProposalRung.proposal_pk.in_(proposal_pks))
                .order_by(OrderProposalRung.proposal_pk, OrderProposalRung.rung_index)
                .limit(MAX_SOURCE_ROWS * 4)
            )
            return list((await session.execute(stmt)).scalars().all())

        found, rung_ok2 = await guarded(
            session, unknowns, "source.proposal_rungs", read_proposal_rungs, []
        )
        wanted = set(proposal_pks)
        data.proposal_rungs = [r for r in found if r.proposal_pk in wanted]
        _track(
            data,
            unknowns,
            "proposal_rungs",
            found,
            rung_ok2,
            MAX_SOURCE_ROWS * 4,
        )
    elif not proposal_ok:
        # The proposals read failed, so dependent reads never ran: their
        # absence is unavailable evidence, not empty evidence.
        data.incomplete.update(("proposal_rungs", "exit_plans"))

    async def read_attempts() -> list[Any]:
        stmt = (
            select(
                DECISION_LINKS.c.attempt_id,
                DECISION_LINKS.c.decision_id,
                DECISION_LINKS.c.account_scope,
                DECISION_LINKS.c.send_key,
                DECISION_LINKS.c.proposal_id,
                DECISION_LINKS.c.rung_index,
            )
            .where(DECISION_LINKS.c.decision_id.in_(ids))
            .order_by(DECISION_LINKS.c.attempt_id)
            .limit(MAX_SOURCE_ROWS * 4)
        )
        return list((await session.execute(stmt)).all())

    attempts, attempts_ok = await guarded(
        session, unknowns, "source.decision_links", read_attempts, []
    )
    data.attempts = [a for a in attempts if a.decision_id in set(ids)]
    _track(data, unknowns, "decision_links", attempts, attempts_ok, MAX_SOURCE_ROWS * 4)

    # Resolution must see every attempt of a shared send key, not only this
    # decision's: a key another decision also attempted must not resolve.
    send_keys = sorted({a.send_key for a in data.attempts} - {""})
    if attempts_ok and send_keys:
        data.order_links = await load_order_links(session, unknowns, send_keys)

        async def read_kis_orders() -> list[Any]:
            stmt = (
                select(KISLiveOrderLedger)
                .where(
                    KISLiveOrderLedger.account_mode == "kis_live",
                    KISLiveOrderLedger.idempotency_key.in_(send_keys),
                )
                .order_by(KISLiveOrderLedger.id)
                .limit(MAX_SOURCE_ROWS)
            )
            return list((await session.execute(stmt)).scalars().all())

        async def read_live_orders() -> list[Any]:
            stmt = (
                select(LiveOrderLedger)
                .where(
                    LiveOrderLedger.account_scope.in_(("kis_live", "upbit_live")),
                    LiveOrderLedger.idempotency_key.in_(send_keys),
                )
                .order_by(LiveOrderLedger.id)
                .limit(MAX_SOURCE_ROWS)
            )
            return list((await session.execute(stmt)).scalars().all())

        async def read_toss_orders() -> list[Any]:
            stmt = (
                select(TossLiveOrderLedger)
                .where(
                    TossLiveOrderLedger.account_mode == "toss_live",
                    TossLiveOrderLedger.operation_kind == "place",
                    TossLiveOrderLedger.client_order_id.in_(send_keys),
                )
                .order_by(TossLiveOrderLedger.id)
                .limit(MAX_SOURCE_ROWS)
            )
            return list((await session.execute(stmt)).scalars().all())

        for name, read in (
            ("kis_orders", read_kis_orders),
            ("live_orders", read_live_orders),
            ("toss_orders", read_toss_orders),
        ):
            rows, order_ok = await guarded(
                session, unknowns, f"source.decision_{name}", read, []
            )
            _track(data, unknowns, name, rows, order_ok, MAX_SOURCE_ROWS)
            wanted_keys = set(send_keys)
            data.orders += [(name, o) for o in rows if order_send_key(o) in wanted_keys]

    sleeve_ids = {d.sleeve_id for d in data.decisions if d.sleeve_id is not None}
    if sleeve_ids:

        async def read_versions() -> list[Any]:
            stmt = (
                select(StrategySleeveVersion)
                .where(StrategySleeveVersion.sleeve_id.in_(sleeve_ids))
                .order_by(
                    StrategySleeveVersion.sleeve_id, StrategySleeveVersion.version
                )
                .limit(MAX_SOURCE_ROWS)
            )
            return list((await session.execute(stmt)).scalars().all())

        versions, versions_ok = await guarded(
            session, unknowns, "source.sleeve_versions", read_versions, []
        )
        _track(
            data,
            unknowns,
            "sleeve_versions",
            versions,
            versions_ok,
            MAX_SOURCE_ROWS,
        )
        for version in versions:
            # Ordered by version ascending: the last write wins, so this keeps
            # the latest (highest version) budget cap per sleeve.
            if version.sleeve_id in sleeve_ids:
                data.sleeve_versions[version.sleeve_id] = version.budget_cap

    proposal_ids = {p.proposal_id for p in data.proposals if p.proposal_id is not None}
    if proposal_ok and proposal_ids:

        async def read_exit_plans() -> list[Any]:
            stmt = (
                select(LotExitPlan)
                .where(
                    LotExitPlan.proposal_id.in_(proposal_ids),
                    LotExitPlan.backfilled.is_(True),
                )
                .order_by(LotExitPlan.id)
                .limit(MAX_SOURCE_ROWS * 4)
            )
            return list((await session.execute(stmt)).scalars().all())

        plans, plans_ok = await guarded(
            session, unknowns, "source.exit_plans", read_exit_plans, []
        )
        _track(data, unknowns, "exit_plans", plans, plans_ok, MAX_SOURCE_ROWS * 4)
        data.exit_plans = [p for p in plans if p.proposal_id in proposal_ids]

    call_ids = {
        d.candidate_call_id for d in data.decisions if d.candidate_call_id is not None
    }
    if call_ids:

        async def read_picks() -> list[Any]:
            stmt = (
                select(ScreenerPickLog)
                .where(ScreenerPickLog.call_id.in_(call_ids))
                .order_by(ScreenerPickLog.recorded_at, ScreenerPickLog.id)
                .limit(MAX_SOURCE_ROWS)
            )
            return list((await session.execute(stmt)).scalars().all())

        picks, picks_ok = await guarded(
            session, unknowns, "source.candidates", read_picks, []
        )
        _track(data, unknowns, "candidates", picks, picks_ok, MAX_SOURCE_ROWS)
        data.picks = [p for p in picks if p.call_id in call_ids]

    correlations = sorted(
        {c for _, o in data.orders for c in (getattr(o, "correlation_id", None),) if c}
    )
    if ids or correlations:

        async def read_reviews() -> list[Any]:
            stmt = select(TradeRetrospective).order_by(
                TradeRetrospective.created_at, TradeRetrospective.id
            )
            if correlations:
                from sqlalchemy import or_

                stmt = stmt.where(
                    or_(
                        TradeRetrospective.decision_id.in_(ids),
                        TradeRetrospective.correlation_id.in_(correlations),
                    )
                )
            else:
                stmt = stmt.where(TradeRetrospective.decision_id.in_(ids))
            stmt = stmt.limit(MAX_SOURCE_ROWS * 4)
            return list((await session.execute(stmt)).scalars().all())

        reviews, reviews_ok = await guarded(
            session, unknowns, "source.retrospectives", read_reviews, []
        )
        _track(
            data,
            unknowns,
            "retrospectives",
            reviews,
            reviews_ok,
            MAX_SOURCE_ROWS * 4,
        )
        data.reviews = [
            r
            for r in reviews
            if r.decision_id in set(ids)
            or (r.correlation_id or "") in set(correlations)
        ]

    order_symbols: dict[str, set[str]] = defaultdict(set)
    for _, order in data.orders:
        broker = getattr(order, "broker", None)
        for info in markets:
            if broker == "upbit" and info.key != "crypto":
                continue
            if broker == "toss" and info.key == "crypto":
                continue
            if getattr(order, "market", None) in (None, info.key) or (
                broker == "kis" and info.key in ("kr", "us")
            ):
                order_symbols[info.instrument_type].add(order.symbol.upper())
    for instrument_type, symbols in order_symbols.items():
        info = _MARKET_BY_INSTRUMENT[instrument_type]
        stmt = fills_stmt(
            info,
            sorted(symbols),
            lo - _AUTHORITY_PAD,
            as_of + _AUTHORITY_PAD,
            MAX_SOURCE_ROWS,
        )

        async def read_fills(stmt: Any = stmt) -> list[Any]:
            return list((await session.execute(stmt)).scalars().all())

        fills, fills_ok = await guarded(
            session,
            unknowns,
            f"source.fills_{info.key}",
            read_fills,
            [],
        )
        _track(data, unknowns, f"fills_{info.key}", fills, fills_ok, MAX_SOURCE_ROWS)
        kept, _ = supersede_provisional(fills)
        data.fills[instrument_type] = [f for f in kept if not is_opening_seed(f)]
        data.raw_fills[instrument_type] = [f for f in fills if not is_opening_seed(f)]

    entry_symbols: dict[str, set[str]] = defaultdict(set)
    first_fill: dict[str, datetime] = {}
    for instrument_type, fills in data.fills.items():
        for f in fills:
            if f.side != "buy":
                continue
            entry_symbols[instrument_type].add(f.symbol.upper())
            prev = first_fill.get(instrument_type)
            first_fill[instrument_type] = (
                f.filled_at if prev is None else min(prev, f.filled_at)
            )
    for instrument_type, symbols in entry_symbols.items():
        info = _MARKET_BY_INSTRUMENT[instrument_type]
        start = first_fill[instrument_type]

        async def read_bars(
            info: MarketInfo = info,
            symbols: set[str] = symbols,
            start: datetime = start,
        ) -> list[daily_bars.Bar]:
            return await daily_bars.load_bars(
                session,
                info,
                "1d",
                sorted(symbols),
                lo=start - _AUTHORITY_PAD,
                hi=as_of + _BAR_LOOKAHEAD,
                cap=MAX_SOURCE_ROWS * 4,
            )

        bars, bars_ok = await guarded(
            session, unknowns, f"source.bars_{info.key}", read_bars, []
        )
        _track(data, unknowns, f"bars_{info.key}", bars, bars_ok, MAX_SOURCE_ROWS * 4)
        by_symbol: dict[str, list[daily_bars.Bar]] = defaultdict(list)
        for bar in bars:
            by_symbol[bar.symbol.upper()].append(bar)
        for bar_symbol in symbols:
            found = by_symbol.get(bar_symbol, [])
            chosen = _one_venue(found, info)
            data.bars[f"{info.key}|{bar_symbol}"] = (
                sorted(chosen, key=lambda b: b.time) if chosen is not None else None
            )
    return data


# --------------------------------------------------------------------------
# pure row builders
# --------------------------------------------------------------------------
def _select_reference(
    prices: Sequence[Any], rung_index: int | None
) -> tuple[Decimal | None, str | None]:
    """§5.1 priority over the rows of one ``(decision_id, rung_index)``.

    The caller falls back to the decision-level rows (``rung_index IS NULL``)
    when the rung has none, so this function only sees one group.
    """
    by_kind = {p.kind: p.price for p in prices}
    for kind in _REFERENCE_KIND_ORDER:
        if kind in by_kind:
            return by_kind[kind], kind
    return None, None


def _risk_rungs(
    rungs: Sequence[Any], ref_prices: Sequence[Any]
) -> tuple[list[RiskRung], Decimal | None, str | None]:
    """One ``RiskRung`` per decision rung, plus the rung-0 display price."""
    per_rung: dict[int, list[Any]] = defaultdict(list)
    per_decision: list[Any] = []
    for price in ref_prices:
        if price.rung_index is None:
            per_decision.append(price)
        else:
            per_rung[price.rung_index].append(price)
    out: list[RiskRung] = []
    display_price: Decimal | None = None
    display_kind: str | None = None
    for rung in sorted(rungs, key=lambda r: r.rung_index):
        price, kind = _select_reference(per_rung.get(rung.rung_index, []), None)
        if kind is None:
            price, kind = _select_reference(per_decision, None)
        if rung.rung_index == 0:
            display_price, display_kind = price, kind
        out.append(
            RiskRung(
                planned_quantity=rung.planned_quantity,
                reference_price=price,
                reference_price_kind=kind,
                reference_price_basis="inference"
                if kind in _INFERENCE_REFERENCE_KINDS
                else ("recorded" if kind else None),
            )
        )
    if display_kind is None:
        display_price, display_kind = _select_reference(per_decision, None)
    return out, display_price, display_kind


def _decision_stop(
    decision: Any,
    proposal: Any | None,
    exit_plans: Sequence[Any],
) -> tuple[Decimal | None, str | None, str | None]:
    """``(stop, basis, source)``: the recorded stop, else a backfilled plan.

    §5.1 stop priority: a decision-recorded stop is ``recorded``; a policy
    backfill (``lot_exit_plans.backfilled``) is not the decider's stop, so it
    is ``inference``. Several different backfilled stops cannot be one
    decision-level stop, so the minimum (most conservative) is taken.
    """
    if decision.stop_price is not None:
        return decision.stop_price, "recorded", decision.stop_source
    if proposal is None:
        return None, None, None
    stops = {
        Decimal(str(p.stop_price))
        for p in exit_plans
        if p.proposal_id == proposal.proposal_id and p.stop_price is not None
    }
    if not stops:
        return None, None, None
    return min(stops), "inference", "lot_exit_plan"


def _decision_orders(decision_id: Any, data: DecisionData) -> list[tuple[str, Any]]:
    """Order ledger rows that resolve to this decision under §3 row 4."""
    found: list[tuple[str, Any]] = []
    for source, order in data.orders:
        resolution = data.order_links.resolve(order)
        if (
            resolution.kind in ("recorded", "unconfirmed")
            and resolution.decision_id == decision_id
        ):
            found.append((source, order))
    return found


def _order_state(
    order: Any,
    info: MarketInfo,
    totals: dict[Any, Decimal],
    rung: Any | None,
) -> str:
    """The closed order-state vocabulary of the chain rows."""
    identity = _order_identity(order, info)
    day = order_trading_day(
        getattr(order, "broker", "kis"), info.instrument_type, order.trade_date
    )
    total = totals.get((identity, day), Decimal(0))
    quantity = Decimal(str(order.quantity)) if order.quantity is not None else None
    terminal = order.status if order.status in _TERMINAL_STATUSES else None
    if quantity and total == quantity:
        return "filled"
    if quantity and Decimal(0) < total < quantity and not terminal:
        return "partially_filled"
    if terminal:
        return terminal
    if rung is not None and getattr(rung, "void_reason", None) == _INFERENCE_MARKER:
        return "expired"
    if (
        order.status in _OPEN_STATUSES
        and day is not None
        and day
        == order_trading_day(
            getattr(order, "broker", "kis"),
            info.instrument_type,
            datetime.now(UTC),
        )
    ):
        return "working"
    return "unresolved"


def _bound_rungs(
    decision_id: Any, data: DecisionData, proposals: dict[Any, Any]
) -> dict[tuple[Any, int], Any]:
    """Proposal rungs bound to this decision's attempts or proposal."""
    out: dict[tuple[Any, int], Any] = {}
    proposals_by_pk = {p.id: p for p in data.proposals}
    for rung in data.proposal_rungs:
        owner = proposals_by_pk.get(rung.proposal_pk)
        if owner is not None and owner.decision_id == decision_id:
            out[(owner.proposal_id, rung.rung_index)] = rung
    for attempt in data.attempts:
        if (
            attempt.decision_id == decision_id
            and attempt.proposal_id is not None
            and attempt.rung_index is not None
        ):
            for rung in data.proposal_rungs:
                owner = proposals_by_pk.get(rung.proposal_pk)
                if (
                    owner is not None
                    and owner.proposal_id == attempt.proposal_id
                    and rung.rung_index == attempt.rung_index
                ):
                    out[(attempt.proposal_id, attempt.rung_index)] = rung
    return out


def _matching_fills(
    orders: Sequence[tuple[str, Any]],
    info: MarketInfo,
    fills: Sequence[Any],
) -> list[Any]:
    """In-effect fills whose exact order identity is one of ``orders``."""
    identities = set()
    for _, order in orders:
        identity = _order_identity(order, info)
        if identity is not None:
            identities.add(identity)
    seen_provisional: set[tuple[Any, ...]] = set()
    out: list[Any] = []
    for fill in fills:
        if order_identity(fill) not in identities:
            continue
        if fill.source == "websocket":
            marker = (
                order_identity(fill),
                fill.filled_at,
                fill.filled_qty,
                fill.filled_price,
            )
            if marker in seen_provisional:
                continue
            seen_provisional.add(marker)
        out.append(fill)
    return out


def _excursion_body(excursion: Excursion) -> dict[str, Any]:
    body: dict[str, Any] = {
        "amount": dec(excursion.amount),
        "pct": dec(excursion.pct),
        "r": dec(excursion.r),
        "status": excursion.status,
        "reason": excursion.reason,
    }
    if excursion.r is None and excursion.status == "complete":
        body["r_reason"] = excursion.r_reason
    return body


def _row(decision: Any, data: DecisionData) -> dict[str, Any]:
    info = _MARKET_BY_INSTRUMENT.get(decision.market)
    rungs = [r for r in data.rungs if r.decision_id == decision.decision_id]
    ref_prices = [p for p in data.ref_prices if p.decision_id == decision.decision_id]
    proposals = [p for p in data.proposals if p.decision_id == decision.decision_id]
    proposal = proposals[0] if len(proposals) == 1 else None
    proposal_reason = (
        None
        if len(proposals) == 1
        else (
            "ambiguous_decision"
            if proposals
            else (
                "no_proposal"
                if decision.decision_kind == "proposal"
                else "not_applicable"
            )
        )
    )
    if "proposals" in data.incomplete:
        # A capped read can retain one proposal and omit another: no unique
        # proposal may be established, so neither its ref nor its exit plan.
        proposal = None
        proposal_reason = "attempts_unavailable"

    # candidate link: the recorded reference resolved to the same pick row the
    # writer chose (latest pick of the call and symbol).
    candidate_ref = None
    candidate_reason = None
    if decision.candidate_call_id is None:
        candidate_reason = "no_candidate_ref"
    elif decision.candidate_link_basis not in LINK_BASES:
        candidate_reason = "candidate_ref_unresolved"
    elif "candidates" in data.incomplete:
        candidate_reason = "candidate_ref_unresolved"
    else:
        named = [
            p
            for p in data.picks
            if p.call_id == decision.candidate_call_id
            and p.symbol.upper() == (decision.candidate_symbol or "").upper()
        ]
        if named:
            pick = max(named, key=lambda p: (p.recorded_at, p.id))
            candidate_ref = make_ref("candidate", pick.id)
        else:
            candidate_reason = "candidate_ref_unresolved"

    risk_rungs, display_price, display_kind = _risk_rungs(rungs, ref_prices)
    stop, stop_basis, stop_source = _decision_stop(decision, proposal, data.exit_plans)
    budget_cap = (
        data.sleeve_versions.get(decision.sleeve_id)
        if decision.sleeve_id is not None
        else None
    )
    if budget_cap is not None and budget_cap <= 0:
        # A zero cap is permitted by the table but cannot divide; the metric
        # helper rejects nonpositive budgets, so treat it as unavailable.
        budget_cap = None
    side: Literal["buy", "sell"] | None = (
        decision.side if decision.side in ("buy", "sell") else None
    )
    if side is None:
        risk_amount = None
        risk_reason: str | None = "not_applicable"
        risk_basis = None
        pct_notional = None
        pct_budget = None
        budget_reason = "not_applicable"
    else:
        result = calculate_risk_amount(
            side=side,
            stop_price=stop,
            stop_basis=stop_basis,
            rungs=risk_rungs,
            budget_cap=budget_cap,
        )
        risk_amount = result.amount
        risk_reason = result.reason
        risk_basis = result.basis
        pct_notional = result.pct_of_notional
        pct_budget = result.pct_of_budget
        budget_reason = result.pct_of_budget_reason

    # A failed or capped source read withholds, it never asserts absence.
    plan_incomplete = bool({"decision_rungs", "reference_prices"} & data.incomplete)
    if side is not None:
        if plan_incomplete or (
            "exit_plans" in data.incomplete and decision.stop_price is None
        ):
            risk_amount = None
            risk_reason = "attempts_unavailable"
            risk_basis = None
            pct_notional = None
            pct_budget = None
            budget_reason = "attempts_unavailable"
        elif "sleeve_versions" in data.incomplete and (decision.sleeve_id is not None):
            # A capped version read may hold a stale budget: withhold the pct.
            pct_budget = None
            budget_reason = "attempts_unavailable"

    planned_quantities = [r.planned_quantity for r in risk_rungs]
    planned_quantity = (
        sum(planned_quantities)
        if planned_quantities and all(q is not None for q in planned_quantities)
        else None
    )
    if plan_incomplete:
        planned_quantity = None
        planned_reason = "attempts_unavailable"
        reference_reason = "attempts_unavailable"
    else:
        planned_reason = None if planned_quantity is not None else "no_planned_quantity"
        reference_reason = None if display_kind is not None else "no_reference_price"

    orders = _decision_orders(decision.decision_id, data)
    # One resolved order never proves the population: a failed read of
    # another ledger could hide more, so completeness is required, not found.
    orders_known = data.order_links.complete and not (
        {
            "decision_links",
            "proposals",
            "proposal_rungs",
            "kis_orders",
            "live_orders",
            "toss_orders",
        }
        & data.incomplete
    )
    bound = _bound_rungs(decision.decision_id, data, {})
    fills = (
        _matching_fills(orders, info, data.fills.get(info.instrument_type, []))
        if info is not None
        else []
    )
    raw_fills = data.raw_fills.get(info.instrument_type, []) if info else []
    fills_known = (
        info is not None and orders_known and f"fills_{info.key}" not in data.incomplete
    )

    totals: dict[Any, Decimal] = defaultdict(Decimal)
    for fill in fills:
        if fill.source in AUTHORITATIVE_SOURCES:
            key = (
                order_identity(fill),
                order_trading_day(
                    fill.broker,
                    info.instrument_type,
                    fill.filled_at,  # type: ignore[union-attr]
                ),
            )
            totals[key] += Decimal(str(fill.filled_qty))
    states: list[str] = []
    if orders_known:
        for _, order in orders:
            resolution = data.order_links.resolve(order)
            rung = (
                bound.get((resolution.proposal_id, resolution.rung_index))
                if resolution.proposal_id is not None
                else None
            )
            states.append(_order_state(order, info, totals, rung))
        order_states: list[str] | None = sorted(states)
        order_states_reason = None
        order_count: int | None = len(orders)
        order_count_reason = None
    else:
        order_states = None
        order_states_reason = "attempts_unavailable"
        order_count = None
        order_count_reason = "attempts_unavailable"

    buy_fills = [f for f in fills if f.side == "buy"]
    # Closing fills pair FIFO over the instrument's in-effect ledger fills —
    # they routinely belong to a different decision than the entry (§ R
    # multiple), so the exit input is not restricted to this decision's orders.
    exit_fills: list[Any] = []
    if fills_known and buy_fills:
        entry_time = max(f.filled_at for f in buy_fills)
        remaining = filled_quantity = sum(
            (Decimal(str(f.filled_qty)) for f in buy_fills), Decimal(0)
        )
        for fill in sorted(
            (
                f
                for f in data.fills.get(info.instrument_type, [])
                if f.side == "sell"
                and f.symbol.upper() == decision.symbol.upper()
                and f.filled_at > entry_time
            ),
            key=lambda f: (f.filled_at, f.id),
        ):
            if remaining <= 0:
                break
            exit_fills.append(fill)
            remaining -= Decimal(str(fill.filled_qty))
    if fills_known and buy_fills:
        filled_quantity = sum(
            (Decimal(str(f.filled_qty)) for f in buy_fills), Decimal(0)
        )
        by_order: dict[Any, list[Any]] = defaultdict(list)
        for f in buy_fills:
            by_order[order_identity(f)].append(f)
        notional = Decimal(0)
        notional_ok = True
        for rows in by_order.values():
            part = _order_entry_notional(rows)
            if part is None:
                notional_ok = False
                break
            notional += part
        entry_avg = (
            notional / filled_quantity if notional_ok and filled_quantity > 0 else None
        )
        fill_reason = None if entry_avg is not None else "missing_notional"
        time_basis = trade_time_basis(buy_fills, raw_fills)
    elif fills_known:
        filled_quantity = Decimal(0) if orders else None
        entry_avg = None
        time_basis = None
        fill_reason = "no_fills" if orders else "no_linked_order"
    else:
        filled_quantity = None
        entry_avg = None
        time_basis = None
        fill_reason = "attempts_unavailable"

    costs: dict[str, Any] | None = None
    if info is not None and fills_known and fills:
        costs_result, includes_tax = trade_costs(fills, info)
        costs = costs_body(costs_result, fee_includes_tax=includes_tax)

    # MFE/MAE over completed daily sessions (§5.2): entry session = last buy
    # fill's KST day; exit session = first sell fill's KST day.
    mfe_out: dict[str, Any] = {}
    mae_out: dict[str, Any] = {}
    excursion_reason: str | None = None
    if entry_avg is None or filled_quantity in (None, Decimal(0)):
        excursion_reason = "missing_entry_price"
    else:
        key = f"{info.key}|{decision.symbol.upper()}"
        if f"bars_{info.key}" in data.incomplete:
            excursion_reason = "attempts_unavailable"
        elif key not in data.bars:
            excursion_reason = "missing_data"
        elif data.bars[key] is None:
            excursion_reason = "source_mixed"
        else:
            entry_session = kst_day(
                max(buy_fills, key=lambda f: (f.filled_at, f.id)).filled_at
            )
            exit_session = (
                kst_day(min(exit_fills, key=lambda f: (f.filled_at, f.id)).filled_at)
                if exit_fills
                else None
            )
            # Bars keep their order: dropping a null-extreme bar would shift
            # the horizon count onto later sessions, so a missing extreme in
            # the measured span makes that horizon unknown instead.
            raw_window = [
                bar
                for bar in data.bars[key]
                if entry_session < kst_day(bar.time)
                and (exit_session is None or kst_day(bar.time) <= exit_session)
            ]
            series = [
                DailyBar(
                    session=kst_day(bar.time),
                    high=bar.high,
                    low=bar.low,
                )
                for bar in raw_window
                if bar.high is not None and bar.low is not None
            ]
            risk_per_unit = (
                risk_amount / planned_quantity
                if risk_amount is not None
                and planned_quantity is not None
                and planned_quantity > 0
                else None
            )
            for horizon in _HORIZONS:
                label = str(horizon)
                span = (
                    raw_window if horizon == "to_exit" else raw_window[: int(horizon)]
                )
                if any(b.high is None or b.low is None for b in span):
                    body = {
                        "amount": None,
                        "pct": None,
                        "r": None,
                        "status": "unknown",
                        "reason": "missing_data",
                    }
                    mfe_out[label] = dict(body)
                    mae_out[label] = dict(body)
                    continue
                pair = calculate_mfe_mae(
                    entry_price=entry_avg,
                    entry_session=entry_session,
                    bars=series,
                    exit_session=exit_session,
                    horizon_sessions=horizon,
                    risk_per_unit=risk_per_unit,
                )
                mfe_out[label] = _excursion_body(pair.mfe)
                mae_out[label] = _excursion_body(pair.mae)
    if excursion_reason is not None:
        mfe_out = {
            str(h): {
                "amount": None,
                "pct": None,
                "r": None,
                "status": "unknown",
                "reason": excursion_reason
                if excursion_reason != "source_mixed"
                else "missing_data",
            }
            for h in _HORIZONS
        }
        mae_out = dict(mfe_out)

    # Retrospective link (§3 row 7): a stored decision_id is recorded; else a
    # single review whose correlation names exactly one order of this decision
    # is inference; anything that fans out is no link.
    stored = [r for r in data.reviews if r.decision_id == decision.decision_id]
    retro_ref = None
    retro_basis = None
    retro_reason = None
    if len(stored) > 1:
        retro_reason = "ambiguous_decision"
    elif stored:
        retro_ref = make_ref("review", stored[0].id)
        retro_basis = "recorded"
    else:
        correlations = {getattr(o, "correlation_id", None) for _, o in orders} - {
            None,
            "",
        }
        # A correlation must name this decision alone: the same value on
        # orders resolving to other decisions is ambiguous, and a review
        # recorded for a different account mode is not this decision's.
        corr_owner: dict[str, set[Any]] = defaultdict(set)
        for _, order in data.orders:
            corr = getattr(order, "correlation_id", None)
            if corr:
                resolved = data.order_links.resolve(order)
                if resolved.decision_id is not None:
                    corr_owner[corr].add(resolved.decision_id)
        derived = [
            r
            for r in data.reviews
            if r.correlation_id in correlations
            and getattr(r, "account_mode", None) == decision.account_mode
        ]
        if len(derived) > 1 or any(
            len(corr_owner.get(r.correlation_id, set())) > 1 for r in derived
        ):
            retro_reason = "ambiguous_decision"
        elif derived:
            retro_ref = make_ref("review", derived[0].id)
            retro_basis = "inference"
        else:
            retro_reason = "no_decision_link"
    if "retrospectives" in data.incomplete:
        retro_ref, retro_basis = None, None
        retro_reason = "attempts_unavailable"

    exit_avg = None
    r_multiple = None
    r_multiple_reason = "no_fills"
    if exit_fills and entry_avg is None:
        r_multiple_reason = "missing_entry_price"
    elif exit_fills:
        exit_qty = sum((Decimal(str(f.filled_qty)) for f in exit_fills), Decimal(0))
        # Toss reconciler rows carry cumulative bookkeeping, so notional must
        # be derived per order the same way the buy side does it.
        by_sell_order: dict[Any, list[Any]] = defaultdict(list)
        for fill in exit_fills:
            by_sell_order[order_identity(fill)].append(fill)
        exit_notional = Decimal(0)
        exit_notional_ok = True
        for order_rows in by_sell_order.values():
            part = _order_entry_notional(order_rows)
            if part is None:
                exit_notional_ok = False
                break
            exit_notional += part
        if exit_notional_ok and exit_qty > 0:
            exit_avg = exit_notional / exit_qty
            if (
                risk_amount is not None
                and planned_quantity is not None
                and planned_quantity > 0
            ):
                risk_per_unit = risk_amount / planned_quantity
                if risk_per_unit > 0:
                    r_multiple = (exit_avg - entry_avg) / risk_per_unit
                    r_multiple_reason = None
                else:
                    r_multiple_reason = "no_risk_per_unit"
            else:
                r_multiple_reason = "no_risk_per_unit"
        else:
            r_multiple_reason = "missing_notional"
    elif not fills_known:
        r_multiple_reason = "attempts_unavailable"
    elif not exit_fills:
        r_multiple_reason = "no_fills" if not orders else "no_exit_fill"

    row = {
        "decision_ref": decision_ref(decision.decision_id),
        "decision_kind": safe_code(decision.decision_kind, fallback=None),
        "decided_at": iso(decision.decided_at),
        "decided_by": safe_code(decision.decided_by, fallback=None),
        "market": info.key if info else None,
        "symbol": decision.symbol,
        "side": safe_code(decision.side, fallback=None),
        "strategy": safe_code(decision.strategy, fallback=None),
        "candidate_ref": candidate_ref,
        "candidate_link_basis": decision.candidate_link_basis
        if decision.candidate_link_basis in LINK_BASES
        else None,
        "candidate_reason": candidate_reason,
        "reference_price": dec(display_price),
        "reference_kind": safe_code(display_kind, fallback=None),
        "reference_reason": reference_reason,
        "stop_price": dec(stop),
        "stop_source": safe_code(stop_source, fallback=None),
        "risk_basis": risk_basis,
        "planned_quantity": dec(planned_quantity),
        "planned_reason": planned_reason,
        "planned_risk_amount": dec(risk_amount),
        "risk_reason": risk_reason,
        "risk_pct_of_notional": dec(pct_notional),
        "risk_pct_of_budget": dec(pct_budget),
        "risk_pct_of_budget_reason": budget_reason,
        "proposal_ref": make_ref("proposal", proposal.id)
        if proposal is not None
        else None,
        "proposal_reason": proposal_reason,
        "order_count": order_count,
        "order_count_reason": order_count_reason,
        "order_states": order_states,
        "order_states_reason": order_states_reason,
        "entry_avg_price": dec(entry_avg),
        "filled_quantity": dec(filled_quantity),
        "fill_reason": fill_reason,
        "fill_time_basis": time_basis,
        "costs": costs,
        "mfe": mfe_out,
        "mae": mae_out,
        "excursion_basis": "inference",
        "adjusted": None,
        "retrospective_ref": retro_ref,
        "retrospective_link_basis": retro_basis,
        "retrospective_reason": retro_reason,
        "r_multiple": dec(r_multiple),
        "r_multiple_reason": r_multiple_reason,
        "basis": "inference",
    }
    return row


def _deep_nulls(value: Any, path: str) -> Any:
    """Dotted paths of every null inside a nested ``costs``/``mfe``/``mae``."""
    if isinstance(value, dict):
        for key, child in value.items():
            yield from _deep_nulls(child, f"{path}.{key}")
    elif value is None:
        yield path


def _hints(row: dict[str, Any]) -> dict[str, str]:
    """Unknown-reason hints for the fields a row can legitimately leave null."""
    hints: dict[str, str] = {}
    for name, value in row.items():
        if value is not None or name in {"mfe", "mae"}:
            continue
        if name == "market":
            hints[name] = "unsupported"
        elif name in {
            "side",
            "strategy",
            "decision_kind",
            "decided_by",
            "stop_source",
            "candidate_link_basis",
        }:
            hints[name] = "not_applicable" if value is None else "missing_data"
        elif name in {
            "entry_avg_price",
            "fill_time_basis",
            "r_multiple",
            "planned_risk_amount",
            "risk_pct_of_notional",
            "risk_pct_of_budget",
            "risk_basis",
            "reference_price",
            "reference_kind",
            "stop_price",
            "planned_quantity",
            "candidate_ref",
            "proposal_ref",
            "retrospective_ref",
            "retrospective_link_basis",
            "order_states",
            "filled_quantity",
        }:
            hints[name] = "missing_data"
        elif name in {"order_count"}:
            hints[name] = "count_unavailable"
        else:
            hints[name] = "not_applicable"
    return hints


async def read_retro_decisions(
    session: AsyncSession,
    *,
    to: Any,
    from_: Any = None,
    market: str | None = None,
    symbol: str | None = None,
    limit: int = DEFAULT_LIMIT,
    cursor: str | None = None,
    **extra: Any,
) -> dict[str, Any]:
    """One row per ``review.decision_evidence`` in the KST window."""
    check_limit(limit)
    start, end = resolve_from(from_, extra), parse_date(to)
    check_span(start, end)
    info = None
    if market is not None:
        if market not in MARKETS:
            from app.services.retro_v1.envelope import RetroReadError

            raise RetroReadError("validation_error")
        info = MARKETS[market]
    symbol_norm = None
    if symbol is not None:
        clean = check_symbol(symbol)
        # DB form for the equality filter: only US symbols have alternate
        # broker spellings, so infer the US shape when market is omitted.
        symbol_norm = normalize_symbol(
            clean, info if info is not None else infer_market(clean)
        )
    digest = params_digest(
        op="retro_decisions",
        start=start,
        end=end,
        market=market,
        symbol=symbol_norm,
    )
    offset = decode_cursor(cursor, digest)
    lo, hi = kst_window(start, end)
    unknowns = Unknowns()
    as_of = datetime.now(UTC)

    async with read_snapshot(session):
        data = await _load_decision_data(
            session,
            unknowns,
            lo=lo,
            hi=hi,
            market=info,
            symbol=symbol_norm,
            as_of=as_of,
        )
    complete = "decision_evidence" not in data.incomplete
    rows = []
    for decision in data.decisions:
        row = _row(decision, data)
        rows.append((row, _hints(row)))
    times: list[datetime | None] = [d.decided_at for d in data.decisions]
    times += [o.trade_date for _, o in data.orders]
    times += [f.filled_at for fills in data.fills.values() for f in fills]
    aware = [t for t in times if t is not None and t.tzinfo is not None]
    response = build_response(
        as_of=as_of,
        data_as_of=min(max(aware), as_of) if aware else None,
        rows=rows,
        total=len(rows) if complete else None,
        limit=limit,
        cursor=cursor,
        digest=digest,
        offset=offset,
        unknowns=unknowns,
    )
    for index, row in enumerate(response["data"]):
        for name in ("costs", "mfe", "mae"):
            if row.get(name) is None:
                continue  # the top-level null is already explained by _hints
            for path in _deep_nulls(row.get(name), f"data.{index}.{name}"):
                leaf = path.rsplit(".", 1)[-1]
                unknowns.note(
                    path,
                    "not_applicable" if leaf.endswith("reason") else "missing_data",
                )
    response["unknowns"] += [
        item for item in unknowns.items() if item not in response["unknowns"]
    ]
    return response
