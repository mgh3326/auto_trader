"""ROB-845 Binance Spot Demo canonical adapter contract tests."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest
import pytest_asyncio
from sqlalchemy import delete

from app.core.db import AsyncSessionLocal
from app.models.binance_demo_order_ledger import BinanceDemoOrderLedger
from app.models.crypto_instruments import CryptoInstrument
from app.models.scalp_trade_analytics import ScalpTradeAnalytics
from app.services.brokers.binance.demo.ledger import BinanceDemoLedgerService
from app.services.brokers.binance.demo_scalping.contract import MarketConditions
from app.services.brokers.binance.demo_scalping.market_data import (
    MarketConditionsUnavailable,
)
from app.services.brokers.binance.demo_scalping_exec.executor import (
    DemoScalpingExecutor,
)
from app.services.brokers.binance.demo_scalping_exec.reference import SymbolReference
from app.services.brokers.binance.paper_adapter import BinanceSpotDemoPaperAdapter
from app.services.brokers.capabilities import Broker
from app.services.brokers.paper.contracts import (
    PaperOperationStatus,
    VerifiedPaperOrderIntent,
)

pytestmark = pytest.mark.usefixtures("binance_demo_reservation_lock")

_NOW = dt.datetime(2026, 7, 13, 2, 0, tzinfo=dt.UTC)
_MARKET = MarketConditions(
    spread_bps=Decimal("2"),
    data_age_seconds=Decimal("5"),
    spot_free_base_qty=Decimal("0"),
)
_REFERENCE = SymbolReference(
    price=Decimal("50000"),
    step_size=Decimal("0.00001"),
    min_notional=Decimal("5"),
    tick_size=Decimal("0.01"),
)


def _intent(**overrides) -> VerifiedPaperOrderIntent:
    values = {
        "intent_id": "intent-binance-1",
        "experiment_id": "experiment-1",
        "run_id": "run-1",
        "cohort_id": "cohort-1",
        "strategy_version_id": "strategy-v1",
        "strategy_hash": "a" * 64,
        "config_hash": "b" * 64,
        "policy_hash": "c" * 64,
        "venue": Broker.BINANCE,
        "account_mode": "demo",
        "product": "spot",
        "symbol": "BTCUSDT",
        "side": "buy",
        "order_type": "market",
        "time_in_force": None,
        "qty": None,
        "notional": Decimal("10"),
        "price": None,
        "market_snapshot_id": "snapshot-1",
        "market_snapshot_hash": "d" * 64,
        "market_snapshot_as_of": _NOW - dt.timedelta(seconds=5),
        "market_snapshot_source": "binance_public_spot",
        "source_buy_reference": None,
        "decision_id": "decision-1",
        "reference_price": Decimal("50000"),
        "source_buy_client_order_id": None,
        "origin": "experiment",
        "idempotency_key": "rob845-" + "1" * 29,
    }
    values.update(overrides)
    return VerifiedPaperOrderIntent(**values)


class _Order:
    def __init__(
        self,
        status: str,
        client_order_id: str,
        *,
        executed_qty: Decimal = Decimal("0.0002"),
    ) -> None:
        self.status = status
        self.client_order_id = client_order_id
        self.broker_order_id = (
            "84501" if client_order_id.startswith("rob845r-") else "84502"
        )
        self.executed_qty = executed_qty
        self.cummulative_quote_qty = Decimal("10")


class _Balance:
    def __init__(self, free: Decimal) -> None:
        self.asset = "BTC"
        self.free = free
        self.locked = Decimal("0")


class _OpenOrders:
    orders: list[object] = []


class _SpotClient:
    credential_fingerprint = "sha256:" + "12" * 32

    def __init__(
        self,
        *,
        initial_free: Decimal = Decimal("0"),
        buy_executed_qty: Decimal = Decimal("0.0002"),
        buy_submit_status: str = "FILLED",
        buy_submit_executed_qty: Decimal | None = None,
        timeout_after_accept_sides: frozenset[str] = frozenset(),
    ) -> None:
        self.submits: list[dict[str, object]] = []
        self.free = initial_free
        self.buy_executed_qty = buy_executed_qty
        self.buy_submit_status = buy_submit_status
        self.buy_submit_executed_qty = buy_submit_executed_qty
        self.timeout_after_accept_sides = set(timeout_after_accept_sides)
        self.orders: dict[str, dict[str, object]] = {}
        self.order_status_calls: list[str] = []
        self.closed = False

    async def submit_order(self, **kwargs):
        self.submits.append(kwargs)
        if kwargs["side"] == "BUY":
            balance_delta = self.buy_executed_qty
            self.free += balance_delta
            submit_executed_qty = (
                self.buy_submit_executed_qty
                if self.buy_submit_executed_qty is not None
                else balance_delta
            )
            submit_status = self.buy_submit_status
        else:
            balance_delta = kwargs["qty"]
            self.free -= balance_delta
            submit_executed_qty = balance_delta
            submit_status = "FILLED"
        order = _Order(
            submit_status,
            kwargs["client_order_id"],
            executed_qty=submit_executed_qty,
        )
        self.orders[kwargs["client_order_id"]] = {
            "clientOrderId": kwargs["client_order_id"],
            "orderId": order.broker_order_id,
            "symbol": kwargs["symbol"],
            "side": kwargs["side"],
            "type": kwargs["order_type"],
            "origQty": str(kwargs["qty"]),
            "executedQty": str(balance_delta),
            "cummulativeQuoteQty": "10",
            "status": "FILLED",
        }
        if kwargs["side"] in self.timeout_after_accept_sides:
            self.timeout_after_accept_sides.remove(kwargs["side"])
            raise TimeoutError(f"response lost after {kwargs['side']} acceptance")
        return order

    async def get_order_status(self, *, symbol: str, client_order_id: str):
        self.order_status_calls.append(client_order_id)
        return self.orders[client_order_id]

    async def get_asset_balance(self, *, asset: str):
        return _Balance(self.free)

    async def get_open_orders(self, *, symbol: str):
        return _OpenOrders()

    async def aclose(self) -> None:
        self.closed = True


class _Reference:
    def __init__(self) -> None:
        self.closed = False

    async def fetch(self, product: str, symbol: str) -> SymbolReference:
        return _REFERENCE

    async def aclose(self) -> None:
        self.closed = True


class _MarketData:
    def __init__(self) -> None:
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class _Dependencies:
    def __init__(
        self,
        *,
        initial_free: Decimal = Decimal("0"),
        buy_executed_qty: Decimal = Decimal("0.0002"),
        buy_submit_status: str = "FILLED",
        buy_submit_executed_qty: Decimal | None = None,
        timeout_after_accept_sides: frozenset[str] = frozenset(),
    ) -> None:
        self.clients: list[_SpotClient] = []
        self.references: list[_Reference] = []
        self.market_data: list[_MarketData] = []
        self.market_error: Exception | None = None
        self.market_calls = 0
        self.now = _NOW
        self.initial_free = initial_free
        self.buy_executed_qty = buy_executed_qty
        self.buy_submit_status = buy_submit_status
        self.buy_submit_executed_qty = buy_submit_executed_qty
        self.timeout_after_accept_sides = timeout_after_accept_sides

    def client_factory(self) -> _SpotClient:
        client = _SpotClient(
            initial_free=self.initial_free,
            buy_executed_qty=self.buy_executed_qty,
            buy_submit_status=self.buy_submit_status,
            buy_submit_executed_qty=self.buy_submit_executed_qty,
            timeout_after_accept_sides=self.timeout_after_accept_sides,
        )
        self.clients.append(client)
        return client

    def reference_factory(self) -> _Reference:
        reference = _Reference()
        self.references.append(reference)
        return reference

    def market_data_factory(self) -> _MarketData:
        market_data = _MarketData()
        self.market_data.append(market_data)
        return market_data

    async def market_builder(self, market_data, *, product: str, symbol: str):
        self.market_calls += 1
        if self.market_error is not None:
            raise self.market_error
        return _MARKET

    def adapter(self) -> BinanceSpotDemoPaperAdapter:
        return BinanceSpotDemoPaperAdapter(
            session_factory=AsyncSessionLocal,
            client_factory=self.client_factory,
            reference_factory=self.reference_factory,
            market_data_factory=self.market_data_factory,
            market_conditions_builder=self.market_builder,
            clock=lambda: self.now,
        )


@pytest_asyncio.fixture(autouse=True)
async def _clean_rows(binance_demo_reservation_lock, monkeypatch):
    async def resolve_test_instrument(self, symbol: str) -> int:
        return await self.ledger.resolve_or_create_instrument(
            venue="binance",
            product="spot",
            venue_symbol=f"ROB845PAPER{symbol}",
            base_asset=f"ROB845PAPER{symbol.removesuffix('USDT')}",
            quote_asset="USDT",
        )

    monkeypatch.setattr(
        DemoScalpingExecutor,
        "_resolve_or_create_instrument",
        resolve_test_instrument,
    )

    async def clean() -> None:
        async with AsyncSessionLocal() as session:
            await session.execute(
                delete(ScalpTradeAnalytics).where(
                    ScalpTradeAnalytics.open_client_order_id.like("rob845r-%")
                )
            )
            await session.execute(
                delete(BinanceDemoOrderLedger).where(
                    BinanceDemoOrderLedger.client_order_id.like("rob845r-%")
                    | BinanceDemoOrderLedger.client_order_id.like("rob845c-%")
                )
            )
            instrument_filter = CryptoInstrument.venue_symbol.like("ROB845PAPER%")
            await session.execute(
                delete(CryptoInstrument).where(
                    CryptoInstrument.venue == "binance",
                    CryptoInstrument.product == "spot",
                    instrument_filter,
                )
            )
            await session.commit()

    # Tests map the canonical order symbol to a unique test instrument, so they
    # never create, mutate, or delete shared BTC/ETH instrument identities.
    await clean()
    yield
    await clean()


@pytest.mark.asyncio
async def test_preview_runs_guarded_preflight_without_signed_client() -> None:
    deps = _Dependencies()
    result = await deps.adapter().preview(_intent())

    assert result.status is PaperOperationStatus.SUCCEEDED
    assert result.reason_code == "ok"
    assert result.risk_snapshot is not None
    assert result.risk_snapshot.open_exposure is None
    assert result.risk_snapshot.reserved_notional is None
    assert result.risk_snapshot.daily_realized_loss == 0
    assert result.risk_snapshot.quote_price == _REFERENCE.price
    assert result.risk_snapshot.quote_source == "binance_public_spot"
    assert result.risk_snapshot.quote_as_of == _NOW - dt.timedelta(seconds=5)
    assert deps.clients == []
    assert deps.market_calls == 1


@pytest.mark.asyncio
async def test_blocked_preview_returns_truthful_native_risk_snapshot() -> None:
    deps = _Dependencies()
    result = await deps.adapter().preview(_intent(notional=Decimal("11")))

    assert result.status is PaperOperationStatus.BLOCKED
    assert result.reason_code == "notional_above_cap"
    assert result.risk_snapshot is not None
    assert result.risk_snapshot.quote_price == Decimal("50000")
    assert result.risk_snapshot.daily_realized_loss == 0
    assert deps.clients == []


@pytest.mark.asyncio
async def test_risk_snapshot_does_not_mix_canonical_quote_or_native_policy_owners() -> (
    None
):
    deps = _Dependencies()
    intent = _intent(reference_price=Decimal("40000"))

    result = await deps.adapter().preview(intent)

    assert result.risk_snapshot is not None
    snapshot = result.risk_snapshot
    assert snapshot.quote_price == Decimal("40000")
    assert snapshot.quote_source == intent.market_snapshot_source
    assert snapshot.quote_as_of == intent.market_snapshot_as_of
    assert snapshot.policy_hash != intent.policy_hash
    assert result.evidence["canonical_market_snapshot"] == {
        "price": "40000",
        "source": intent.market_snapshot_source,
        "as_of": intent.market_snapshot_as_of.isoformat(),
        "snapshot_id": intent.market_snapshot_id,
        "snapshot_hash": intent.market_snapshot_hash,
        "experiment_policy_hash": intent.policy_hash,
    }
    assert result.evidence["native_demo_risk"] == {
        "reference_price": "50000",
        "reference_source": "binance_demo_ticker_price",
        "policy_version": snapshot.policy_version,
        "policy_hash": snapshot.policy_hash,
    }


@pytest.mark.asyncio
async def test_submit_uses_deterministic_native_round_trip_and_links_evidence() -> None:
    deps = _Dependencies()
    adapter = deps.adapter()
    result = await adapter.submit(_intent())

    assert result.status is PaperOperationStatus.SUCCEEDED
    assert result.reason_code == "ok"
    assert result.native_client_order_id is not None
    assert result.native_client_order_id.startswith("rob845r-")
    assert result.evidence["close_client_order_id"].startswith("rob845c-")
    assert [call["side"] for call in deps.clients[0].submits] == ["BUY", "SELL"]
    assert deps.clients[0].closed is True
    assert deps.references[0].closed is True
    assert deps.market_data[0].closed is True

    linked = await adapter.link_native_order(_intent())
    assert linked.status is PaperOperationStatus.SUCCEEDED
    assert linked.native_client_order_id == result.native_client_order_id
    assert linked.evidence["root"]["lifecycle_state"] == "reconciled"
    assert (
        linked.evidence["close"]["parent_client_order_id"]
        == result.native_client_order_id
    )


@pytest.mark.asyncio
async def test_round_trip_close_preserves_preexisting_spot_balance() -> None:
    preexisting = Decimal("0.001")
    bought = Decimal("0.0002")
    deps = _Dependencies(initial_free=preexisting, buy_executed_qty=bought)

    result = await deps.adapter().submit(_intent())

    assert result.status is PaperOperationStatus.SUCCEEDED
    assert [call["side"] for call in deps.clients[0].submits] == ["BUY", "SELL"]
    assert deps.clients[0].submits[1]["qty"] == bought
    assert deps.clients[0].free == preexisting


@pytest.mark.asyncio
@pytest.mark.parametrize("preexisting", [Decimal("0"), Decimal("0.001")])
async def test_spot_new_submit_polled_filled_closes_only_observed_buy_delta(
    preexisting: Decimal,
) -> None:
    bought = Decimal("0.0002")
    deps = _Dependencies(
        initial_free=preexisting,
        buy_executed_qty=bought,
        buy_submit_status="NEW",
        buy_submit_executed_qty=Decimal("0"),
    )

    result = await deps.adapter().submit(_intent())

    assert result.status is PaperOperationStatus.SUCCEEDED
    assert [call["side"] for call in deps.clients[0].submits] == ["BUY", "SELL"]
    assert deps.clients[0].submits[1]["qty"] == bought
    assert deps.clients[0].free == preexisting
    assert deps.clients[0].order_status_calls == [result.native_client_order_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("lost_side", ["BUY", "SELL"])
async def test_accept_then_timeout_recovers_by_client_order_id_without_repost(
    lost_side: str,
) -> None:
    deps = _Dependencies(timeout_after_accept_sides=frozenset({lost_side}))
    adapter = deps.adapter()
    intent = _intent()

    result = await adapter.submit(intent)

    assert result.status is PaperOperationStatus.SUCCEEDED
    assert [call["side"] for call in deps.clients[0].submits] == ["BUY", "SELL"]
    assert sum(call["side"] == lost_side for call in deps.clients[0].submits) == 1
    assert deps.clients[0].free == 0

    linked = await adapter.link_native_order(intent)
    leg = "root" if lost_side == "BUY" else "close"
    assert linked.evidence[leg]["metadata"]["submit_recovered_by_client_order_id"]


@pytest.mark.asyncio
async def test_terminal_submit_replays_before_market_or_client_construction() -> None:
    deps = _Dependencies()
    adapter = deps.adapter()
    first = await adapter.submit(_intent())
    assert first.status is PaperOperationStatus.SUCCEEDED
    initial_market_calls = deps.market_calls
    initial_client_count = len(deps.clients)
    deps.market_error = MarketConditionsUnavailable("stale_data")
    deps.now += dt.timedelta(hours=1)

    replay = await adapter.submit(_intent())

    assert replay.status is PaperOperationStatus.SUCCEEDED
    assert replay.replayed is True
    assert replay.native_client_order_id == first.native_client_order_id
    assert deps.market_calls == initial_market_calls
    assert len(deps.clients) == initial_client_count


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "release_reason", ["broker_order_not_found", "terminal_zero_fill"]
)
async def test_released_reservation_replays_as_non_success_without_dependencies(
    release_reason: str,
) -> None:
    deps = _Dependencies()
    adapter = deps.adapter()
    intent = _intent()
    identity = adapter._execution_identity(intent)
    native_intent = adapter._native_intent(intent)

    async with AsyncSessionLocal() as session:
        ledger = BinanceDemoLedgerService(session)
        instrument_id = await ledger.resolve_or_create_instrument(
            venue="binance",
            product="spot",
            venue_symbol="ROB845PAPERBTCUSDT",
            base_asset="ROB845PAPERBTC",
            quote_asset="USDT",
        )
        reservation = await ledger.reserve_root_planned(
            instrument_id=instrument_id,
            product="spot",
            venue_host="demo-api.binance.com",
            client_order_id=identity.root_client_order_id,
            side="BUY",
            order_type="MARKET",
            qty=Decimal("0.0002"),
            price=None,
            notional_usdt=Decimal("10"),
            extra_metadata={
                "credential_fingerprint": _SpotClient.credential_fingerprint
            },
            idempotency_metadata=identity.ledger_metadata(native_intent),
            global_open_root_cap=100,
            now=_NOW,
        )
        assert reservation.status == "reserved"
        release_evidence = {"reservation_reconcile_reason": release_reason}
        if release_reason == "terminal_zero_fill":
            release_evidence["reservation_reconcile_broker_order_id"] = "12345"
        await ledger.record_cancelled(
            client_order_id=identity.root_client_order_id,
            now=_NOW + dt.timedelta(hours=1),
            extra_metadata_merge=release_evidence,
        )
        await ledger.record_reconciled(
            client_order_id=identity.root_client_order_id,
            now=_NOW + dt.timedelta(hours=1),
            extra_metadata_merge=release_evidence,
        )
        await session.commit()

    replay = await adapter.submit(intent)

    assert replay.status is PaperOperationStatus.BLOCKED
    assert replay.reason_code == release_reason
    assert replay.replayed is True
    assert replay.native_order_id is None
    assert deps.market_calls == 0
    assert deps.clients == []
    assert deps.references == []
    assert deps.market_data == []


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["cancel", "reconcile"])
async def test_unsupported_methods_touch_no_dependency(operation: str) -> None:
    deps = _Dependencies()
    result = await getattr(deps.adapter(), operation)(_intent())

    assert result.status is PaperOperationStatus.BLOCKED
    assert result.reason_code == "unsupported_capability"
    assert deps.clients == []
    assert deps.references == []
    assert deps.market_data == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "overrides",
    [
        {"symbol": "SOLUSDT"},
        {
            "side": "sell",
            "source_buy_reference": "source",
            "source_buy_client_order_id": "native-source",
        },
        {"order_type": "limit", "price": Decimal("50000")},
        {"qty": Decimal("0.001"), "notional": None},
        {"account_mode": "paper"},
    ],
)
async def test_unsupported_order_contract_touches_no_dependency(overrides) -> None:
    deps = _Dependencies()
    result = await deps.adapter().submit(_intent(**overrides))

    assert result.status is PaperOperationStatus.BLOCKED
    assert result.reason_code == "unsupported_capability"
    assert deps.clients == []
    assert deps.references == []
    assert deps.market_data == []
