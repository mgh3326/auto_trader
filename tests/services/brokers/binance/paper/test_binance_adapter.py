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
    def __init__(self, status: str, client_order_id: str) -> None:
        self.status = status
        self.client_order_id = client_order_id
        self.broker_order_id = f"broker-{client_order_id}"
        self.executed_qty = Decimal("0.0002")
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

    def __init__(self) -> None:
        self.submits: list[dict[str, object]] = []
        self.free = Decimal("0")
        self.closed = False

    async def submit_order(self, **kwargs):
        self.submits.append(kwargs)
        self.free = Decimal("0.0002") if kwargs["side"] == "BUY" else Decimal("0")
        return _Order("FILLED", kwargs["client_order_id"])

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
    def __init__(self) -> None:
        self.clients: list[_SpotClient] = []
        self.references: list[_Reference] = []
        self.market_data: list[_MarketData] = []
        self.market_error: Exception | None = None
        self.market_calls = 0
        self.now = _NOW

    def client_factory(self) -> _SpotClient:
        client = _SpotClient()
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
