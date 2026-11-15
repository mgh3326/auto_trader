"""TREND-TSMOM-v1 executor: the only module here that can reach a broker.

Everything that can send an order lives in this file: the lane's Futures Demo
client identity (:class:`TrendDemoClient`) and the tick runner
(:class:`TrendExecutor`). No scheduler, CLI, loop or alert is wired to it
(CT-5 adds the operator surface).

Order of every send, and where each rule is enforced:

1. ``_guard``: ``BINANCE_TREND_DEMO_ENABLED`` AND ``BINANCE_FUTURES_DEMO_ENABLED``
   (both exactly ``"true"``) AND per-call ``confirm=True``, plus the exact demo
   origin and client identity, before any network or DB use.
2. ``TrendStateService.reserve_entry`` (caps, kill switches, uncertain symbols).
3. Account-wide gate #1 (complete positions + open orders) right after the
   reservation, then the demo ledger root, then the intent reservation.
4. Account-wide gate #2 and a fresh executable-price re-check right before the
   send fence.
5. ``TrendStateService.mark_sending`` (the committed fence; entry rules judged
   again) and exactly one ``submit_order``.
6. A bounded poll of the order by client id, then a position read that must
   confirm the fill. A response echo mismatch, an unknown status, a transport
   error or any exception after the fence keeps the intent ``uncertain``; an
   ``uncertain`` intent is never resent and is resolved only by broker order
   evidence plus a complete position read.

Serialization: one live trend order per signal. Entry and exit fills on one
broker position cannot be reconciled against a single position read, so a
partially filled (still working) entry is never closed while the order can still
fill. The lane cancels its remainder (``cancel_order``, same gates as
``submit_order``), records a proven hard stop at the moment it is decided
(``TrendStateService.record_exit_decision``, which is what the daily kill switch
counts), and sends the sole exit for the broker-proven quantity once the entry is
terminal. The entry order itself is never resent and an ``uncertain`` intent is
never cancelled.
"""

from __future__ import annotations

import asyncio
import datetime as dt
import logging
import os
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from sqlalchemy.ext.asyncio import AsyncSession

from app.services.brokers.binance.demo.errors import BinanceDemoOrderNotFound
from app.services.brokers.binance.demo.ledger.service import BinanceDemoLedgerService
from app.services.brokers.binance.demo.quantity import venue_order_quantity
from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoCancelResult,
    FuturesDemoOpenOrder,
    FuturesDemoOpenOrdersResult,
    FuturesDemoOrderStatusResult,
    FuturesDemoOrderSubmitResult,
    FuturesDemoPositionModeResult,
    FuturesDemoPositionResult,
)
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
)
from app.services.brokers.binance.futures_demo.signing import (
    BINANCE_FUTURES_DEMO_RECV_WINDOW_MS,
    _sign_request_params,
)
from research.nautilus_scalping.rob974_features import FOUR_HOUR_MS, Bar4h

from .constants import (
    CLIENT_ORDER_PREFIX,
    DEMO_URL,
    EXIT_HARD_STOP,
    IDENTITY,
    LOCKED_LIMITS,
    assert_trend_demo_url,
)
from .exits import (
    ExitDecision,
    HardStopObservation,
    HeldPosition,
    choose_exit,
)
from .exposure import (
    TrendExposureMismatch,
    entry_conflicts,
    exit_quantity,
    position_amount,
)
from .forecast import ensure_entry_forecast, repair_forecast, repair_forecasts
from .history import (
    collect_hard_stop_minutes,
    collect_signal_bars,
    minute_high,
    minute_low,
)
from .keys import trend_client_order_id
from .sizing import TrendSizingBlocked, floor_to_step, size_entry
from .state import (
    TrendEntryRefused,
    TrendIntentSnapshot,
    TrendOrderEvidence,
    TrendSignalSnapshot,
    TrendStateBlocked,
    TrendStateService,
)
from .strategy import (
    TrendBar4h,
    TrendStrategy,
    bar_close,
    bar_close_text,
    evaluate_all,
    momentum_sign,
    realized_vol_annual,
)

logger = logging.getLogger(__name__)

_ENV_TREND = "BINANCE_TREND_DEMO_ENABLED"
_ENV_FUTURES = "BINANCE_FUTURES_DEMO_ENABLED"
_TERMINAL_STATUSES = frozenset(
    {"FILLED", "CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"}
)
_KNOWN_STATUSES = _TERMINAL_STATUSES | {"NEW", "PARTIALLY_FILLED"}
_NO_FILL_TERMINAL = frozenset({"CANCELED", "EXPIRED", "REJECTED", "EXPIRED_IN_MATCH"})


class TrendBrokerTruthUnavailable(RuntimeError):
    """Missing, malformed or contradictory broker evidence blocks trend action."""


@dataclass(frozen=True)
class TrendQuote:
    symbol: str
    bid: Decimal
    ask: Decimal

    def executable_price(self, side: str) -> Decimal:
        if side == "BUY":
            return self.ask
        if side == "SELL":
            return self.bid
        raise ValueError("invalid trend side")


@dataclass(frozen=True)
class TrendFilters:
    step_size: Decimal
    min_notional_usdt: Decimal
    min_qty: Decimal
    max_qty: Decimal
    quantity_precision: int


def _positive_decimal(raw: Any, field: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise TrendBrokerTruthUnavailable(f"invalid {field}") from exc
    if not value.is_finite() or value <= 0:
        raise TrendBrokerTruthUnavailable(f"invalid {field}")
    return value


def _usable_lot(lot: Any) -> bool:
    """A lot filter that carries a positive step (``0`` means "use LOT_SIZE")."""
    if not isinstance(lot, dict):
        return False
    try:
        return Decimal(str(lot.get("stepSize", "0"))) > 0
    except (InvalidOperation, ValueError):
        return False


def _lot_bound(
    lot: dict, plain_lot: Any, key: str, *, default: Decimal | None = None
) -> Decimal:
    """``minQty``/``maxQty``; a zero (documented "no limit for market orders")
    falls back to LOT_SIZE's value, then to ``default`` (one lot step for a
    minimum, since nothing smaller than a step can be sent anyway)."""
    try:
        value = Decimal(str(lot.get(key)))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise TrendBrokerTruthUnavailable(f"invalid {key}") from exc
    if not value.is_finite() or value < 0:
        raise TrendBrokerTruthUnavailable(f"invalid {key}")
    if value > 0:
        return value
    if isinstance(plain_lot, dict) and plain_lot is not lot:
        return _lot_bound(plain_lot, None, key, default=default)
    if default is not None:
        return default
    raise TrendBrokerTruthUnavailable(f"invalid {key}")


class TrendDemoClient(BinanceFuturesDemoExecutionClient):
    """Trend identity over the existing signed Futures Demo transport.

    Read-only account, quote and filter reads plus a gated ``submit_order`` and
    a gated ``cancel_order``. Leverage, margin mode and the smoke order-test are
    disabled: configuration is required by readback only (operator decision). An
    unresolved order is still resolved by evidence, never by cancelling; the only
    cancel is the remainder of a partially filled entry, the serialization step
    that must stop further fills before an exit may be sent.
    """

    def __init__(
        self, *, api_key: str, api_secret: str, base_url: str = DEMO_URL
    ) -> None:
        assert_trend_demo_url(base_url)
        super().__init__(api_key=api_key, api_secret=api_secret, base_url=base_url)

    @classmethod
    def from_env(cls) -> TrendDemoClient:
        if os.environ.get(_ENV_TREND) != "true":
            raise ValueError(f"{_ENV_TREND} is disabled")
        assert_trend_demo_url(os.environ.get("BINANCE_FUTURES_DEMO_BASE_URL", DEMO_URL))
        return super().from_env()  # type: ignore[return-value]

    def _assert_trend(self) -> None:
        assert_trend_demo_url(self._base_url)
        assert_trend_demo_url(str(self._client.base_url).rstrip("/"))

    def assert_demo_identity(self) -> None:
        """Read-only identity check shared with the truth gate."""
        self._assert_trend()

    def kline_transport(self) -> Any:
        """The unsigned market-data transport, origin re-validated per call."""
        self._assert_trend()
        return self._client

    async def _signed_get(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> Any:
        self._assert_trend()
        signed = _sign_request_params(
            params={
                **(params or {}),
                "recvWindow": str(BINANCE_FUTURES_DEMO_RECV_WINDOW_MS),
            },
            api_secret=self._api_secret,
        )
        response = await self._client.get(path, params=signed)
        response.raise_for_status()
        try:
            return response.json()
        except ValueError as exc:
            raise TrendBrokerTruthUnavailable("broker JSON unavailable") from exc

    async def get_book_quote(self, symbol: str) -> TrendQuote:
        self._assert_trend()
        if symbol not in LOCKED_LIMITS.universe:
            raise ValueError("symbol outside trend universe")
        response = await self._client.get(
            "/fapi/v1/ticker/bookTicker", params={"symbol": symbol}
        )
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise TrendBrokerTruthUnavailable("quote body unreadable") from exc
        if not isinstance(body, dict) or body.get("symbol") != symbol:
            raise TrendBrokerTruthUnavailable("book quote symbol mismatch")
        bid = _positive_decimal(body.get("bidPrice"), "bid")
        ask = _positive_decimal(body.get("askPrice"), "ask")
        if bid > ask:
            raise TrendBrokerTruthUnavailable("crossed book quote")
        return TrendQuote(symbol=symbol, bid=bid, ask=ask)

    async def get_trend_filters(self, symbol: str) -> TrendFilters:
        self._assert_trend()
        if symbol not in LOCKED_LIMITS.universe:
            raise ValueError("symbol outside trend universe")
        response = await self._client.get(
            "/fapi/v1/exchangeInfo", params={"symbol": symbol}
        )
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise TrendBrokerTruthUnavailable("exchange info unreadable") from exc
        rows = body.get("symbols") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise TrendBrokerTruthUnavailable("exchange filters unavailable")
        matches = [
            row for row in rows if isinstance(row, dict) and row.get("symbol") == symbol
        ]
        if len(matches) != 1 or matches[0].get("status") != "TRADING":
            raise TrendBrokerTruthUnavailable("trend symbol not trading")
        row = matches[0]
        by_type = {
            item.get("filterType"): item
            for item in row.get("filters", [])
            if isinstance(item, dict)
        }
        market_lot = by_type.get("MARKET_LOT_SIZE")
        plain_lot = by_type.get("LOT_SIZE")
        lot = market_lot if _usable_lot(market_lot) else plain_lot
        if not isinstance(lot, dict):
            raise TrendBrokerTruthUnavailable("market lot filter missing")
        notional = by_type.get("MIN_NOTIONAL") or by_type.get("NOTIONAL")
        if not isinstance(lot, dict) or not isinstance(notional, dict):
            raise TrendBrokerTruthUnavailable("trend filter missing")
        minimum = notional.get("notional", notional.get("minNotional"))
        precision = row.get("quantityPrecision")
        if type(precision) is not int:
            raise TrendBrokerTruthUnavailable("quantity precision unavailable")
        step_size = _positive_decimal(lot.get("stepSize"), "stepSize")
        return TrendFilters(
            step_size=step_size,
            min_notional_usdt=_positive_decimal(minimum, "MIN_NOTIONAL"),
            min_qty=_lot_bound(lot, plain_lot, "minQty", default=step_size),
            max_qty=_lot_bound(lot, plain_lot, "maxQty"),
            quantity_precision=precision,
        )

    async def assert_symbol_isolated_1x(self, symbol: str) -> None:
        """Require positive readback of BOTH, isolated margin and leverage 1."""
        if symbol not in LOCKED_LIMITS.universe:
            raise ValueError("symbol outside trend universe")
        body = await self._signed_get(
            "/fapi/v2/positionRisk", params={"symbol": symbol}
        )
        if not isinstance(body, list):
            raise TrendBrokerTruthUnavailable("margin configuration unreadable")
        rows = [
            row for row in body if isinstance(row, dict) and row.get("symbol") == symbol
        ]
        if len(rows) != 1:
            raise TrendBrokerTruthUnavailable("margin configuration not unique")
        row = rows[0]
        if (
            str(row.get("marginType")).lower() != "isolated"
            or str(row.get("leverage")) != "1"
            or row.get("positionSide") != "BOTH"
        ):
            raise TrendBrokerTruthUnavailable(
                "trend requires preconfigured isolated 1x BOTH"
            )

    async def get_position_mode(self) -> FuturesDemoPositionModeResult:
        body = await self._signed_get("/fapi/v1/positionSide/dual")
        flag = body.get("dualSidePosition") if isinstance(body, dict) else None
        # Documented as a boolean, shown as "true"/"false" in some references:
        # accept exactly those four spellings and nothing else.
        if flag is True or flag == "true":
            hedge = True
        elif flag is False or flag == "false":
            hedge = False
        else:
            raise TrendBrokerTruthUnavailable("position mode evidence unavailable")
        return FuturesDemoPositionModeResult(is_hedge_mode=hedge)

    async def get_all_positions(self) -> list[FuturesDemoPositionResult]:
        body = await self._signed_get("/fapi/v2/positionRisk")
        if not isinstance(body, list) or not body:
            raise TrendBrokerTruthUnavailable("complete position array unavailable")
        positions: list[FuturesDemoPositionResult] = []
        seen: set[str] = set()
        for row in body:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("symbol"), str)
                or not row["symbol"]
            ):
                raise TrendBrokerTruthUnavailable("position symbol unavailable")
            if row["symbol"] in seen or row.get("positionSide") != "BOTH":
                raise TrendBrokerTruthUnavailable("position scope is not one-way")
            seen.add(row["symbol"])
            try:
                qty = Decimal(row["positionAmt"])
                price = Decimal(row["entryPrice"])
                leverage = Decimal(str(row["leverage"]))
            except (KeyError, TypeError, InvalidOperation) as exc:
                raise TrendBrokerTruthUnavailable(
                    "position fields unavailable"
                ) from exc
            if (
                not all(value.is_finite() for value in (qty, price, leverage))
                or leverage <= 0
                or leverage != leverage.to_integral_value()
                or price < 0
            ):
                raise TrendBrokerTruthUnavailable("invalid position fields")
            positions.append(
                FuturesDemoPositionResult(
                    symbol=row["symbol"],
                    position_amt=qty,
                    entry_price=price,
                    leverage=int(leverage),
                    is_flat=qty == 0,
                    position_side="BOTH",
                )
            )
        return positions

    async def get_all_open_orders(self) -> FuturesDemoOpenOrdersResult:
        body = await self._signed_get("/fapi/v1/openOrders")
        if not isinstance(body, list):
            raise TrendBrokerTruthUnavailable("complete open-order array unavailable")
        orders: list[FuturesDemoOpenOrder] = []
        for row in body:
            if (
                not isinstance(row, dict)
                or not row.get("symbol")
                or not row.get("clientOrderId")
                or not row.get("orderId")
                or type(row.get("reduceOnly")) is not bool
                or row.get("positionSide") != "BOTH"
            ):
                raise TrendBrokerTruthUnavailable("incomplete open-order fields")
            orders.append(
                FuturesDemoOpenOrder(
                    client_order_id=row["clientOrderId"],
                    broker_order_id=str(row["orderId"]),
                    symbol=row["symbol"],
                    side=row.get("side", ""),
                    qty=_positive_decimal(row.get("origQty"), "open order qty"),
                    status=row.get("status", ""),
                    reduce_only=row["reduceOnly"],
                    position_side="BOTH",
                )
            )
        return FuturesDemoOpenOrdersResult(orders=orders)

    async def get_order(  # type: ignore[override]
        self, *, symbol: str, client_order_id: str
    ) -> FuturesDemoOrderStatusResult:
        self._assert_trend()
        if symbol not in LOCKED_LIMITS.universe or not client_order_id.startswith(
            CLIENT_ORDER_PREFIX
        ):
            raise ValueError("order outside trend identity")
        try:
            result = await super().get_order(
                symbol=symbol, client_order_id=client_order_id
            )
        except ValueError as exc:
            raise TrendBrokerTruthUnavailable("order evidence unreadable") from exc
        raw = result.raw_response_redacted
        if (
            raw.get("symbol") != symbol
            or raw.get("clientOrderId") != client_order_id
            or type(raw.get("reduceOnly")) is not bool
        ):
            raise TrendBrokerTruthUnavailable("order identity needs a broker echo")
        return result

    async def set_leverage(self, **kwargs):  # type: ignore[override]
        raise ValueError("trend requires preconfigured isolated 1x; no mutation")

    async def set_margin_type(self, *args, **kwargs):  # type: ignore[override]
        raise ValueError("trend requires preconfigured isolated 1x; no mutation")

    async def cancel_order(  # type: ignore[override]
        self, *, symbol: str, client_order_id: str, confirm: bool = False
    ) -> FuturesDemoCancelResult:
        """Cancel the remaining quantity of this lane's own working entry.

        Only one caller exists: the serialization step of a partially filled
        entry, which must stop further fills before an exit can be sent. That
        is not a resolution of an unresolved send — the entry order is never
        resent — and it carries the same gates as :meth:`submit_order`: exact
        demo origin, trend client identity, both env gates and per-call
        ``confirm=True``.
        """
        self._assert_trend()
        if symbol not in LOCKED_LIMITS.universe:
            raise ValueError("symbol outside trend universe")
        if not isinstance(client_order_id, str) or not client_order_id.startswith(
            CLIENT_ORDER_PREFIX
        ):
            raise ValueError("order outside trend identity")
        if os.environ.get(_ENV_TREND) != "true":
            raise ValueError(f"{_ENV_TREND} is disabled")
        if os.environ.get(_ENV_FUTURES) != "true":
            raise ValueError(f"{_ENV_FUTURES} is disabled")
        if confirm is not True:
            raise ValueError("trend requires per-call confirm=True")
        result = await super().cancel_order(
            symbol=symbol, client_order_id=client_order_id
        )
        raw = result.raw_response_redacted
        if raw.get("symbol") != symbol or raw.get("clientOrderId") != client_order_id:
            raise TrendBrokerTruthUnavailable("cancel response did not echo the order")
        return result

    async def order_test(self, **kwargs):  # type: ignore[override]
        raise ValueError("trend does not expose the smoke order-test path")

    async def submit_order(self, **kwargs):  # type: ignore[override]
        """MARKET, one-way (``BOTH``), trend-identified, gated twice."""
        self._assert_trend()
        if kwargs.get("symbol") not in LOCKED_LIMITS.universe:
            raise ValueError("symbol outside trend universe")
        client_order_id = kwargs.get("client_order_id")
        if not isinstance(client_order_id, str) or not client_order_id.startswith(
            CLIENT_ORDER_PREFIX
        ):
            raise ValueError("order outside trend identity")
        if (
            kwargs.get("order_type") != "MARKET"
            or kwargs.get("position_side") != "BOTH"
        ):
            raise ValueError("trend sends MARKET one-way orders only")
        # Both gates exactly "true" and per-call confirm, checked here too: a
        # directly constructed client must not bypass the futures gate.
        if os.environ.get(_ENV_TREND) != "true":
            raise ValueError(f"{_ENV_TREND} is disabled")
        if os.environ.get(_ENV_FUTURES) != "true":
            raise ValueError(f"{_ENV_FUTURES} is disabled")
        if kwargs.get("confirm") is not True:
            raise ValueError("trend requires per-call confirm=True")
        return await super().submit_order(**kwargs)


@dataclass(frozen=True)
class TrendEvent:
    symbol: str
    event: str
    detail: str | None = None


@dataclass(frozen=True)
class TrendTickResult:
    decision_ts: int
    events: tuple[TrendEvent, ...]


def now_ms(value: dt.datetime) -> int:
    return int(value.timestamp() * 1000)


def _broker_time(order: FuturesDemoOrderStatusResult) -> dt.datetime | None:
    value = order.raw_response_redacted.get("updateTime")
    if type(value) is not int or not 0 < value < 2**63:
        return None
    return dt.datetime.fromtimestamp(value / 1000, tz=dt.UTC)


def _evidence(order: FuturesDemoOrderStatusResult) -> TrendOrderEvidence:
    return TrendOrderEvidence(
        client_order_id=order.client_order_id,
        broker_order_id=order.broker_order_id,
        symbol=order.symbol,
        side=order.side,
        orig_qty=order.orig_qty,
        executed_qty=order.executed_qty,
        avg_price=order.avg_price,
        status=order.status,
        reduce_only=order.reduce_only,
        position_side=order.position_side,
    )


def affirmative_rejection(exc: BaseException) -> str | None:
    """``http_<status>:<code>`` only for a broker 4xx carrying a negative error code.

    That shape is Binance's explicit "order not created" answer. Timeouts, 5xx
    (including -1007 "send status unknown"), rate-limit answers (408/418/429),
    unreadable bodies and everything else are NOT proof that no order exists.
    """
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if type(status) is not int or not 400 <= status < 500 or status in {408, 418, 429}:
        return None
    try:
        body = response.json()
    except Exception:
        return None
    code = body.get("code") if isinstance(body, dict) else None
    if type(code) is not int or code >= 0:
        return None
    return f"http_{status}:{code}"


class TrendExecutor:
    def __init__(
        self,
        *,
        client: TrendDemoClient,
        strategy: TrendStrategy,
        state: TrendStateService,
        demo_ledger: BinanceDemoLedgerService,
        ledger_session: AsyncSession,
        poll_attempts: int = 5,
        poll_interval_s: float = 0.5,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        if poll_attempts < 1:
            raise ValueError("poll_attempts must be at least 1")
        self.client = client
        self.strategy = strategy
        self.state = state
        self.demo_ledger = demo_ledger
        self.ledger_session = ledger_session
        self._lease: Any = None
        self._manage_only = False
        self._poll_attempts = poll_attempts
        self._poll_interval_s = poll_interval_s
        self._sleep = sleep

    # ------------------------------------------------------------------ gate

    def _guard(self, *, confirm: bool) -> None:
        """Refuse before any network or DB use."""
        if os.environ.get(_ENV_TREND) != "true":
            raise TrendStateBlocked(f"{_ENV_TREND} is disabled")
        if os.environ.get(_ENV_FUTURES) != "true":
            raise TrendStateBlocked(f"{_ENV_FUTURES} is disabled")
        if confirm is not True:
            raise TrendStateBlocked("per-call confirm=True required")
        if not isinstance(self.client, TrendDemoClient):
            raise TrendStateBlocked("trend requires its Futures Demo client identity")
        self.client._assert_trend()
        if self.strategy.strategy_id != IDENTITY:
            raise TrendStateBlocked("non-trend plugin identity")

    def _check_origin(self) -> None:
        self.client._assert_trend()

    # ----------------------------------------------------------- broker reads

    async def _read_account(
        self,
    ) -> tuple[list[FuturesDemoPositionResult], FuturesDemoOpenOrdersResult]:
        positions = await self.client.get_all_positions()
        orders = await self.client.get_all_open_orders()
        return positions, orders

    # ---------------------------------------------------------------- ledger

    @asynccontextmanager
    async def _ledger_guard(self) -> AsyncIterator[None]:
        """A failed ledger write must not leave the shared session aborted.

        The next sweep or tick reuses ``ledger_session``; without a rollback a
        database error would poison every later ledger call until restart.
        """
        try:
            yield
        except BaseException:
            try:
                await self.ledger_session.rollback()
            except Exception:
                logger.exception("trend ledger session rollback failed")
            raise

    async def _instrument_id(self, *args: Any, **kwargs: Any) -> Any:
        async with self._ledger_guard():
            return await self._instrument_id_unguarded(*args, **kwargs)

    async def _prepare_demo_entry(self, *args: Any, **kwargs: Any) -> Any:
        async with self._ledger_guard():
            return await self._prepare_demo_entry_unguarded(*args, **kwargs)

    async def _prepare_demo_exit(self, *args: Any, **kwargs: Any) -> Any:
        async with self._ledger_guard():
            return await self._prepare_demo_exit_unguarded(*args, **kwargs)

    async def _release_demo_unsent(self, *args: Any, **kwargs: Any) -> Any:
        async with self._ledger_guard():
            return await self._release_demo_unsent_unguarded(*args, **kwargs)

    async def _sync_demo_ledger(self, *args: Any, **kwargs: Any) -> Any:
        async with self._ledger_guard():
            return await self._sync_demo_ledger_unguarded(*args, **kwargs)

    async def _close_root_if_proven(self, *args: Any, **kwargs: Any) -> Any:
        async with self._ledger_guard():
            return await self._close_root_if_proven_unguarded(*args, **kwargs)

    @staticmethod
    def _entry_cid(signal_key: str) -> str:
        return trend_client_order_id(signal_key, "entry")

    async def _instrument_id_unguarded(self, symbol: str) -> int:
        instrument_id = await self.demo_ledger.resolve_or_create_instrument(
            venue="binance",
            product="usdm_futures",
            venue_symbol=symbol,
            base_asset=symbol.removesuffix("USDT"),
            quote_asset="USDT",
        )
        await self.ledger_session.commit()
        return instrument_id

    async def _prepare_demo_entry_unguarded(
        self,
        *,
        signal: TrendSignalSnapshot,
        qty: Decimal,
        notional_usdt: Decimal,
        now: dt.datetime,
    ) -> None:
        instrument_id = await self._instrument_id(signal.symbol)
        cid = self._entry_cid(signal.signal_key)
        reservation = await self.demo_ledger.reserve_root_planned(
            instrument_id=instrument_id,
            product="usdm_futures",
            venue_host="demo-fapi.binance.com",
            client_order_id=cid,
            side=signal.side,
            order_type="MARKET",
            qty=qty,
            price=None,
            notional_usdt=notional_usdt,
            extra_metadata={
                "source": "trend-tsmom-v1",
                "trend_signal_key": signal.signal_key,
                "correlation_id": signal.correlation_id,
                "decision_ts": signal.decision_ts,
            },
            idempotency_metadata={
                "trend_signal_key": signal.signal_key,
                "strategy_id": IDENTITY,
            },
            global_open_root_cap=LOCKED_LIMITS.max_concurrent_positions,
            now=now,
        )
        if reservation.status != "reserved":
            raise TrendStateBlocked(reservation.reason or reservation.status)
        await self.demo_ledger.record_previewed(client_order_id=cid, now=now)
        await self.demo_ledger.record_validated(client_order_id=cid, now=now)
        await self.ledger_session.commit()

    async def _prepare_demo_exit_unguarded(
        self,
        *,
        signal: TrendSignalSnapshot,
        intent: TrendIntentSnapshot,
        reason: str,
        now: dt.datetime,
    ) -> None:
        instrument_id = await self._instrument_id(signal.symbol)
        await self.demo_ledger.record_planned(
            instrument_id=instrument_id,
            product="usdm_futures",
            venue_host="demo-fapi.binance.com",
            client_order_id=intent.client_order_id,
            parent_client_order_id=signal.entry_client_order_id,
            side=intent.side,
            order_type="MARKET",
            qty=intent.qty,
            price=None,
            extra_metadata={
                "source": "trend-tsmom-v1",
                "trend_signal_key": signal.signal_key,
                "correlation_id": signal.correlation_id,
                "exit_reason": reason,
                "reduce_only": True,
            },
            now=now,
        )
        await self.demo_ledger.record_previewed(
            client_order_id=intent.client_order_id, now=now
        )
        await self.demo_ledger.record_validated(
            client_order_id=intent.client_order_id, now=now
        )
        await self.ledger_session.commit()

    async def _release_demo_unsent_unguarded(
        self, cid: str, *, now: dt.datetime
    ) -> None:
        """Only a pre-send ledger row may be cancelled and reconciled."""
        row = await self.demo_ledger.get_by_client_order_id(cid)
        if row is None or row.lifecycle_state in {"cancelled", "reconciled"}:
            return
        if row.lifecycle_state not in {"planned", "previewed", "validated"}:
            raise TrendStateBlocked("cannot release a possibly sent demo row")
        await self.demo_ledger.record_cancelled(client_order_id=cid, now=now)
        await self.demo_ledger.record_reconciled(client_order_id=cid, now=now)
        await self.ledger_session.commit()

    async def _sync_demo_ledger_unguarded(
        self,
        intent: TrendIntentSnapshot,
        evidence: TrendOrderEvidence,
        *,
        now: dt.datetime,
    ) -> None:
        """Every write to the unified demo ledger goes through its service."""
        cid = intent.client_order_id
        row = await self.demo_ledger.get_by_client_order_id(cid)
        if row is None:
            raise TrendStateBlocked("demo ledger order row missing")
        state = row.lifecycle_state
        no_fill = evidence.status in _NO_FILL_TERMINAL
        if state == "validated":
            if no_fill and evidence.executed_qty == 0:
                await self.demo_ledger.record_cancelled(client_order_id=cid, now=now)
                await self.demo_ledger.record_reconciled(client_order_id=cid, now=now)
                await self.ledger_session.commit()
                return
            await self.demo_ledger.record_submitted(
                client_order_id=cid, broker_order_id=evidence.broker_order_id, now=now
            )
            state = "submitted"
        if state == "submitted" and no_fill and evidence.executed_qty == 0:
            await self.demo_ledger.record_cancelled(client_order_id=cid, now=now)
            await self.demo_ledger.record_reconciled(client_order_id=cid, now=now)
            await self.ledger_session.commit()
            return
        if (
            state == "submitted"
            and evidence.executed_qty > 0
            and (evidence.status == "FILLED" or no_fill)
        ):
            # A terminal order with a positive executed quantity is a fill of
            # that quantity (broker evidence, confirmed by the position read in
            # the state service). It stays on the closable lifecycle: an
            # ``anomaly`` root could never be closed or released again.
            await self.demo_ledger.record_filled(
                client_order_id=cid,
                now=now,
                extra_metadata_merge={
                    "fill_evidence": "broker_order_status",
                    "filled_qty": format(evidence.executed_qty, "f"),
                    "avg_price": format(evidence.avg_price, "f"),
                    "partial_terminal_fill": evidence.status != "FILLED",
                },
            )
            state = "filled"
        if intent.reduce_only and state == "filled":
            await self.demo_ledger.record_closed(client_order_id=cid, now=now)
            await self.demo_ledger.record_reconciled(client_order_id=cid, now=now)
        await self.ledger_session.commit()

    async def _close_root_if_proven_unguarded(
        self, signal: TrendSignalSnapshot, *, now: dt.datetime
    ) -> None:
        """Close the entry root only on flat position and no open order evidence."""
        if signal.state != "closed" or signal.entry_client_order_id is None:
            return
        positions, orders = await self._read_account()
        if position_amount(positions, signal.symbol) != 0 or any(
            order.symbol == signal.symbol for order in orders.orders
        ):
            raise TrendStateBlocked("root release lacks flat and empty-order evidence")
        row = await self.demo_ledger.get_by_client_order_id(
            signal.entry_client_order_id
        )
        if row is None:
            raise TrendStateBlocked("demo root missing")
        if row.lifecycle_state == "reconciled":
            return
        if row.lifecycle_state not in {"filled", "closed"}:
            raise TrendStateBlocked("demo root not evidence-filled")
        if row.lifecycle_state == "filled":
            await self.demo_ledger.record_closed(
                client_order_id=signal.entry_client_order_id,
                now=now,
                extra_metadata_merge={
                    "exit_reason": signal.exit_reason,
                    "trend_signal_key": signal.signal_key,
                },
            )
        await self.demo_ledger.record_reconciled(
            client_order_id=signal.entry_client_order_id, now=now
        )
        await self.ledger_session.commit()

    # ------------------------------------------------------ evidence resolution

    async def _keep_uncertain(self, cid: str, *, now: dt.datetime) -> None:
        try:
            await self.state.mark_uncertain(cid, now=now)
        except TrendStateBlocked:
            logger.info("trend intent already uncertain or resolved")

    async def _reconcile_intent(
        self, intent: TrendIntentSnapshot, *, now: dt.datetime, poll: bool
    ) -> str:
        """Resolve one sent intent from broker evidence only.

        Returns ``filled`` / ``closed`` / ``partial`` / ``no_fill`` /
        ``pending`` / ``uncertain`` / ``ledger_incomplete``.
        """
        signal = await self.state.get_signal(intent.signal_key)
        if signal is None:
            raise TrendStateBlocked("intent signal missing")
        attempts = self._poll_attempts if poll else 1
        order: FuturesDemoOrderStatusResult | None = None
        for attempt in range(attempts):
            try:
                order = await self.client.get_order(
                    symbol=signal.symbol, client_order_id=intent.client_order_id
                )
            except BinanceDemoOrderNotFound:
                order = None
            except Exception:
                logger.exception("trend order evidence read failed")
                order = None
            if order is not None and order.status in _TERMINAL_STATUSES:
                break
            if attempt + 1 < attempts:
                await self._sleep(self._poll_interval_s)
        if order is None or order.status not in _KNOWN_STATUSES:
            # Not-found or unreadable evidence never proves "no order".
            await self._keep_uncertain(intent.client_order_id, now=now)
            return "uncertain"
        try:
            positions = await self.client.get_all_positions()
            amount = position_amount(positions, signal.symbol)
            evidence = _evidence(order)
            current, current_intent = await self.state.apply_order_evidence(
                evidence,
                broker_position_amt=amount,
                now=now,
                # An entry is exposed from its reservation (a bound that is never
                # later than the first fill); the order's updateTime is only its
                # last change and would hide earlier price action.
                filled_at=(
                    _broker_time(order) if intent.reduce_only else intent.created_at
                ),
            )
        except Exception:
            logger.exception("trend order evidence could not be applied")
            await self._keep_uncertain(intent.client_order_id, now=now)
            return "uncertain"
        try:
            await self._sync_demo_ledger(intent, evidence, now=now)
            if current.state == "closed":
                await self._close_root_if_proven(current, now=now)
            if current_intent.state == "evidenced":
                current_intent = await self.state.settle_intent(
                    intent.client_order_id, now=now
                )
        except Exception:
            # The evidence is committed; the next tick replays the idempotent
            # ledger sync from the still-unsettled intent.
            logger.exception("trend demo ledger sync incomplete")
            return "ledger_incomplete"
        # Forecast writes are durable but never a trading decision: they run
        # only on broker-proven evidence (entry fill -> holding, held close ->
        # closed) and any failure is logged and retried on later ticks.
        if (
            current.state == "holding"
            and not current_intent.reduce_only
            and current_intent.state == "settled"
        ):
            try:
                await ensure_entry_forecast(current, now=now)
            except Exception:
                logger.exception("trend entry forecast write failed")
        if current.state == "closed":
            try:
                await repair_forecast(current, now=now)
            except Exception:
                logger.exception("trend held-outcome forecast resolution failed")
        if current_intent.state in {"acknowledged", "uncertain"}:
            # An open order with an executed quantity is real exposure managed by
            # ``_manage_working_entries`` (acknowledged) or reported only because
            # its status is unreadable (uncertain). No resend in either case. An
            # intent that was marked uncertain stays uncertain (it never goes
            # back to acknowledged) but is not a "no fill".
            if evidence.executed_qty > 0:
                return "pending_partial"
            return "pending" if current_intent.state == "acknowledged" else "uncertain"
        if intent.reduce_only:
            if current.state == "closed":
                return "closed"
            return "partial" if evidence.executed_qty > 0 else "no_fill"
        return "filled" if current.state == "holding" else "no_fill"

    async def _reconcile_unresolved(
        self, now: dt.datetime, events: list[TrendEvent]
    ) -> set[str]:
        """Settle what the last run left behind; return symbols that stay blocked."""
        for intent in await self.state.list_unresolved_intents():
            signal = await self.state.get_signal(intent.signal_key)
            if signal is None:
                continue
            if intent.state == "reserved":
                # Never fenced: nothing was sent, so the reservation is released.
                try:
                    # State first: this is the atomic claim. Once the intent is
                    # released a concurrent sender can no longer fence it; if the
                    # sender fenced first, the claim fails and nothing is touched.
                    if intent.reduce_only:
                        await self.state.release_unsent_exit(
                            intent.client_order_id, now=now
                        )
                    else:
                        await self.state.block_unsent(intent.signal_key, now=now)
                    await self._release_demo_unsent(intent.client_order_id, now=now)
                    events.append(TrendEvent(signal.symbol, "unsent_released"))
                except Exception as exc:
                    logger.exception("trend unsent reservation release failed")
                    events.append(
                        TrendEvent(signal.symbol, "blocked", type(exc).__name__)
                    )
                continue
            outcome = await self._reconcile_intent(intent, now=now, poll=False)
            events.append(TrendEvent(signal.symbol, f"reconciled_{outcome}"))
        await self._release_orphaned_entries(now, events)
        await self._sweep_released_roots(now)
        blocked: set[str] = set()
        for intent in await self.state.list_unresolved_intents():
            signal = await self.state.get_signal(intent.signal_key)
            if signal is None:
                continue
            if (
                not intent.reduce_only
                and intent.state in {"acknowledged", "uncertain"}
                and intent.executed_qty > 0
            ):
                # A still-working, partially filled entry. It is real exposure,
                # and it is *managed* (see ``_manage_working_entries``): the
                # lane cancels the remainder and exits the broker-proven
                # quantity after the entry is terminal. An intent that was
                # marked uncertain has no trustworthy order status, so
                # serialization cannot be proven and nothing is sent for it.
                events.append(
                    TrendEvent(
                        signal.symbol,
                        "partial_entry_exposure_open",
                        format(intent.executed_qty, "f"),
                    )
                )
                if intent.state == "acknowledged":
                    continue
            blocked.add(signal.symbol)
        for signal in await self.state.list_active_signals():
            if signal.state == "uncertain":
                blocked.add(signal.symbol)
        return blocked

    async def _sweep_released_roots(self, now: dt.datetime) -> None:
        """Retry a ledger release that failed after the state was released.

        Any recently rejected/blocked order whose demo row is still pre-send has
        no order behind it (the state release is the atomic claim), so it only
        wastes a shared open-root slot until released.
        """
        for cid in await self.state.list_released_client_order_ids():
            try:
                row = await self.demo_ledger.get_by_client_order_id(cid)
                if row is not None and row.lifecycle_state in {
                    "planned",
                    "previewed",
                    "validated",
                }:
                    await self._release_demo_unsent(cid, now=now)
            except Exception:
                logger.exception("trend demo ledger sweep release failed")

    async def _working_entry_decision(
        self,
        signal: TrendSignalSnapshot,
        intent: TrendIntentSnapshot,
        bars: tuple[Bar4h, ...],
        *,
        decision_ts: int,
        now: dt.datetime,
    ) -> ExitDecision | None:
        """The exit rules judged against a partially filled, still-working entry.

        The exposure is the executed quantity of the working order; the stop is
        derived from the average of the fills recorded so far, and its watch
        starts at the reservation (a bound that is never later than the first
        fill). The same 1m extremes / 4h close / quote evidence as for a holding
        decides, with the 4h-only fallback when the 1m window is incomplete.
        """
        if intent.avg_price is None or intent.created_at is None:
            return None
        positions = await self.client.get_all_positions()
        # The broker amount is signed; a SHORT (SELL) entry sits negative and
        # must be read as the same positive quantity the holding path uses.
        amount = position_amount(positions, signal.symbol)
        remaining = amount if signal.side == "BUY" else -amount
        if remaining <= 0:
            return None
        filters = await self.client.get_trend_filters(signal.symbol)
        if signal.exit_reason == EXIT_HARD_STOP:
            # A recorded stop is a latch here too: the decision was already
            # taken (and counted by the daily kill switch), so a recovery or a
            # failed cancel must not stop the lane from closing the remainder.
            latched = floor_to_step(remaining, filters.step_size)
            return (
                ExitDecision(reason=EXIT_HARD_STOP, qty=latched)
                if latched > 0
                else None
            )
        close_side = "SELL" if signal.side == "BUY" else "BUY"
        quote_price: Decimal | None
        try:
            quote_price = (
                await self.client.get_book_quote(signal.symbol)
            ).executable_price(close_side)
        except Exception:
            logger.exception("trend book quote unavailable; deciding without it")
            quote_price = None
        observed = await self._observe_hard_stop(
            signal.symbol,
            intent.created_at,
            bars,
            decision_ts=decision_ts,
            quote_price=quote_price,
            now=now,
        )
        return choose_exit(
            HeldPosition(
                side=signal.side,  # type: ignore[arg-type]
                entry_price=intent.avg_price,
                entered_at=intent.created_at,
                broker_remaining_qty=remaining,
                step_size=filters.step_size,
            ),
            stop_price=self.strategy.hard_stop_price(
                side=signal.side,  # type: ignore[arg-type]
                entry_price=intent.avg_price,
            ),
            observed=observed,
            signal_side=momentum_sign(
                bars,
                decision_ts=decision_ts,
                lookback_days=self.strategy.lookback_days,
            ),
            now=now,
        )

    async def _manage_working_entries(
        self,
        histories: dict[str, tuple[Bar4h, ...]],
        *,
        decision_ts: int,
        now: dt.datetime,
        events: list[TrendEvent],
    ) -> None:
        """Serialized management of a partially filled, still-working entry.

        Entry and exit fills on one broker position are two streams the state
        cannot reconcile against a single position read, so the lane never has
        both live: while the entry can still fill the only action is cancelling
        its remainder. The exit follows the entry's terminal evidence on a later
        step of this tick or on the next one. A proven hard stop is recorded
        durably *before* that action, so the daily kill switch counts it on the
        day it fired even if the entry only terminates on a later UTC day.

        A partial entry whose intent is ``uncertain`` has no trustworthy order
        status: serialization cannot be proven, so neither cancel nor exit is
        sent and only the stop is reported (the symbol stays blocked).
        """
        for intent in await self.state.list_unresolved_intents():
            if intent.reduce_only or intent.executed_qty <= 0:
                continue
            if intent.state not in {"acknowledged", "uncertain"}:
                continue
            signal = await self.state.get_signal(intent.signal_key)
            if signal is None:
                continue
            try:
                decision = await self._working_entry_decision(
                    signal,
                    intent,
                    histories.get(signal.symbol, ()),
                    decision_ts=decision_ts,
                    now=now,
                )
            except Exception as exc:
                logger.exception("trend working-entry decision failed")
                events.append(TrendEvent(signal.symbol, "blocked", type(exc).__name__))
                continue
            if decision is None:
                continue
            if intent.state == "uncertain":
                if decision.reason == EXIT_HARD_STOP and intent.avg_price is not None:
                    stop = self.strategy.hard_stop_price(
                        side=signal.side,  # type: ignore[arg-type]
                        entry_price=intent.avg_price,
                    )
                    events.append(
                        TrendEvent(
                            signal.symbol,
                            "partial_entry_stop_breached",
                            format(stop, "f"),
                        )
                    )
                continue
            if decision.reason == EXIT_HARD_STOP:
                try:
                    await self.state.record_exit_decision(
                        signal.signal_key, reason=decision.reason, now=now
                    )
                except Exception as exc:
                    # One symbol's state refusal or DB error must not skip the
                    # management of every other symbol this tick. The cancel
                    # below still runs: it is the action the just-computed
                    # decision calls for.
                    logger.exception("trend working-entry decision record failed")
                    events.append(
                        TrendEvent(signal.symbol, "blocked", type(exc).__name__)
                    )
            events.append(
                TrendEvent(signal.symbol, "partial_entry_exit_decided", decision.reason)
            )
            try:
                await self.client.cancel_order(
                    symbol=signal.symbol,
                    client_order_id=intent.client_order_id,
                    confirm=True,
                )
            except Exception as exc:
                logger.exception("trend working-entry cancel unconfirmed")
                events.append(
                    TrendEvent(
                        signal.symbol,
                        "partial_entry_cancel_unconfirmed",
                        type(exc).__name__,
                    )
                )
                continue
            events.append(TrendEvent(signal.symbol, "partial_entry_cancel_sent"))

    @asynccontextmanager
    async def _bound(self, lease: Any) -> AsyncIterator[None]:
        self._lease = lease
        try:
            yield
        finally:
            self._lease = None

    async def _check_lease(self) -> None:
        if self._lease is not None:
            await self._lease.check()

    async def _release_orphaned_entries(
        self, now: dt.datetime, events: list[TrendEvent]
    ) -> None:
        """Release a reservation whose process died before ``reserve_intent``.

        Sound only under the tick lease: no other tick can be mid-entry, and no
        intent exists, so nothing could have been fenced or sent. The demo root
        (deterministic client order id) is released only from a pre-send state.
        """
        for signal in await self.state.list_active_signals():
            if signal.state != "entry_reserved":
                continue
            if await self.state.list_intents(signal.signal_key):
                continue  # reserved intents were handled above; sent ones block
            try:
                await self.state.block_unsent(signal.signal_key, now=now)
                await self._release_demo_unsent(
                    self._entry_cid(signal.signal_key), now=now
                )
                events.append(TrendEvent(signal.symbol, "orphan_reservation_released"))
            except Exception as exc:
                logger.exception("trend orphaned reservation release failed")
                events.append(TrendEvent(signal.symbol, "blocked", type(exc).__name__))

    # ------------------------------------------------------------------ send

    async def _release_unsent(
        self, intent: TrendIntentSnapshot, signal: TrendSignalSnapshot, *, now
    ) -> None:
        await self._release_demo_unsent(intent.client_order_id, now=now)
        if intent.reduce_only:
            await self.state.release_unsent_exit(intent.client_order_id, now=now)
        else:
            await self.state.block_unsent(signal.signal_key, now=now)

    def _echo_matches(
        self, result: object, *, intent: TrendIntentSnapshot, symbol: str
    ) -> bool:
        return (
            isinstance(result, FuturesDemoOrderSubmitResult)
            and result.client_order_id == intent.client_order_id
            and result.symbol == symbol
            and result.side == intent.side
            and result.reduce_only is intent.reduce_only
            and result.position_side == "BOTH"
            and result.qty == intent.qty
            and bool(result.broker_order_id)
            and result.status in _KNOWN_STATUSES
        )

    async def _send(
        self,
        intent: TrendIntentSnapshot,
        signal: TrendSignalSnapshot,
        *,
        now: dt.datetime,
    ) -> str:
        """Fence, send exactly once, resolve from evidence. Never resends."""
        # Ownership is re-verified right before the point of no return.
        await self._check_lease()
        try:
            await self.state.mark_sending(intent.client_order_id, now=now)
        except TrendStateBlocked:
            # Refused at the fence: nothing was sent.
            await self._release_unsent(intent, signal, now=now)
            return "refused_at_fence"
        # A committed ``sending`` row exists: from here every doubt is uncertain.
        try:
            result = await self.client.submit_order(
                symbol=signal.symbol,
                side=intent.side,
                order_type="MARKET",
                qty=venue_order_quantity(intent.qty),
                client_order_id=intent.client_order_id,
                reduce_only=intent.reduce_only,
                position_side="BOTH",
                confirm=True,
            )
        except Exception as exc:
            rejection = affirmative_rejection(exc)
            if rejection is not None:
                return await self._handle_rejection(
                    intent, signal, rejection=rejection, now=now
                )
            logger.exception("trend submit outcome unknown")
            await self._keep_uncertain(intent.client_order_id, now=now)
            return "uncertain"
        if not self._echo_matches(result, intent=intent, symbol=signal.symbol):
            logger.error("trend submit response did not echo the order")
            await self._keep_uncertain(intent.client_order_id, now=now)
            return "uncertain"
        try:
            await self.state.acknowledge(
                intent.client_order_id,
                broker_order_id=result.broker_order_id,
                broker_status=result.status,
                now=now,
            )
        except Exception:
            logger.exception("trend acknowledgement not recorded")
            await self._keep_uncertain(intent.client_order_id, now=now)
            return "uncertain"
        # The submit response (even FILLED) is not fill authority: read the
        # order by client id (bounded poll) and the complete positions.
        fresh = await self.state.get_intent(intent.client_order_id)
        return await self._reconcile_intent(fresh or intent, now=now, poll=True)

    async def _handle_rejection(
        self,
        intent: TrendIntentSnapshot,
        signal: TrendSignalSnapshot,
        *,
        rejection: str,
        now: dt.datetime,
    ) -> str:
        try:
            await self._release_demo_unsent(intent.client_order_id, now=now)
        except Exception:
            logger.exception("trend demo ledger release after rejection failed")
        conflicts: tuple[str, ...] = ()
        if not intent.reduce_only:
            try:
                positions, orders = await self._read_account()
                conflicts = entry_conflicts(
                    symbol=signal.symbol, positions=positions, open_orders=orders
                )
            except Exception:
                logger.exception("trend account read after rejection failed")
        if conflicts:
            await self.state.record_conflict_rejected(
                intent.client_order_id,
                broker_status=rejection,
                now=now,
                detail=",".join(conflicts)[:200],
            )
            return "conflict_rejected"
        await self.state.record_rejected(
            intent.client_order_id, broker_status=rejection, now=now
        )
        return "rejected"

    # ----------------------------------------------------------------- entry

    async def _enter(
        self,
        record: TrendSignalSnapshot,
        *,
        sigma_hat: Decimal,
        now: dt.datetime,
    ) -> str:
        if self._manage_only:
            # Defense in depth: the tick returns before this loop, but no
            # caller may reach a send while a manage-only tick owns the flag.
            raise TrendStateBlocked("manage-only tick cannot open an entry")
        symbol = record.symbol
        cid = self._entry_cid(record.signal_key)
        try:
            self._check_origin()
            await self.client.assert_symbol_isolated_1x(symbol)
            if (await self.client.get_position_mode()).is_hedge_mode:
                raise TrendStateBlocked("trend requires one-way position mode")
            quote = await self.client.get_book_quote(symbol)
            filters = await self.client.get_trend_filters(symbol)
            price = quote.executable_price(record.side)
            size = size_entry(
                sigma_hat=sigma_hat,
                executable_price=price,
                step_size=filters.step_size,
                min_notional_usdt=filters.min_notional_usdt,
                min_qty=filters.min_qty,
                max_qty=filters.max_qty,
                quantity_precision=filters.quantity_precision,
            )
        except (TrendSizingBlocked, TrendStateBlocked) as exc:
            await self.state.block_unsent(record.signal_key, now=now)
            return f"entry_refused:{type(exc).__name__}"
        except Exception as exc:
            logger.exception("trend entry preparation failed")
            await self.state.block_unsent(record.signal_key, now=now)
            return f"entry_aborted:{type(exc).__name__}"
        try:
            reserved = await self.state.reserve_entry(
                signal_key=record.signal_key,
                notional_usdt=size.reserved_notional_usdt,
                now=now,
            )
        except TrendEntryRefused as exc:
            await self.state.block_unsent(record.signal_key, now=now)
            return "entry_refused:" + ",".join(exc.reasons)
        except TrendStateBlocked as exc:
            await self.state.block_unsent(record.signal_key, now=now)
            return f"entry_refused:{exc}"
        intent: TrendIntentSnapshot | None = None
        ledger_prepared = False
        try:
            # Gate #1: complete account truth right after the reservation.
            positions, orders = await self._read_account()
            conflicts = entry_conflicts(
                symbol=symbol, positions=positions, open_orders=orders
            )
            if conflicts:
                await self.state.record_conflict_blocked(
                    record.signal_key, now=now, detail=",".join(conflicts)
                )
                return "conflict_blocked"
            await self._prepare_demo_entry(
                signal=reserved,
                qty=size.qty,
                notional_usdt=size.reserved_notional_usdt,
                now=now,
            )
            ledger_prepared = True
            intent = await self.state.reserve_intent(
                signal_key=record.signal_key,
                leg_key="entry",
                side=record.side,
                qty=size.qty,
                reference_price=price,
                reduce_only=False,
                now=now,
            )
            # Gate #2: complete account truth right before the send fence, and
            # a fresh executable price that must still fit the reservation.
            positions, orders = await self._read_account()
            conflicts = entry_conflicts(
                symbol=symbol, positions=positions, open_orders=orders
            )
            if conflicts:
                await self._release_demo_unsent(cid, now=now)
                await self.state.record_conflict_blocked(
                    record.signal_key, now=now, detail=",".join(conflicts)
                )
                return "conflict_blocked"
            await self.client.assert_symbol_isolated_1x(symbol)
            fresh = await self.client.get_book_quote(symbol)
            if size.qty * fresh.executable_price(record.side) > (
                size.reserved_notional_usdt
            ):
                await self._release_demo_unsent(cid, now=now)
                await self.state.block_unsent(record.signal_key, now=now)
                return "entry_refused:price_moved_beyond_reservation"
        except Exception as exc:
            logger.exception("trend entry aborted before the send fence")
            await self._abort_entry(
                record, cid, intent=intent, ledger_prepared=ledger_prepared, now=now
            )
            return f"entry_aborted:{type(exc).__name__}"
        return await self._send(intent, reserved, now=now)

    async def _abort_entry(
        self,
        record: TrendSignalSnapshot,
        cid: str,
        *,
        intent: TrendIntentSnapshot | None,
        ledger_prepared: bool,
        now: dt.datetime,
    ) -> None:
        """Release a never-fenced entry; a failure here leaves it for restart."""
        try:
            if ledger_prepared:
                await self._release_demo_unsent(cid, now=now)
            await self.state.block_unsent(record.signal_key, now=now)
        except Exception:
            logger.exception("trend unsent entry release failed; restart releases it")

    # ------------------------------------------------------------------ exit

    async def _observe_hard_stop(
        self,
        symbol: str,
        entered_at: dt.datetime | None,
        bars: tuple[Bar4h, ...],
        *,
        decision_ts: int,
        quote_price: Decimal | None,
        now: dt.datetime,
    ) -> HardStopObservation:
        entered_ms = now_ms(entered_at) if entered_at else 0
        bar_close_value: Decimal | None = None
        for bar in bars:
            if bar.close_ts == decision_ts and decision_ts > entered_ms:
                bar_close_value = bar_close(bar)
        try:
            minutes = await collect_hard_stop_minutes(
                self.client.kline_transport(),
                symbol,
                entered_ms=entered_ms,
                now_ms=now_ms(now),
                check=self._check_origin,
            )
        except Exception:
            # Fall back to the 4h decision only: a partial 1m picture is worse
            # than none, and the quote is not consulted on this tick.
            logger.exception("trend 1m hard-stop watch unavailable; 4h decision only")
            return HardStopObservation(
                minutes_ok=False,
                minute_low=None,
                minute_high=None,
                bar_close=bar_close_value,
                quote=None,
            )
        return HardStopObservation(
            minutes_ok=True,
            minute_low=min((minute_low(row) for row in minutes), default=None),
            minute_high=max((minute_high(row) for row in minutes), default=None),
            bar_close=bar_close_value,
            quote=quote_price,
        )

    async def _manage_holding(
        self,
        signal: TrendSignalSnapshot,
        bars: tuple[Bar4h, ...],
        *,
        decision_ts: int,
        now: dt.datetime,
    ) -> TrendEvent | None:
        if signal.entered_at is None or signal.entry_price is None:
            raise TrendStateBlocked("held signal missing fill evidence")
        if signal.remaining_qty <= 0:
            # Flat interval of a still-working entry: nothing to close until a
            # further fill arrives (the entry reconciliation records it).
            return None
        symbol = signal.symbol
        positions, orders = await self._read_account()
        try:
            remaining = exit_quantity(
                symbol=symbol,
                side=signal.side,
                expected_remaining=signal.remaining_qty,
                positions=positions,
                open_orders=orders,
            )
        except TrendExposureMismatch as exc:
            return TrendEvent(symbol, "exposure_mismatch", str(exc))
        filters = await self.client.get_trend_filters(symbol)
        close_side = "SELL" if signal.side == "BUY" else "BUY"
        quote_price: Decimal | None
        try:
            quote_price = (await self.client.get_book_quote(symbol)).executable_price(
                close_side
            )
        except Exception:
            # An outage of the quote is not a reason to hold a stopped-out,
            # flipped or overdue position: the other evidence still decides.
            logger.exception("trend book quote unavailable; deciding without it")
            quote_price = None
        observed = await self._observe_hard_stop(
            symbol,
            signal.entered_at,
            bars,
            decision_ts=decision_ts,
            quote_price=quote_price,
            now=now,
        )
        if signal.exit_reason == EXIT_HARD_STOP:
            # A recorded stop is a latch: the decision was already taken (and
            # counted by the daily kill switch), so a price recovery after it
            # does not keep the position. The quantity is the broker remainder.
            latched = floor_to_step(remaining, filters.step_size)
            decision = (
                ExitDecision(reason=EXIT_HARD_STOP, qty=latched)
                if latched > 0
                else None
            )
        else:
            decision = choose_exit(
                HeldPosition(
                    side=signal.side,  # type: ignore[arg-type]
                    entry_price=signal.entry_price,
                    entered_at=signal.entered_at,
                    broker_remaining_qty=remaining,
                    step_size=filters.step_size,
                ),
                stop_price=self.strategy.hard_stop_price(
                    side=signal.side,  # type: ignore[arg-type]
                    entry_price=signal.entry_price,
                ),
                observed=observed,
                signal_side=momentum_sign(
                    bars,
                    decision_ts=decision_ts,
                    lookback_days=self.strategy.lookback_days,
                ),
                now=now,
            )
        if decision is None:
            return None
        if decision.reason == EXIT_HARD_STOP:
            # First write wins: a stop proven while the entry was working keeps
            # its own day here.
            await self.state.record_exit_decision(
                signal.signal_key, reason=decision.reason, now=now
            )
        return TrendEvent(
            symbol,
            await self._exit(signal, reason=decision.reason, qty=decision.qty, now=now),
            decision.reason,
        )

    async def _exit(
        self,
        signal: TrendSignalSnapshot,
        *,
        reason: str,
        qty: Decimal,
        now: dt.datetime,
    ) -> str:
        symbol = signal.symbol
        intent = await self.state.reserve_exit_intent(
            signal_key=signal.signal_key, reason=reason, qty=qty, now=now
        )
        try:
            await self._prepare_demo_exit(
                signal=signal, intent=intent, reason=reason, now=now
            )
            # Fresh complete account truth right before the fence: the bound
            # must still hold, otherwise nothing is sent.
            positions, orders = await self._read_account()
            bound = exit_quantity(
                symbol=symbol,
                side=signal.side,
                expected_remaining=signal.remaining_qty,
                positions=positions,
                open_orders=orders,
            )
            if floor_to_step(
                bound, (await self.client.get_trend_filters(symbol)).step_size
            ) < (intent.qty):
                raise TrendExposureMismatch("broker remainder fell below the exit")
        except Exception as exc:
            logger.exception("trend exit aborted before the send fence")
            try:
                await self._release_unsent(intent, signal, now=now)
            except Exception:
                logger.exception("trend unsent exit release failed; restart releases")
            return f"exit_aborted:{type(exc).__name__}"
        outcome = await self._send(intent, signal, now=now)
        return f"exit_{outcome}"

    # ------------------------------------------------------------------ tick

    async def run_tick(
        self,
        *,
        now: dt.datetime,
        confirm: bool = False,
        manage_only: bool = False,
    ) -> TrendTickResult:
        self._guard(confirm=confirm)
        if now.tzinfo is None:
            raise TrendStateBlocked("aware clock required")
        decision_ts = (now_ms(now) // FOUR_HOUR_MS) * FOUR_HOUR_MS
        events: list[TrendEvent] = []
        self._manage_only = manage_only
        # One tick owns the lane at a time: recovery below treats an unfenced
        # reservation as abandoned, which is only true while no other tick lives.
        async with self.state.tick_lease() as lease, self._bound(lease):
            blocked = await self._reconcile_unresolved(now, events)
            histories: dict[str, tuple[Bar4h, ...]] = {}
            for symbol in LOCKED_LIMITS.universe:
                try:
                    histories[symbol] = await collect_signal_bars(
                        self.client.kline_transport(),
                        symbol,
                        decision_ts=decision_ts,
                        lookback_days=self.strategy.lookback_days,
                        check=self._check_origin,
                    )
                except Exception as exc:
                    logger.exception("trend signal bars unavailable")
                    histories[symbol] = ()
                    events.append(
                        TrendEvent(symbol, "bars_unavailable", type(exc).__name__)
                    )
            await self._manage_working_entries(
                histories, decision_ts=decision_ts, now=now, events=events
            )
            # Manage what is held first; exits never wait for entry capacity.
            for signal in await self.state.list_active_signals():
                if signal.state != "holding" or signal.symbol in blocked:
                    continue
                try:
                    managed = await self._manage_holding(
                        signal,
                        histories.get(signal.symbol, ()),
                        decision_ts=decision_ts,
                        now=now,
                    )
                except Exception as exc:
                    logger.exception("trend holding management failed")
                    events.append(
                        TrendEvent(signal.symbol, "blocked", type(exc).__name__)
                    )
                    continue
                if managed is not None:
                    events.append(managed)
                    if managed.event.endswith("uncertain"):
                        blocked.add(signal.symbol)
            # Forecast repair lives in the manage section of every tick (and
            # therefore in manage-only ticks): it only retries durable
            # forecast writes for proven fills — it never sends, blocks or
            # changes a trading decision. One batched lookup covers every
            # candidate so per-tick DB work does not grow with history.
            forecast_candidates = await self.state.list_forecast_recovery_signals()
            if forecast_candidates:
                _, forecast_failures = await repair_forecasts(
                    forecast_candidates, now=now
                )
                for forecast_signal, exc in forecast_failures:
                    logger.exception("trend forecast recovery failed")
                    events.append(
                        TrendEvent(
                            forecast_signal.symbol,
                            "forecast_recovery_failed",
                            type(exc).__name__,
                        )
                    )
            if manage_only:
                # Existing exposure and unresolved intents were handled above
                # (including forecast repair); nothing below may observe a
                # signal or open an entry.
                events.append(
                    TrendEvent(
                        "*",
                        "entries_skipped_manage_only",
                        "manage-only mode: no signal evaluation or entry",
                    )
                )
                return TrendTickResult(decision_ts=decision_ts, events=tuple(events))
            occupied = {
                signal.symbol for signal in await self.state.list_active_signals()
            }
            for found in evaluate_all(
                self.strategy, histories, decision_ts=decision_ts
            ):
                if found.symbol in blocked or found.symbol in occupied:
                    continue
                bars = histories[found.symbol]
                decision_bar = next(bar for bar in bars if bar.close_ts == decision_ts)
                record = await self.state.observe_signal(
                    symbol=found.symbol,
                    side=found.side,
                    decision_ts=decision_ts,
                    price_text=bar_close_text(decision_bar),
                )
                if record.state != "observed":
                    continue
                sigma_hat = realized_vol_annual(bars, decision_ts=decision_ts)
                if sigma_hat is None:
                    continue
                outcome = await self._enter(record, sigma_hat=sigma_hat, now=now)
                # ``_enter`` already prefixes its refusal/abort results with
                # ``entry_``; prefix only the bare outcomes so the emitted
                # vocabulary is ``entry_refused:...``, not ``entry_entry_...``.
                event = outcome if outcome.startswith("entry_") else f"entry_{outcome}"
                events.append(TrendEvent(found.symbol, event))
                if outcome == "uncertain":
                    break
        return TrendTickResult(decision_ts=decision_ts, events=tuple(events))


__all__ = [
    "TrendBar4h",
    "TrendBrokerTruthUnavailable",
    "TrendDemoClient",
    "TrendEvent",
    "TrendExecutor",
    "TrendFilters",
    "TrendQuote",
    "TrendTickResult",
    "affirmative_rejection",
]
