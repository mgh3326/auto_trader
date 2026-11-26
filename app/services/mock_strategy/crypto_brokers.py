"""Demo/paper-only adapters. Broker evidence, never account deltas, owns fills."""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol

from app.services.alpaca_paper_crypto_fee_evidence import (
    activity_posting_date,
    count_filled_legs_for_day,
    resolve_crypto_fee_evidence,
)
from app.services.brokers.alpaca.schemas import OrderRequest
from app.services.brokers.alpaca.service import AlpacaPaperBrokerService
from app.services.brokers.binance.demo.quantity import venue_order_quantity
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

MY_TRADES_LIMIT = 1000  # Binance myTrades max; a full page may be truncated
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
    fee_amount: Decimal | None = None
    fee_currency: str | None = None
    fee_evidence_reason: str | None = None


class CryptoBroker(Protocol):
    account_ref: str

    async def rules(self, symbol: str) -> MarketRules: ...
    async def price(self, symbol: str) -> Decimal: ...
    async def preflight(self, order: Any, *, confirm: bool) -> None: ...
    async def submit(self, order: Any, *, confirm: bool) -> FillEvidence: ...
    async def evidence(self, order: Any) -> FillEvidence: ...
    async def closeable(self, order: Any, qty: Decimal) -> None: ...
    async def available_qty(self, order: Any) -> Decimal: ...


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

    async def entry_rules(self, symbol: str) -> MarketRules:
        return binance_rules(await self.client.get_symbol_info(symbol=symbol))

    async def inspect_entry_settings(self, symbol: str) -> dict | None:
        position = await self.client.get_preparation_state(symbol=symbol)
        before = {"margin_type": position.margin_type, "leverage": position.leverage}
        if before == {"margin_type": "isolated", "leverage": 1}:
            return None
        if position.position_amt != 0:
            raise PreDispatchRefusal("settings_position_open")
        if await self.client.has_open_orders_for_preparation(symbol=symbol):
            raise PreDispatchRefusal("settings_open_orders")
        return {
            "symbol": symbol,
            "before": before,
            "after": {"margin_type": "isolated", "leverage": 1},
        }

    async def prepare_entry_settings(
        self, symbol: str, *, confirm: bool, audit: Any
    ) -> dict | None:
        require_enabled(self.venue, confirm=confirm)
        change = None
        try:
            change = await self.inspect_entry_settings(symbol)
            if change is None:
                return None
            await audit(
                {
                    "symbol": symbol,
                    "before": change["before"],
                    "target": change["after"],
                    "state": "attempting",
                }
            )
            await self.client.set_margin_type(
                symbol=symbol, margin_type="ISOLATED", confirm=confirm
            )
            interim = await self.client.get_preparation_state(symbol=symbol)
            if interim.margin_type != "isolated" or interim.position_amt != 0:
                raise ValueError("margin readback mismatch")
            if await self.client.has_open_orders_for_preparation(symbol=symbol):
                raise ValueError("open orders appeared during preparation")
            await audit(
                {
                    "symbol": symbol,
                    "before": change["before"],
                    "after": {
                        "margin_type": interim.margin_type,
                        "leverage": interim.leverage,
                    },
                    "state": "observed",
                }
            )
            await self.client.set_leverage(
                symbol=symbol, leverage=1, require_symbol_echo=True
            )
            position = await self.client.get_preparation_state(symbol=symbol)
            if (
                position.margin_type != "isolated"
                or position.leverage != 1
                or position.position_amt != 0
            ):
                raise ValueError("settings readback mismatch")
            await audit({**change, "state": "verified"})
            return change
        except PreDispatchRefusal:
            raise
        except Exception:
            if change is not None:
                try:
                    position = await self.client.get_preparation_state(symbol=symbol)
                    observed = {
                        "margin_type": position.margin_type,
                        "leverage": position.leverage,
                    }
                except Exception:
                    observed = None
                try:
                    await audit(
                        {
                            "symbol": symbol,
                            "before": change["before"],
                            "after": observed,
                            "state": "refused",
                        }
                    )
                except Exception:
                    pass
            raise PreDispatchRefusal("settings_preparation_failed") from None

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
            qty=venue_order_quantity(order.qty),
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
            base_fee if base_fee else None,
            order.symbol.removesuffix("USDT") if base_fee else None,
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

    async def confirm_absence(
        self,
        order: Any,
        *,
        window_start: datetime,
        other_order_ids: frozenset[str],
    ) -> bool:
        """True only if myTrades fully covers the window and shows no own trade.

        Spot demo only. Trades already attributed to another ledger order are
        not this order's; any other same-side trade, a truncated read or a
        malformed row keeps the leg unresolved.
        """
        if self.futures:
            return False
        trades = await self.client.get_symbol_trades_since(
            symbol=order.symbol,
            start_time_ms=int(window_start.timestamp() * 1000),
            limit=MY_TRADES_LIMIT,
        )
        if len(trades) >= MY_TRADES_LIMIT:
            return False
        buyer = order.side in {"buy", "cover"}
        for trade in trades:
            order_id = trade.get("orderId")
            if (
                trade.get("symbol") != order.symbol
                or not isinstance(trade.get("isBuyer"), bool)
                or isinstance(order_id, bool)
                or not (
                    isinstance(order_id, int)
                    or (
                        isinstance(order_id, str)
                        and order_id.isascii()
                        and order_id.isdigit()
                    )
                )
            ):
                raise ValueError("demo trade row malformed")
        for trade in trades:
            if str(trade["orderId"]) in other_order_ids:
                continue
            if trade["isBuyer"] is buyer:
                return False
        return True

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
        available = await self.available_qty(order)
        if available < qty:
            raise ValueError("paper crypto quantity unavailable")

    async def available_qty(self, order: Any) -> Decimal:
        position = await self.client.get_position(
            order.symbol.removesuffix("USDT") + "USD"
        )
        if position is None or position.qty_available is None:
            raise ValueError("paper crypto quantity unavailable")
        if not position.qty_available.is_finite() or position.qty_available < 0:
            raise ValueError("paper crypto quantity unavailable")
        return position.qty_available

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

    async def _evidence_with_fees(self, response: Any, order: Any) -> FillEvidence:
        evidence = self._evidence(response, order)
        if evidence.filled_qty <= 0:
            return evidence
        if evidence.status.lower() != "filled":
            return FillEvidence(
                **{
                    **evidence.__dict__,
                    "fee_evidence_reason": "fee_pending_eod",
                }
            )
        try:
            fills = await self.client.list_order_fills(
                order_id=evidence.broker_order_id
            )
            total_fill_qty = sum((fill.qty for fill in fills), Decimal("0"))
            if not fills or total_fill_qty != evidence.filled_qty:
                raise ValueError("incomplete order fill activity set")
            activities = await self.client.list_crypto_fee_activities(
                order_id=evidence.broker_order_id
            )
            posting_date = (
                activity_posting_date(activities[0]) if activities else None
            ) or next(
                (
                    fill.transaction_time.date()
                    for fill in reversed(fills)
                    if fill.transaction_time is not None
                ),
                datetime.now(UTC).date(),
            )
            if activities and all(
                activity_posting_date(activity) == posting_date
                for activity in activities
            ):
                day_fills = await self.client.list_fills_for_date(date=posting_date)
                filled_leg_count = count_filled_legs_for_day(
                    fills=day_fills,
                    symbol=self.execution_symbol(order.symbol),
                    posting_date=posting_date,
                    expected_order_id=evidence.broker_order_id,
                )
            else:
                filled_leg_count = 0
        except Exception:
            return FillEvidence(
                **{
                    **evidence.__dict__,
                    "fee_evidence_reason": "fee_fill_set_incomplete",
                }
            )
        fee_evidence = resolve_crypto_fee_evidence(
            symbol=self.execution_symbol(order.symbol),
            side=order.side,
            filled_qty=evidence.filled_qty,
            activities=activities,
            posting_date=posting_date,
            filled_leg_count=filled_leg_count,
            terminal_filled=True,
        )
        return FillEvidence(
            **{
                **evidence.__dict__,
                "held_qty": fee_evidence.net_received_qty,
                "fee_amount": fee_evidence.amount,
                "fee_currency": fee_evidence.currency,
                "fee_evidence_reason": fee_evidence.reason,
            }
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
        return await self._evidence_with_fees(response, order)

    async def evidence(self, order: Any) -> FillEvidence:
        response = await self.client.get_order_by_client_order_id(order.client_order_id)
        if response is None:
            raise ValueError("paper order evidence unavailable; no resend")
        return await self._evidence_with_fees(response, order)

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
        expected_id = settings.expected_account_id_suffix
        expected_number = settings.expected_account_number_suffix
        if not (expected_id or "").strip() or not (expected_number or "").strip():
            raise ValueError("clean Alpaca expected account identity required")
        alpaca = AlpacaPaperBrokerService(settings=settings, profile="clean")
        # Existing episode/client IDs include this exact account_ref encoding.
        account_ref = f"alpaca-paper:{expected_id}:{expected_number}"
        return AlpacaCryptoPaper(alpaca, demo, account_ref=account_ref)
    except BaseException:
        await client.aclose()
        raise
