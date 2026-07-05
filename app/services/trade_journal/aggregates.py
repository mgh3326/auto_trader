"""ROB-713 — deterministic trade-journal aggregates (expectancy / R-multiple /
MAE) over live-ledger fills. Read-only, no LLM (ROB-501), no schema change."""

from __future__ import annotations

from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import datetime

_EPS = 1e-9


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
