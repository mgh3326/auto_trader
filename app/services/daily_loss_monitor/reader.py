"""Ledger read for one account (SELECT only, in-effect rows only).

Every shape this reader cannot prove becomes a closed reason instead of a
number (hk 7119 review, PR #2714):

* the latest opening seed per symbol is a position snapshot: earlier rows are
  dropped (``kis_lots._apply_seed_cutover`` rule); a seed inside the day means
  the midnight position is unknown;
* rows of one broker order (``supersede.order_identity``) are folded into one
  fill: KIS reconciler / Upbit ``fill_seq=0`` rows are cumulative (exactly one
  is accepted), Toss rows priced at the cumulative average are rebuilt from the
  previous -> cumulative chain;
* a websocket row counts as covered only when an authoritative row of the same
  identity books at least the websocket quantity; any uncovered websocket row
  after the seed cutover (not only today's) is unreconciled;
* a fill whose time basis is not the broker fill time and that could sit on
  either side of KST midnight is unproven (KR sessions never cross midnight);
* a successful, recent non-dry-run reconcile run of the broker is required.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation
from types import SimpleNamespace
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol, to_upbit_symbol
from app.models.execution_ledger import (
    ExecutionLedger,
    ExecutionLedgerReconcileRun,
    execution_ledger_in_effect,
)
from app.services.daily_loss_monitor.accounts import AccountScope
from app.services.daily_loss_monitor.rules import Fill
from app.services.execution_ledger import supersede

AUTHORITATIVE_SOURCES: tuple[str, ...] = ("reconciler", "manual_import")
SEED_PREFIX = "SEED-"
#: Same cap as the KIS lots reader (``kis_lots.FRESH_MAX_MINUTES``).
RECONCILE_MAX_AGE = timedelta(minutes=90)
_AMBIGUOUS_BEFORE = timedelta(days=1)
_OBSERVED_LAG = timedelta(hours=1)


@dataclass(frozen=True)
class AccountFills:
    history: list[Fill]
    today: list[Fill]
    #: Websocket order identities no authoritative row covers yet.
    unreconciled_orders: frozenset[str] = frozenset()
    reasons: tuple[str, ...] = ()
    #: Signed quantity per symbol across every counted fill (for the holdings check).
    net_quantity: dict[str, Decimal] = field(default_factory=dict)

    @property
    def provisional_unreconciled(self) -> bool:
        return bool(self.unreconciled_orders)


def _aware(when: datetime) -> datetime:
    return when if when.tzinfo else when.replace(tzinfo=UTC)


def symbol_key(scope: AccountScope, symbol: str) -> str:
    raw = str(symbol).strip()
    if scope.instrument_type == "equity_us":
        return to_db_symbol(raw).upper()
    if scope.instrument_type == "crypto":
        return to_upbit_symbol(raw)
    return raw.upper()


def _is_seed(row: Any) -> bool:
    return row.source == "manual_import" and str(row.broker_order_id).startswith(
        SEED_PREFIX
    )


def _is_cumulative(row: Any) -> bool:
    if row.source != "reconciler":
        return False
    return row.broker == "kis" or (row.broker == "upbit" and row.fill_seq == 0)


def _toss_chain(rows: list[Any]) -> tuple[Decimal, Decimal] | None | bool:
    """``(qty, avg)`` of a cumulative-priced Toss order, None if broken, False if n/a."""
    payloads = [r.raw_payload_json or {} for r in rows]
    marked = [
        p
        for p in payloads
        if "broker_cumulative_filled_qty" in p and "previous_filled_qty" in p
    ]
    if not marked:
        return False
    if len(marked) != len(rows):
        return None
    try:
        chain = sorted(
            (
                Decimal(str(p["broker_cumulative_filled_qty"])),
                Decimal(str(p["previous_filled_qty"])),
                r.filled_price,
            )
            for p, r in zip(payloads, rows, strict=True)
        )
    except (InvalidOperation, TypeError, ValueError):
        return None
    expected = Decimal(0)
    for cum, prev, _ in chain:
        if prev != expected or cum <= prev:
            return None
        expected = cum
    return expected, chain[-1][2]


def _sum_or_none(values: list[Decimal | None]) -> Decimal | None:
    if any(v is None for v in values):
        return None
    return sum((v for v in values if v is not None), Decimal(0))


def _fee(row: Any) -> Decimal | None:
    return None if row.fee_basis in (None, "not_provided") else row.fee_amount


def _fill_time_unproven(scope: AccountScope, row: Any, day_start: datetime) -> bool:
    if (
        scope.instrument_type == "equity_kr"
        or row.filled_at_basis == "broker_fill_time"
    ):
        return False
    at = _aware(row.filled_at)
    if row.filled_at_basis == "broker_order_time":
        return day_start - _AMBIGUOUS_BEFORE <= at < day_start
    if row.filled_at_basis == "observed_at":
        return day_start <= at < day_start + _OBSERVED_LAG
    return day_start - _AMBIGUOUS_BEFORE <= at < day_start + _OBSERVED_LAG


def _order_fill(scope: AccountScope, rows: list[Any]) -> Fill | None:
    """One fill for one order's authoritative rows; None when not provable."""
    first = rows[0]
    at = max(_aware(r.filled_at) for r in rows)
    fee = _sum_or_none([_fee(r) for r in rows])
    tax = _sum_or_none([r.tax_amount for r in rows])
    currencies = {r.fee_currency for r in rows}
    fee_currency = currencies.pop() if len(currencies) == 1 else None
    cumulative = [r for r in rows if _is_cumulative(r)]
    if cumulative:
        if len(cumulative) != len(rows) or len(rows) != 1:
            return None
        qty, price = first.filled_qty, first.filled_price
    elif first.broker == "toss":
        chain = _toss_chain(rows)
        if chain is None:
            return None
        if chain is False:
            qty = sum((r.filled_qty for r in rows), Decimal(0))
            notional = sum((r.filled_qty * r.filled_price for r in rows), Decimal(0))
            price = notional / qty if qty else Decimal(0)
        else:
            qty, price = chain  # type: ignore[misc]
    elif len(rows) == 1 or first.broker == "upbit":
        qty = sum((r.filled_qty for r in rows), Decimal(0))
        notional = sum((r.filled_qty * r.filled_price for r in rows), Decimal(0))
        price = notional / qty if qty else Decimal(0)
    else:
        return None
    if qty <= 0:
        return None
    return Fill(
        symbol=symbol_key(scope, first.symbol),
        side=first.side,
        quantity=qty,
        price=price,
        fee=fee,
        tax=tax,
        fee_currency=fee_currency if fee is not None else first.currency,
        currency=first.currency,
        filled_at=at,
        fx_krw=None,
    )


async def _reconcile_fresh(
    session: AsyncSession, scope: AccountScope, now: datetime
) -> bool:
    finished = (
        await session.execute(
            select(func.max(ExecutionLedgerReconcileRun.finished_at)).where(
                ExecutionLedgerReconcileRun.broker == scope.broker,
                ExecutionLedgerReconcileRun.dry_run.is_(False),
                ExecutionLedgerReconcileRun.finished_at.is_not(None),
            )
        )
    ).scalar_one_or_none()
    if finished is None:
        return False
    age = now - _aware(finished)
    return timedelta(0) <= age <= RECONCILE_MAX_AGE


async def load_account_fills(
    session: AsyncSession,
    scope: AccountScope,
    *,
    day_start: datetime,
    now: datetime | None = None,
) -> AccountFills:
    rows = (
        (
            await session.execute(
                select(ExecutionLedger)
                .where(
                    execution_ledger_in_effect(),
                    ExecutionLedger.broker == scope.broker,
                    ExecutionLedger.account_mode == "live",
                    ExecutionLedger.instrument_type == scope.instrument_type,
                )
                .order_by(ExecutionLedger.filled_at, ExecutionLedger.id)
            )
        )
        .scalars()
        .all()
    )
    reasons: set[str] = set()
    if now is not None and not await _reconcile_fresh(session, scope, now):
        reasons.add("reconcile_unproven")

    by_symbol: dict[str, list[Any]] = defaultdict(list)
    for row in rows:
        by_symbol[symbol_key(scope, row.symbol)].append(row)

    history: list[Fill] = []
    today: list[Fill] = []
    unreconciled: set[str] = set()
    net: dict[str, Decimal] = {}
    for symbol, symbol_rows in sorted(by_symbol.items()):
        seeds = [r for r in symbol_rows if _is_seed(r)]
        cutover = max((_aware(r.filled_at) for r in seeds), default=None)
        live = [
            r for r in symbol_rows if cutover is None or _aware(r.filled_at) >= cutover
        ]
        if cutover is not None and cutover >= day_start:
            reasons.add("seed_within_day")
        authority: dict[Any, list[Any]] = defaultdict(list)
        provisional: dict[Any, list[Any]] = defaultdict(list)
        for row in live:
            if _is_seed(row):
                continue
            ident = supersede.order_identity(
                SimpleNamespace(
                    broker=row.broker,
                    account_mode=row.account_mode,
                    instrument_type=row.instrument_type,
                    symbol=symbol,
                    side=row.side,
                    currency=row.currency,
                    broker_order_id=row.broker_order_id,
                    filled_at=_aware(row.filled_at),
                )
            )
            if row.source in AUTHORITATIVE_SOURCES:
                authority[ident].append(row)
            else:
                provisional[ident].append(row)
            if _fill_time_unproven(scope, row, day_start):
                reasons.add("fill_day_unproven")

        fills: list[Fill] = [
            Fill(
                symbol=symbol,
                side="buy",
                quantity=r.filled_qty,
                price=r.filled_price,
                fee=Decimal(0),
                tax=Decimal(0),
                fee_currency=r.currency,
                currency=r.currency,
                filled_at=_aware(r.filled_at),
                fx_krw=None,
            )
            for r in seeds
            if _aware(r.filled_at) == cutover
        ]
        for order_rows in authority.values():
            fill = _order_fill(scope, order_rows)
            if fill is None:
                reasons.add("order_evidence_unproven")
                continue
            fills.append(fill)
        for ident, ws_rows in provisional.items():
            booked = sum((r.filled_qty for r in authority.get(ident, [])), Decimal(0))
            if ident not in authority:
                booked = Decimal(0)
            elif any(_is_cumulative(r) for r in authority[ident]):
                booked = max(r.filled_qty for r in authority[ident])
            if sum((r.filled_qty for r in ws_rows), Decimal(0)) > booked:
                unreconciled.add("|".join(str(part) for part in ident))
        for fill in fills:
            sign = Decimal(1) if fill.side == "buy" else Decimal(-1)
            net[symbol] = net.get(symbol, Decimal(0)) + sign * fill.quantity
            (today if fill.filled_at >= day_start else history).append(fill)
    return AccountFills(
        history=sorted(history, key=lambda f: f.filled_at),
        today=sorted(today, key=lambda f: f.filled_at),
        unreconciled_orders=frozenset(unreconciled),
        reasons=tuple(sorted(reasons)),
        net_quantity=net,
    )
