"""Decision link, fill time basis and costs of a closed trade (hk 4188, #4154).

``retro_trades`` rows describe a closed journal trade and its retrospectives.
This module adds what the design (§3 row 7, §5.3, §6.2) derives from them,
reading SELECTs only:

* ``decision_ref``: the ``decision_id`` stored on a retrospective is
  ``recorded``. Without one, the link is derived (``inference``, never stored)
  from the retrospective's correlation id to its one order ledger row and that
  order's send-attempt rows; anything that fans out is ``ambiguous``;
* ``fill_time_basis``: the weakest time basis among the ledger fills of the
  orders recorded for the trade;
* ``costs``: the broker-reported fees of those fills kept apart from the policy
  estimate (``metrics.calculate_costs``). A fee a fill did not provide is not a
  recorded zero, and a partial sum is never reported.

The orders of a trade are the live order ledger rows whose ``correlation_id`` is
the journal's or one of its retrospectives'.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field, replace
from datetime import timedelta
from decimal import Decimal
from typing import Any, Final

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.review import KISLiveOrderLedger, LiveOrderLedger, TossLiveOrderLedger
from app.services.execution_ledger.supersede import (
    AUTHORITATIVE_SOURCES,
    order_identity,
    supersede_provisional,
)
from app.services.retro_v1.chain import (
    _order_identity,
    fills_stmt,
    is_opening_seed,
)
from app.services.retro_v1.envelope import (
    MAX_SOURCE_ROWS,
    Unknowns,
    dec,
    guarded,
    safe_code,
)
from app.services.retro_v1.links import (
    OrderLinkEvidence,
    decision_ref,
    load_order_links,
    order_send_key,
)
from app.services.retro_v1.market import MARKETS, MarketInfo
from app.services.retro_v1.metrics import Costs, calculate_costs

#: Authority window around the orders, as in ``chain`` (a reconciler row stamped
#: at order time must still cover a websocket fill stamped later).
_PAD: Final = timedelta(days=2)
#: Weakest last: a trade is only as precise as its least precise fill.
_TIME_BASIS_RANK: Final = {
    "broker_fill_time": 0,
    "broker_order_time": 1,
    "observed_at": 2,
}

_ORDER_LEDGERS: Final = (
    ("kis_orders", KISLiveOrderLedger),
    ("live_orders", LiveOrderLedger),
    ("toss_orders", TossLiveOrderLedger),
)


@dataclass(slots=True)
class TradeEvidence:
    """Order ledger rows (by correlation), their attempts and their fills."""

    orders: list[Any] = field(default_factory=list)
    links: OrderLinkEvidence = field(default_factory=OrderLinkEvidence.empty)
    fills: list[Any] = field(default_factory=list)
    #: the same fills before websocket duplicates are superseded (time basis).
    raw_fills: list[Any] = field(default_factory=list)
    #: a failed read: absence of a link or a fill proves nothing.
    orders_ok: bool = True
    fills_ok: bool = True


def _live_orders_stmt(model: Any, correlations: list[str]) -> Any:
    stmt = select(model).where(model.correlation_id.in_(correlations))
    if model is KISLiveOrderLedger:
        stmt = stmt.where(model.account_mode == "kis_live")
    elif model is LiveOrderLedger:
        stmt = stmt.where(model.account_scope.in_(("kis_live", "upbit_live")))
    else:
        stmt = stmt.where(
            model.account_mode == "toss_live", model.operation_kind == "place"
        )
    return stmt.order_by(model.trade_date, model.id).limit(MAX_SOURCE_ROWS)


async def load_trade_evidence(
    session: AsyncSession,
    unknowns: Unknowns,
    journals: Sequence[Any],
    reviews: Sequence[Any],
) -> TradeEvidence:
    evidence = TradeEvidence()
    correlations = sorted(
        {
            c
            for c in (
                *(j.correlation_id for j in journals),
                *(r.correlation_id for r in reviews),
            )
            if c
        }
    )
    if not correlations:
        return evidence
    wanted = set(correlations)
    for name, model in _ORDER_LEDGERS:

        async def read(model: Any = model) -> list[Any]:
            stmt = _live_orders_stmt(model, correlations)
            return list((await session.execute(stmt)).scalars().all())

        rows, ok = await guarded(session, unknowns, f"source.trade_{name}", read, [])
        if not ok:
            evidence.orders_ok = False
        elif len(rows) >= MAX_SOURCE_ROWS:
            evidence.orders_ok = False
            unknowns.note(f"source.trade_{name}", "incomplete_evidence")
        evidence.orders += [o for o in rows if o.correlation_id in wanted]
    evidence.links = await load_order_links(
        session, unknowns, [k for k in map(order_send_key, evidence.orders) if k]
    )
    if not evidence.orders:
        return evidence
    # Fills come from the chain's fills_stmt (live, in effect, #1175), one read
    # per market over the symbols and dates of that market's recorded orders.
    infos = {info.instrument_type: info for info in MARKETS.values()}
    spans: dict[str, tuple[MarketInfo, set[str], list[Any]]] = {}
    for journal in journals:
        kind = str(getattr(journal.instrument_type, "value", journal.instrument_type))
        info = infos.get(kind)
        if info is None:
            continue
        mine = {c for c in (journal.correlation_id,) if c}
        mine |= {r.correlation_id for r in reviews if r.journal_id == journal.id}
        found = [
            o
            for o in evidence.orders
            if o.correlation_id in mine and o.symbol.upper() == journal.symbol.upper()
        ]
        if not found:
            continue
        _, symbols, moments = spans.setdefault(kind, (info, set(), []))
        symbols.update(o.symbol.upper() for o in found)
        moments += [o.trade_date for o in found]
        moments += [d for d in (journal.exit_date, journal.created_at) if d]
    for info, symbols, moments in spans.values():
        stmt = fills_stmt(
            info,
            sorted(symbols),
            min(moments) - _PAD,
            max(moments) + _PAD,
            MAX_SOURCE_ROWS,
        )

        async def read_fills(stmt: Any = stmt) -> list[Any]:
            return list((await session.execute(stmt)).scalars().all())

        fills, ok = await guarded(
            session, unknowns, "source.trade_fills", read_fills, []
        )
        if not ok:
            evidence.fills_ok = False
        elif len(fills) >= MAX_SOURCE_ROWS:
            evidence.fills_ok = False
            unknowns.note("source.trade_fills", "incomplete_evidence")
        kept, _ = supersede_provisional(fills)
        evidence.fills += [f for f in kept if not is_opening_seed(f)]
        evidence.raw_fills += [f for f in fills if not is_opening_seed(f)]
    return evidence


# --------------------------------------------------------------------------
# decision link (§3 row 7)
# --------------------------------------------------------------------------
def trade_decision_link(
    reviews: Sequence[Any], evidence: TradeEvidence
) -> dict[str, str]:
    """``decision_ref``/``decision_link_basis``/``link_reason`` of one trade.

    Stored ``decision_id`` values are ``recorded`` (the L6 writer checked them
    against the decision row); two different ones are ambiguous. With none
    stored, each retrospective is derived through its correlation id: exactly
    one order ledger row, then the attempt rule of §3 row 4. A derivation that
    fans out to several orders or decisions is ``ambiguous_decision`` and one
    that the attempts cannot settle is ``ambiguous_attempts`` — no link either
    way. The result of a derivation is always ``inference`` and not stored.
    """
    stored = {r.decision_id for r in reviews if r.decision_id is not None}
    if len(stored) > 1:
        return {"link_reason": "ambiguous_decision"}
    if stored:
        return {
            "decision_ref": decision_ref(next(iter(stored))),
            "decision_link_basis": "recorded",
        }
    if not evidence.orders_ok:
        return {}  # a failed read does not show that no link exists
    derived: set[Any] = set()
    weak = False
    for review in reviews:
        if not review.correlation_id:
            continue
        orders = [
            o
            for o in evidence.orders
            if o.correlation_id == review.correlation_id
            and o.symbol.upper() == review.symbol.upper()
        ]
        if len(orders) > 1:
            return {"link_reason": "ambiguous_decision"}
        if not orders:
            continue
        resolution = evidence.links.resolve(orders[0])
        if resolution.kind == "unavailable":
            return {"link_reason": "attempts_unavailable"}
        if resolution.kind == "ambiguous":
            return {"link_reason": "ambiguous_attempts"}
        if resolution.kind in ("recorded", "unconfirmed"):
            derived.add(resolution.decision_id)
            weak = weak or resolution.kind == "unconfirmed"
    if len(derived) > 1:
        return {"link_reason": "ambiguous_decision"}
    if not derived:
        return {"link_reason": "no_decision_link"}
    fields = {
        "decision_ref": decision_ref(next(iter(derived))),
        "decision_link_basis": "inference",
    }
    if weak:
        fields["link_reason"] = "attempt_unconfirmed"
    return fields


# --------------------------------------------------------------------------
# fills of the trade: time basis and costs (§5.3, §6.2)
# --------------------------------------------------------------------------
def _trade_orders(
    journal: Any, reviews: Sequence[Any], evidence: TradeEvidence
) -> list[Any]:
    """Order ledger rows of one trade (journal or review correlation + symbol)."""
    correlations = {
        c for c in (journal.correlation_id, *(r.correlation_id for r in reviews)) if c
    }
    return [
        o
        for o in evidence.orders
        if o.correlation_id in correlations
        and o.symbol.upper() == journal.symbol.upper()
    ]


def trade_fills(
    journal: Any, reviews: Sequence[Any], info: MarketInfo, evidence: TradeEvidence
) -> list[Any]:
    """Ledger fills of the orders recorded for one trade (exact order identity)."""
    # ``order_identity`` already carries the KIS trading day (and no day for
    # brokers whose order ids are unique), so a fill after KST midnight of an
    # Upbit order still matches.
    identities = set()
    for order in _trade_orders(journal, reviews, evidence):
        identity = _order_identity(order, info)
        if identity is not None:
            identities.add(identity)
    return [f for f in evidence.fills if order_identity(f) in identities]


def _orders_fill_coverage(
    orders: Sequence[Any], fills: Sequence[Any], info: MarketInfo
) -> bool:
    """Every order's recorded filled quantity is matched by the fills read.

    A successful, uncapped read can still lack one order's fills (the row was
    never written); matching the filled quantity the order ledger recorded is
    what proves the surviving fills are the whole trade.
    """
    matched: dict[Any, Decimal] = defaultdict(Decimal)
    for fill in fills:
        matched[order_identity(fill)] += fill.filled_qty
    for order in orders:
        expected = getattr(order, "filled_qty", None)
        if expected is None and getattr(order, "status", None) == "filled":
            expected = getattr(order, "quantity", None)
        seen = matched.get(_order_identity(order, info), Decimal(0))
        if seen != (expected if expected is not None else Decimal(0)):
            return False
    return True


def fill_time_basis(fill: Any) -> str | None:
    """A fill's time basis; a pre-L5 row is read by ``(broker, source)`` (§6.2-4).

    Only a KIS reconciler row has a known meaning (the order time stands in for
    the fill time); any other row without the column is unknown.
    """
    if fill.filled_at_basis in _TIME_BASIS_RANK:
        return fill.filled_at_basis
    if (
        fill.filled_at_basis is None
        and fill.broker == "kis"
        and fill.source == "reconciler"
    ):
        return "broker_order_time"
    return None


def _weakest(bases: Sequence[str | None]) -> str | None:
    known = [b for b in bases if b is not None]
    if not known or len(known) != len(bases):
        return None
    return max(known, key=_TIME_BASIS_RANK.__getitem__)


def weakest_time_basis(fills: Iterable[Any]) -> str | None:
    return _weakest([fill_time_basis(f) for f in fills])


def trade_time_basis(fills: Sequence[Any], raw_fills: Sequence[Any]) -> str | None:
    """Weakest time basis of a trade's orders (§6.2-4).

    A reconciler row stands in for its order's websocket rows, which carry the
    broker's own fill times. Those times are used for an order only when its
    in-effect websocket rows all say ``broker_fill_time`` and their quantity
    equals the authoritative quantity; otherwise the authoritative rows' basis
    (the order time) stands for the whole order.
    """
    by_order: dict[Any, list[Any]] = defaultdict(list)
    for fill in fills:
        by_order[order_identity(fill)].append(fill)
    bases: list[str | None] = []
    for identity, rows in by_order.items():
        authority = [r for r in rows if r.source in AUTHORITATIVE_SOURCES]
        # ROB-1379: fillwire and the in-app monitor can record the same
        # websocket fill under different ``fill_seq`` values, so equal rows
        # count once — otherwise two copies of one fill can masquerade as
        # coverage of another fill that was never observed.
        taps = []
        seen_taps: set[tuple[Any, ...]] = set()
        for row in raw_fills:
            if row.source != "websocket" or order_identity(row) != identity:
                continue
            marker = (row.filled_at, row.filled_qty, row.filled_price)
            if marker in seen_taps:
                continue
            seen_taps.add(marker)
            taps.append(row)
        if (
            authority
            and taps
            and all(r.filled_at_basis == "broker_fill_time" for r in taps)
            and sum((r.filled_qty for r in taps), Decimal(0))
            == sum((r.filled_qty for r in authority), Decimal(0))
        ):
            bases.append("broker_fill_time")
        else:
            bases.append(weakest_time_basis(rows))
    return _weakest(bases)


def _fill_fee(fill: Any) -> tuple[Decimal | None, bool]:
    """``(recorded fee, fee_includes_tax)`` of one fill; ``None`` = not provided.

    ``fee_basis=not_provided`` and the pre-L5 KIS reconciler zero (a missing
    ``fee`` coerced to 0.0, §5.3) are not recorded fees. A pre-L5 Toss fee is
    commission plus tax.
    """
    if fill.fee_basis == "not_provided":
        return None, False
    fee = fill.fee_amount
    if fill.fee_basis is None:
        if fee is not None and fill.broker == "kis" and fill.source == "reconciler":
            fee = None if fee == 0 else fee
        return fee, fee is not None and fill.broker == "toss"
    return fee, False


def _sum_or_none(values: Sequence[Decimal | None]) -> Decimal | None:
    """No partial sums: one unknown makes the total unknown."""
    if not values or any(v is None for v in values):
        return None
    return sum((v for v in values if v is not None), Decimal(0))


def _round_trip_bps(
    market: MarketInfo,
) -> tuple[Decimal | None, str | None, str | None]:
    """``(bps, policy version, policy content hash)``; ``None`` when unreadable."""
    try:
        from app.services.trading_policy_service import (
            load_trading_policy,
            policy_version_stamp,
        )

        policy = load_trading_policy().order_proposals.auto_approve
        stamp = policy_version_stamp()
        return (
            Decimal(str(policy.round_trip_cost_bps[market.key])),
            safe_code(stamp["version"], fallback=None),
            safe_code(stamp["content_hash"], fallback=None),
        )
    except (OSError, KeyError, ValueError):
        return None, None, None


def _order_entry_notional(rows: Sequence[Any]) -> Decimal | None:
    """True entry notional of one order's buy fills.

    Toss reconciler rows price each incremental quantity at the broker's
    *cumulative* averageFilledPrice (``toss_execution_ledger``), so their
    ``filled_notional`` does not sum to the entry notional. When every row
    carries the cumulative bookkeeping, the notional is the final cumulative
    quantity times its average price — and only while the previous→cumulative
    chain is unbroken. Anything else is summed directly; mixed or broken
    evidence cannot be established.
    """
    cumulative = [
        (payload, r.filled_price)
        for r in rows
        if (payload := getattr(r, "raw_payload_json", None) or {})
        and "broker_cumulative_filled_qty" in payload
        and "previous_filled_qty" in payload
    ]
    if not cumulative:
        return _sum_or_none([r.filled_notional for r in rows])
    if len(cumulative) != len(rows):
        return None
    chain = sorted(
        (
            (
                Decimal(str(payload["broker_cumulative_filled_qty"])),
                Decimal(str(payload["previous_filled_qty"])),
                price,
            )
            for payload, price in cumulative
        ),
        key=lambda item: item[0],
    )
    expected = Decimal(0)
    for cum, prev, _price in chain:
        if prev != expected or cum <= prev:
            return None
        expected = cum
    price = chain[-1][2]
    return expected * price if price is not None else None


def _entry_notional(entry: Sequence[Any]) -> Decimal | None:
    """Entry notional across the trade's orders; ``None`` when not established."""
    by_order: dict[Any, list[Any]] = defaultdict(list)
    for fill in entry:
        by_order[order_identity(fill)].append(fill)
    if not by_order:
        return None
    total = Decimal(0)
    for rows in by_order.values():
        notional = _order_entry_notional(rows)
        if notional is None:
            return None
        total += notional
    return total


def trade_costs(fills: Sequence[Any], market: MarketInfo) -> tuple[Costs, bool]:
    """Costs of the fills; the flag says a pre-split Toss fee includes its tax."""
    fees = [_fill_fee(f) for f in fills]
    all_fees_known = bool(fees) and all(fee is not None for fee, _ in fees)
    # The fee's own currency, not the fill's: the ingest schema allows a
    # missing or different ``fee_currency``, and fees in different currencies
    # cannot be added together. An unknown or mixed fee currency withholds
    # the recorded sum.
    fee_currencies = {
        getattr(f, "fee_currency", None)
        for f, (fee, _) in zip(fills, fees, strict=True)
        if fee is not None
    }
    one_fee_currency = (
        all_fees_known and len(fee_currencies) == 1 and None not in fee_currencies
    )
    fee_total = _sum_or_none([fee for fee, _ in fees]) if one_fee_currency else None
    tax_total = _sum_or_none([f.tax_amount for f in fills])
    # The documented basis is the buy entry notional: a trade whose entry fill
    # was never recorded has no entry notional (``missing_notional``), and its
    # exit proceeds are not a stand-in.
    entry = [f for f in fills if f.side == "buy"]
    bps, version, content_hash = _round_trip_bps(market)
    costs = calculate_costs(
        fee=fee_total,
        tax=tax_total,
        currency=fee_currencies.pop() if one_fee_currency else None,
        fee_basis="broker_reported" if fee_total is not None else "not_provided",
        notional=_entry_notional(entry),
        round_trip_cost_bps=bps,
        policy_version=version,
        policy_content_hash=content_hash,
    )
    if fee_total is None and all_fees_known and not one_fee_currency:
        # A fee sum whose currency cannot be stated is withheld entirely.
        costs = replace(costs, recorded=None, recorded_reason="missing_currency")
    elif fee_total is None and costs.recorded is not None:
        # A fee that any fill did not provide leaves no recorded cost, even
        # when the tax is known. The estimate stays either way.
        costs = replace(costs, recorded=None, recorded_reason="not_provided")
    return costs, any(includes_tax for _, includes_tax in fees)


def costs_body(costs: Costs, *, fee_includes_tax: bool) -> dict[str, Any]:
    recorded = costs.recorded
    estimated = costs.estimated
    body: dict[str, Any] = {
        "recorded": None
        if recorded is None
        else {
            "fee": dec(recorded.fee),
            "tax": dec(recorded.tax),
            "currency": safe_code(recorded.currency, fallback=None),
            "fee_includes_tax": fee_includes_tax,
        },
        "estimated": None
        if estimated is None
        else {
            "amount": dec(estimated.amount),
            "rate_source": estimated.rate_source,
            "policy_version": estimated.policy_version,
            "policy_content_hash": estimated.policy_content_hash,
            "basis": estimated.basis,
        },
    }
    # Reasons only where a side is missing (a null reason would itself be a gap).
    if costs.recorded_reason is not None:
        body["recorded_reason"] = costs.recorded_reason
    if costs.estimated_reason is not None:
        body["estimated_reason"] = costs.estimated_reason
    return body


def trade_evidence_fields(
    journal: Any,
    reviews: Sequence[Any],
    info: MarketInfo | None,
    evidence: TradeEvidence,
) -> dict[str, Any]:
    """The optional TradeRow fields of one trade (those without a value are absent)."""
    fields: dict[str, Any] = dict(trade_decision_link(reviews, evidence))
    # Fees and times describe the whole trade: withhold them unless both the
    # order ledgers and the fills were read completely.
    if info is None or not evidence.fills_ok or not evidence.orders_ok:
        return fields
    fills = trade_fills(journal, reviews, info, evidence)
    # Fees and times describe the whole trade: an order whose recorded filled
    # quantity is not matched by the fills read means some fills were never
    # written, so a partial fee or time is withheld rather than reported.
    if not fills or not _orders_fill_coverage(
        _trade_orders(journal, reviews, evidence), fills, info
    ):
        return fields
    basis = trade_time_basis(fills, evidence.raw_fills)
    if basis is not None:
        fields["fill_time_basis"] = basis
    costs, includes_tax = trade_costs(fills, info)
    fields["costs"] = costs_body(costs, fee_includes_tax=includes_tax)
    return fields
