"""Live read-only current open-order service for /invest (ROB-572)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol

from app.schemas.open_orders import (
    OpenOrderDataState,
    OpenOrderMarket,
    OpenOrderRow,
    OpenOrdersQueryMarket,
    OpenOrdersResponse,
    OpenOrderSourceState,
)
from app.services.brokers.kis.client import KISClient
from app.services.brokers.upbit import orders as upbit_orders

logger = logging.getLogger(__name__)

_KST = dt.timezone(dt.timedelta(hours=9), name="KST")
_KIS_SIDE_BUY = {"02", "buy", "b", "매수"}
_KIS_SIDE_SELL = {"01", "sell", "s", "매도"}


def _first_str(row: dict[str, Any], keys: tuple[str, ...]) -> str | None:
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return None


def _decimal(value: object) -> Decimal | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return Decimal(text.replace(",", ""))
    except (InvalidOperation, ValueError):
        return None


def _parse_kis_ordered_at(row: dict[str, Any]) -> dt.datetime | None:
    explicit = _parse_datetime(row.get("ordered_at") or row.get("placed_at"))
    if explicit is not None:
        return explicit
    ord_dt = row.get("ord_dt")
    ord_tmd = row.get("ord_tmd")
    if not ord_dt or not ord_tmd:
        return None
    try:
        return dt.datetime.strptime(f"{ord_dt}{ord_tmd}", "%Y%m%d%H%M%S").replace(
            tzinfo=_KST
        )
    except ValueError:
        return None


def _parse_datetime(value: object) -> dt.datetime | None:
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value if value.tzinfo else value.replace(tzinfo=dt.UTC)
    if isinstance(value, str):
        try:
            parsed = dt.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=dt.UTC)
    return None


def _kis_side(row: dict[str, Any]) -> Literal["buy", "sell", "unknown"]:
    raw = (
        str(
            row.get("sll_buy_dvsn_cd")
            or row.get("sll_buy_dvsn_cd_name")
            or row.get("side")
            or ""
        )
        .strip()
        .lower()
    )
    if raw in _KIS_SIDE_BUY:
        return "buy"
    if raw in _KIS_SIDE_SELL:
        return "sell"
    return "unknown"


def normalize_kis_order(
    row: dict[str, Any],
    *,
    market: Literal["kr", "us"],
    exchange: str,
) -> OpenOrderRow:
    order_no = _first_str(row, ("ord_no", "odno", "order_id")) or "unknown"
    symbol = _first_str(row, ("pdno", "symbol", "ticker")) or "unknown"
    quantity = _decimal(_first_str(row, ("ord_qty", "ft_ord_qty", "quantity", "qty")))
    remaining = _decimal(
        _first_str(row, ("nccs_qty", "rmn_qty", "remaining_qty", "remaining_quantity"))
    )
    if remaining is None:
        remaining = quantity
    status = _first_str(row, ("prcs_stat_name", "status", "raw_status")) or "pending"

    return OpenOrderRow(
        broker="kis",
        market=market,
        symbol=symbol.upper() if market == "us" else symbol,
        symbol_name=_first_str(row, ("prdt_name", "symbol_name", "name")),
        side=_kis_side(row),
        order_type=_first_str(row, ("ord_dvsn_name", "ord_dvsn", "order_type")),
        time_in_force=None,
        price=_decimal(_first_str(row, ("ord_unpr", "ft_ord_unpr3", "ord_unpr3", "price"))),
        quantity=quantity,
        remaining_qty=remaining,
        filled_qty=_decimal(_first_str(row, ("ft_ccld_qty", "ccld_qty", "filled_qty"))),
        status="pending",
        raw_status=status,
        ordered_at=_parse_kis_ordered_at(row),
        order_no=order_no,
        exchange=exchange,
        currency="KRW" if market == "kr" else "USD",
    )


def normalize_upbit_order(row: dict[str, Any]) -> OpenOrderRow:
    side_raw = str(row.get("side") or "").strip().lower()
    side: Literal["buy", "sell", "unknown"]
    if side_raw == "bid":
        side = "buy"
    elif side_raw == "ask":
        side = "sell"
    else:
        side = "unknown"
    symbol = str(row.get("market") or "unknown").strip().upper()
    quote = symbol.split("-", 1)[0] if "-" in symbol else "KRW"
    return OpenOrderRow(
        broker="upbit",
        market="crypto",
        symbol=symbol,
        symbol_name=None,
        side=side,
        order_type=_first_str(row, ("ord_type", "order_type")),
        time_in_force=None,
        price=_decimal(row.get("price")),
        quantity=_decimal(row.get("volume")),
        remaining_qty=_decimal(row.get("remaining_volume")),
        filled_qty=_decimal(row.get("executed_volume")),
        status="pending",
        raw_status=_first_str(row, ("state", "status")) or "wait",
        ordered_at=_parse_datetime(row.get("created_at") or row.get("ordered_at")),
        order_no=str(row.get("uuid") or "unknown"),
        exchange="UPBIT",
        currency=quote,
    )


_KIS_US_EXCHANGES: tuple[str, ...] = ("NASD", "NYSE", "AMEX")


class _KISClientProtocol(Protocol):
    async def inquire_korea_orders(self, is_mock: bool = False) -> list[dict[str, Any]]: ...
    async def inquire_overseas_orders(self, exchange_code: str = "NASD", is_mock: bool = False) -> list[dict[str, Any]]: ...


class _UpbitClientProtocol(Protocol):
    async def fetch_open_orders(self, market: str | None = None) -> list[dict[str, Any]]: ...


def _default_kis_client() -> _KISClientProtocol:
    return KISClient()


def _source(
    *,
    broker: Literal["kis", "toss", "upbit"],
    market: OpenOrderMarket,
    status: OpenOrderDataState,
    fetched_at: dt.datetime | None,
    count: int,
    message: str | None = None,
) -> OpenOrderSourceState:
    return OpenOrderSourceState(
        broker=broker,
        market=market,
        status=status,
        fetched_at=fetched_at,
        count=count,
        message=message,
    )


def _overall_state(sources: list[OpenOrderSourceState]) -> OpenOrderDataState:
    if not sources or all(source.status == "unavailable" for source in sources):
        return "unavailable"
    if any(source.status != "ok" for source in sources):
        return "degraded"
    return "ok"


def _sort_key(row: OpenOrderRow) -> tuple[int, dt.datetime]:
    if row.ordered_at is None:
        return (1, dt.datetime.min.replace(tzinfo=dt.UTC))
    return (0, row.ordered_at.astimezone(dt.UTC))


class CurrentOrdersService:
    def __init__(
        self,
        *,
        kis_client_factory: Callable[[], _KISClientProtocol] | None = _default_kis_client,
        upbit_client: _UpbitClientProtocol | None = upbit_orders,
        toss_client_factory: Callable[[], Any] | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._kis_client_factory = kis_client_factory
        self._upbit_client = upbit_client
        self._toss_client_factory = toss_client_factory
        self._clock = clock or (lambda: dt.datetime.now(tz=dt.UTC))

    async def list_open_orders(
        self,
        *,
        market: OpenOrdersQueryMarket = "all",
    ) -> OpenOrdersResponse:
        tasks = []
        if market in ("all", "kr"):
            tasks.append(self._collect_kis_kr())
        if market in ("all", "us"):
            tasks.append(self._collect_kis_us())
        if market in ("all", "crypto"):
            tasks.append(self._collect_upbit())

        results = await asyncio.gather(*tasks)
        rows: list[OpenOrderRow] = []
        sources: list[OpenOrderSourceState] = []
        for result_rows, result_source in results:
            rows.extend(result_rows)
            sources.append(result_source)

        rows.sort(key=_sort_key, reverse=True)
        data_state = _overall_state(sources)
        warnings = [
            f"{source.broker}/{source.market}: {source.message or source.status}"
            for source in sources
            if source.status != "ok"
        ]
        empty_reason = None
        if not rows:
            empty_reason = (
                "all requested broker sources are unavailable"
                if data_state == "unavailable"
                else "no open orders for the selected market"
            )
        return OpenOrdersResponse(
            market=market,
            count=len(rows),
            data_state=data_state,
            as_of=self._clock(),
            items=rows,
            sources=sources,
            warnings=warnings,
            empty_reason=empty_reason,
        )

    def _kis(self) -> _KISClientProtocol | None:
        if self._kis_client_factory is None:
            return None
        return self._kis_client_factory()

    async def _collect_kis_kr(self) -> tuple[list[OpenOrderRow], OpenOrderSourceState]:
        now = self._clock()
        kis = self._kis()
        if kis is None:
            return [], _source(broker="kis", market="kr", status="unavailable", fetched_at=None, count=0, message="kis_client_unavailable")
        try:
            raw = await kis.inquire_korea_orders(is_mock=False)
        except Exception as exc:  # noqa: BLE001 - endpoint must fail open per broker
            logger.warning("KIS KR open-order fetch failed", exc_info=True)
            return [], _source(broker="kis", market="kr", status="unavailable", fetched_at=now, count=0, message=f"{type(exc).__name__}: {exc}")
        rows = [normalize_kis_order(row, market="kr", exchange="KRX") for row in raw or [] if isinstance(row, dict)]
        return rows, _source(broker="kis", market="kr", status="ok", fetched_at=now, count=len(rows))

    async def _collect_kis_us(self) -> tuple[list[OpenOrderRow], OpenOrderSourceState]:
        now = self._clock()
        kis = self._kis()
        if kis is None:
            return [], _source(broker="kis", market="us", status="unavailable", fetched_at=None, count=0, message="kis_client_unavailable")
        rows: list[OpenOrderRow] = []
        seen: set[str] = set()
        errors: dict[str, str] = {}
        for exchange in _KIS_US_EXCHANGES:
            try:
                raw = await kis.inquire_overseas_orders(exchange_code=exchange, is_mock=False)
            except Exception as exc:  # noqa: BLE001
                errors[exchange] = f"{type(exc).__name__}: {exc}"
                continue
            for row in raw or []:
                if not isinstance(row, dict):
                    continue
                order_no = _first_str(row, ("ord_no", "odno", "order_id"))
                if order_no and order_no in seen:
                    continue
                if order_no:
                    seen.add(order_no)
                rows.append(normalize_kis_order(row, market="us", exchange=exchange))
        if errors and not rows:
            return [], _source(broker="kis", market="us", status="unavailable", fetched_at=now, count=0, message="; ".join(f"{k}={v}" for k, v in errors.items()))
        status: OpenOrderDataState = "degraded" if errors else "ok"
        message = "; ".join(f"{k}={v}" for k, v in errors.items()) if errors else None
        return rows, _source(broker="kis", market="us", status=status, fetched_at=now, count=len(rows), message=message)

    async def _collect_upbit(self) -> tuple[list[OpenOrderRow], OpenOrderSourceState]:
        now = self._clock()
        if self._upbit_client is None:
            return [], _source(broker="upbit", market="crypto", status="unavailable", fetched_at=None, count=0, message="upbit_client_unavailable")
        try:
            raw = await self._upbit_client.fetch_open_orders(market=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Upbit open-order fetch failed", exc_info=True)
            return [], _source(broker="upbit", market="crypto", status="unavailable", fetched_at=now, count=0, message=f"{type(exc).__name__}: {exc}")
        rows = [normalize_upbit_order(row) for row in raw or [] if isinstance(row, dict)]
        return rows, _source(broker="upbit", market="crypto", status="ok", fetched_at=now, count=len(rows))

