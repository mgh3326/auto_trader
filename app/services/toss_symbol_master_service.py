from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Protocol

import sqlalchemy as sa
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.kr_symbol_universe import KRSymbolUniverse
from app.models.us_symbol_universe import USSymbolUniverse
from app.services.brokers.toss.dto import TossPrice, TossStockInfo
from app.services.market_valuation_snapshots.repository import (
    MarketValuationSnapshotsRepository,
    MarketValuationSnapshotUpsert,
)

TOSS_VALUATION_SOURCE = "toss_openapi"
_BATCH_SIZE = 200


class TossSymbolMasterClient(Protocol):
    async def stocks(
        self, symbols: list[str] | tuple[str, ...]
    ) -> list[TossStockInfo]: ...
    async def prices(self, symbols: list[str] | tuple[str, ...]) -> list[TossPrice]: ...


@dataclass(frozen=True)
class TossSymbolMasterSyncRequest:
    market: str
    symbols: tuple[str, ...] = ()
    all_symbols: bool = False
    limit: int | None = 20
    commit: bool = False
    include_market_cap: bool = True
    snapshot_date: dt.date | None = None


@dataclass(frozen=True)
class TossSymbolMasterSyncResult:
    market: str
    commit: bool
    symbols_requested: int
    batches: int
    stocks_matched: int
    stocks_missing: int
    master_updates: int
    market_cap_payloads: int
    market_cap_nonnull: int
    warnings: tuple[str, ...] = ()
    samples: tuple[str, ...] = ()


def _chunks(symbols: list[str], size: int = _BATCH_SIZE) -> list[list[str]]:
    return [symbols[idx : idx + size] for idx in range(0, len(symbols), size)]


async def _resolve_symbols(
    db: AsyncSession,
    *,
    market: str,
    symbols: tuple[str, ...],
    all_symbols: bool,
    limit: int | None,
) -> list[str]:
    if market not in {"kr", "us"}:
        raise ValueError(f"unsupported market: {market}")
    if symbols:
        return [s.strip().upper() for s in symbols if s.strip()]
    model = KRSymbolUniverse if market == "kr" else USSymbolUniverse
    stmt = (
        sa.select(model.symbol).where(model.is_active.is_(True)).order_by(model.symbol)
    )
    if not all_symbols:
        stmt = stmt.limit(limit or 20)
    result = await db.execute(stmt)
    return [row[0] for row in result.all()]


def _parse_date(value: str | None) -> dt.date | None:
    if not value:
        return None
    return dt.date.fromisoformat(value[:10])


def _kr_suspended(detail: dict | None, key: str) -> bool | None:
    if not detail:
        return None
    if key in detail:
        return bool(detail[key])
    return None


def _kr_assignments(stock: TossStockInfo, *, now: dt.datetime) -> dict[str, object]:
    return {
        "security_type": stock.security_type,
        "is_common_share": stock.is_common_share,
        "listing_status": stock.status,
        "list_date": _parse_date(stock.list_date),
        "delist_date": _parse_date(stock.delist_date),
        "shares_outstanding": stock.shares_outstanding,
        "leverage_factor": stock.leverage_factor,
        "krx_trading_suspended": _kr_suspended(
            stock.korean_market_detail, "krxTradingSuspended"
        ),
        "nxt_trading_suspended": _kr_suspended(
            stock.korean_market_detail, "nxtTradingSuspended"
        ),
        "isin": stock.isin_code,
        "toss_master_updated_at": now,
    }


def _us_assignments(stock: TossStockInfo, *, now: dt.datetime) -> dict[str, object]:
    return {
        "security_type": stock.security_type,
        "is_common_share": stock.is_common_share,
        "listing_status": stock.status,
        "list_date": _parse_date(stock.list_date),
        "delist_date": _parse_date(stock.delist_date),
        "shares_outstanding": stock.shares_outstanding,
        "leverage_factor": stock.leverage_factor,
        "isin": stock.isin_code,
        "toss_master_updated_at": now,
        "is_common_stock": stock.is_common_share,
    }


def _count_or_apply(
    row: object, assignments: dict[str, object], *, apply: bool
) -> bool:
    changed = False
    for field, value in assignments.items():
        if getattr(row, field) != value:
            if apply:
                setattr(row, field, value)
            changed = True
    return changed


def _market_cap_payloads(
    *,
    market: str,
    snapshot_date: dt.date,
    stocks: dict[str, TossStockInfo],
    prices: dict[str, TossPrice],
) -> list[MarketValuationSnapshotUpsert]:
    payloads: list[MarketValuationSnapshotUpsert] = []
    for symbol, stock in stocks.items():
        price = prices.get(symbol)
        market_cap = (
            stock.shares_outstanding * price.last_price
            if price is not None and stock.shares_outstanding is not None
            else None
        )
        if market_cap is None:
            continue
        payloads.append(
            MarketValuationSnapshotUpsert(
                market=market,
                symbol=symbol,
                snapshot_date=snapshot_date,
                source=TOSS_VALUATION_SOURCE,
                market_cap=market_cap,
                raw_payload={
                    "source": TOSS_VALUATION_SOURCE,
                    "sharesOutstanding": str(stock.shares_outstanding),
                    "lastPrice": str(price.last_price),
                    "priceTimestamp": price.timestamp,
                    "currency": price.currency,
                },
            )
        )
    return payloads


async def sync_toss_symbol_master(
    db: AsyncSession,
    *,
    client: TossSymbolMasterClient,
    request: TossSymbolMasterSyncRequest,
) -> TossSymbolMasterSyncResult:
    market = request.market.strip().lower()
    snapshot_date = request.snapshot_date or dt.datetime.now(dt.UTC).date()
    symbols = await _resolve_symbols(
        db,
        market=market,
        symbols=request.symbols,
        all_symbols=request.all_symbols,
        limit=request.limit,
    )
    if not symbols:
        return TossSymbolMasterSyncResult(
            market=market,
            commit=request.commit,
            symbols_requested=0,
            batches=0,
            stocks_matched=0,
            stocks_missing=0,
            master_updates=0,
            market_cap_payloads=0,
            market_cap_nonnull=0,
            warnings=("no symbols resolved",),
        )

    model = KRSymbolUniverse if market == "kr" else USSymbolUniverse
    existing_result = await db.execute(
        sa.select(model).where(model.symbol.in_(symbols))
    )
    existing = {row.symbol: row for row in existing_result.scalars().all()}
    now = dt.datetime.now(dt.UTC)
    all_stocks: dict[str, TossStockInfo] = {}
    all_prices: dict[str, TossPrice] = {}
    updates = 0

    batches = _chunks(symbols)
    for batch in batches:
        stock_rows = {row.symbol: row for row in await client.stocks(batch)}
        price_rows = (
            {row.symbol: row for row in await client.prices(batch)}
            if request.include_market_cap
            else {}
        )
        all_stocks.update(stock_rows)
        all_prices.update(price_rows)
        for symbol, stock in stock_rows.items():
            row = existing.get(symbol)
            if row is None:
                continue
            assignments = (
                _kr_assignments(stock, now=now)
                if market == "kr"
                else _us_assignments(stock, now=now)
            )
            changed = _count_or_apply(row, assignments, apply=request.commit)
            if changed:
                updates += 1

    existing_stocks = {
        symbol: stock for symbol, stock in all_stocks.items() if symbol in existing
    }
    payloads = _market_cap_payloads(
        market=market,
        snapshot_date=snapshot_date,
        stocks=existing_stocks,
        prices=all_prices,
    )
    if request.commit:
        if payloads:
            await MarketValuationSnapshotsRepository(db).upsert(payloads)
        await db.flush()

    missing = len(set(symbols) - set(all_stocks))
    return TossSymbolMasterSyncResult(
        market=market,
        commit=request.commit,
        symbols_requested=len(symbols),
        batches=len(batches),
        stocks_matched=len(all_stocks),
        stocks_missing=missing,
        master_updates=updates,
        market_cap_payloads=len(payloads),
        market_cap_nonnull=len(payloads),
        samples=tuple(symbols[:10]),
    )
