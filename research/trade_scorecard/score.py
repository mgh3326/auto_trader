"""Offline 90-day KR scorecard over the canonical trade_retro fill stream."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from math import isfinite
from zoneinfo import ZoneInfo

from research.trade_retro.canonical import Cleaning, apply_splits, build_canon
from research.trade_retro.export import Export, ExportIntegrityError
from research.trade_retro.positions import walk_symbol
from research.trade_retro.prices import Bars, PriceBook


@dataclass(frozen=True)
class Decision:
    rationale: str | None
    limit: float | None
    quantity: float | None
    origin: str | None


@dataclass(frozen=True)
class Cashflows:
    dividends: float | None
    costs: float | None


@dataclass(frozen=True)
class Declined:
    decision_ref: str
    symbol: str
    decided_at: datetime
    side: str | None
    limit: float | None
    quantity: float | None
    rationale: str | None
    condition_status: str | None
    next_day_review_status: str | None


@dataclass(frozen=True)
class Evidence:
    """Verified projections for one export/account, supplied by a future reader.

    Decision keys are canonical order keys; peers are dated, point-in-time
    sector membership excluding the target. Cashflows are allocated to the
    exact FIFO slice, identified by symbol, lot sequence and exit order key
    (None for the open slice).
    """

    declined: tuple[Declined, ...] = ()
    decisions: dict[str, Decision] = field(default_factory=dict)
    peers: dict[tuple[str, date], tuple[str, ...]] = field(default_factory=dict)
    cashflows: dict[tuple[str, int, str | None], Cashflows] = field(
        default_factory=dict
    )


def _index(bars: Bars | None, day: date) -> int | None:
    if bars is None:
        return None
    i = bars.idx_on_or_after(day)
    return i if i is not None and bars.date_at(i) == day else None


def _price(bars: Bars | None, day: date, *, opening: bool = False) -> float | None:
    i = _index(bars, day)
    if bars is None or i is None:
        return None
    value = float((bars.open if opening else bars.close)[i])
    return value if isfinite(value) and value > 0 else None


def _return(start: float | None, end: float | None) -> float | None:
    return end / start - 1 if start is not None and end is not None else None


def _subtract(left: float | None, right: float | None) -> float | None:
    return left - right if left is not None and right is not None else None


def selection(book: PriceBook, symbol: str, day: date, evidence: Evidence) -> dict:
    benchmark = book.benchmarks["kr"]
    bars = book.get("kr", symbol)
    i = _index(benchmark, day)
    peers = evidence.peers.get((symbol, day))
    results = {}
    for horizon in (5, 20):
        maturity = (
            benchmark.date_at(i + horizon)
            if i is not None and i + horizon < len(benchmark)
            else None
        )
        stock = index = sector = None
        if maturity is not None:
            stock = _return(_price(bars, day), _price(bars, maturity))
            index = _return(_price(benchmark, day), _price(benchmark, maturity))
            peer_returns = [
                _return(_price(peer, day), _price(peer, maturity))
                for code in (peers or ())
                if (peer := book.get("kr", code)) is not None
            ]
            if (
                peers
                and len(peer_returns) == len(peers)
                and all(value is not None for value in peer_returns)
            ):
                sector = sum(
                    value for value in peer_returns if value is not None
                ) / len(peers)
        results[str(horizon)] = {
            "return": stock,
            "missing_reason": "no_price" if stock is None or index is None else None,
            "excess_index": _subtract(stock, index),
            "excess_sector": _subtract(stock, sector),
            "sector_missing_reason": (
                "no_sector" if not peers else "no_price" if sector is None else None
            ),
        }
    return results


def _entries(rows, orders, bars, benchmark, evidence, cutover, unknown_initial):
    """Compare each buy order with inventory before its first fill."""
    entries = {}
    lots = deque([(unknown_initial, float("nan"))] if unknown_initial else [])
    cutover_seed = ((rows["seed"]) & (rows["ts"] == cutover)).any()

    def reduce(quantity):
        while quantity > 1e-12 and lots:
            size, price = lots.popleft()
            taken = min(size, quantity)
            quantity -= taken
            if size - taken > 1e-12:
                lots.appendleft((size - taken, price))

    for row in rows.itertuples(index=False):
        if row.seed:
            delta = (
                row.qty - sum(quantity for quantity, _ in lots)
                if row.ts > cutover
                else row.qty
            )
            if delta > 0:
                lots.append((delta, row.price))
            else:
                reduce(-delta)
            continue
        if row.side == "sell":
            reduce(row.qty)
            continue
        if cutover_seed and row.ts < cutover:
            continue
        if row.key not in entries:
            held = sum(quantity for quantity, _ in lots)
            basis = sum(quantity * price for quantity, price in lots)
            entries[row.key] = {
                "type": "add" if held else "first",
                "prior_average": basis / held if held and isfinite(basis) else None,
                "day": row.date,
                "quantity": 0.0,
                "amount": 0.0,
            }
        entry = entries[row.key]
        entry["quantity"] += row.qty
        entry["amount"] += row.qty * row.price
        lots.append((row.qty, row.price))

    for key, entry in entries.items():
        day = entry["day"]
        fill = entry["amount"] / entry["quantity"]
        factor = bars.split_factor_after(day) if bars is not None else 1.0
        order = orders.get(key)
        decision = evidence.decisions.get(key)
        plan_limit = decision.limit if decision else None
        planned_qty = decision.quantity if decision else None
        order_limit = (
            order.limit_price if order and isfinite(order.limit_price) else None
        )
        order_qty = order.qty if order and isfinite(order.qty) else None
        i = _index(benchmark, day)
        tomorrow = (
            benchmark.date_at(i + 1)
            if i is not None and i + 1 < len(benchmark)
            else None
        )
        opening, close = _price(bars, day, opening=True), _price(bars, day)
        proxy = (
            (opening + close) / 2 if opening is not None and close is not None else None
        )
        prior = entry["prior_average"]
        entry.update(
            fill=fill,
            origin=(
                decision.origin
                if decision and decision.origin
                else "AT proposal"
                if order and order.proposal_ref
                else "unknown"
            ),
            rationale=(
                decision.rationale if decision and decision.rationale else "근거 미기록"
            ),
            decision_missing_reason=None if decision else "no_decision_evidence",
            fill_vs_plan_limit=_return(
                plan_limit / factor if plan_limit else None, fill
            ),
            fill_vs_order_limit=_return(
                order_limit / factor if order_limit else None, fill
            ),
            size_vs_plan=_return(
                planned_qty * factor if planned_qty else None, entry["quantity"]
            ),
            size_vs_order=_return(
                order_qty * factor if order_qty else None, entry["quantity"]
            ),
            fill_vs_open=_return(opening, fill),
            fill_vs_vwap_proxy=_return(proxy, fill),
            fill_vs_next_open=_return(
                _price(bars, tomorrow, opening=True) if tomorrow else None, fill
            ),
            average_down=fill < prior
            if entry["type"] == "add" and prior is not None
            else None,
        )
    return entries


def score(
    export: Export,
    book: PriceBook,
    account: str,
    until: date,
    evidence: Evidence | None = None,
) -> list[dict]:
    """Score exit dates in [until-90d, until) and holdings at that cutoff."""
    evidence = evidence or Evidence()
    since = until - timedelta(days=90)
    zone = ZoneInfo("Asia/Seoul")
    window = export.manifest["window"]
    if datetime.fromisoformat(window["since_utc"]) > datetime.combine(
        since, time(), zone
    ) or datetime.fromisoformat(window["until_utc"]) < datetime.combine(
        until, time(), zone
    ):
        raise ExportIntegrityError(
            "export window does not cover the full KR scoring window"
        )
    canon = build_canon(export, Cleaning(preserve_fill_time=True))
    fills = canon.fills[
        (canon.fills["market"] == "kr") & (canon.fills["alias"] == account)
    ]
    fills, _ = apply_splits(fills, book)
    fills = fills[fills["date"] < until]
    orders = {
        row.key: row
        for row in canon.orders.itertuples(index=False)
        if row.alias == account and row.market == "kr"
    }
    benchmark = book.benchmarks["kr"]
    complete_through_cutoff = date.fromisoformat(book.meta["end_exclusive"]) >= until
    mark_day = (
        benchmark.idx_on_or_before(until - timedelta(days=1))
        if complete_through_cutoff
        else None
    )
    day = (
        benchmark.date_at(mark_day)
        if mark_day is not None
        else until - timedelta(days=1)
    )
    cards = []
    cutover = fills.loc[fills["seed"], "ts"].min()
    for symbol, rows in fills.groupby("symbol", sort=True):
        rows = rows.sort_values(["ts", "side"], kind="stable")
        bars = book.get("kr", symbol)
        mark = _price(bars, day) if mark_day is not None else None
        walk = walk_symbol(
            rows, cutover, (mark if mark is not None else float("nan"), day)
        )
        entries = _entries(
            rows,
            orders,
            bars,
            benchmark,
            evidence,
            cutover,
            sum(sell.qty for sell in walk.unknown_sells),
        )
        for match in walk.matches:
            if match.realized and match.exit_day < since:
                continue
            entry = entries.get(match.entry_key)
            pnl = match.qty * (match.exit_price - match.entry_price)
            pnl = pnl if isfinite(pnl) else None
            cash = evidence.cashflows.get(
                (symbol, match.lot, match.exit_key), Cashflows(None, None)
            )
            net = (
                pnl + cash.dividends - cash.costs
                if pnl is not None
                and cash.dividends is not None
                and cash.costs is not None
                else None
            )
            exit_order = orders.get(match.exit_key)
            cards.append(
                {
                    "symbol": symbol,
                    "kind": "exit" if match.realized else "open",
                    "exit_ref": match.exit_key,
                    "entry_ref": match.entry_key,
                    "exit_intent": exit_order.exit_intent or None
                    if exit_order
                    else None,
                    "date": match.exit_day,
                    "selection": selection(book, symbol, match.entry_day, evidence)
                    if entry
                    else None,
                    "entry": entry,
                    "quantity": match.qty,
                    "lot": match.lot,
                    "exit_wait": {
                        "realized": pnl if match.realized else 0.0,
                        "unrealized": 0.0 if match.realized else pnl,
                        "dividends": cash.dividends,
                        "dividend_missing_reason": "no_dividend"
                        if cash.dividends is None
                        else None,
                        "costs": cash.costs,
                        "cost_missing_reason": "no_cost"
                        if cash.costs is None
                        else None,
                        "net": net,
                        "add_money_gross_pnl": pnl
                        if entry and entry["type"] == "add"
                        else None,
                    },
                    "entry_missing_reason": None if entry else "no_decision_evidence",
                }
            )
        for sell in walk.unknown_sells:
            if sell.day >= since:
                cards.append(
                    {
                        "symbol": symbol,
                        "kind": "exit",
                        "date": sell.day,
                        "selection": None,
                        "entry": None,
                        "entry_missing_reason": "no_decision_evidence",
                        "exit_wait": {
                            "realized": None,
                            "basis_missing_reason": "no_cost_basis",
                            "dividend_missing_reason": "no_dividend",
                            "cost_missing_reason": "no_cost",
                        },
                    }
                )
    return cards
