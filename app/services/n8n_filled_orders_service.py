"""Filled-orders service — fetches recent fills across all markets."""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import Any

import app.services.brokers.upbit.client as upbit_service
from app.core.timezone import KST, now_kst
from app.services.brokers.kis.client import KISClient
from app.services.execution_ledger.normalizers import (
    _normalize_kis_domestic_filled,
    _normalize_kis_overseas_filled,
    normalize_upbit_order,
)
from app.services.market_data import get_quote
from app.services.n8n_filled_orders_indicators import _enrich_with_indicators

logger = logging.getLogger(__name__)

_EQUITY_QUOTE_CONCURRENCY = 5


def _resolve_kst_window(
    *,
    days: int,
    start_at: datetime | None,
    end_at: datetime | None,
) -> tuple[datetime, datetime]:
    resolved_end = end_at.astimezone(KST) if end_at else now_kst()
    resolved_start = (
        start_at.astimezone(KST) if start_at else resolved_end - timedelta(days=days)
    )
    if resolved_start >= resolved_end:
        raise ValueError("start_at must be before end_at")
    return resolved_start, resolved_end


def _parse_upbit_fill_datetime(value: object) -> datetime | None:
    """Strictly parse an Upbit fill timestamp for window filtering.

    Execution-ledger normalizers may default malformed provider timestamps to
    ``now`` for persistence compatibility, but the legacy n8n filled-orders
    surface must skip rows whose provider timestamp cannot be parsed.
    """
    text = str(value or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        if len(text) != 8:
            return None
        try:
            parsed = datetime.strptime(text, "%Y%m%d").replace(tzinfo=KST)
        except ValueError:
            return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=KST)
    return parsed.astimezone(KST)


_UPBIT_CLOSED_ORDERS_WINDOW = timedelta(days=7)
_UPBIT_CLOSED_ORDERS_LIMIT = 1000


def _iter_upbit_windows(
    start_at: datetime, end_at: datetime
) -> list[tuple[datetime, datetime]]:
    windows: list[tuple[datetime, datetime]] = []
    cursor_end = end_at
    while cursor_end > start_at:
        cursor_start = max(start_at, cursor_end - _UPBIT_CLOSED_ORDERS_WINDOW)
        windows.append((cursor_start, cursor_end))
        cursor_end = cursor_start
    return windows


async def _fetch_upbit_closed_window(
    start_at: datetime,
    end_at: datetime,
) -> list[dict[str, Any]]:
    rows = await upbit_service.fetch_closed_orders(
        market=None,
        limit=_UPBIT_CLOSED_ORDERS_LIMIT,
        states=["done", "cancel"],
        order_by="desc",
        start_time=start_at,
        end_time=end_at,
    )
    if len(rows) >= _UPBIT_CLOSED_ORDERS_LIMIT:
        if end_at - start_at <= timedelta(hours=1):
            raise RuntimeError(
                "Upbit closed orders may be truncated in a <=1h window; "
                f"start={start_at.isoformat()} end={end_at.isoformat()}"
            )
        midpoint = start_at + (end_at - start_at) / 2
        left = await _fetch_upbit_closed_window(start_at, midpoint)
        right = await _fetch_upbit_closed_window(midpoint, end_at)
        return left + right
    return rows


async def _fetch_upbit_filled(
    days: int,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    max_pages: int = 100,
) -> tuple[list[dict], list[dict]]:  # NOSONAR
    """Paginate through Upbit closed orders and expand each into per-trade fills."""
    try:
        start_kst, end_kst = _resolve_kst_window(
            days=days, start_at=start_at, end_at=end_at
        )
        all_fills: list[dict[str, Any]] = []
        seen_order_uuids: set[str] = set()

        for window_start, window_end in _iter_upbit_windows(start_kst, end_kst):
            closed = await _fetch_upbit_closed_window(window_start, window_end)
            for raw in closed:
                uuid = str(raw.get("uuid") or "")
                if uuid and uuid in seen_order_uuids:
                    continue
                if uuid:
                    seen_order_uuids.add(uuid)
                executed_vol = float(raw.get("executed_volume") or 0)
                if executed_vol <= 0:
                    continue

                if not raw.get("trades"):
                    try:
                        raw = await upbit_service.fetch_order_detail(uuid)
                    except Exception as exc:
                        logger.warning(
                            "Upbit order detail fetch failed for %s: %s",
                            uuid,
                            exc,
                        )

                fills = normalize_upbit_order(raw)
                for fill in fills:
                    parsed_filled_at = _parse_upbit_fill_datetime(
                        fill.get("filled_at", "")
                    )
                    if parsed_filled_at is None:
                        logger.warning(
                            "Upbit filled order skipped due to invalid filled_at: order_id=%s filled_at=%r",
                            fill.get("order_id"),
                            fill.get("filled_at"),
                        )
                        continue
                    if start_kst <= parsed_filled_at <= end_kst:
                        all_fills.append(fill)

        return all_fills, []
    except Exception as exc:
        logger.warning("Upbit filled-orders fetch failed: %s", exc)
        return [], [{"market": "crypto", "error": str(exc)}]


async def _fetch_kis_domestic_filled(
    days: int,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    max_pages: int = 100,
) -> tuple[list[dict], list[dict]]:
    try:
        kis = KISClient()
        start_kst, end_kst = _resolve_kst_window(
            days=days, start_at=start_at, end_at=end_at
        )
        raw_orders = await kis.inquire_daily_order_domestic(
            start_date=start_kst.strftime("%Y%m%d"),
            end_date=end_kst.strftime("%Y%m%d"),
            stock_code="",
            side="00",
            max_pages=max_pages,
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


async def _fetch_kis_overseas_filled(
    days: int,
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    max_pages: int = 100,
) -> tuple[list[dict], list[dict]]:
    try:
        kis = KISClient()
        start_kst, end_kst = _resolve_kst_window(
            days=days, start_at=start_at, end_at=end_at
        )

        all_orders: list[dict] = []
        seen_fill_keys: set[tuple[str, int]] = set()

        try:
            raw_orders = await kis.inquire_daily_order_overseas(
                start_date=start_kst.strftime("%Y%m%d"),
                end_date=end_kst.strftime("%Y%m%d"),
                symbol="%",
                exchange_code="NASD",
                side="00",
                max_pages=max_pages,
            )
            for raw in raw_orders or []:
                normalized = _normalize_kis_overseas_filled(raw)
                if normalized:
                    fill_key = (normalized["order_id"], normalized["fill_seq"])
                    if fill_key not in seen_fill_keys:
                        seen_fill_keys.add(fill_key)
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
    start_at: datetime | None = None,
    end_at: datetime | None = None,
    max_pages: int = 100,
) -> dict[str, Any]:
    market_set = {m.strip().lower() for m in markets.split(",") if m.strip()}
    all_orders: list[dict[str, Any]] = []
    all_errors: list[dict[str, Any]] = []

    tasks = []
    if "crypto" in market_set:
        tasks.append(
            _fetch_upbit_filled(
                days, start_at=start_at, end_at=end_at, max_pages=max_pages
            )
        )
    if "kr" in market_set:
        tasks.append(
            _fetch_kis_domestic_filled(
                days, start_at=start_at, end_at=end_at, max_pages=max_pages
            )
        )
    if "us" in market_set:
        tasks.append(
            _fetch_kis_overseas_filled(
                days, start_at=start_at, end_at=end_at, max_pages=max_pages
            )
        )

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
