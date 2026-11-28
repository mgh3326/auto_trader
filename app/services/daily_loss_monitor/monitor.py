"""Run the daily-loss check once for each account (hk 7119)."""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, time
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.daily_loss_monitor.accounts import ACCOUNTS, AccountScope
from app.services.daily_loss_monitor.marks import (
    FxSnapshot,
    policy_cost_estimate,
    sell_tax_bps,
)
from app.services.daily_loss_monitor.reader import load_account_fills
from app.services.daily_loss_monitor.rules import (
    CostEstimate,
    LossResult,
    Mark,
    compute_daily_pnl,
    should_freeze,
)
from app.services.daily_loss_monitor.writer import freeze_if_active

KST = ZoneInfo("Asia/Seoul")

MarkFn = Callable[
    [AsyncSession, AccountScope, Sequence[str], datetime],
    Awaitable[dict[str, Mark | None]],
]
FxFn = Callable[[], Awaitable[FxSnapshot | None]]
CostFn = Callable[[AccountScope], CostEstimate | None]
#: Broker-reported holdings now: {symbol key: quantity}, or None when unknown.
HoldingsFn = Callable[[AccountScope], Awaitable[dict[str, Decimal] | None]]


async def _no_holdings(scope: AccountScope) -> dict[str, Decimal] | None:
    return None


def holdings_match(
    ledger: dict[str, Decimal], broker: dict[str, Decimal] | None
) -> bool:
    """The ledger position must equal the broker's, symbol by symbol."""
    if broker is None:
        return False
    keys = {k for k, v in ledger.items() if v != 0} | {
        k for k, v in broker.items() if v != 0
    }
    return all(ledger.get(k, Decimal(0)) == broker.get(k, Decimal(0)) for k in keys)


@dataclass(frozen=True)
class AccountOutcome:
    account: str
    pnl_krw: Decimal | None
    reasons: tuple[str, ...]
    freeze: bool
    frozen_now: bool
    fee_bases: tuple[str, ...] = ()
    mark_bases: tuple[str, ...] = ()
    cost_estimate: CostEstimate | None = None
    fx: FxSnapshot | None = None


def kst_day_start(now: datetime) -> datetime:
    return datetime.combine(now.astimezone(KST).date(), time(0), tzinfo=KST)


def _with_fx(marks: dict[str, Mark | None], fx: Decimal | None):
    return {
        k: None if m is None else dataclasses.replace(m, fx_krw=fx)
        for k, m in marks.items()
    }


async def evaluate_account(
    session: AsyncSession,
    scope: AccountScope,
    *,
    now: datetime,
    reference_fn: MarkFn,
    quote_fn: MarkFn,
    fx_fn: FxFn,
    cost: CostEstimate | None = None,
    holdings_fn: HoldingsFn = _no_holdings,
) -> tuple[LossResult, FxSnapshot | None]:
    try:
        fills = await load_account_fills(
            session, scope, day_start=kst_day_start(now), now=now
        )
    except Exception:  # noqa: BLE001 - an unreadable ledger is unmeasured
        return LossResult(pnl_krw=None, reasons=("ledger_unreadable",)), None
    extra = set(fills.reasons)
    try:
        broker = await holdings_fn(scope)
    except Exception:  # noqa: BLE001
        broker = None
    if not holdings_match(fills.net_quantity, broker):
        extra.add("holdings_unverified")
    symbols = sorted({f.symbol for f in (*fills.history, *fills.today)})
    snapshot: FxSnapshot | None = None
    if scope.currency != "KRW":
        try:
            snapshot = await fx_fn()
        except Exception:  # noqa: BLE001
            snapshot = None
    fx: Decimal | None = None if snapshot is None else snapshot.rate
    try:
        reference = await reference_fn(session, scope, symbols, now)
    except Exception:  # noqa: BLE001
        reference = {}
    try:
        quotes = await quote_fn(session, scope, symbols, now)
    except Exception:  # noqa: BLE001
        quotes = {}
    today = [dataclasses.replace(f, fx_krw=fx) for f in fills.today]
    result = compute_daily_pnl(
        currency=scope.currency,
        history=fills.history,
        today=today,
        reference=_with_fx(reference, fx),
        quotes=_with_fx(quotes, fx),
        now=now,
        provisional_unreconciled=fills.provisional_unreconciled,
        cost_estimate=cost,
        sell_tax_bps=sell_tax_bps(scope),
        input_reasons=tuple(sorted(extra)),
    )
    return result, snapshot


async def run_once(
    session: AsyncSession,
    *,
    now: datetime,
    reference_fn: MarkFn,
    quote_fn: MarkFn,
    fx_fn: FxFn,
    commit: bool,
    accounts: Sequence[str] | None = None,
    cost_fn: CostFn = policy_cost_estimate,
    holdings_fn: HoldingsFn = _no_holdings,
) -> list[AccountOutcome]:
    outcomes: list[AccountOutcome] = []
    for name in accounts or tuple(ACCOUNTS):
        scope = ACCOUNTS[name]
        cost = cost_fn(scope)
        result, snapshot = await evaluate_account(
            session,
            scope,
            now=now,
            reference_fn=reference_fn,
            quote_fn=quote_fn,
            fx_fn=fx_fn,
            cost=cost,
            holdings_fn=holdings_fn,
        )
        await session.rollback()
        freeze = should_freeze(result)
        frozen_now = False
        if freeze and commit:
            why = (
                f"daily_loss pnl_krw={result.pnl_krw}"
                if result.measured
                else "daily_loss_unmeasured: " + ",".join(result.reasons)
            )
            frozen_now = await freeze_if_active(session, account=name, reason=why)
            await session.commit()
        outcomes.append(
            AccountOutcome(
                name,
                result.pnl_krw,
                result.reasons,
                freeze,
                frozen_now,
                result.fee_bases,
                result.mark_bases,
                cost if "estimated" in result.fee_bases else None,
                snapshot,
            )
        )
    return outcomes
