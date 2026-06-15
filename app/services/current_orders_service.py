"""Live read-only current open-order service for /invest (ROB-572)."""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
from collections.abc import Callable
from decimal import Decimal, InvalidOperation
from typing import Any, Literal, Protocol

from sqlalchemy.ext.asyncio import AsyncSession

from app.core.timezone import KST, now_kst
from app.schemas.open_orders import (
    OpenOrderDataState,
    OpenOrderMarket,
    OpenOrderRow,
    OpenOrderSourceState,
    OpenOrdersQueryMarket,
    OpenOrdersResponse,
)
from app.services.brokers.kis.client import KISClient
from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.dto import TossOrder
from app.services.brokers.upbit import orders as upbit_orders
from app.services.kr_symbol_universe_service import get_kr_names_by_symbols
from app.services.upbit_symbol_universe_service import get_upbit_market_display_names
from app.services.us_symbol_universe_service import get_us_names_by_symbols

logger = logging.getLogger(__name__)

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
    ord_tmd = str(row.get("ord_tmd") or "").strip()
    if not ord_tmd:
        return None
    ord_dt = str(row.get("ord_dt") or "").strip() or now_kst().strftime("%Y%m%d")
    try:
        return dt.datetime.strptime(f"{ord_dt}{ord_tmd.zfill(6)}", "%Y%m%d%H%M%S").replace(
            tzinfo=KST
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
        price=_decimal(
            _first_str(row, ("ord_unpr", "ft_ord_unpr3", "ord_unpr3", "price"))
        ),
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


def _default_toss_client() -> Any:
    return TossReadClient.from_settings()


def _toss_market(symbol: str) -> Literal["kr", "us"]:
    normalized = symbol.strip().upper()
    return "kr" if len(normalized) == 6 and normalized.isdigit() else "us"


def normalize_toss_order(order: TossOrder) -> OpenOrderRow:
    filled = _decimal(order.execution.get("filledQuantity"))
    remaining = order.quantity - (filled or Decimal("0"))
    side_raw = order.side.strip().lower()
    side: Literal["buy", "sell", "unknown"]
    if side_raw in {"buy", "bid", "매수"}:
        side = "buy"
    elif side_raw in {"sell", "ask", "매도"}:
        side = "sell"
    else:
        side = "unknown"
    market = _toss_market(order.symbol)
    return OpenOrderRow(
        broker="toss",
        market=market,
        symbol=order.symbol.strip().upper() if market == "us" else order.symbol.strip(),
        symbol_name=None,
        side=side,
        order_type=order.order_type,
        time_in_force=order.time_in_force,
        price=order.price,
        quantity=order.quantity,
        remaining_qty=remaining if remaining >= 0 else Decimal("0"),
        filled_qty=filled,
        status="pending",
        raw_status=order.status,
        ordered_at=_parse_datetime(order.ordered_at),
        order_no=order.order_id,
        exchange="TOSS",
        currency=order.currency,
    )


_KIS_US_EXCHANGES: tuple[str, ...] = ("NASD", "NYSE", "AMEX")
# Bound the Toss OPEN-order pagination: a single operator's open orders never
# need many pages, so cap it to convert a stuck/echoing cursor (broker
# misbehavior) into a bounded partial result instead of an infinite loop.
_TOSS_MAX_PAGES = 50


class _KISClientProtocol(Protocol):
    async def inquire_korea_orders(
        self, is_mock: bool = False
    ) -> list[dict[str, Any]]: ...
    async def inquire_overseas_orders(
        self, exchange_code: str = "NASD", is_mock: bool = False
    ) -> list[dict[str, Any]]: ...


class _UpbitClientProtocol(Protocol):
    async def fetch_open_orders(
        self, market: str | None = None
    ) -> list[dict[str, Any]]: ...


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
        kis_client_factory: Callable[[], _KISClientProtocol]
        | None = _default_kis_client,
        upbit_client: _UpbitClientProtocol | None = upbit_orders,
        toss_client_factory: Callable[[], Any] | None = _default_toss_client,
        db: AsyncSession | None = None,
        clock: Callable[[], dt.datetime] | None = None,
    ) -> None:
        self._kis_client_factory = kis_client_factory
        self._kis_client_initialized = False
        self._kis_client: _KISClientProtocol | None = None
        self._upbit_client = upbit_client
        self._toss_client_factory = toss_client_factory
        self._db = db
        self._clock = clock or (lambda: dt.datetime.now(tz=dt.UTC))

    async def _attach_symbol_names(self, rows: list[OpenOrderRow]) -> list[OpenOrderRow]:
        """Best-effort display-name enrichment for broker rows that lack names."""
        if self._db is None or not rows:
            return rows

        kr_symbols = sorted({row.symbol for row in rows if row.market == "kr" and not row.symbol_name})
        us_symbols = sorted({row.symbol for row in rows if row.market == "us" and not row.symbol_name})
        crypto_markets = sorted({row.symbol.strip().upper() for row in rows if row.market == "crypto" and not row.symbol_name})

        async def _safe(coro, label: str):
            try:
                return await coro
            except Exception:  # noqa: BLE001 - display names must fail open
                logger.warning("open-order symbol-name resolution failed for %s", label, exc_info=True)
                return {}

        kr_names = (
            await _safe(get_kr_names_by_symbols(kr_symbols, self._db), "kr")
            if kr_symbols
            else {}
        )
        us_names = (
            await _safe(get_us_names_by_symbols(us_symbols, self._db), "us")
            if us_symbols
            else {}
        )
        crypto_names = (
            await _safe(get_upbit_market_display_names(crypto_markets, self._db), "crypto")
            if crypto_markets
            else {}
        )

        enriched: list[OpenOrderRow] = []
        for row in rows:
            if row.symbol_name:
                enriched.append(row)
                continue
            name: str | None = None
            if row.market == "kr":
                name = kr_names.get(row.symbol)
            elif row.market == "us":
                name = us_names.get(row.symbol)
            elif row.market == "crypto":
                display = crypto_names.get(row.symbol.strip().upper())
                if display:
                    name = display.get("korean_name") or display.get("english_name")
            if name and name != row.symbol:
                enriched.append(row.model_copy(update={"symbol_name": name}))
            else:
                enriched.append(row)
        return enriched

    async def list_open_orders(
        self,
        *,
        market: OpenOrdersQueryMarket = "all",
    ) -> OpenOrdersResponse:
        def _fallback(
            broker: Literal["kis", "toss", "upbit"],
            markets: tuple[OpenOrderMarket, ...],
        ) -> list[OpenOrderSourceState]:
            return [
                _source(
                    broker=broker,
                    market=m,
                    status="unavailable",
                    fetched_at=None,
                    count=0,
                    message="collector_error",
                )
                for m in markets
            ]

        specs: list[tuple[Any, list[OpenOrderSourceState]]] = []
        if market in ("all", "kr"):
            specs.append((self._collect_kis_kr(), _fallback("kis", ("kr",))))
        if market in ("all", "us"):
            specs.append((self._collect_kis_us(), _fallback("kis", ("us",))))
        if market in ("all", "crypto"):
            specs.append((self._collect_upbit(), _fallback("upbit", ("crypto",))))
        if market in ("all", "kr", "us"):
            toss_markets: tuple[OpenOrderMarket, ...] = (
                ("kr",)
                if market == "kr"
                else ("us",)
                if market == "us"
                else ("kr", "us")
            )
            specs.append(
                (
                    self._collect_toss_equities(target_market=market),
                    _fallback("toss", toss_markets),
                )
            )

        # return_exceptions=True: collectors already fail open per broker, but if
        # one ever raises unexpectedly it must degrade only its market(s), never
        # 500 the whole endpoint (which would blank every tab).
        results = await asyncio.gather(
            *(coro for coro, _ in specs), return_exceptions=True
        )
        rows: list[OpenOrderRow] = []
        sources: list[OpenOrderSourceState] = []
        for (_, fallback_sources), result in zip(specs, results, strict=True):
            if isinstance(result, BaseException):
                logger.warning(
                    "open-order collector raised unexpectedly", exc_info=result
                )
                sources.extend(fallback_sources)
                continue
            result_rows, result_sources = result
            rows.extend(result_rows)
            if isinstance(result_sources, list):
                sources.extend(result_sources)
            else:
                sources.append(result_sources)

        rows.sort(key=_sort_key, reverse=True)
        rows = await self._attach_symbol_names(rows)
        data_state = _overall_state(sources)
        warnings = [
            f"{source.broker}/{source.market}: {source.message or source.status}"
            for source in sources
            if source.status != "ok"
        ]
        empty_reason = None
        if not rows:
            if data_state == "unavailable":
                empty_reason = "all requested broker sources are unavailable"
            elif data_state == "degraded":
                empty_reason = (
                    "some broker sources are unavailable; no open orders from "
                    "available sources"
                )
            else:
                empty_reason = "no open orders for the selected market"
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
        if not self._kis_client_initialized:
            self._kis_client_initialized = True
            if self._kis_client_factory is not None:
                self._kis_client = self._kis_client_factory()
        return self._kis_client

    async def _collect_kis_kr(self) -> tuple[list[OpenOrderRow], OpenOrderSourceState]:
        now = self._clock()
        kis = self._kis()
        if kis is None:
            return [], _source(
                broker="kis",
                market="kr",
                status="unavailable",
                fetched_at=None,
                count=0,
                message="kis_client_unavailable",
            )
        try:
            raw = await kis.inquire_korea_orders(is_mock=False)
        except Exception as exc:  # noqa: BLE001 - endpoint must fail open per broker
            logger.warning("KIS KR open-order fetch failed", exc_info=True)
            return [], _source(
                broker="kis",
                market="kr",
                status="unavailable",
                fetched_at=now,
                count=0,
                message=type(exc).__name__,
            )
        rows = [
            normalize_kis_order(row, market="kr", exchange="KRX")
            for row in raw or []
            if isinstance(row, dict)
        ]
        return rows, _source(
            broker="kis", market="kr", status="ok", fetched_at=now, count=len(rows)
        )

    async def _collect_kis_us(self) -> tuple[list[OpenOrderRow], OpenOrderSourceState]:
        now = self._clock()
        kis = self._kis()
        if kis is None:
            return [], _source(
                broker="kis",
                market="us",
                status="unavailable",
                fetched_at=None,
                count=0,
                message="kis_client_unavailable",
            )
        rows: list[OpenOrderRow] = []
        seen: set[str] = set()
        errors: dict[str, str] = {}
        for exchange in _KIS_US_EXCHANGES:
            try:
                raw = await kis.inquire_overseas_orders(
                    exchange_code=exchange, is_mock=False
                )
            except Exception as exc:  # noqa: BLE001
                errors[exchange] = type(exc).__name__
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
            return [], _source(
                broker="kis",
                market="us",
                status="unavailable",
                fetched_at=now,
                count=0,
                message="; ".join(f"{k}={v}" for k, v in errors.items()),
            )
        status: OpenOrderDataState = "degraded" if errors else "ok"
        message = "; ".join(f"{k}={v}" for k, v in errors.items()) if errors else None
        return rows, _source(
            broker="kis",
            market="us",
            status=status,
            fetched_at=now,
            count=len(rows),
            message=message,
        )

    async def _collect_upbit(self) -> tuple[list[OpenOrderRow], OpenOrderSourceState]:
        now = self._clock()
        if self._upbit_client is None:
            return [], _source(
                broker="upbit",
                market="crypto",
                status="unavailable",
                fetched_at=None,
                count=0,
                message="upbit_client_unavailable",
            )
        try:
            raw = await self._upbit_client.fetch_open_orders(market=None)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Upbit open-order fetch failed", exc_info=True)
            return [], _source(
                broker="upbit",
                market="crypto",
                status="unavailable",
                fetched_at=now,
                count=0,
                message=type(exc).__name__,
            )
        rows = [
            normalize_upbit_order(row) for row in raw or [] if isinstance(row, dict)
        ]
        return rows, _source(
            broker="upbit",
            market="crypto",
            status="ok",
            fetched_at=now,
            count=len(rows),
        )

    async def _collect_toss_equities(
        self,
        *,
        target_market: OpenOrdersQueryMarket,
    ) -> tuple[list[OpenOrderRow], OpenOrderSourceState | list[OpenOrderSourceState]]:
        now = self._clock()
        markets: tuple[Literal["kr", "us"], ...]
        if target_market == "kr":
            markets = ("kr",)
        elif target_market == "us":
            markets = ("us",)
        else:
            markets = ("kr", "us")

        if self._toss_client_factory is None:
            states = [
                _source(
                    broker="toss",
                    market=market,
                    status="unavailable",
                    fetched_at=None,
                    count=0,
                    message="toss_client_unavailable",
                )
                for market in markets
            ]
            return [], states

        client: Any | None = None
        try:
            client = self._toss_client_factory()
            cursor: str | None = None
            rows: list[OpenOrderRow] = []
            seen_cursors: set[str] = set()
            for _ in range(_TOSS_MAX_PAGES):
                page = await client.list_orders(status="OPEN", cursor=cursor)
                rows.extend(normalize_toss_order(order) for order in page.orders)
                if not page.has_next or not page.next_cursor:
                    break
                if page.next_cursor in seen_cursors:
                    logger.warning("Toss pagination cursor did not advance; stopping")
                    break
                seen_cursors.add(page.next_cursor)
                cursor = page.next_cursor
            else:
                logger.warning(
                    "Toss pagination hit max page cap (%d); returning partial",
                    _TOSS_MAX_PAGES,
                )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Toss open-order fetch failed", exc_info=True)
            states = [
                _source(
                    broker="toss",
                    market=market,
                    status="unavailable",
                    fetched_at=now,
                    count=0,
                    message=type(exc).__name__,
                )
                for market in markets
            ]
            return [], states
        finally:
            close = getattr(client, "aclose", None)
            if callable(close):
                try:
                    await close()
                except Exception:  # noqa: BLE001 - close must never break the request
                    logger.warning("Toss client close failed", exc_info=True)

        filtered = [row for row in rows if row.market in markets]
        states = [
            _source(
                broker="toss",
                market=market,
                status="ok",
                fetched_at=now,
                count=sum(1 for row in filtered if row.market == market),
            )
            for market in markets
        ]
        return filtered, states
