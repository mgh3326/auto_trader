"""Demo/paper-only adapters. Broker evidence, never account deltas, owns fills."""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Protocol

from app.services.brokers.alpaca.schemas import OrderRequest
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService
from app.services.brokers.binance.futures_demo.execution_client import (
    BinanceFuturesDemoExecutionClient,
)
from app.services.brokers.binance.spot_demo.execution_client import (
    BinanceSpotDemoExecutionClient,
)
from app.services.mock_strategy.crypto_orders import (
    MarketRules,
    binance_rules,
    positive,
)

VENUES = ("binance_spot_demo", "binance_futures_demo", "alpaca_paper_crypto")


class PreDispatchRefusal(Exception):
    """Raised only by the checks that precede the broker POST call."""

    def __init__(self, reason_class: str):
        self.reason_class = reason_class
        super().__init__("crypto_pre_dispatch_refused")


@dataclass(frozen=True)
class FillEvidence:
    broker_order_id: str
    status: str
    filled_qty: Decimal
    price: Decimal | None
    terminal: bool
    held_qty: Decimal | None = None


class CryptoBroker(Protocol):
    account_ref: str

    async def rules(self, symbol: str) -> MarketRules: ...
    async def price(self, symbol: str) -> Decimal: ...
    async def preflight(self, order: Any, *, confirm: bool) -> None: ...
    async def submit(self, order: Any, *, confirm: bool) -> FillEvidence: ...
    async def evidence(self, order: Any) -> FillEvidence: ...
    async def closeable(self, order: Any, qty: Decimal) -> None: ...


def require_enabled(venue: str, *, confirm: bool) -> None:
    if venue not in VENUES:
        raise ValueError("unsupported crypto paper venue")
    if (
        os.environ.get("MOCK_STRATEGY_ENABLED", "false").lower() != "true"
        or confirm is not True
    ):
        raise ValueError("MOCK_STRATEGY_ENABLED=true and confirm=True required")
    key = {
        "binance_spot_demo": "BINANCE_SPOT_DEMO_ENABLED",
        "binance_futures_demo": "BINANCE_FUTURES_DEMO_ENABLED",
        "alpaca_paper_crypto": "ALPACA_PAPER_CRYPTO_ENABLED",
    }[venue]
    if os.environ.get(key, "false").lower() != "true":
        raise ValueError(f"{key}=true required")


def _binance_evidence(body: dict, order: Any, *, futures: bool) -> FillEvidence:
    side = "BUY" if order.side in {"buy", "cover"} else "SELL"
    expected = {
        "symbol": order.symbol,
        "side": side,
        "type": "MARKET",
        "clientOrderId": order.client_order_id,
    }
    if any(body.get(k) != v for k, v in expected.items()):
        raise ValueError("broker order identity mismatch")
    if positive(body["origQty"]) != order.qty or not str(body.get("orderId", "")):
        raise ValueError("broker order size/id mismatch")
    filled = Decimal(str(body["executedQty"]))
    if not filled.is_finite() or filled < 0 or filled > order.qty:
        raise ValueError("invalid broker fill quantity")
    if futures and (
        body.get("positionSide") != "BOTH"
        or body.get("reduceOnly") is not (order.side == "cover")
    ):
        raise ValueError("broker futures mode mismatch")
    status = body["status"]
    if status == "FILLED" and filled != order.qty:
        raise ValueError("incomplete filled response")
    price = None
    if filled:
        price = (
            positive(body["avgPrice"])
            if futures
            else positive(body["cummulativeQuoteQty"]) / filled
        )
    return FillEvidence(
        str(body["orderId"]),
        status,
        filled,
        price,
        status in {"FILLED", "CANCELED", "EXPIRED", "EXPIRED_IN_MATCH", "REJECTED"},
    )


class BinanceCryptoDemo:
    def __init__(self, client: Any, *, futures: bool, expected_fingerprint: str):
        if (
            not expected_fingerprint
            or client.credential_fingerprint != expected_fingerprint
        ):
            raise ValueError("demo credential identity mismatch")
        self.client = client
        self.futures = futures
        self.venue = "binance_futures_demo" if futures else "binance_spot_demo"
        self.account_ref = client.credential_fingerprint

    async def rules(self, symbol: str) -> MarketRules:
        if self.futures:
            await self._position(symbol)
        return binance_rules(await self.client.get_symbol_info(symbol=symbol))

    async def price(self, symbol: str) -> Decimal:
        return positive(await self.client.get_ticker_price(symbol=symbol))

    async def _position(self, symbol: str) -> Any:
        mode = await self.client.get_position_mode()
        position = await self.client.get_position(symbol=symbol)
        if (
            mode.is_hedge_mode
            or position.symbol != symbol
            or position.require_position_side() != "BOTH"
            or position.leverage != 1
            or position.margin_type != "isolated"
            or position.position_amt > 0
        ):
            raise ValueError("futures require isolated 1x one-way, no opposing long")
        return position

    async def closeable(self, order: Any, qty: Decimal) -> None:
        if self.futures:
            position = await self._position(order.symbol)
            if -position.position_amt < qty:
                raise ValueError("demo short quantity unavailable")
        else:
            balance = await self.client.get_asset_balance(
                asset=order.symbol.removesuffix("USDT")
            )
            if balance.free < qty:
                raise ValueError("demo free quantity unavailable")

    async def preflight(self, order: Any, *, confirm: bool) -> None:
        try:
            require_enabled(self.venue, confirm=confirm)
            rules, price = (
                await self.rules(order.symbol),
                await self.price(order.symbol),
            )
            if rules.floor(order.qty, price) != order.qty:
                raise ValueError("demo order no longer matches venue step")
            if order.side in {"sell", "cover"}:
                await self.closeable(order, order.qty)
        except Exception as exc:
            raise PreDispatchRefusal(type(exc).__name__) from None

    async def submit(self, order: Any, *, confirm: bool) -> FillEvidence:
        # Recheck immediately before dispatch, with a typed no-POST outcome.
        await self.preflight(order, confirm=confirm)
        kwargs: dict[str, Any] = {}
        if self.futures:
            kwargs = {"reduce_only": order.side == "cover", "position_side": "BOTH"}
        result = await self.client.submit_order(
            symbol=order.symbol,
            side="BUY" if order.side in {"buy", "cover"} else "SELL",
            order_type="MARKET",
            qty=order.qty,
            client_order_id=order.client_order_id,
            confirm=confirm,
            **kwargs,
        )
        return await self._evidence_with_commissions(
            result.raw_response_redacted, order
        )

    async def _evidence_with_commissions(self, body: dict, order: Any) -> FillEvidence:
        evidence = _binance_evidence(body, order, futures=self.futures)
        if self.futures or order.side != "buy" or not evidence.filled_qty:
            return evidence
        trades = await self.client.get_order_trades(
            symbol=order.symbol, order_id=evidence.broker_order_id
        )
        total, base_fee = Decimal("0"), Decimal("0")
        for trade in trades:
            if (
                trade.get("symbol") != order.symbol
                or str(trade.get("orderId")) != evidence.broker_order_id
                or trade.get("isBuyer") is not True
            ):
                raise ValueError("demo trade attribution mismatch")
            total += positive(trade["qty"])
            fee = Decimal(str(trade["commission"]))
            if not fee.is_finite() or fee < 0:
                raise ValueError("invalid demo commission")
            if trade["commissionAsset"] == order.symbol.removesuffix("USDT"):
                base_fee += fee
        if total != evidence.filled_qty or not 0 <= base_fee < total:
            raise ValueError("demo trade coverage incomplete")
        return FillEvidence(
            evidence.broker_order_id,
            evidence.status,
            total,
            evidence.price,
            evidence.terminal,
            total - base_fee,
        )

    async def evidence(self, order: Any) -> FillEvidence:
        if self.futures:
            result = await self.client.get_order(
                symbol=order.symbol, client_order_id=order.client_order_id
            )
            body = result.raw_response_redacted
        else:
            body = await self.client.get_order_status(
                symbol=order.symbol, client_order_id=order.client_order_id
            )
        return await self._evidence_with_commissions(body, order)

    async def aclose(self) -> None:
        await self.client.aclose()


class AlpacaCryptoPaper:
    def __init__(
        self,
        client: AlpacaPaperBrokerService,
        pricing: BinanceCryptoDemo,
        *,
        account_ref: str,
    ):
        self.client, self.pricing, self.account_ref = client, pricing, account_ref

    @staticmethod
    def execution_symbol(symbol: str) -> str:
        if not symbol.endswith("USDT"):
            raise ValueError("expected Binance USDT symbol")
        return symbol.removesuffix("USDT") + "/USD"

    async def rules(self, symbol: str) -> MarketRules:
        execution = self.execution_symbol(symbol)
        assets = await self.client.list_assets(status="active", asset_class="crypto")
        asset = next(
            (
                a
                for a in assets
                if a.symbol == execution and a.tradable and a.asset_class == "crypto"
            ),
            None,
        )
        if asset is None:
            raise ValueError("paper crypto symbol unavailable")
        return MarketRules(
            positive(asset.min_trade_increment),
            positive(asset.min_order_size),
            Decimal("0"),
        )

    async def price(self, symbol: str) -> Decimal:
        return await self.pricing.price(symbol)

    async def closeable(self, order: Any, qty: Decimal) -> None:
        position = await self.client.get_position(
            order.symbol.removesuffix("USDT") + "USD"
        )
        if (
            position is None
            or position.qty_available is None
            or position.qty_available < qty
        ):
            raise ValueError("paper crypto quantity unavailable")

    def _evidence(self, response: Any, order: Any) -> FillEvidence:
        execution = self.execution_symbol(order.symbol)
        if (
            response.symbol
            not in {execution, order.symbol.removesuffix("USDT") + "USD"}
            or response.side != order.side
            or response.client_order_id != order.client_order_id
            or response.qty != order.qty
            or response.type != "market"
            or response.time_in_force != "gtc"
            or not response.id
        ):
            raise ValueError("paper broker order identity mismatch")
        filled = response.filled_qty
        if filled is None or not filled.is_finite() or not 0 <= filled <= order.qty:
            raise ValueError("paper fill quantity unavailable")
        if response.status == "filled" and filled != order.qty:
            raise ValueError("incomplete paper filled response")
        return FillEvidence(
            response.id,
            response.status.upper(),
            filled,
            positive(response.filled_avg_price) if filled else None,
            response.status in {"filled", "canceled", "expired", "rejected"},
        )

    async def preflight(self, order: Any, *, confirm: bool) -> None:
        try:
            require_enabled("alpaca_paper_crypto", confirm=confirm)
            rules, price = (
                await self.rules(order.symbol),
                await self.price(order.symbol),
            )
            if rules.floor(order.qty, price) != order.qty:
                raise ValueError("paper order no longer matches venue step")
            if order.side == "sell":
                await self.closeable(order, order.qty)
        except Exception as exc:
            raise PreDispatchRefusal(type(exc).__name__) from None

    async def submit(self, order: Any, *, confirm: bool) -> FillEvidence:
        await self.preflight(order, confirm=confirm)
        response = await self.client.submit_order(
            OrderRequest(
                symbol=self.execution_symbol(order.symbol),
                side=order.side,
                qty=order.qty,
                type="market",
                time_in_force="gtc",
                client_order_id=order.client_order_id,
            )
        )
        return self._evidence(response, order)

    async def evidence(self, order: Any) -> FillEvidence:
        response = await self.client.get_order_by_client_order_id(order.client_order_id)
        if response is None:
            raise ValueError("paper order evidence unavailable; no resend")
        return self._evidence(response, order)

    async def aclose(self) -> None:
        await self.pricing.aclose()


async def build_broker(
    venue: str, *, confirm: bool
) -> BinanceCryptoDemo | AlpacaCryptoPaper:
    require_enabled(venue, confirm=confirm)
    futures = venue == "binance_futures_demo"
    prefix = "BINANCE_FUTURES_DEMO" if futures else "BINANCE_SPOT_DEMO"
    exact_url = (
        "https://demo-fapi.binance.com" if futures else "https://demo-api.binance.com"
    )
    if os.environ.get(f"{prefix}_BASE_URL", exact_url).rstrip("/") != exact_url:
        raise ValueError("exact demo HTTPS base URL required")
    expected = os.environ.get(f"{prefix}_EXPECTED_FINGERPRINT", "")
    if not expected:
        raise ValueError(f"{prefix}_EXPECTED_FINGERPRINT required")
    client = (
        BinanceFuturesDemoExecutionClient.from_env()
        if futures
        else BinanceSpotDemoExecutionClient.from_env()
    )
    try:
        demo = BinanceCryptoDemo(client, futures=futures, expected_fingerprint=expected)
        if venue != "alpaca_paper_crypto":
            return demo
        from app.services.brokers.alpaca.config import AlpacaPaperSettings

        settings = AlpacaPaperSettings.from_app_settings(profile="clean")
        alpaca = AlpacaPaperBrokerService(settings=settings, profile="clean")
        account_ref = f"alpaca-paper:{settings.expected_account_id_suffix}:{settings.expected_account_number_suffix}"
        return AlpacaCryptoPaper(alpaca, demo, account_ref=account_ref)
    except BaseException:
        await client.aclose()
        raise
