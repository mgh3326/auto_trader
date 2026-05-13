"""Filled-orders service — fetches recent fills across all markets."""

from __future__ import annotations

import asyncio
import logging
from datetime import timedelta
from typing import Any

import app.services.brokers.upbit.client as upbit_service
from app.core.timezone import now_kst
from app.services.brokers.kis.client import KISClient
from app.services.execution_ledger.normalizers import (
    _normalize_kis_domestic_filled,
    _normalize_kis_overseas_filled,
    _normalize_upbit_filled,
)
from app.services.execution_ledger.normalizers import (
    _parse_filled_at as _parse_filled_at_kst,
)
from app.services.market_data import get_quote
from app.services.n8n_filled_orders_indicators import _enrich_with_indicators

logger = logging.getLogger(__name__)

_EQUITY_QUOTE_CONCURRENCY = 5


async def _fetch_upbit_filled(days: int) -> tuple[list[dict], list[dict]]:
    try:
        closed = await upbit_service.fetch_closed_orders(market=None, limit=100)
        orders = [
            n for raw in closed if (n := _normalize_upbit_filled(raw)) is not None
        ]
        cutoff = now_kst() - timedelta(days=days)
        filtered_orders: list[dict[str, Any]] = []

        for order in orders:
            parsed_filled_at = _parse_filled_at_kst(order.get("filled_at", ""))
            if parsed_filled_at is None:
                logger.warning(
                    "Upbit filled order skipped due to invalid filled_at: order_id=%s filled_at=%r",
                    order.get("order_id"),
                    order.get("filled_at"),
                )
                continue
            if parsed_filled_at >= cutoff:
                filtered_orders.append(order)

        return filtered_orders, []
    except Exception as exc:
        logger.warning("Upbit filled-orders fetch failed: %s", exc)
        return [], [{"market": "crypto", "error": str(exc)}]


async def _fetch_kis_domestic_filled(days: int) -> tuple[list[dict], list[dict]]:
    try:
        kis = KISClient()
        end_date = now_kst().strftime("%Y%m%d")
        start_date = (now_kst() - timedelta(days=days)).strftime("%Y%m%d")
        raw_orders = await kis.inquire_daily_order_domestic(
            start_date=start_date, end_date=end_date, stock_code="", side="00"
        )
        orders = [
            n
            for raw in (raw_orders or [])
            if (n := _normalize_kis_domestic_filled(raw)) is not None
        ]
        return orders, []
    except Exception as exc:
        logger.warning("KIS domestic filled-orders fetch failed: %s", exc)
        return [], [{"market": "kr", "error": str(exc)}]


async def _fetch_kis_overseas_filled(days: int) -> tuple[list[dict], list[dict]]:
    try:
        kis = KISClient()
        end_date = now_kst().strftime("%Y%m%d")
        start_date = (now_kst() - timedelta(days=days)).strftime("%Y%m%d")

        all_orders: list[dict] = []
        seen_order_ids: set[str] = set()

        # KIS overseas daily-order inquiry treats NASD as a US-wide history
        # selector in practice: rows may include NYSE/AMEX via ovrs_excg_cd.
        # Calling NASD/NYSE/AMEX separately produces duplicates and increases
        # the chance of SYDB0050 while following continuation pages.
        try:
            raw_orders = await kis.inquire_daily_order_overseas(
                start_date=start_date,
                end_date=end_date,
                symbol="%",
                exchange_code="NASD",
                side="00",
            )
            for raw in raw_orders or []:
                normalized = _normalize_kis_overseas_filled(raw)
                if normalized and normalized["order_id"] not in seen_order_ids:
                    seen_order_ids.add(normalized["order_id"])
                    all_orders.append(normalized)
        except Exception as exc:
            logger.warning("KIS overseas US-wide fetch failed: %s", exc)

        return all_orders, []
    except Exception as exc:
        logger.warning("KIS overseas filled-orders fetch failed: %s", exc)
        return [], [{"market": "us", "error": str(exc)}]


async def _enrich_with_current_prices(
    orders: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    seen: dict[str, float | None] = {}
    sem = asyncio.Semaphore(_EQUITY_QUOTE_CONCURRENCY)

    crypto_symbols = list(
        {o["raw_symbol"] for o in orders if o["instrument_type"] == "crypto"}
    )
    if crypto_symbols:
        try:
            prices = await upbit_service.fetch_multiple_current_prices_cached(
                crypto_symbols
            )
            for sym, price in prices.items():
                seen[sym] = price
        except Exception as exc:
            logger.warning("Crypto batch price fetch failed: %s", exc)

    equity_symbols = list(
        {
            (o["raw_symbol"], o["instrument_type"])
            for o in orders
            if o["instrument_type"] in ("equity_kr", "equity_us")
        }
    )

    async def _fetch_one(symbol: str, itype: str) -> None:
        if symbol in seen:
            return
        async with sem:
            try:
                market = "kr" if itype == "equity_kr" else "us"
                quote = await get_quote(symbol, market=market)
                seen[symbol] = quote.price or None
            except Exception as exc:
                logger.warning("Quote fetch failed for %s: %s", symbol, exc)
                seen[symbol] = None

    await asyncio.gather(
        *[_fetch_one(sym, itype) for sym, itype in equity_symbols],
        return_exceptions=True,
    )

    for order in orders:
        cp = seen.get(order["raw_symbol"])
        order["current_price"] = cp
        if cp and order["price"]:
            if order["side"] == "buy":
                pnl = ((cp - order["price"]) / order["price"]) * 100
            else:
                pnl = ((order["price"] - cp) / order["price"]) * 100
            order["pnl_pct"] = round(pnl, 2)
            sign = "+" if pnl >= 0 else ""
            order["pnl_pct_fmt"] = f"{sign}{pnl:.2f}%"
        else:
            order["pnl_pct"] = None
            order["pnl_pct_fmt"] = None

    return orders


async def fetch_filled_orders(
    days: int = 1,
    markets: str = "crypto,kr,us",
    min_amount: float = 0,
    include_indicators: bool = False,
) -> dict[str, Any]:
    market_set = {m.strip().lower() for m in markets.split(",") if m.strip()}
    all_orders: list[dict[str, Any]] = []
    all_errors: list[dict[str, Any]] = []

    tasks = []
    if "crypto" in market_set:
        tasks.append(_fetch_upbit_filled(days))
    if "kr" in market_set:
        tasks.append(_fetch_kis_domestic_filled(days))
    if "us" in market_set:
        tasks.append(_fetch_kis_overseas_filled(days))

    results = await asyncio.gather(*tasks, return_exceptions=True)
    for result in results:
        if isinstance(result, BaseException):
            all_errors.append({"error": str(result)})
        elif isinstance(result, tuple):
            orders, errors = result
            all_orders.extend(orders)
            all_errors.extend(errors)

    if min_amount > 0:
        all_orders = [o for o in all_orders if o.get("total_amount", 0) >= min_amount]

    if all_orders:
        all_orders = await _enrich_with_current_prices(all_orders)

    if all_orders and include_indicators:
        all_orders = await _enrich_with_indicators(all_orders)

    all_orders.sort(key=lambda o: o.get("filled_at", ""), reverse=True)

    return {"orders": all_orders, "errors": all_errors}
