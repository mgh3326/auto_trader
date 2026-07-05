"""ROB-713 — deterministic trade-journal aggregates (expectancy / R-multiple /
MAE) over live-ledger fills. Read-only, no LLM (ROB-501), no schema change."""

from __future__ import annotations

import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import date, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.services.market_data import get_ohlcv

from app.core.symbol import to_db_symbol
from app.models.investment_reports import InvestmentReportItem
from app.models.review import (
    KISLiveOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
    TradeRetrospective,
)
from app.services.trade_journal.forecast_service import _normalize_symbol_for_filter

_EPS = 1e-9
_SMOKE_TOKENS = ("smoke",)

_MARKET_TO_INSTRUMENT = {"kr": "equity_kr", "us": "equity_us", "crypto": "crypto"}


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


def _coerce_uuid(value: str | None) -> uuid.UUID | None:
    if not value:
        return None
    try:
        return uuid.UUID(str(value))
    except (ValueError, AttributeError):
        return None


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


@dataclass(frozen=True)
class TagInfo:
    tag: str
    tag_source: str
    link_quality: str


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


async def resolve_setup_tag(
    db: AsyncSession, trade: ClosedTrade, *, window_days: int = 45
) -> TagInfo:
    """Resolve the setup tag for a closed round-trip.

    Precedence: ``strategy_key`` (exact via correlation_id, then symbol_window) →
    ``intent`` (exact via item_uuid, then symbol_window) → ``untagged``.
    """
    instrument = _MARKET_TO_INSTRUMENT.get(trade.market)
    norm = _normalize_symbol_for_filter(trade.symbol, instrument)
    window_start = trade.entry_ts - timedelta(days=window_days)

    corr_ids = [c for c in (*trade.entry_correlation_ids, trade.exit_correlation_id) if c]
    if corr_ids:
        row = (
            await db.execute(
                select(TradeRetrospective.strategy_key)
                .where(
                    TradeRetrospective.correlation_id.in_(corr_ids),
                    TradeRetrospective.strategy_key.isnot(None),
                )
                .order_by(TradeRetrospective.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if row and not _is_smoke(row):
            return TagInfo(row, "strategy_key", "exact")

    retro_key = (
        await db.execute(
            select(TradeRetrospective.strategy_key)
            .where(
                TradeRetrospective.symbol == norm,
                TradeRetrospective.strategy_key.isnot(None),
                TradeRetrospective.created_at <= trade.exit_ts,
                TradeRetrospective.created_at >= window_start,
            )
            .order_by(TradeRetrospective.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if retro_key and not _is_smoke(retro_key):
        return TagInfo(retro_key, "strategy_key", "symbol_window")

    item_uuids_raw = [u for u in (*trade.entry_item_uuids, trade.exit_item_uuid) if u]
    item_uuids = [u for u in (uid if isinstance(uid, uuid.UUID) else _coerce_uuid(uid) for uid in item_uuids_raw) if u is not None]
    if item_uuids:
        intent = (
            await db.execute(
                select(InvestmentReportItem.intent)
                .where(InvestmentReportItem.item_uuid.in_(item_uuids))
                .order_by(InvestmentReportItem.created_at.desc())
                .limit(1)
            )
        ).scalar_one_or_none()
        if intent:
            return TagInfo(intent, "intent", "exact")

    intent_win = (
        await db.execute(
            select(InvestmentReportItem.intent)
            .where(
                InvestmentReportItem.symbol == norm,
                InvestmentReportItem.created_at <= trade.entry_ts,
                InvestmentReportItem.created_at >= window_start,
            )
            .order_by(InvestmentReportItem.created_at.desc())
            .limit(1)
        )
    ).scalar_one_or_none()
    if intent_win:
        return TagInfo(intent_win, "intent", "symbol_window")

    return TagInfo("untagged", "untagged", "symbol_window")

_MAX_OHLCV_BARS = 200


def compute_r_multiple(trade: ClosedTrade, planned_stop: float | None) -> float | None:
    if planned_stop is None:
        return None
    risk = abs(trade.entry_price - planned_stop)
    if risk <= _EPS:
        return None
    return (trade.exit_price - trade.entry_price) / risk


async def planned_stop_for(
    db: AsyncSession, trade: ClosedTrade, *, window_days: int = 45
) -> float | None:
    instrument = _MARKET_TO_INSTRUMENT.get(trade.market)
    norm = _normalize_symbol_for_filter(trade.symbol, instrument)
    window_start = trade.entry_ts - timedelta(days=window_days)

    item_uuids_raw = [u for u in (*trade.entry_item_uuids, trade.exit_item_uuid) if u]
    item_uuids = [
        u for u in (
            x if isinstance(x, uuid.UUID) else _coerce_uuid(x)
            for x in item_uuids_raw
        )
        if u is not None
    ]
    if item_uuids:
        stmt = (
            select(InvestmentReportItem.evidence_snapshot)
            .where(InvestmentReportItem.item_uuid.in_(item_uuids))
            .order_by(InvestmentReportItem.created_at.desc())
            .limit(1)
        )
    else:
        stmt = (
            select(InvestmentReportItem.evidence_snapshot)
            .where(
                InvestmentReportItem.symbol == norm,
                InvestmentReportItem.created_at <= trade.entry_ts,
                InvestmentReportItem.created_at >= window_start,
            )
            .order_by(InvestmentReportItem.created_at.desc())
            .limit(1)
        )
    snapshot = (await db.execute(stmt)).scalar_one_or_none()
    if not isinstance(snapshot, dict):
        return None
    stop = (snapshot.get("trade_setup") or {}).get("stop")
    try:
        return float(stop) if stop is not None else None
    except (TypeError, ValueError):
        return None


async def compute_excursions(
    trade: ClosedTrade,
) -> tuple[float | None, float | None, bool]:
    """Compute MAE / MFE over daily candles spanning the trade.

    Returns ``(mae, mfe, degraded)``. When the trade spans >200 trading days the
    result carries ``degraded=True`` (we cap the candle fetch at 200 bars).
    """

    span_days = (trade.exit_ts.date() - trade.entry_ts.date()).days + 1
    degraded = span_days > _MAX_OHLCV_BARS
    count = min(max(span_days + 2, 2), _MAX_OHLCV_BARS)
    candles = await get_ohlcv(
        trade.symbol, trade.market, period="day", count=count, end=trade.exit_ts
    )
    window = [
        c
        for c in candles
        if trade.entry_ts.date() <= c.timestamp.date() <= trade.exit_ts.date()
    ]
    if not window:
        return None, None, degraded
    entry = trade.entry_price
    if entry <= _EPS:
        return None, None, degraded
    mae = (min(float(c.low) for c in window) - entry) / entry
    mfe = (max(float(c.high) for c in window) - entry) / entry
    return mae, mfe, degraded
