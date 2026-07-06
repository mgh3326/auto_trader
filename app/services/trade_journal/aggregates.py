"""ROB-713 — deterministic trade-journal aggregates (expectancy / R-multiple /
MAE) over live-ledger fills. Read-only, no LLM (ROB-501), no schema change."""

from __future__ import annotations

import copy
import uuid
from collections import defaultdict, deque
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from statistics import fmean, median
from typing import Any

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.symbol import to_db_symbol
from app.models.investment_reports import InvestmentReportItem
from app.models.review import (
    KISLiveOrderLedger,
    KISMockOrderLedger,
    LiveOrderLedger,
    TossLiveOrderLedger,
    TradeRetrospective,
)
from app.models.trading import InstrumentType
from app.services.market_data import get_ohlcv
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
    return getattr(row, "account_scope", None) or getattr(row, "broker", None) or source


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
    cohort: str = "live_gated"
    source_bucket: str | None = None


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
    cohort: str = "live_gated"
    source_bucket: str | None = None


@dataclass
class _Lot:
    qty: float
    orig_qty: float
    price: float
    fee: float
    ts: datetime
    item_uuid: str | None
    correlation_id: str | None
    cohort: str = "live_gated"
    source_bucket: str | None = None


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
    cohort: str = "live_gated",
) -> list[Fill]:
    """Read filled rows from the three live order ledgers and normalize to ``Fill``.

    ``account_mode`` is accepted for API symmetry but not currently used for
    filtering — the live ledgers store account via ``account_mode`` /
    ``account_scope`` / ``broker`` (see ``_account_of``) and the FIFO pairing
    downstream already segregates lots per account label.
    """
    fills: list[Fill] = []

    if cohort in ("live_gated", "all"):
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

                if _is_smoke(
                    getattr(r, "correlation_id", None),
                    getattr(r, "status", None),
                    getattr(r, "reason", None),
                    getattr(r, "thesis", None),
                    getattr(r, "strategy", None),
                    getattr(r, "notes", None),
                ):
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
                        price=float(r.avg_fill_price)
                        if r.avg_fill_price is not None
                        else 0.0,
                        fee=_fee_of(r),
                        ts=r.trade_date,
                        item_uuid=str(item_uuid) if item_uuid else None,
                        correlation_id=corr,
                        source=source,
                        cohort="live_gated",
                    )
                )

    if cohort in ("mock_counterfactual", "all") and account_mode in (None, "kis_mock"):
        from app.mcp_server.tooling.kis_mock_ledger import _derive_shadow_fill

        stmt = select(KISMockOrderLedger).where(
            KISMockOrderLedger.mirror_cohort == "mock_counterfactual",
            KISMockOrderLedger.lifecycle_state == "fill",
        )
        rows = (await db.execute(stmt)).scalars().all()
        for r in rows:
            row_market = "kr" if r.instrument_type == InstrumentType.equity_kr else "us"
            if market and row_market != market:
                continue
            if r.trade_date is not None:
                d = r.trade_date.date()
                if date_from and d < date_from:
                    continue
                if date_to and d > date_to:
                    continue

            filled_qty, _remaining, status = _derive_shadow_fill(r, float(r.quantity))
            if status not in {"filled", "partial"} or filled_qty <= 0:
                continue

            fills.append(
                Fill(
                    market=row_market,
                    symbol=to_db_symbol(r.symbol),
                    account="kis_mock",
                    side=r.side,
                    qty=filled_qty,
                    price=float(r.price),
                    fee=float(r.fee or 0),
                    ts=r.trade_date,
                    item_uuid=str(r.report_item_uuid) if r.report_item_uuid else None,
                    correlation_id=r.correlation_id,
                    source="kis_mock",
                    cohort="mock_counterfactual",
                    source_bucket=r.mirror_source_bucket,
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
                    _Lot(
                        f.qty,
                        f.qty,
                        f.price,
                        f.fee,
                        f.ts,
                        f.item_uuid,
                        f.correlation_id,
                        f.cohort,
                        f.source_bucket,
                    )
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

            entry_cohorts = {lot.cohort for _, lot in consumed}
            resolved_cohort = entry_cohorts.pop() if len(entry_cohorts) == 1 else "mixed"

            entry_source_buckets = {lot.source_bucket for _, lot in consumed if lot.source_bucket}
            resolved_source_bucket = entry_source_buckets.pop() if len(entry_source_buckets) == 1 else None

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
                    pnl_pct=(f.price - entry_price) / entry_price
                    if entry_price
                    else 0.0,
                    fees=fees,
                    entry_item_uuids=tuple(
                        dict.fromkeys(
                            lot.item_uuid for _, lot in consumed if lot.item_uuid
                        )
                    ),
                    exit_item_uuid=f.item_uuid,
                    entry_correlation_ids=tuple(
                        dict.fromkeys(
                            lot.correlation_id
                            for _, lot in consumed
                            if lot.correlation_id
                        )
                    ),
                    exit_correlation_id=f.correlation_id,
                    cohort=resolved_cohort,
                    source_bucket=resolved_source_bucket,
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

    corr_ids = [
        c for c in (*trade.entry_correlation_ids, trade.exit_correlation_id) if c
    ]
    if corr_ids:
        row = (
            await db.execute(
                select(TradeRetrospective.strategy_key)
                .where(
                    TradeRetrospective.correlation_id.in_(corr_ids),
                    TradeRetrospective.strategy_key.isnot(None),
                    TradeRetrospective.account_mode == trade.account,
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
    item_uuids = [
        u
        for u in (
            uid if isinstance(uid, uuid.UUID) else _coerce_uuid(uid)
            for uid in item_uuids_raw
        )
        if u is not None
    ]
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
        u
        for u in (
            x if isinstance(x, uuid.UUID) else _coerce_uuid(x) for x in item_uuids_raw
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


_INSUFFICIENT_SAMPLE_N = 10
_SCOREBOARD_TTL_SECONDS = 300
_scoreboard_cache: dict[tuple, tuple[float, dict]] = {}


@dataclass
class TradeMetrics:
    trade: ClosedTrade
    tag: TagInfo
    r_multiple: float | None
    mae: float | None
    mfe: float | None
    degraded: bool = False


def _agg_one(tag: str, rows: list[TradeMetrics]) -> dict:
    pnls = [r.trade.pnl_pct for r in rows if r.trade.pnl_pct is not None]
    wins = [p for p in pnls if p > 0]
    losses = [p for p in pnls if p <= 0]
    gross_win = sum(r.trade.pnl_abs for r in rows if r.trade.pnl_abs > 0)
    gross_loss = abs(sum(r.trade.pnl_abs for r in rows if r.trade.pnl_abs < 0))
    rs = [r.r_multiple for r in rows if r.r_multiple is not None]
    maes = [r.mae for r in rows if r.mae is not None]
    mfes = [r.mfe for r in rows if r.mfe is not None]
    n = len(rows)
    sources = {r.tag.tag_source for r in rows}
    quals = {r.tag.link_quality for r in rows}
    return {
        "tag": tag,
        "tag_source": next(iter(sources)) if len(sources) == 1 else "mixed",
        "link_quality": "exact" if quals == {"exact"} else "symbol_window",
        "n": n,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": (len(wins) / len(pnls)) if pnls else None,
        "expectancy_pct": fmean(pnls) if pnls else None,
        "expectancy_r": fmean(rs) if rs else None,
        "profit_factor": (gross_win / gross_loss) if gross_loss > _EPS else None,
        "avg_r": fmean(rs) if rs else None,
        "median_r": median(rs) if rs else None,
        "r_coverage": (len(rs) / n) if n else None,
        "excursions_degraded": sum(1 for r in rows if r.degraded),
        "avg_mae": fmean(maes) if maes else None,
        "avg_mfe": fmean(mfes) if mfes else None,
        "worst_mae": min(maes) if maes else None,
        "insufficient_sample": n < _INSUFFICIENT_SAMPLE_N,
    }


def aggregate_by_tag(rows: list[TradeMetrics]) -> list[dict]:
    by_tag: dict[str, list[TradeMetrics]] = defaultdict(list)
    for r in rows:
        by_tag[r.tag.tag].append(r)
    groups = [_agg_one(tag, tag_rows) for tag, tag_rows in by_tag.items()]
    groups.sort(key=lambda g: g["n"], reverse=True)
    return groups


async def build_trading_scoreboard(
    db: AsyncSession,
    *,
    market: str | None = None,
    account_mode: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    setup_tag: str | None = None,
    min_sample: int = 1,
    include_excursions: bool = True,
    use_cache: bool = True,
    now: datetime | None = None,
    cohort: str = "live_gated",
) -> dict:
    """Compute per-tag setup aggregates from live-ledger fills.

    ``now`` is exposed for tests so the TTL comparison is deterministic; in
    production the orchestrator defaults to ``datetime.now(timezone.utc)``.
    """
    key = (
        market,
        account_mode,
        date_from,
        date_to,
        setup_tag,
        min_sample,
        include_excursions,
        cohort,
    )
    stamp = (now or datetime.now(UTC)).timestamp()
    if use_cache:
        cached = _scoreboard_cache.get(key)
        if cached and stamp - cached[0] < _SCOREBOARD_TTL_SECONDS:
            return copy.deepcopy(cached[1])

    fills = await load_fills(
        db,
        market=market,
        account_mode=account_mode,
        date_from=date_from,
        date_to=date_to,
        cohort=cohort,
    )
    trades = pair_fills_fifo(fills)
    rows: list[TradeMetrics] = []
    for t in trades:
        try:
            tag = await resolve_setup_tag(db, t)
        except Exception:
            tag = TagInfo("untagged", "untagged", "symbol_window")
        try:
            stop = await planned_stop_for(db, t)
        except Exception:
            stop = None
        mae, mfe = None, None
        degraded = False
        if include_excursions:
            try:
                mae, mfe, degraded = await compute_excursions(t)
            except Exception:
                mae, mfe, degraded = None, None, False
        rows.append(
            TradeMetrics(t, tag, compute_r_multiple(t, stop), mae, mfe, degraded)
        )

    groups = aggregate_by_tag(rows)
    if setup_tag:
        groups = [g for g in groups if g["tag"] == setup_tag]
    groups = [g for g in groups if g["n"] >= min_sample]
    result = {
        "groups": groups,
        "overall": _agg_one("__overall__", rows) if rows else None,
        "as_of": (now or datetime.now(UTC)).isoformat(),
        "count": len(rows),
    }
    if use_cache:
        _scoreboard_cache[key] = (stamp, copy.deepcopy(result))
    return result


def _pair_by_entry_correlation(
    live_trades: list[ClosedTrade],
    mock_trades: list[ClosedTrade],
) -> list[tuple[ClosedTrade, ClosedTrade]]:
    live_by_corr = {}
    for t in live_trades:
        corr_id = next((c for c in t.entry_correlation_ids if c), None)
        if corr_id:
            live_by_corr[corr_id] = t

    paired = []
    for t in mock_trades:
        corr_id = next((c for c in t.entry_correlation_ids if c), None)
        if corr_id and corr_id in live_by_corr:
            paired.append((live_by_corr[corr_id], t))

    return paired


def _paired_delta(paired: list[tuple[ClosedTrade, ClosedTrade]]) -> dict[str, Any]:
    n = len(paired)
    if n == 0:
        return {
            "mock_minus_live_expectancy_pct": 0.0,
            "mock_minus_live_hit_rate": 0.0,
            "paired_n": 0,
        }

    mock_pnl_pcts = [mock.pnl_pct for _, mock in paired]
    live_pnl_pcts = [live.pnl_pct for live, _ in paired]

    mock_avg_pnl = sum(mock_pnl_pcts) / n
    live_avg_pnl = sum(live_pnl_pcts) / n
    mock_minus_live_expectancy_pct = mock_avg_pnl - live_avg_pnl

    mock_wins = sum(1 for _, mock in paired if mock.pnl_abs > 0)
    live_wins = sum(1 for live, _ in paired if live.pnl_abs > 0)

    mock_hit_rate = mock_wins / n
    live_hit_rate = live_wins / n
    mock_minus_live_hit_rate = mock_hit_rate - live_hit_rate

    return {
        "mock_minus_live_expectancy_pct": float(mock_minus_live_expectancy_pct),
        "mock_minus_live_hit_rate": float(mock_minus_live_hit_rate),
        "paired_n": n,
    }


async def build_counterfactual_delta_scoreboard(
    db: AsyncSession,
    *,
    market: str | None = None,
    date_from: date | None = None,
    date_to: date | None = None,
    include_excursions: bool = False,
    use_cache: bool = True,
) -> dict[str, Any]:
    board_live = await build_trading_scoreboard(
        db,
        market=market,
        date_from=date_from,
        date_to=date_to,
        include_excursions=include_excursions,
        cohort="live_gated",
        use_cache=use_cache,
    )
    board_mock = await build_trading_scoreboard(
        db,
        market=market,
        account_mode="kis_mock",
        date_from=date_from,
        date_to=date_to,
        include_excursions=include_excursions,
        cohort="mock_counterfactual",
        use_cache=use_cache,
    )
    live_trades = pair_fills_fifo(
        await load_fills(db, market=market, date_from=date_from, date_to=date_to, cohort="live_gated")
    )
    mock_trades = pair_fills_fifo(
        await load_fills(db, market=market, date_from=date_from, date_to=date_to, cohort="mock_counterfactual")
    )
    paired = _pair_by_entry_correlation(live_trades, mock_trades)
    return {
        "live_gated": board_live,
        "mock_counterfactual": board_mock,
        "paired_count": len(paired),
        "overall_delta": _paired_delta(paired),
        "caveats": [
            "KIS mock fills do not model queue priority, liquidity, slippage, or market impact; mock performance is upward biased."
        ],
    }
