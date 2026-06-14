"""Read-only projection service for execution ledger /invest fill endpoints."""

from __future__ import annotations

import logging
from collections import deque
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.execution_ledger import ExecutionLedger
from app.schemas.execution_ledger import (
    DataState,
    ExecutionLedgerFreshnessEntry,
    ExecutionLedgerFreshnessReport,
    ExecutionLedgerListResponse,
    ExecutionLedgerRead,
    SourceBreakdown,
)
from app.services.execution_ledger.repository import ExecutionLedgerRepository
from app.services.kr_symbol_universe_service import get_kr_names_by_symbols
from app.services.upbit_symbol_universe_service import get_upbit_market_display_names
from app.services.us_symbol_universe_service import get_us_names_by_symbols

logger = logging.getLogger(__name__)

_FRESH_HOURS = 48
_STALE_HOURS = 72


def _compute_source_breakdown(items: list[ExecutionLedgerRead]) -> SourceBreakdown:
    bd = SourceBreakdown()
    for item in items:
        if item.source == "reconciler":
            bd.reconciler += 1
        elif item.source == "websocket":
            bd.websocket += 1
        elif item.source == "manual_import":
            bd.manual_import += 1
    return bd


def _data_state_from_lag(lag_minutes: float | None) -> DataState:
    if lag_minutes is None:
        return "missing"
    if lag_minutes <= _FRESH_HOURS * 60:
        return "fresh"
    if lag_minutes <= _STALE_HOURS * 60:
        return "stale"
    return "missing"


def _ledger_match_key(item: ExecutionLedgerRead) -> tuple[str, str, str, str, str, str]:
    return (
        item.broker,
        item.account_mode,
        item.venue,
        item.instrument_type,
        item.symbol,
        item.currency,
    )


def _ledger_item_key(item: ExecutionLedgerRead) -> tuple[str, str, str, str, int]:
    return (
        item.broker,
        item.account_mode,
        item.venue,
        item.broker_order_id,
        item.fill_seq,
    )


# Source authority: reconciler (broker REST) and manual_import (seeded opening
# lots) are authoritative; websocket rows are provisional real-time notifications.
_PROVISIONAL_SOURCE = "websocket"


def _supersede_key(
    item: ExecutionLedgerRead,
) -> tuple[str, str, str, str, str, str]:
    """Order-level identity shared across sources for one logical order.

    Excludes fill_seq, filled_at and correlation_id on purpose: the websocket
    monitor and the reconciler derive divergent fill_seq (independent hashes) and
    timestamps for the same order, so only the order-level tuple links the two
    sources. broker_order_id is leading-zero-normalized to absorb formatting drift.

    ``venue`` is intentionally excluded: for US (KIS overseas) the websocket event
    carries no exchange code so the monitor defaults venue to ``krx`` while the
    reconciler reads ``ovrs_excg_cd`` (e.g. ``NASD``). Keying on venue would leave
    those two rows un-merged forever. (broker, account_mode, instrument_type,
    symbol, side, order_id) already disambiguates distinct orders within a broker.
    """
    normalized_order_id = item.broker_order_id.lstrip("0") or item.broker_order_id
    return (
        item.broker,
        item.account_mode,
        item.instrument_type,
        item.symbol,
        item.side,
        normalized_order_id,
    )


def _supersede_provisional_fills(
    items: list[ExecutionLedgerRead],
) -> list[ExecutionLedgerRead]:
    """Drop provisional websocket rows for orders an authoritative row covers.

    The ledger unique key excludes ``source`` and the two writers derive different
    ``fill_seq`` for the same fill, so one order can land as two+ rows. Once the
    reconciler books an order it is the authoritative record (it re-fetches the
    broker's complete filled-order set, aggregating partials), so any websocket row
    for that order is a duplicate. Websocket rows for not-yet-reconciled orders are
    preserved. Input order is preserved.
    """
    authoritative_orders = {
        _supersede_key(item) for item in items if item.source != _PROVISIONAL_SOURCE
    }
    return [
        item
        for item in items
        if item.source != _PROVISIONAL_SOURCE
        or _supersede_key(item) not in authoritative_orders
    ]


def _annotate_realized_profit(
    sell_items: list[ExecutionLedgerRead],
    history_items: list[ExecutionLedgerRead],
) -> list[ExecutionLedgerRead]:
    """Attach FIFO realized P/L to sells using earlier buy fills in the same account.

    The execution ledger is append-only fill data, so this is intentionally a
    read-model calculation. Unmatched sells remain visible with P/L fields null
    instead of guessing a cost basis.
    """
    if not sell_items:
        return sell_items

    sell_keys = {_ledger_item_key(item) for item in sell_items}
    annotations: dict[
        tuple[str, str, str, str, int], tuple[Decimal, Decimal, Decimal]
    ] = {}
    lots: dict[tuple[str, str, str, str, str, str], deque[tuple[Decimal, Decimal]]] = {}

    for item in sorted(history_items, key=lambda row: row.filled_at):
        qty = Decimal(item.filled_qty)
        if qty <= 0:
            continue
        key = _ledger_match_key(item)
        if item.side == "buy":
            unit_cost = Decimal(item.filled_notional) / qty
            lots.setdefault(key, deque()).append((qty, unit_cost))
            continue
        if item.side != "sell":
            continue

        remaining = qty
        cost_basis = Decimal("0")
        queue = lots.setdefault(key, deque())
        while remaining > 0 and queue:
            lot_qty, lot_unit_cost = queue[0]
            matched_qty = min(remaining, lot_qty)
            cost_basis += matched_qty * lot_unit_cost
            remaining -= matched_qty
            lot_qty -= matched_qty
            if lot_qty <= 0:
                queue.popleft()
            else:
                queue[0] = (lot_qty, lot_unit_cost)

        if remaining > 0:
            # Not enough historical buys in this ledger scope. Keep the row but
            # do not present a potentially misleading Toss-style return.
            continue

        item_key = _ledger_item_key(item)
        if item_key in sell_keys:
            proceeds = Decimal(item.filled_notional)
            profit = proceeds - cost_basis
            rate = (
                (profit / cost_basis * Decimal("100")) if cost_basis else Decimal("0")
            )
            annotations[item_key] = (cost_basis, profit, rate)

    annotated: list[ExecutionLedgerRead] = []
    for item in sell_items:
        values = annotations.get(_ledger_item_key(item))
        if values is None:
            annotated.append(item)
        else:
            cost_basis, profit, rate = values
            annotated.append(
                item.model_copy(
                    update={
                        "cost_basis_notional": cost_basis,
                        "realized_profit": profit,
                        "realized_profit_rate": rate,
                    }
                )
            )
    return annotated


def _state_from_items_and_freshness(
    items: list[ExecutionLedgerRead],
    freshness: ExecutionLedgerFreshnessReport,
    market: str | None,
) -> tuple[DataState | None, str | None]:
    """Return (data_state, empty_reason) for a list response."""
    # Determine which brokers are relevant to the market filter
    if market == "crypto":
        relevant_brokers = {"upbit"}
    elif market in ("kr", "us"):
        relevant_brokers = {"kis"}
    else:
        relevant_brokers = {"kis", "upbit"}

    relevant_entries = [e for e in freshness.items if e.broker in relevant_brokers]

    # Worst state across relevant brokers
    states: list[DataState] = [e.dataState for e in relevant_entries]
    if not states:
        overall: DataState = "missing"
    elif "missing" in states:
        overall = "missing"
    elif "stale" in states:
        overall = "stale"
    else:
        overall = "fresh"

    if items:
        return overall, None

    # Empty results — explain why
    if overall == "missing":
        return overall, "no reconcile data available yet"
    return overall, "no fills in the requested window"


class ExecutionLedgerQueryService:
    def __init__(self, db: AsyncSession):
        self.db = db
        self.repo = ExecutionLedgerRepository(db)

    async def _attach_symbol_names(
        self, items: list[ExecutionLedgerRead]
    ) -> list[ExecutionLedgerRead]:
        """Best-effort: populate symbol_name from the per-market universe tables.

        Names are cosmetic, so every resolver call fails open — a lookup error
        leaves symbol_name None and the UI falls back to the raw symbol.
        """
        if not items:
            return items

        kr_symbols = sorted(
            {i.symbol for i in items if i.instrument_type == "equity_kr"}
        )
        us_symbols = sorted(
            {i.symbol for i in items if i.instrument_type == "equity_us"}
        )
        crypto_markets = sorted(
            {i.raw_symbol for i in items if i.instrument_type == "crypto"}
        )

        async def _safe(coro, label):
            try:
                return await coro
            except Exception:  # noqa: BLE001 - names are best-effort
                logger.warning(
                    "symbol-name resolution failed for %s", label, exc_info=True
                )
                return {}

        kr_names = (
            await _safe(get_kr_names_by_symbols(kr_symbols, self.db), "kr")
            if kr_symbols
            else {}
        )
        us_names = (
            await _safe(get_us_names_by_symbols(us_symbols, self.db), "us")
            if us_symbols
            else {}
        )
        crypto_disp = (
            await _safe(
                get_upbit_market_display_names(crypto_markets, self.db), "crypto"
            )
            if crypto_markets
            else {}
        )

        def _name_for(item: ExecutionLedgerRead) -> str | None:
            if item.instrument_type == "equity_kr":
                return kr_names.get(item.symbol)
            if item.instrument_type == "equity_us":
                return us_names.get(item.symbol)
            if item.instrument_type == "crypto":
                # get_upbit_market_display_names keys its result by the canonical
                # upper-case market (e.g. KRW-BTC); normalize before lookup.
                disp = crypto_disp.get(item.raw_symbol.strip().upper())
                if disp:
                    return disp.get("korean_name") or disp.get("english_name")
            return None

        annotated: list[ExecutionLedgerRead] = []
        for item in items:
            name = _name_for(item)
            if name and name != item.symbol:
                annotated.append(item.model_copy(update={"symbol_name": name}))
            else:
                annotated.append(item)
        return annotated

    async def list_recent(
        self, *, limit: int = 50, market: str | None = None
    ) -> ExecutionLedgerListResponse:
        # Over-fetch before de-dup so superseded websocket rows do not consume the
        # page budget (otherwise a dup-heavy page returns fewer than `limit` rows).
        # 3x covers the worst-case number of sources for one order.
        stmt = (
            select(ExecutionLedger)
            .order_by(ExecutionLedger.filled_at.desc())
            .limit(limit * 3)
        )
        stmt = ExecutionLedgerRepository.apply_market_filter(stmt, market)
        rows = (await self.db.execute(stmt)).scalars().all()
        items = [ExecutionLedgerRead.model_validate(row) for row in rows]
        items = _supersede_provisional_fills(items)[:limit]
        items = await self._attach_symbol_names(items)

        freshness = await self.freshness()
        data_state, empty_reason = _state_from_items_and_freshness(
            items, freshness, market
        )
        return ExecutionLedgerListResponse(
            count=len(items),
            items=items,
            data_state=data_state,
            source_breakdown=_compute_source_breakdown(items),
            empty_reason=empty_reason,
        )

    async def list_by_symbol(
        self, *, symbol: str, days: int = 30
    ) -> ExecutionLedgerListResponse:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        stmt = (
            select(ExecutionLedger)
            .where(ExecutionLedger.symbol == symbol)
            .where(ExecutionLedger.filled_at >= cutoff)
            .order_by(ExecutionLedger.filled_at.desc())
        )
        rows = (await self.db.execute(stmt)).scalars().all()
        items = [ExecutionLedgerRead.model_validate(row) for row in rows]
        items = _supersede_provisional_fills(items)
        items = await self._attach_symbol_names(items)

        freshness = await self.freshness()
        data_state, empty_reason = _state_from_items_and_freshness(
            items, freshness, None
        )
        # For a symbol query, be specific about why it's empty
        if not items and empty_reason == "no fills in the requested window":
            empty_reason = f"no fills for {symbol} in the last {days} days"
        return ExecutionLedgerListResponse(
            count=len(items),
            items=items,
            data_state=data_state,
            source_breakdown=_compute_source_breakdown(items),
            empty_reason=empty_reason,
        )

    async def list_sell_history(
        self, *, days: int = 30, market: str | None = None, limit: int = 100
    ) -> ExecutionLedgerListResponse:
        cutoff = datetime.now(UTC) - timedelta(days=days)
        # No SQL LIMIT: provisional websocket rows must be superseded BEFORE
        # truncating. Limiting first would let a dup pair straddle the boundary
        # (the windowed sells are small, so fetching the full window is cheap).
        stmt = (
            select(ExecutionLedger)
            .where(ExecutionLedger.side == "sell")
            .where(ExecutionLedger.filled_at >= cutoff)
            .order_by(ExecutionLedger.filled_at.desc())
        )
        stmt = ExecutionLedgerRepository.apply_market_filter(stmt, market)
        rows = (await self.db.execute(stmt)).scalars().all()
        items = [ExecutionLedgerRead.model_validate(row) for row in rows]
        items = _supersede_provisional_fills(items)
        if items:
            max_sell_at = max(item.filled_at for item in items)
            symbols = {item.symbol for item in items}
            brokers = {item.broker for item in items}
            account_modes = {item.account_mode for item in items}
            venues = {item.venue for item in items}
            instrument_types = {item.instrument_type for item in items}
            currencies = {item.currency for item in items}
            history_stmt = (
                select(ExecutionLedger)
                .where(ExecutionLedger.filled_at <= max_sell_at)
                .where(ExecutionLedger.symbol.in_(symbols))
                .where(ExecutionLedger.broker.in_(brokers))
                .where(ExecutionLedger.account_mode.in_(account_modes))
                .where(ExecutionLedger.venue.in_(venues))
                .where(ExecutionLedger.instrument_type.in_(instrument_types))
                .where(ExecutionLedger.currency.in_(currencies))
                .order_by(ExecutionLedger.filled_at.asc(), ExecutionLedger.id.asc())
            )
            history_rows = (await self.db.execute(history_stmt)).scalars().all()
            history_items = [
                ExecutionLedgerRead.model_validate(row) for row in history_rows
            ]
            history_items = _supersede_provisional_fills(history_items)
            items = _annotate_realized_profit(items, history_items)
        # count is the true de-duped window total; items is the trimmed page so the
        # UI footer ("총 N건 중 M건 표시") and totals stay consistent.
        total = len(items)
        items = items[:limit]
        items = await self._attach_symbol_names(items)

        freshness = await self.freshness()
        data_state, empty_reason = _state_from_items_and_freshness(
            items, freshness, market
        )
        return ExecutionLedgerListResponse(
            count=total,
            items=items,
            data_state=data_state,
            source_breakdown=_compute_source_breakdown(items),
            empty_reason=empty_reason,
        )

    async def freshness(
        self, *, freshness_window_hours: int = 24
    ) -> ExecutionLedgerFreshnessReport:
        latest = await self.repo.latest_run_per_broker()
        now = datetime.now(UTC)
        items: list[ExecutionLedgerFreshnessEntry] = []
        for broker in ("kis", "upbit"):
            run = latest.get(broker)
            if run is None or run.finished_at is None:
                items.append(
                    ExecutionLedgerFreshnessEntry(
                        broker=broker,
                        dataState="missing",
                        notes="no successful reconcile run",
                    )
                )
                continue
            finished_at = (
                run.finished_at.astimezone(UTC)
                if run.finished_at.tzinfo
                else run.finished_at.replace(tzinfo=UTC)
            )
            lag_minutes = (now - finished_at).total_seconds() / 60
            if lag_minutes <= freshness_window_hours * 2 * 60:
                state: DataState = "fresh"
            elif lag_minutes <= 24 * 3 * 60:
                state = "stale"
            else:
                state = "missing"
            items.append(
                ExecutionLedgerFreshnessEntry(
                    broker=broker,
                    last_run_at=run.finished_at,
                    lag_minutes=round(lag_minutes, 2),
                    dataState=state,
                    last_run_id=run.run_id,
                )
            )
        return ExecutionLedgerFreshnessReport(items=items)
