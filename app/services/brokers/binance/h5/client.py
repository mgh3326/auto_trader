"""H5-only adapter around the existing signed USD-M Futures Demo transport.

It adds read-only NAV, margin configuration, executable quote and filter reads.
All H5 calls revalidate the exact demo origin before signing or dispatching.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

from app.services.brokers.binance.futures_demo.dto import (
    FuturesDemoOpenOrder,
    FuturesDemoOpenOrdersResult,
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

from .strategy import DEMO_URL, UNIVERSE, assert_h5_demo_url

_ASSET_NAME = re.compile(r"[A-Z0-9]{1,20}")


class H5BrokerTruthUnavailable(RuntimeError):
    """Missing, malformed or contradictory broker evidence blocks H5 action."""


@dataclass(frozen=True)
class H5Account:
    nav_usdt: Decimal
    per_symbol_isolated_1x: dict[str, bool]
    # #1272 (hk 1271 B): non-USDT assets holding a positive balance. They are
    # admitted only because the same /fapi/v2/account response proved
    # multiAssetsMargin is exactly False, so they cannot margin USDT-M H5.
    non_usdt_assets: tuple[str, ...] = ()
    multi_assets_margin: bool | None = None


@dataclass(frozen=True)
class H5Quote:
    symbol: str
    bid: Decimal
    ask: Decimal

    def executable_price(self, side: str) -> Decimal:
        if side == "BUY":
            return self.ask
        if side == "SELL":
            return self.bid
        raise ValueError("invalid H5 side")


@dataclass(frozen=True)
class H5Filters:
    step_size: Decimal
    min_notional_usdt: Decimal
    min_qty: Decimal
    max_qty: Decimal
    quantity_precision: int


def _positive_decimal(raw: Any, field: str) -> Decimal:
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise H5BrokerTruthUnavailable(f"invalid {field}") from exc
    if not value.is_finite() or value <= 0:
        raise H5BrokerTruthUnavailable(f"invalid {field}")
    return value


class H5DemoClient(BinanceFuturesDemoExecutionClient):
    """H5 identity, existing demo credentials, existing signed transport."""

    def __init__(
        self, *, api_key: str, api_secret: str, base_url: str = DEMO_URL
    ) -> None:
        assert_h5_demo_url(base_url)
        super().__init__(api_key=api_key, api_secret=api_secret, base_url=base_url)

    @classmethod
    def from_env(cls) -> H5DemoClient:
        if os.environ.get("BINANCE_H5_DEMO_ENABLED") != "true":
            raise ValueError("BINANCE_H5_DEMO_ENABLED is disabled")
        assert_h5_demo_url(os.environ.get("BINANCE_FUTURES_DEMO_BASE_URL", DEMO_URL))
        return super().from_env()

    def _assert_h5(self) -> None:
        assert_h5_demo_url(self._base_url)
        assert_h5_demo_url(str(self._client.base_url).rstrip("/"))

    async def _signed_get(
        self, path: str, *, params: dict[str, str] | None = None
    ) -> Any:
        self._assert_h5()
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
            raise H5BrokerTruthUnavailable("broker JSON evidence unavailable") from exc

    async def read_account(self) -> H5Account:
        body = await self._signed_get("/fapi/v2/account")
        if not isinstance(body, dict) or body.get("canTrade") is not True:
            raise H5BrokerTruthUnavailable("account trading status unavailable")
        if body.get("multiAssetsMargin") is not False:
            raise H5BrokerTruthUnavailable("single-asset margin evidence unavailable")
        nav = _positive_decimal(body.get("totalMarginBalance"), "USDT NAV")
        assets = body.get("assets")
        if not isinstance(assets, list) or not assets:
            raise H5BrokerTruthUnavailable("complete account assets unavailable")
        usdt_seen = False
        usdt_margin: Any = None
        non_usdt: set[str] = set()
        for asset in assets:
            if not isinstance(asset, dict):
                raise H5BrokerTruthUnavailable("malformed account asset")
            if asset.get("asset") == "USDT":
                if usdt_seen:
                    raise H5BrokerTruthUnavailable("duplicate USDT account asset")
                usdt_seen = True
                usdt_margin = asset.get("marginBalance")
            else:
                try:
                    balance = Decimal(str(asset.get("marginBalance")))
                except (InvalidOperation, TypeError, ValueError) as exc:
                    raise H5BrokerTruthUnavailable("non-USDT asset unreadable") from exc
                if not balance.is_finite() or balance < 0:
                    raise H5BrokerTruthUnavailable("foreign account asset exposure")
                if balance != 0:
                    # Admissible only under the single-asset proof above.
                    name = asset.get("asset")
                    if not isinstance(name, str) or not _ASSET_NAME.fullmatch(name):
                        raise H5BrokerTruthUnavailable("non-USDT asset unreadable")
                    non_usdt.add(name)
        if not usdt_seen:
            raise H5BrokerTruthUnavailable("USDT account asset unavailable")
        if non_usdt:
            # Single-asset mode reports totalMarginBalance for USDT only; with
            # foreign balances present, prove NAV did not absorb them.
            try:
                usdt_balance = Decimal(str(usdt_margin))
            except (InvalidOperation, TypeError, ValueError) as exc:
                raise H5BrokerTruthUnavailable(
                    "USDT margin balance unreadable"
                ) from exc
            if not usdt_balance.is_finite() or usdt_balance != nav:
                raise H5BrokerTruthUnavailable(
                    "USDT NAV not separable from non-USDT assets"
                )
        positions = body.get("positions")
        if not isinstance(positions, list):
            raise H5BrokerTruthUnavailable("complete margin configuration unavailable")
        configured: dict[str, bool] = {}
        for row in positions:
            if not isinstance(row, dict):
                raise H5BrokerTruthUnavailable("malformed account position")
            symbol = row.get("symbol")
            if symbol in UNIVERSE:
                if symbol in configured:
                    raise H5BrokerTruthUnavailable("duplicate account position row")
                configured[symbol] = (
                    row.get("isolated") is True
                    and str(row.get("leverage")) == "1"
                    and row.get("positionSide") == "BOTH"
                )
        return H5Account(
            nav_usdt=nav,
            per_symbol_isolated_1x=configured,
            non_usdt_assets=tuple(sorted(non_usdt)),
            multi_assets_margin=body.get("multiAssetsMargin"),
        )

    async def get_book_quote(self, symbol: str) -> H5Quote:
        self._assert_h5()
        if symbol not in UNIVERSE:
            raise ValueError("symbol outside H5 universe")
        response = await self._client.get(
            "/fapi/v1/ticker/bookTicker", params={"symbol": symbol}
        )
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise H5BrokerTruthUnavailable("broker response body unreadable") from exc
        if not isinstance(body, dict) or body.get("symbol") != symbol:
            raise H5BrokerTruthUnavailable("book quote symbol mismatch")
        bid = _positive_decimal(body.get("bidPrice"), "bid")
        ask = _positive_decimal(body.get("askPrice"), "ask")
        if bid > ask:
            raise H5BrokerTruthUnavailable("crossed book quote")
        return H5Quote(symbol=symbol, bid=bid, ask=ask)

    async def get_completed_minute_close_text(
        self, symbol: str, *, decision_ts: int
    ) -> str:
        """Preserve the exchange's decimal text for the completed 4h close."""
        self._assert_h5()
        if symbol not in UNIVERSE or decision_ts <= 0:
            raise ValueError("invalid H5 close identity")
        start = decision_ts - 60_000
        response = await self._client.get(
            "/fapi/v1/klines",
            params={
                "symbol": symbol,
                "interval": "1m",
                "startTime": start,
                "endTime": decision_ts - 1,
                "limit": 1,
            },
        )
        response.raise_for_status()
        try:
            rows = response.json()
        except ValueError as exc:
            raise H5BrokerTruthUnavailable("broker response body unreadable") from exc
        if (
            not isinstance(rows, list)
            or len(rows) != 1
            or not isinstance(rows[0], list)
            or len(rows[0]) < 7
            or int(rows[0][0]) != start
            or int(rows[0][6]) > decision_ts
            or not isinstance(rows[0][4], str)
        ):
            raise H5BrokerTruthUnavailable("completed close minute unavailable")
        raw = rows[0][4]
        _positive_decimal(raw, "completed close")
        return raw

    async def get_h5_filters(self, symbol: str) -> H5Filters:
        self._assert_h5()
        if symbol not in UNIVERSE:
            raise ValueError("symbol outside H5 universe")
        response = await self._client.get(
            "/fapi/v1/exchangeInfo", params={"symbol": symbol}
        )
        response.raise_for_status()
        try:
            body = response.json()
        except ValueError as exc:
            raise H5BrokerTruthUnavailable("broker response body unreadable") from exc
        rows = body.get("symbols") if isinstance(body, dict) else None
        if not isinstance(rows, list):
            raise H5BrokerTruthUnavailable("exchange filters unavailable")
        matches = [
            row for row in rows if isinstance(row, dict) and row.get("symbol") == symbol
        ]
        if len(matches) != 1 or matches[0].get("status") != "TRADING":
            raise H5BrokerTruthUnavailable("H5 symbol not trading")
        row = matches[0]
        by_type = {
            item.get("filterType"): item
            for item in row.get("filters", [])
            if isinstance(item, dict)
        }
        lot = by_type.get("MARKET_LOT_SIZE") or by_type.get("LOT_SIZE")
        if not isinstance(lot, dict):
            raise H5BrokerTruthUnavailable("market lot filter missing")
        if Decimal(str(lot.get("stepSize", "0"))) == 0:
            lot = by_type.get("LOT_SIZE")
        notional = by_type.get("MIN_NOTIONAL") or by_type.get("NOTIONAL")
        if not isinstance(lot, dict) or not isinstance(notional, dict):
            raise H5BrokerTruthUnavailable("H5 filter missing")
        minimum = notional.get("notional", notional.get("minNotional"))
        precision = row.get("quantityPrecision")
        if type(precision) is not int:
            raise H5BrokerTruthUnavailable("quantity precision unavailable")
        return H5Filters(
            step_size=_positive_decimal(lot.get("stepSize"), "stepSize"),
            min_notional_usdt=_positive_decimal(minimum, "MIN_NOTIONAL"),
            min_qty=_positive_decimal(lot.get("minQty"), "minQty"),
            max_qty=_positive_decimal(lot.get("maxQty"), "maxQty"),
            quantity_precision=precision,
        )

    async def assert_symbol_isolated_1x(self, symbol: str) -> None:
        """Require positive readback of BOTH, isolated margin and leverage 1."""
        if symbol not in UNIVERSE:
            raise ValueError("symbol outside H5 universe")
        body = await self._signed_get(
            "/fapi/v2/positionRisk", params={"symbol": symbol}
        )
        if not isinstance(body, list):
            raise H5BrokerTruthUnavailable("margin configuration unreadable")
        rows = [
            row for row in body if isinstance(row, dict) and row.get("symbol") == symbol
        ]
        if len(rows) != 1:
            raise H5BrokerTruthUnavailable("margin configuration not unique")
        row = rows[0]
        if (
            row.get("marginType") != "isolated"
            or str(row.get("leverage")) != "1"
            or row.get("positionSide") != "BOTH"
        ):
            raise H5BrokerTruthUnavailable("H5 requires preconfigured isolated 1x BOTH")

    async def get_all_positions(self) -> list[FuturesDemoPositionResult]:
        body = await self._signed_get("/fapi/v2/positionRisk")
        if not isinstance(body, list) or not body:
            raise H5BrokerTruthUnavailable("complete position array unavailable")
        positions = []
        seen = set()
        for row in body:
            if (
                not isinstance(row, dict)
                or not isinstance(row.get("symbol"), str)
                or not row["symbol"]
            ):
                raise H5BrokerTruthUnavailable("position symbol unavailable")
            if row["symbol"] in seen or row.get("positionSide") != "BOTH":
                raise H5BrokerTruthUnavailable("position scope is not complete one-way")
            seen.add(row["symbol"])
            try:
                qty = Decimal(row["positionAmt"])
                price = Decimal(row["entryPrice"])
                leverage = Decimal(str(row["leverage"]))
            except (KeyError, TypeError, InvalidOperation) as exc:
                raise H5BrokerTruthUnavailable("position fields unavailable") from exc
            if (
                not all(value.is_finite() for value in (qty, price, leverage))
                or leverage <= 0
                or leverage != leverage.to_integral_value()
                or price < 0
            ):
                raise H5BrokerTruthUnavailable("invalid position fields")
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
            raise H5BrokerTruthUnavailable("complete open-order array unavailable")
        orders = []
        for row in body:
            if (
                not isinstance(row, dict)
                or not row.get("symbol")
                or not row.get("clientOrderId")
                or not row.get("orderId")
                or type(row.get("reduceOnly")) is not bool
                or row.get("positionSide") != "BOTH"
            ):
                raise H5BrokerTruthUnavailable("incomplete open-order fields")
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

    async def get_order(self, *, symbol: str, client_order_id: str):  # type: ignore[override]
        self._assert_h5()
        if symbol not in UNIVERSE or not client_order_id.startswith("h5-"):
            raise ValueError("order outside H5 identity")
        try:
            result = await super().get_order(
                symbol=symbol, client_order_id=client_order_id
            )
        except ValueError as exc:
            raise H5BrokerTruthUnavailable(
                "order evidence response unreadable"
            ) from exc
        raw = result.raw_response_redacted
        if (
            raw.get("symbol") != symbol
            or raw.get("clientOrderId") != client_order_id
            or type(raw.get("reduceOnly")) is not bool
        ):
            raise H5BrokerTruthUnavailable("order identity requires actual broker echo")
        return result

    async def get_position_mode(self) -> FuturesDemoPositionModeResult:
        body = await self._signed_get("/fapi/v1/positionSide/dual")
        if not isinstance(body, dict) or type(body.get("dualSidePosition")) is not bool:
            raise H5BrokerTruthUnavailable("position mode evidence unavailable")
        return FuturesDemoPositionModeResult(is_hedge_mode=body["dualSidePosition"])

    async def get_position(self, *, symbol: str) -> FuturesDemoPositionResult:
        self._assert_h5()
        if symbol not in UNIVERSE:
            raise ValueError("symbol outside H5 universe")
        positions = [
            row for row in await self.get_all_positions() if row.symbol == symbol
        ]
        if len(positions) != 1:
            raise H5BrokerTruthUnavailable("H5 position row unavailable")
        return positions[0]

    async def get_open_orders(self, *, symbol: str) -> FuturesDemoOpenOrdersResult:
        self._assert_h5()
        if symbol not in UNIVERSE:
            raise ValueError("symbol outside H5 universe")
        orders = await self.get_all_open_orders()
        return FuturesDemoOpenOrdersResult(
            orders=[row for row in orders.orders if row.symbol == symbol]
        )

    async def set_leverage(self, **kwargs):  # type: ignore[override]
        raise ValueError(
            "H5 requires preconfigured isolated 1x; configuration mutation is disabled"
        )

    async def cancel_order(self, **kwargs):  # type: ignore[override]
        raise ValueError(
            "H5 resolves intents by evidence; automatic cancellation is disabled"
        )

    async def order_test(self, **kwargs):  # type: ignore[override]
        raise ValueError("H5 adapter does not expose the old smoke order-test path")

    async def submit_order(self, **kwargs):  # type: ignore[override]
        self._assert_h5()
        if kwargs.get("symbol") not in UNIVERSE:
            raise ValueError("symbol outside H5 universe")
        if not isinstance(kwargs.get("client_order_id"), str) or not kwargs[
            "client_order_id"
        ].startswith("h5-"):
            raise ValueError("order outside H5 identity")
        if os.environ.get("BINANCE_H5_DEMO_ENABLED") != "true":
            raise ValueError("BINANCE_H5_DEMO_ENABLED is disabled")
        if kwargs.get("confirm") is not True:
            raise ValueError("H5 requires per-call confirm=True")
        return await super().submit_order(**kwargs)


def assert_h5_client(client: H5DemoClient) -> None:
    if not isinstance(client, H5DemoClient):
        raise ValueError("H5 requires its Futures Demo client identity")
    client._assert_h5()
