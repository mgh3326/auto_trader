"""ROB-713 — deterministic trade-journal aggregates (expectancy / R-multiple /
MAE) over live-ledger fills. Read-only, no LLM (ROB-501), no schema change."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
)

_EPS = 1e-9
_SMOKE_TOKENS = ("smoke",)


def _is_smoke(*values: str | None) -> bool:
    return any(v and any(tok in v.lower() for tok in _SMOKE_TOKENS) for v in values)


def _fee_of(row: object) -> float:
    total = 0.0
    for attr in ("fee", "commission", "tax"):
        val = getattr(row, attr, None)
        if val is not None:
            total += float(val)
    return total


def _market_for(source: str, row: object) -> str:
    if source == "kis":
        return "kr"
    raw = (getattr(row, "market", None) or "").lower()
    if source == "toss":
        return "us" if raw == "us" else "kr"
    return "crypto" if raw == "crypto" else "us"  # live ledger


def _account_of(source: str, row: object) -> str:
    return (
        getattr(row, "account_scope", None)
        or getattr(row, "broker", None)
        or source
    )


@dataclass(frozen=True)
class Fill:
    market: str
    symbol: str
    account: str
    side: str
    qty: float
    price: float
    fee: float
    ts: datetime
    item_uuid: str | None
    correlation_id: str | None
    source: str


@dataclass(frozen=True)
class ClosedTrade:
    market: str
    symbol: str
    account: str
    qty: float
    entry_price: float
    exit_price: float
    entry_ts: datetime
    exit_ts: datetime
    pnl_abs: float
    pnl_pct: float
    fees: float
    entry_item_uuids: tuple[str, ...]
    exit_item_uuid: str | None
    entry_correlation_ids: tuple[str, ...]
    exit_correlation_id: str | None


@dataclass
class _Lot:
    qty: float
    orig_qty: float
    price: float
    fee: float
    ts: datetime
    item_uuid: str | None
    correlation_id: str | None


async def load_fills(
    db: AsyncSession,
    *,
    market: str | None = None,
    account_mode: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
) -> list[Fill]:
    """Read filled rows from the three live order ledgers and normalize to ``Fill``.

    ``account_mode`` is accepted for API symmetry but not currently used for
    filtering — the live ledgers store account via ``account_mode`` /
    ``account_scope`` / ``broker`` (see ``_account_of``) and the FIFO pairing
    downstream already segregates lots per account label.
    """
    fills: list[Fill] = []
    for source, model in (
        ("kis", KISLiveOrderLedger),
        ("live", LiveOrderLedger),
        ("toss", TossLiveOrderLedger),
    ):
        stmt = select(model).where(model.filled_qty.isnot(None), model.filled_qty > 0)
        rows = (await db.execute(stmt)).scalars().all()
        for r in rows:
            row_market = _market_for(source, r)
            if market and row_market != market:
                continue
            if r.trade_date is not None:
                d = r.trade_date.date()
                if date_from and d < date_from:
                    continue
                if date_to and d > date_to:
                    continue
            if _is_smoke(getattr(r, "correlation_id", None), getattr(r, "status", None)):
                continue
            corr = getattr(r, "correlation_id", None)
            item_uuid = getattr(r, "report_item_uuid", None)
            fills.append(
                Fill(
                    market=row_market,
                    symbol=to_db_symbol(r.symbol),
                    account=_account_of(source, r),
                    side=r.side,
                    qty=float(r.filled_qty),
                    price=float(r.avg_fill_price) if r.avg_fill_price is not None else 0.0,
                    fee=_fee_of(r),
                    ts=r.trade_date,
                    item_uuid=str(item_uuid) if item_uuid else None,
                    correlation_id=corr,
                    source=source,
                )
            )
    return [f for f in fills if f.price > 0 and f.ts is not None]


def pair_fills_fifo(fills: list[Fill]) -> list[ClosedTrade]:
    groups: dict[tuple[str, str, str], list[Fill]] = defaultdict(list)
    for f in fills:
        groups[(f.market, f.account, f.symbol)].append(f)

    closed: list[ClosedTrade] = []
    for (market, account, symbol), group in groups.items():
        group_sorted = sorted(group, key=lambda f: f.ts)
        open_lots: deque[_Lot] = deque()
        for f in group_sorted:
            if f.side == "buy":
                open_lots.append(
                    _Lot(f.qty, f.qty, f.price, f.fee, f.ts, f.item_uuid, f.correlation_id)
                )
                continue
            if f.side != "sell":
                continue
            remaining = f.qty
            consumed: list[tuple[float, _Lot]] = []
            while remaining > _EPS and open_lots:
                lot = open_lots[0]
                take = min(remaining, lot.qty)
                consumed.append((take, lot))
                lot.qty -= take
                remaining -= take
                if lot.qty <= _EPS:
                    open_lots.popleft()
            if not consumed:
                continue  # oversell / no matching entry (long-only)
            matched_qty = sum(t for t, _ in consumed)
            entry_price = sum(t * lot.price for t, lot in consumed) / matched_qty
            entry_ts = min(lot.ts for _, lot in consumed)
            entry_fee = sum(lot.fee * (t / lot.orig_qty) for t, lot in consumed)
            exit_fee = f.fee * (matched_qty / f.qty) if f.qty else 0.0
            fees = entry_fee + exit_fee
            gross = (f.price - entry_price) * matched_qty
            closed.append(
                ClosedTrade(
                    market=market,
                    symbol=symbol,
                    account=account,
                    qty=matched_qty,
                    entry_price=entry_price,
                    exit_price=f.price,
                    entry_ts=entry_ts,
                    exit_ts=f.ts,
                    pnl_abs=gross - fees,
                    pnl_pct=(f.price - entry_price) / entry_price if entry_price else 0.0,
                    fees=fees,
                    entry_item_uuids=tuple(
                        dict.fromkeys(lot.item_uuid for _, lot in consumed if lot.item_uuid)
                    ),
                    exit_item_uuid=f.item_uuid,
                    entry_correlation_ids=tuple(
                        dict.fromkeys(
                            lot.correlation_id for _, lot in consumed if lot.correlation_id
                        )
                    ),
                    exit_correlation_id=f.correlation_id,
                )
            )
    return closed
