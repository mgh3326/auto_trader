from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.schemas.open_orders import (
    OpenOrderRow,
    OpenOrderSourceState,
    OpenOrdersResponse,
)


def test_open_orders_schema_serializes_decimal_rows() -> None:
    ordered_at = dt.datetime(2026, 6, 15, 9, 1, tzinfo=dt.UTC)
    response = OpenOrdersResponse(
        market="all",
        count=1,
        data_state="ok",
        as_of=ordered_at,
        items=[
            OpenOrderRow(
                broker="kis",
                market="kr",
                symbol="005930",
                symbol_name="삼성전자",
                side="buy",
                order_type="limit",
                time_in_force=None,
                price=Decimal("70000"),
                quantity=Decimal("10"),
                remaining_qty=Decimal("8"),
                filled_qty=Decimal("2"),
                status="pending",
                raw_status="접수",
                ordered_at=ordered_at,
                order_no="K1",
                exchange="KRX",
                currency="KRW",
            )
        ],
        sources=[
            OpenOrderSourceState(
                broker="kis",
                market="kr",
                status="ok",
                fetched_at=ordered_at,
                count=1,
                message=None,
            )
        ],
        warnings=[],
        empty_reason=None,
    )

    dumped = response.model_dump(mode="json")
    assert dumped["data_state"] == "ok"
    assert dumped["items"][0]["price"] == "70000"
    assert dumped["items"][0]["remaining_qty"] == "8"
    assert dumped["sources"][0]["broker"] == "kis"


def test_normalize_kis_kr_order_maps_domestic_pending_shape() -> None:
    from app.services.current_orders_service import normalize_kis_order

    row = normalize_kis_order(
        {
            "ord_no": "K1",
            "pdno": "005930",
            "prdt_name": "삼성전자",
            "sll_buy_dvsn_cd": "02",
            "ord_qty": "10",
            "ord_unpr": "70000",
            "rmn_qty": "8",
            "ord_dt": "20260615",
            "ord_tmd": "090100",
            "ord_dvsn_name": "지정가",
        },
        market="kr",
        exchange="KRX",
    )

    assert row.broker == "kis"
    assert row.market == "kr"
    assert row.symbol == "005930"
    assert row.symbol_name == "삼성전자"
    assert row.side == "buy"
    assert row.price == Decimal("70000")
    assert row.quantity == Decimal("10")
    assert row.remaining_qty == Decimal("8")
    assert row.order_no == "K1"
    assert row.exchange == "KRX"
    assert row.currency == "KRW"
    assert row.ordered_at is not None
    assert row.ordered_at.tzinfo is not None


def test_normalize_kis_us_order_maps_overseas_pending_shape() -> None:
    from app.services.current_orders_service import normalize_kis_order

    row = normalize_kis_order(
        {
            "odno": "U1",
            "pdno": "AAPL",
            "prdt_name": "Apple",
            "sll_buy_dvsn_cd": "01",
            "ft_ord_qty": "5",
            "ft_ord_unpr3": "180.5",
            "ft_ccld_qty": "2",
            "nccs_qty": "3",
            "prcs_stat_name": "접수",
            "ord_dt": "20260615",
            "ord_tmd": "223000",
        },
        market="us",
        exchange="NASD",
    )

    assert row.market == "us"
    assert row.symbol == "AAPL"
    assert row.side == "sell"
    assert row.price == Decimal("180.5")
    assert row.quantity == Decimal("5")
    assert row.filled_qty == Decimal("2")
    assert row.remaining_qty == Decimal("3")
    assert row.exchange == "NASD"
    assert row.currency == "USD"


def test_normalize_upbit_order_maps_wait_order_shape() -> None:
    from app.services.current_orders_service import normalize_upbit_order

    row = normalize_upbit_order(
        {
            "uuid": "UP1",
            "market": "KRW-BTC",
            "side": "bid",
            "ord_type": "limit",
            "price": "96000000",
            "volume": "0.01",
            "remaining_volume": "0.006",
            "executed_volume": "0.004",
            "state": "wait",
            "created_at": "2026-06-15T00:01:00+00:00",
        }
    )

    assert row.broker == "upbit"
    assert row.market == "crypto"
    assert row.symbol == "KRW-BTC"
    assert row.side == "buy"
    assert row.order_type == "limit"
    assert row.price == Decimal("96000000")
    assert row.quantity == Decimal("0.01")
    assert row.remaining_qty == Decimal("0.006")
    assert row.filled_qty == Decimal("0.004")
    assert row.status == "pending"
    assert row.raw_status == "wait"
    assert row.exchange == "UPBIT"
    assert row.currency == "KRW"


@pytest.mark.asyncio
async def test_current_orders_all_merges_kis_and_upbit_with_us_dedupe() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.current_orders_service import CurrentOrdersService

    async def inquire_overseas_orders(
        exchange_code: str = "NASD", is_mock: bool = False
    ):
        assert is_mock is False
        return {
            "NASD": [
                {
                    "odno": "U1",
                    "pdno": "AAPL",
                    "sll_buy_dvsn_cd": "02",
                    "ft_ord_qty": "1",
                    "ft_ord_unpr3": "180",
                    "nccs_qty": "1",
                }
            ],
            "NYSE": [
                {
                    "odno": "U1",
                    "pdno": "AAPL",
                    "sll_buy_dvsn_cd": "02",
                    "ft_ord_qty": "1",
                    "ft_ord_unpr3": "180",
                    "nccs_qty": "1",
                }
            ],
            "AMEX": [],
        }[exchange_code]

    fake_kis = SimpleNamespace(
        inquire_korea_orders=AsyncMock(
            return_value=[
                {
                    "ord_no": "K1",
                    "pdno": "005930",
                    "sll_buy_dvsn_cd": "02",
                    "ord_qty": "10",
                    "ord_unpr": "70000",
                }
            ]
        ),
        inquire_overseas_orders=AsyncMock(side_effect=inquire_overseas_orders),
    )
    fake_upbit = SimpleNamespace(
        fetch_open_orders=AsyncMock(
            return_value=[
                {
                    "uuid": "C1",
                    "market": "KRW-BTC",
                    "side": "ask",
                    "price": "99000000",
                    "volume": "0.02",
                    "remaining_volume": "0.02",
                }
            ]
        )
    )

    from app.services.brokers.toss.dto import TossOrdersPage

    fake_toss = SimpleNamespace(
        list_orders=AsyncMock(
            return_value=TossOrdersPage(orders=[], next_cursor=None, has_next=False)
        ),
        aclose=AsyncMock(),
    )

    service = CurrentOrdersService(
        kis_client_factory=lambda: fake_kis,
        upbit_client=fake_upbit,
        toss_client_factory=lambda: fake_toss,
        clock=lambda: dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.UTC),
    )

    response = await service.list_open_orders(market="all")

    assert response.data_state == "ok"
    assert response.count == 3
    assert {(item.broker, item.market, item.order_no) for item in response.items} == {
        ("kis", "kr", "K1"),
        ("kis", "us", "U1"),
        ("upbit", "crypto", "C1"),
    }
    assert fake_kis.inquire_korea_orders.await_args.kwargs == {"is_mock": False}
    assert fake_kis.inquire_overseas_orders.await_count == 3
    assert fake_upbit.fetch_open_orders.await_args.kwargs == {"market": None}


@pytest.mark.asyncio
async def test_current_orders_fails_open_when_one_kis_us_exchange_fails() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.current_orders_service import CurrentOrdersService

    async def inquire_overseas_orders(
        exchange_code: str = "NASD", is_mock: bool = False
    ):
        if exchange_code == "NYSE":
            raise RuntimeError("NYSE down")
        return [
            {
                "odno": exchange_code,
                "pdno": "AAPL",
                "sll_buy_dvsn_cd": "02",
                "ft_ord_qty": "1",
            }
        ]

    fake_kis = SimpleNamespace(
        inquire_korea_orders=AsyncMock(return_value=[]),
        inquire_overseas_orders=AsyncMock(side_effect=inquire_overseas_orders),
    )
    service = CurrentOrdersService(
        kis_client_factory=lambda: fake_kis,
        upbit_client=None,
        toss_client_factory=None,
        clock=lambda: dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.UTC),
    )

    response = await service.list_open_orders(market="us")

    assert response.data_state == "degraded"
    assert response.count == 2
    kis_us = [s for s in response.sources if s.broker == "kis" and s.market == "us"][0]
    assert kis_us.status == "degraded"
    assert "NYSE" in (kis_us.message or "")
    assert any("kis/us" in warning for warning in response.warnings)


@pytest.mark.asyncio
async def test_current_orders_unavailable_when_requested_sources_all_fail() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.current_orders_service import CurrentOrdersService

    fake_upbit = SimpleNamespace(
        fetch_open_orders=AsyncMock(side_effect=RuntimeError("upbit down"))
    )
    service = CurrentOrdersService(
        kis_client_factory=None,
        upbit_client=fake_upbit,
        toss_client_factory=None,
        clock=lambda: dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.UTC),
    )

    response = await service.list_open_orders(market="crypto")

    assert response.data_state == "unavailable"
    assert response.items == []
    assert response.empty_reason == "all requested broker sources are unavailable"
    assert response.sources[0].broker == "upbit"
    assert response.sources[0].status == "unavailable"


@pytest.mark.asyncio
async def test_current_orders_toss_pages_and_splits_kr_us() -> None:
    from app.services.brokers.toss.dto import TossOrder, TossOrdersPage
    from app.services.current_orders_service import CurrentOrdersService

    class _FakeTossClient:
        def __init__(
            self,
            pages: list[TossOrdersPage] | None = None,
            exc: Exception | None = None,
        ) -> None:
            self.pages = pages or []
            self.exc = exc
            self.calls: list[dict[str, object]] = []
            self.closed = False

        async def list_orders(self, **kwargs):
            self.calls.append(kwargs)
            if self.exc is not None:
                raise self.exc
            index = len(self.calls) - 1
            return self.pages[index]

        async def aclose(self) -> None:
            self.closed = True

    def _toss_order(order_id: str, symbol: str, *, filled: str = "0") -> TossOrder:
        return TossOrder(
            order_id=order_id,
            symbol=symbol,
            side="BUY",
            order_type="LIMIT",
            time_in_force="DAY",
            status="OPEN",
            price=Decimal("100"),
            quantity=Decimal("10"),
            order_amount=None,
            currency="KRW" if symbol.isdigit() else "USD",
            ordered_at="2026-06-15T09:00:00+09:00",
            canceled_at=None,
            execution={"filledQuantity": Decimal(filled)},
        )

    fake_toss = _FakeTossClient(
        pages=[
            TossOrdersPage(
                orders=[_toss_order("T1", "005930")], next_cursor="next", has_next=True
            ),
            TossOrdersPage(
                orders=[_toss_order("T2", "AAPL", filled="2")],
                next_cursor=None,
                has_next=False,
            ),
        ]
    )
    service = CurrentOrdersService(
        kis_client_factory=None,
        upbit_client=None,
        toss_client_factory=lambda: fake_toss,
        clock=lambda: dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.UTC),
    )

    response = await service.list_open_orders(market="all")

    toss_rows = [item for item in response.items if item.broker == "toss"]
    assert [(row.market, row.symbol, row.order_no) for row in toss_rows] == [
        ("kr", "005930", "T1"),
        ("us", "AAPL", "T2"),
    ]
    assert toss_rows[1].remaining_qty == Decimal("8")
    assert fake_toss.calls == [
        {"status": "OPEN", "cursor": None},
        {"status": "OPEN", "cursor": "next"},
    ]
    assert fake_toss.closed is True


@pytest.mark.asyncio
async def test_current_orders_toss_kr_filter_keeps_only_kr_orders() -> None:
    from app.services.brokers.toss.dto import TossOrder, TossOrdersPage
    from app.services.current_orders_service import CurrentOrdersService

    class _FakeTossClient:
        def __init__(
            self,
            pages: list[TossOrdersPage] | None = None,
            exc: Exception | None = None,
        ) -> None:
            self.pages = pages or []
            self.exc = exc
            self.calls: list[dict[str, object]] = []
            self.closed = False

        async def list_orders(self, **kwargs):
            self.calls.append(kwargs)
            if self.exc is not None:
                raise self.exc
            index = len(self.calls) - 1
            return self.pages[index]

        async def aclose(self) -> None:
            self.closed = True

    def _toss_order(order_id: str, symbol: str, *, filled: str = "0") -> TossOrder:
        return TossOrder(
            order_id=order_id,
            symbol=symbol,
            side="BUY",
            order_type="LIMIT",
            time_in_force="DAY",
            status="OPEN",
            price=Decimal("100"),
            quantity=Decimal("10"),
            order_amount=None,
            currency="KRW" if symbol.isdigit() else "USD",
            ordered_at="2026-06-15T09:00:00+09:00",
            canceled_at=None,
            execution={"filledQuantity": Decimal(filled)},
        )

    fake_toss = _FakeTossClient(
        pages=[
            TossOrdersPage(
                orders=[_toss_order("T1", "005930"), _toss_order("T2", "AAPL")],
                next_cursor=None,
                has_next=False,
            )
        ]
    )
    service = CurrentOrdersService(
        kis_client_factory=None,
        upbit_client=None,
        toss_client_factory=lambda: fake_toss,
        clock=lambda: dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.UTC),
    )

    response = await service.list_open_orders(market="kr")

    assert [(row.broker, row.market, row.symbol) for row in response.items] == [
        ("toss", "kr", "005930")
    ]


@pytest.mark.asyncio
async def test_current_orders_toss_disabled_fails_open() -> None:
    from app.services.brokers.toss.dto import TossOrdersPage
    from app.services.current_orders_service import CurrentOrdersService

    class _FakeTossClient:
        def __init__(
            self,
            pages: list[TossOrdersPage] | None = None,
            exc: Exception | None = None,
        ) -> None:
            self.pages = pages or []
            self.exc = exc
            self.calls: list[dict[str, object]] = []
            self.closed = False

        async def list_orders(self, **kwargs):
            self.calls.append(kwargs)
            if self.exc is not None:
                raise self.exc
            index = len(self.calls) - 1
            return self.pages[index]

        async def aclose(self) -> None:
            self.closed = True

    fake_toss = _FakeTossClient(exc=RuntimeError("TOSS_API_ENABLED"))
    service = CurrentOrdersService(
        kis_client_factory=None,
        upbit_client=None,
        toss_client_factory=lambda: fake_toss,
        clock=lambda: dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.UTC),
    )

    response = await service.list_open_orders(market="kr")

    toss_kr = [s for s in response.sources if s.broker == "toss" and s.market == "kr"][
        0
    ]
    assert toss_kr.status == "unavailable"
    assert response.data_state == "unavailable"


@pytest.mark.asyncio
async def test_current_orders_reuses_one_kis_client_per_request() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.brokers.toss.dto import TossOrdersPage
    from app.services.current_orders_service import CurrentOrdersService

    fake_kis = SimpleNamespace(
        inquire_korea_orders=AsyncMock(return_value=[]),
        inquire_overseas_orders=AsyncMock(return_value=[]),
    )
    fake_toss = SimpleNamespace(
        list_orders=AsyncMock(
            return_value=TossOrdersPage(orders=[], next_cursor=None, has_next=False)
        ),
        aclose=AsyncMock(),
    )
    factory_calls = 0

    def _kis_factory():
        nonlocal factory_calls
        factory_calls += 1
        return fake_kis

    service = CurrentOrdersService(
        kis_client_factory=_kis_factory,
        upbit_client=None,
        toss_client_factory=lambda: fake_toss,
        clock=lambda: dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.UTC),
    )

    response = await service.list_open_orders(market="all")

    assert response.data_state == "degraded"
    assert factory_calls == 1
    assert fake_kis.inquire_korea_orders.await_count == 1
    assert fake_kis.inquire_overseas_orders.await_count == 3


@pytest.mark.asyncio
async def test_current_orders_empty_reason_reports_partial_source_unavailable() -> None:
    from types import SimpleNamespace
    from unittest.mock import AsyncMock

    from app.services.current_orders_service import CurrentOrdersService

    fake_kis = SimpleNamespace(
        inquire_korea_orders=AsyncMock(return_value=[]),
        inquire_overseas_orders=AsyncMock(return_value=[]),
    )

    class _DisabledToss:
        async def list_orders(self, **kwargs):
            raise RuntimeError("TOSS_API_ENABLED")

        async def aclose(self) -> None:
            return None

    service = CurrentOrdersService(
        kis_client_factory=lambda: fake_kis,
        upbit_client=None,
        toss_client_factory=lambda: _DisabledToss(),
        clock=lambda: dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.UTC),
    )

    response = await service.list_open_orders(market="kr")

    assert response.data_state == "degraded"
    assert response.items == []
    assert (
        response.empty_reason
        == "some broker sources are unavailable; no open orders from available sources"
    )
