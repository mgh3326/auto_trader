# ROB-572 Current Orders View Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a read-only `/invest/my` "현재 주문" view that shows live open/pending orders across KIS KR/US, Toss KR/US, and Upbit crypto without writing to the DB.

**Architecture:** Build a dedicated backend read service that fans out to broker read APIs, normalizes rows into one transport schema, and fails open per broker/source. Expose it through an authenticated `/trading/api/invest/open-orders` route, then add a `/my` tab and panel that renders the normalized rows with broker/market filters and degraded-source warnings.

**Tech Stack:** Python 3.13, FastAPI, Pydantic v2, pytest/pytest-asyncio, React 19, TypeScript, Vitest, Testing Library.

---

## Scope Decisions

- Linear ROB-572 already fixes the product decisions: read-only, live broker API, no DB/migration, KR/US/crypto/all tabs, `/my` tab placement, Toss/KIS as separate broker rows with no dedupe across brokers.
- S5 NXT fallback is deferred. Do not call `inquire_daily_order_domestic` in v1.
- Add the tab to both desktop and mobile `/my` because `PORTFOLIO_TABS` is shared. If product wants desktop-only, stop before Task 8 and split mobile handling.
- Do not render cancel/modify buttons. Tests must assert no cancel action is exposed.
- `LinkedOrderRow` requires a durable `ledgerId`, so do not fake one for live open orders. Reuse `LINKED_ORDER_STATUS_LABELS` and `LINKED_ORDER_STATUS_TONES` from `components/orders/LinkedOrderRow.tsx`, and implement a dedicated current-order row.
- Review lane: routine read-only implementation on `keep_on_gpt54`. This does touch live broker reads, so the PR summary should explicitly state "read-only, no order mutation, no DB migration".

## File Structure

- Create `app/schemas/open_orders.py`: Pydantic response DTOs for open orders and per-source health.
- Create `app/services/current_orders_service.py`: broker fan-out, fail-open source collection, normalizers, sorting, source-state aggregation.
- Create `app/routers/invest_open_orders.py`: authenticated read endpoint.
- Modify `app/main.py`: import and include the new router beside `invest_fills`.
- Create `tests/test_current_orders_service.py`: unit tests for normalization, fan-out, Toss paging, fail-open behavior.
- Create `tests/routers/test_invest_open_orders_router.py`: route/auth/query validation tests.
- Create `frontend/invest/src/types/currentOrders.ts`: TypeScript DTOs matching backend JSON.
- Create `frontend/invest/src/api/currentOrders.ts`: fetch wrapper using `credentials: "include"`.
- Create `frontend/invest/src/components/my/CurrentOrdersPanel.tsx`: read-only panel with market tabs, source warnings, summary counts, table/list rows.
- Modify `frontend/invest/src/components/my/portfolioTabs.ts`: add `currentOrders`.
- Modify `frontend/invest/src/pages/desktop/DesktopPortfolioPage.tsx`: title/copy/render branch for current orders.
- Modify `frontend/invest/src/pages/mobile/MobilePortfolioPage.tsx`: compact current-orders panel branch.
- Create `frontend/invest/src/__tests__/currentOrders.api.test.ts`: API wrapper tests.
- Create `frontend/invest/src/__tests__/CurrentOrdersPanel.test.tsx`: panel render/filter/degraded/no-cancel tests.

---

### Task 1: Backend Schema Contract

**Files:**
- Create: `app/schemas/open_orders.py`
- Test: `tests/test_current_orders_service.py`

- [ ] **Step 1: Write the failing schema test**

Add this first test to `tests/test_current_orders_service.py`:

```python
from __future__ import annotations

import datetime as dt
from decimal import Decimal

import pytest

from app.schemas.open_orders import (
    OpenOrderRow,
    OpenOrdersResponse,
    OpenOrderSourceState,
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
uv run pytest tests/test_current_orders_service.py::test_open_orders_schema_serializes_decimal_rows -q
```

Expected: FAIL with `ModuleNotFoundError: No module named 'app.schemas.open_orders'`.

- [ ] **Step 3: Create the schema module**

Create `app/schemas/open_orders.py`:

```python
"""Read-only live open-order schemas for /invest current orders (ROB-572)."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_serializer

OpenOrderMarket = Literal["kr", "us", "crypto"]
OpenOrdersQueryMarket = Literal["all", "kr", "us", "crypto"]
OpenOrderBroker = Literal["kis", "toss", "upbit"]
OpenOrderSide = Literal["buy", "sell", "unknown"]
OpenOrderDataState = Literal["ok", "degraded", "unavailable"]


class OpenOrderRow(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker: OpenOrderBroker
    market: OpenOrderMarket
    symbol: str = Field(min_length=1)
    symbol_name: str | None = None
    side: OpenOrderSide = "unknown"
    order_type: str | None = None
    time_in_force: str | None = None
    price: Decimal | None = None
    quantity: Decimal | None = None
    remaining_qty: Decimal | None = None
    filled_qty: Decimal | None = None
    status: str = "pending"
    raw_status: str | None = None
    ordered_at: datetime | None = None
    order_no: str = Field(min_length=1)
    exchange: str | None = None
    currency: str | None = None

    @field_serializer("price", "quantity", "remaining_qty", "filled_qty")
    def _decimal_to_json(self, value: Decimal | None) -> str | None:
        if value is None:
            return None
        return format(value, "f")


class OpenOrderSourceState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    broker: OpenOrderBroker
    market: OpenOrderMarket
    status: OpenOrderDataState
    fetched_at: datetime | None = None
    count: int = Field(ge=0)
    message: str | None = None


class OpenOrdersResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    market: OpenOrdersQueryMarket
    count: int = Field(ge=0)
    data_state: OpenOrderDataState
    as_of: datetime
    items: list[OpenOrderRow]
    sources: list[OpenOrderSourceState]
    warnings: list[str] = Field(default_factory=list)
    empty_reason: str | None = None
```

- [ ] **Step 4: Run the schema test**

Run:

```bash
uv run pytest tests/test_current_orders_service.py::test_open_orders_schema_serializes_decimal_rows -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/schemas/open_orders.py tests/test_current_orders_service.py
git commit -m "test: define current open orders schema"
```

---

### Task 2: KIS and Upbit Normalizers

**Files:**
- Modify: `tests/test_current_orders_service.py`
- Create: `app/services/current_orders_service.py`

- [ ] **Step 1: Write failing normalizer tests**

Append:

```python
from app.services.current_orders_service import (
    normalize_kis_order,
    normalize_upbit_order,
)


def test_normalize_kis_kr_order_maps_domestic_pending_shape() -> None:
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_current_orders_service.py -q
```

Expected: FAIL because `app.services.current_orders_service` does not exist.

- [ ] **Step 3: Create normalizer implementation**

Create `app/services/current_orders_service.py` with the imports, constants, helpers, and normalizers:

```python
"""Live read-only current open-order service for /invest (ROB-572)."""

from __future__ import annotations

import datetime as dt
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from app.schemas.open_orders import OpenOrderMarket, OpenOrderRow

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
```

- [ ] **Step 4: Run normalizer tests**

Run:

```bash
uv run pytest tests/test_current_orders_service.py -q
```

Expected: PASS for the schema and normalizer tests.

- [ ] **Step 5: Commit**

```bash
git add app/services/current_orders_service.py tests/test_current_orders_service.py
git commit -m "test: normalize live current order rows"
```

---

### Task 3: KIS and Upbit Fan-Out With Fail-Open Source States

**Files:**
- Modify: `app/services/current_orders_service.py`
- Modify: `tests/test_current_orders_service.py`

- [ ] **Step 1: Write failing service fan-out tests**

Append:

```python
from types import SimpleNamespace
from unittest.mock import AsyncMock

from app.services.current_orders_service import CurrentOrdersService


@pytest.mark.asyncio
async def test_current_orders_all_merges_kis_and_upbit_with_us_dedupe() -> None:
    async def inquire_overseas_orders(exchange_code: str = "NASD", is_mock: bool = False):
        assert is_mock is False
        return {
            "NASD": [
                {"odno": "U1", "pdno": "AAPL", "sll_buy_dvsn_cd": "02", "ft_ord_qty": "1", "ft_ord_unpr3": "180", "nccs_qty": "1"}
            ],
            "NYSE": [
                {"odno": "U1", "pdno": "AAPL", "sll_buy_dvsn_cd": "02", "ft_ord_qty": "1", "ft_ord_unpr3": "180", "nccs_qty": "1"}
            ],
            "AMEX": [],
        }[exchange_code]

    fake_kis = SimpleNamespace(
        inquire_korea_orders=AsyncMock(
            return_value=[
                {"ord_no": "K1", "pdno": "005930", "sll_buy_dvsn_cd": "02", "ord_qty": "10", "ord_unpr": "70000"}
            ]
        ),
        inquire_overseas_orders=AsyncMock(side_effect=inquire_overseas_orders),
    )
    fake_upbit = SimpleNamespace(
        fetch_open_orders=AsyncMock(
            return_value=[
                {"uuid": "C1", "market": "KRW-BTC", "side": "ask", "price": "99000000", "volume": "0.02", "remaining_volume": "0.02"}
            ]
        )
    )

    service = CurrentOrdersService(
        kis_client_factory=lambda: fake_kis,
        upbit_client=fake_upbit,
        toss_client_factory=None,
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
    async def inquire_overseas_orders(exchange_code: str = "NASD", is_mock: bool = False):
        if exchange_code == "NYSE":
            raise RuntimeError("NYSE down")
        return [{"odno": exchange_code, "pdno": "AAPL", "sll_buy_dvsn_cd": "02", "ft_ord_qty": "1"}]

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
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_current_orders_service.py -q
```

Expected: FAIL because `CurrentOrdersService` is missing.

- [ ] **Step 3: Implement service fan-out**

Add these pieces to `app/services/current_orders_service.py`:

```python
import asyncio
import logging
from collections.abc import Callable
from typing import Protocol

from app.schemas.open_orders import (
    OpenOrderDataState,
    OpenOrdersQueryMarket,
    OpenOrdersResponse,
    OpenOrderSourceState,
)
from app.services.brokers import upbit as _unused_upbit_package
from app.services.brokers.kis.client import KISClient
from app.services.brokers.upbit import orders as upbit_orders

logger = logging.getLogger(__name__)
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
```

Remove the unused `_unused_upbit_package` import before running lint:

```python
from app.services.brokers.upbit import orders as upbit_orders
```

- [ ] **Step 4: Run service tests**

Run:

```bash
uv run pytest tests/test_current_orders_service.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/current_orders_service.py tests/test_current_orders_service.py
git commit -m "test: fan out current open order reads"
```

---

### Task 4: Toss KR/US Open Orders

**Files:**
- Modify: `app/services/current_orders_service.py`
- Modify: `tests/test_current_orders_service.py`

- [ ] **Step 1: Write failing Toss tests**

Append:

```python
from app.services.brokers.toss.dto import TossOrder, TossOrdersPage


class _FakeTossClient:
    def __init__(self, pages: list[TossOrdersPage] | None = None, exc: Exception | None = None) -> None:
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


@pytest.mark.asyncio
async def test_current_orders_toss_pages_and_splits_kr_us() -> None:
    fake_toss = _FakeTossClient(
        pages=[
            TossOrdersPage(orders=[_toss_order("T1", "005930")], next_cursor="next", has_next=True),
            TossOrdersPage(orders=[_toss_order("T2", "AAPL", filled="2")], next_cursor=None, has_next=False),
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
    fake_toss = _FakeTossClient(exc=RuntimeError("TOSS_API_ENABLED"))
    service = CurrentOrdersService(
        kis_client_factory=None,
        upbit_client=None,
        toss_client_factory=lambda: fake_toss,
        clock=lambda: dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.UTC),
    )

    response = await service.list_open_orders(market="kr")

    toss_kr = [s for s in response.sources if s.broker == "toss" and s.market == "kr"][0]
    assert toss_kr.status == "unavailable"
    assert response.data_state == "unavailable"
```

- [ ] **Step 2: Run tests to verify they fail**

Run:

```bash
uv run pytest tests/test_current_orders_service.py -q
```

Expected: FAIL because Toss collection is not wired.

- [ ] **Step 3: Implement Toss normalizer and collection**

Extend `app/services/current_orders_service.py`:

```python
from app.services.brokers.toss.client import TossReadClient
from app.services.brokers.toss.dto import TossOrder


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
```

Change the `CurrentOrdersService.__init__` default:

```python
toss_client_factory: Callable[[], Any] | None = _default_toss_client,
```

Change `list_open_orders` task assembly so Toss is included for KR/US:

```python
if market in ("all", "kr", "us"):
    tasks.append(self._collect_toss_equities(target_market=market))
```

Add this method:

```python
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
                _source(broker="toss", market=market, status="unavailable", fetched_at=None, count=0, message="toss_client_unavailable")
                for market in markets
            ]
            return [], states

        client: Any | None = None
        try:
            client = self._toss_client_factory()
            cursor: str | None = None
            rows: list[OpenOrderRow] = []
            while True:
                page = await client.list_orders(status="OPEN", cursor=cursor)
                rows.extend(normalize_toss_order(order) for order in page.orders)
                if not page.has_next or not page.next_cursor:
                    break
                cursor = page.next_cursor
        except Exception as exc:  # noqa: BLE001
            logger.warning("Toss open-order fetch failed", exc_info=True)
            states = [
                _source(broker="toss", market=market, status="unavailable", fetched_at=now, count=0, message=f"{type(exc).__name__}: {exc}")
                for market in markets
            ]
            return [], states
        finally:
            close = getattr(client, "aclose", None)
            if callable(close):
                await close()

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
```

Then update the result merge loop in `list_open_orders` to flatten one or many source states:

```python
for result_rows, result_sources in results:
    rows.extend(result_rows)
    if isinstance(result_sources, list):
        sources.extend(result_sources)
    else:
        sources.append(result_sources)
```

- [ ] **Step 4: Run Toss tests**

Run:

```bash
uv run pytest tests/test_current_orders_service.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/current_orders_service.py tests/test_current_orders_service.py
git commit -m "test: include toss current open orders"
```

---

### Task 5: Authenticated `/trading/api/invest/open-orders` Endpoint

**Files:**
- Create: `app/routers/invest_open_orders.py`
- Modify: `app/main.py`
- Test: `tests/routers/test_invest_open_orders_router.py`

- [ ] **Step 1: Write failing router tests**

Create `tests/routers/test_invest_open_orders_router.py`:

```python
from __future__ import annotations

import datetime as dt
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.schemas.open_orders import OpenOrdersResponse


class _StubCurrentOrdersService:
    def __init__(self) -> None:
        self.calls: list[str] = []

    async def list_open_orders(self, *, market: str = "all") -> OpenOrdersResponse:
        self.calls.append(market)
        return OpenOrdersResponse(
            market=market,  # type: ignore[arg-type]
            count=0,
            data_state="ok",
            as_of=dt.datetime(2026, 6, 15, 0, 0, tzinfo=dt.UTC),
            items=[],
            sources=[],
            warnings=[],
            empty_reason="no open orders for the selected market",
        )


def _make_client(service: _StubCurrentOrdersService) -> TestClient:
    from app.routers import invest_open_orders
    from app.routers.dependencies import get_authenticated_user

    app = FastAPI()
    app.include_router(invest_open_orders.router)
    app.dependency_overrides[get_authenticated_user] = lambda: SimpleNamespace(id=1)
    app.dependency_overrides[invest_open_orders.get_current_orders_service] = lambda: service
    return TestClient(app)


@pytest.mark.unit
def test_open_orders_endpoint_defaults_to_all() -> None:
    service = _StubCurrentOrdersService()
    client = _make_client(service)

    response = client.get("/trading/api/invest/open-orders")

    assert response.status_code == 200
    assert response.json()["market"] == "all"
    assert service.calls == ["all"]


@pytest.mark.unit
def test_open_orders_endpoint_accepts_market_filter() -> None:
    service = _StubCurrentOrdersService()
    client = _make_client(service)

    response = client.get("/trading/api/invest/open-orders?market=crypto")

    assert response.status_code == 200
    assert response.json()["market"] == "crypto"
    assert service.calls == ["crypto"]


@pytest.mark.unit
def test_open_orders_endpoint_rejects_unknown_market() -> None:
    service = _StubCurrentOrdersService()
    client = _make_client(service)

    response = client.get("/trading/api/invest/open-orders?market=paper")

    assert response.status_code == 422
    assert service.calls == []
```

- [ ] **Step 2: Run router tests to verify they fail**

Run:

```bash
uv run pytest tests/routers/test_invest_open_orders_router.py -q
```

Expected: FAIL because `app.routers.invest_open_orders` does not exist.

- [ ] **Step 3: Implement router**

Create `app/routers/invest_open_orders.py`:

```python
"""Read-only /invest current open-order endpoint (ROB-572)."""

from __future__ import annotations

from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query

from app.models.trading import User
from app.routers.dependencies import get_authenticated_user
from app.schemas.open_orders import OpenOrdersResponse
from app.services.current_orders_service import CurrentOrdersService

router = APIRouter(
    prefix="/trading/api/invest/open-orders",
    tags=["invest-open-orders"],
)

Market = Literal["all", "kr", "us", "crypto"]


def get_current_orders_service() -> CurrentOrdersService:
    return CurrentOrdersService()


@router.get("")
async def list_open_orders(
    _user: Annotated[User, Depends(get_authenticated_user)],
    service: Annotated[CurrentOrdersService, Depends(get_current_orders_service)],
    market: Annotated[Market, Query()] = "all",
) -> OpenOrdersResponse:
    return await service.list_open_orders(market=market)
```

Modify `app/main.py` imports:

```python
    invest_fills,
    invest_open_orders,
    invest_scalping,
```

Modify router includes:

```python
    app.include_router(invest_fills.router)
    app.include_router(invest_open_orders.router)
    app.include_router(invest_app_spa.router)
```

- [ ] **Step 4: Run router and service tests**

Run:

```bash
uv run pytest tests/test_current_orders_service.py tests/routers/test_invest_open_orders_router.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/main.py app/routers/invest_open_orders.py tests/routers/test_invest_open_orders_router.py
git commit -m "test: expose current open orders endpoint"
```

---

### Task 6: Frontend Type and API Wrapper

**Files:**
- Create: `frontend/invest/src/types/currentOrders.ts`
- Create: `frontend/invest/src/api/currentOrders.ts`
- Test: `frontend/invest/src/__tests__/currentOrders.api.test.ts`

- [ ] **Step 1: Write failing API tests**

Create `frontend/invest/src/__tests__/currentOrders.api.test.ts`:

```typescript
import { afterEach, describe, expect, it, vi } from "vitest";

import { fetchCurrentOrders } from "../api/currentOrders";

const originalFetch = global.fetch;

afterEach(() => {
  global.fetch = originalFetch;
});

describe("fetchCurrentOrders", () => {
  it("calls the open-orders endpoint with credentials and default market", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ market: "all", count: 0, items: [], sources: [], warnings: [] }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    await fetchCurrentOrders();

    expect(fetchMock).toHaveBeenCalledWith(
      "/trading/api/invest/open-orders?market=all",
      { credentials: "include" },
    );
  });

  it("passes the selected market", async () => {
    const fetchMock = vi.fn().mockResolvedValueOnce({
      ok: true,
      status: 200,
      json: async () => ({ market: "crypto", count: 0, items: [], sources: [], warnings: [] }),
    });
    global.fetch = fetchMock as unknown as typeof fetch;

    await fetchCurrentOrders("crypto");

    expect(String(fetchMock.mock.calls[0]?.[0])).toContain("market=crypto");
  });

  it("throws on non-ok responses", async () => {
    global.fetch = vi.fn().mockResolvedValueOnce({
      ok: false,
      status: 500,
    }) as unknown as typeof fetch;

    await expect(fetchCurrentOrders("kr")).rejects.toThrow("open-orders 500");
  });
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd frontend/invest && npm test -- currentOrders.api.test.ts
```

Expected: FAIL because the API module does not exist.

- [ ] **Step 3: Create types and API wrapper**

Create `frontend/invest/src/types/currentOrders.ts`:

```typescript
export type CurrentOrdersMarket = "all" | "kr" | "us" | "crypto";
export type CurrentOrderRowMarket = "kr" | "us" | "crypto";
export type CurrentOrderBroker = "kis" | "toss" | "upbit";
export type CurrentOrderSide = "buy" | "sell" | "unknown";
export type CurrentOrdersDataState = "ok" | "degraded" | "unavailable";

export interface CurrentOrderRow {
  broker: CurrentOrderBroker;
  market: CurrentOrderRowMarket;
  symbol: string;
  symbol_name: string | null;
  side: CurrentOrderSide;
  order_type: string | null;
  time_in_force: string | null;
  price: string | null;
  quantity: string | null;
  remaining_qty: string | null;
  filled_qty: string | null;
  status: string;
  raw_status: string | null;
  ordered_at: string | null;
  order_no: string;
  exchange: string | null;
  currency: string | null;
}

export interface CurrentOrderSourceState {
  broker: CurrentOrderBroker;
  market: CurrentOrderRowMarket;
  status: CurrentOrdersDataState;
  fetched_at: string | null;
  count: number;
  message: string | null;
}

export interface CurrentOrdersResponse {
  market: CurrentOrdersMarket;
  count: number;
  data_state: CurrentOrdersDataState;
  as_of: string;
  items: CurrentOrderRow[];
  sources: CurrentOrderSourceState[];
  warnings: string[];
  empty_reason: string | null;
}
```

Create `frontend/invest/src/api/currentOrders.ts`:

```typescript
import type { CurrentOrdersMarket, CurrentOrdersResponse } from "../types/currentOrders";

const BASE = "/trading/api/invest/open-orders";

export async function fetchCurrentOrders(
  market: CurrentOrdersMarket = "all",
): Promise<CurrentOrdersResponse> {
  const q = new URLSearchParams({ market });
  const res = await fetch(`${BASE}?${q}`, { credentials: "include" });
  if (!res.ok) throw new Error(`open-orders ${res.status}`);
  return res.json();
}
```

- [ ] **Step 4: Run API tests**

Run:

```bash
cd frontend/invest && npm test -- currentOrders.api.test.ts
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/types/currentOrders.ts frontend/invest/src/api/currentOrders.ts frontend/invest/src/__tests__/currentOrders.api.test.ts
git commit -m "test: add current orders frontend api"
```

---

### Task 7: Current Orders Panel

**Files:**
- Create: `frontend/invest/src/components/my/CurrentOrdersPanel.tsx`
- Test: `frontend/invest/src/__tests__/CurrentOrdersPanel.test.tsx`

- [ ] **Step 1: Write failing panel tests**

Create `frontend/invest/src/__tests__/CurrentOrdersPanel.test.tsx`:

```tsx
import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, beforeEach, expect, test, vi } from "vitest";

import { CurrentOrdersPanel } from "../components/my/CurrentOrdersPanel";

const fetchMock = vi.fn();

const baseResponse = {
  market: "all",
  count: 2,
  data_state: "degraded",
  as_of: "2026-06-15T00:00:00Z",
  warnings: ["kis/us: NYSE=RuntimeError: down"],
  empty_reason: null,
  sources: [
    { broker: "kis", market: "kr", status: "ok", fetched_at: "2026-06-15T00:00:00Z", count: 1, message: null },
    { broker: "kis", market: "us", status: "degraded", fetched_at: "2026-06-15T00:00:00Z", count: 0, message: "NYSE=RuntimeError: down" },
  ],
  items: [
    {
      broker: "kis",
      market: "kr",
      symbol: "005930",
      symbol_name: "삼성전자",
      side: "buy",
      order_type: "지정가",
      time_in_force: null,
      price: "70000",
      quantity: "10",
      remaining_qty: "8",
      filled_qty: "2",
      status: "pending",
      raw_status: "접수",
      ordered_at: "2026-06-15T09:01:00+09:00",
      order_no: "K123456789",
      exchange: "KRX",
      currency: "KRW",
    },
    {
      broker: "upbit",
      market: "crypto",
      symbol: "KRW-BTC",
      symbol_name: null,
      side: "sell",
      order_type: "limit",
      time_in_force: null,
      price: "99000000",
      quantity: "0.02",
      remaining_qty: "0.02",
      filled_qty: null,
      status: "pending",
      raw_status: "wait",
      ordered_at: "2026-06-15T00:01:00Z",
      order_no: "UP123456789",
      exchange: "UPBIT",
      currency: "KRW",
    },
  ],
};

beforeEach(() => {
  fetchMock.mockReset();
  fetchMock.mockResolvedValue({ ok: true, json: async () => baseResponse });
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
});

test("CurrentOrdersPanel renders broker rows and degraded source warning", async () => {
  render(<CurrentOrdersPanel />);

  expect(await screen.findByText("삼성전자")).toBeInTheDocument();
  expect(screen.getByText("KRW-BTC")).toBeInTheDocument();
  expect(screen.getByText("현재 주문")).toBeInTheDocument();
  expect(screen.getByText("KIS")).toBeInTheDocument();
  expect(screen.getByText("UPBIT")).toBeInTheDocument();
  expect(screen.getByText(/kis\/us/)).toBeInTheDocument();
  expect(screen.getAllByText("미체결").length).toBeGreaterThan(0);
  expect(screen.queryByRole("button", { name: /취소|정정/ })).not.toBeInTheDocument();
});

test("CurrentOrdersPanel refetches with market filter", async () => {
  render(<CurrentOrdersPanel />);
  await screen.findByText("삼성전자");

  await userEvent.click(screen.getByRole("button", { name: "코인" }));

  await waitFor(() => expect(fetchMock).toHaveBeenCalledTimes(2));
  expect(String(fetchMock.mock.calls[1]?.[0])).toContain("market=crypto");
});

test("CurrentOrdersPanel renders empty reason", async () => {
  fetchMock.mockResolvedValue({
    ok: true,
    json: async () => ({
      ...baseResponse,
      count: 0,
      data_state: "ok",
      items: [],
      warnings: [],
      empty_reason: "no open orders for the selected market",
    }),
  });

  render(<CurrentOrdersPanel compact />);

  expect(await screen.findByText("no open orders for the selected market")).toBeInTheDocument();
});
```

- [ ] **Step 2: Run panel tests to verify they fail**

Run:

```bash
cd frontend/invest && npm test -- CurrentOrdersPanel.test.tsx
```

Expected: FAIL because the component does not exist.

- [ ] **Step 3: Implement panel**

Create `frontend/invest/src/components/my/CurrentOrdersPanel.tsx`:

```tsx
import { useEffect, useMemo, useState } from "react";

import { fetchCurrentOrders } from "../../api/currentOrders";
import { Pill } from "../../ds";
import {
  LINKED_ORDER_STATUS_LABELS,
  LINKED_ORDER_STATUS_TONES,
} from "../orders/LinkedOrderRow";
import type {
  CurrentOrderRow,
  CurrentOrdersMarket,
  CurrentOrdersResponse,
} from "../../types/currentOrders";

const MARKET_OPTIONS: { key: CurrentOrdersMarket; label: string }[] = [
  { key: "all", label: "전체" },
  { key: "kr", label: "국내" },
  { key: "us", label: "미국" },
  { key: "crypto", label: "코인" },
];

const BROKER_LABEL: Record<string, string> = {
  kis: "KIS",
  toss: "TOSS",
  upbit: "UPBIT",
};

const MARKET_LABEL: Record<string, string> = {
  kr: "국내",
  us: "미국",
  crypto: "코인",
};

function toNumber(value: string | number | null | undefined): number | null {
  if (value == null || value === "") return null;
  const parsed = typeof value === "number" ? value : Number(value);
  return Number.isFinite(parsed) ? parsed : null;
}

function formatMoney(value: string | number | null | undefined, currency: string | null): string {
  const n = toNumber(value);
  if (n == null) return "—";
  if (currency === "USD") {
    return `$${n.toLocaleString("en-US", { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  if (currency === "KRW") return `₩${Math.round(n).toLocaleString("ko-KR")}`;
  return `${n.toLocaleString("ko-KR")} ${currency ?? ""}`.trim();
}

function formatQty(value: string | number | null | undefined, market: string): string {
  const n = toNumber(value);
  if (n == null) return "—";
  return market === "crypto"
    ? n.toLocaleString("ko-KR", { maximumFractionDigits: 8 })
    : `${n.toLocaleString("ko-KR", { maximumFractionDigits: 4 })}주`;
}

function formatDateTime(value: string | null): string {
  if (!value) return "—";
  const dt = new Date(value);
  if (Number.isNaN(dt.getTime())) return value;
  return new Intl.DateTimeFormat("ko-KR", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
    timeZone: "Asia/Seoul",
  }).format(dt);
}

function sideLabel(side: CurrentOrderRow["side"]): string {
  if (side === "buy") return "매수";
  if (side === "sell") return "매도";
  return "확인";
}

function symbolName(row: CurrentOrderRow): string {
  return row.symbol_name && row.symbol_name !== row.symbol ? row.symbol_name : row.symbol;
}

function sourceSummary(data: CurrentOrdersResponse): string {
  return data.sources
    .map((source) => `${BROKER_LABEL[source.broker] ?? source.broker}/${MARKET_LABEL[source.market]} ${source.count}`)
    .join(" · ");
}

export function CurrentOrdersPanel({ compact = false }: { compact?: boolean }) {
  const [market, setMarket] = useState<CurrentOrdersMarket>("all");
  const [state, setState] = useState<
    | { status: "loading" }
    | { status: "ready"; data: CurrentOrdersResponse }
    | { status: "error"; message: string }
  >({ status: "loading" });

  useEffect(() => {
    let cancelled = false;
    setState({ status: "loading" });
    fetchCurrentOrders(market)
      .then((data) => {
        if (!cancelled) setState({ status: "ready", data });
      })
      .catch((err: unknown) => {
        if (!cancelled) setState({ status: "error", message: err instanceof Error ? err.message : String(err) });
      });
    return () => {
      cancelled = true;
    };
  }, [market]);

  const rows = useMemo(() => (state.status === "ready" ? state.data.items : []), [state]);
  const dataState = state.status === "ready" ? state.data.data_state : null;

  return (
    <section
      data-testid="current-orders-panel"
      style={{
        border: "1px solid var(--border)",
        borderRadius: 16,
        background: "var(--surface)",
        overflow: "hidden",
      }}
    >
      <div
        style={{
          display: "flex",
          alignItems: compact ? "flex-start" : "center",
          justifyContent: "space-between",
          gap: 12,
          padding: compact ? "14px 14px 10px" : "16px 18px 12px",
          flexDirection: compact ? "column" : "row",
        }}
      >
        <div>
          <div style={{ display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" }}>
            <h2 style={{ margin: 0, fontSize: compact ? 16 : 18 }}>현재 주문</h2>
            {dataState && (
              <Pill tone={dataState === "ok" ? "accent" : dataState === "degraded" ? "warn" : "loss"} size="sm">
                {dataState === "ok" ? "정상" : dataState === "degraded" ? "부분 지연" : "확인 불가"}
              </Pill>
            )}
          </div>
          <p style={{ margin: "4px 0 0", fontSize: 12, color: "var(--fg-3)" }}>
            KIS/Toss/Upbit 라이브 API 기준 미체결·대기 주문입니다.
          </p>
          {state.status === "ready" && state.data.sources.length > 0 && (
            <p style={{ margin: "4px 0 0", fontSize: 11, color: "var(--fg-3)" }}>
              출처 {sourceSummary(state.data)}
            </p>
          )}
        </div>
        <div style={{ display: "flex", gap: 6, flexWrap: "wrap" }}>
          {MARKET_OPTIONS.map((option) => {
            const active = market === option.key;
            return (
              <button
                key={option.key}
                type="button"
                onClick={() => setMarket(option.key)}
                style={{
                  border: "none",
                  borderRadius: 999,
                  padding: "6px 10px",
                  fontSize: 12,
                  fontWeight: 700,
                  cursor: "pointer",
                  fontFamily: "inherit",
                  background: active ? "var(--fg)" : "var(--surface-2)",
                  color: active ? "var(--bg)" : "var(--fg-2)",
                }}
              >
                {option.label}
              </button>
            );
          })}
        </div>
      </div>

      {state.status === "ready" && state.data.warnings.length > 0 && (
        <div role="alert" style={{ margin: "0 14px 12px", padding: "8px 10px", borderRadius: 10, background: "var(--warn-soft)", color: "var(--warn)", fontSize: 12 }}>
          {state.data.warnings.join(" · ")}
        </div>
      )}

      {state.status === "loading" && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>현재 주문을 불러오는 중…</div>
      )}

      {state.status === "error" && (
        <div role="alert" style={{ padding: 16, color: "var(--danger)", fontSize: 13 }}>
          현재 주문을 불러오지 못했습니다. {state.message}
        </div>
      )}

      {state.status === "ready" && rows.length === 0 && (
        <div style={{ padding: 24, color: "var(--fg-3)", fontSize: 13, textAlign: "center" }}>
          {state.data.empty_reason ?? "현재 미체결 주문이 없습니다."}
        </div>
      )}

      {state.status === "ready" && rows.length > 0 && (
        <div style={{ overflowX: "auto" }}>
          <table style={{ width: "100%", borderCollapse: "collapse", minWidth: compact ? 0 : 860 }}>
            <thead>
              <tr style={{ color: "var(--fg-3)", fontSize: 11, textAlign: "left" }}>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>주문</th>
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>종목</th>
                {!compact && <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>수량</th>}
                <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)", textAlign: "right" }}>가격</th>
                {!compact && <th style={{ padding: "8px 14px", borderTop: "1px solid var(--divider)", borderBottom: "1px solid var(--divider)" }}>브로커</th>}
              </tr>
            </thead>
            <tbody>
              {rows.map((row) => (
                <tr key={`${row.broker}:${row.market}:${row.order_no}`}>
                  <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)" }}>
                    <div style={{ display: "flex", gap: 6, flexWrap: "wrap", alignItems: "center" }}>
                      <Pill tone={LINKED_ORDER_STATUS_TONES[row.status] ?? "paper"} size="sm">
                        {LINKED_ORDER_STATUS_LABELS[row.status] ?? row.status}
                      </Pill>
                      <span style={{ fontSize: 13, fontWeight: 800 }}>{sideLabel(row.side)}</span>
                    </div>
                    <div style={{ marginTop: 3, fontSize: 11, color: "var(--fg-3)" }}>
                      {formatDateTime(row.ordered_at)} · order {row.order_no.slice(0, 8)}
                    </div>
                  </td>
                  <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)" }}>
                    <div style={{ fontSize: 13, fontWeight: 800 }}>{symbolName(row)}</div>
                    <div style={{ marginTop: 2, fontSize: 11, color: "var(--fg-3)" }}>
                      {row.symbol} · {MARKET_LABEL[row.market]}{row.exchange ? ` · ${row.exchange}` : ""}
                    </div>
                  </td>
                  {!compact && (
                    <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13 }}>
                      {formatQty(row.remaining_qty, row.market)} / {formatQty(row.quantity, row.market)}
                    </td>
                  )}
                  <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 13, textAlign: "right", fontFeatureSettings: '"tnum"' }}>
                    {formatMoney(row.price, row.currency)}
                  </td>
                  {!compact && (
                    <td style={{ padding: "10px 14px", borderBottom: "1px solid var(--divider)", fontSize: 12, color: "var(--fg-3)" }}>
                      {BROKER_LABEL[row.broker] ?? row.broker}
                    </td>
                  )}
                </tr>
              ))}
            </tbody>
          </table>
          <div style={{ padding: "8px 14px", fontSize: 11, color: "var(--fg-3)" }}>
            총 {state.data.count.toLocaleString("ko-KR")}건
          </div>
        </div>
      )}
    </section>
  );
}
```

- [ ] **Step 4: Run panel tests**

Run:

```bash
cd frontend/invest && npm test -- CurrentOrdersPanel.test.tsx
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add frontend/invest/src/components/my/CurrentOrdersPanel.tsx frontend/invest/src/__tests__/CurrentOrdersPanel.test.tsx
git commit -m "test: render current orders panel"
```

---

### Task 8: Wire `/my` Portfolio Tabs

**Files:**
- Modify: `frontend/invest/src/components/my/portfolioTabs.ts`
- Modify: `frontend/invest/src/pages/desktop/DesktopPortfolioPage.tsx`
- Modify: `frontend/invest/src/pages/mobile/MobilePortfolioPage.tsx`
- Test: `frontend/invest/src/__tests__/CurrentOrdersPanel.test.tsx`

- [ ] **Step 1: Add tab parsing coverage**

Append to `frontend/invest/src/__tests__/CurrentOrdersPanel.test.tsx`:

```tsx
import { MemoryRouter } from "react-router-dom";
import { PORTFOLIO_TABS, usePortfolioTabSearchParam } from "../components/my/portfolioTabs";

function TabProbe() {
  const [activeTab, setActiveTab] = usePortfolioTabSearchParam();
  return (
    <>
      <div data-testid="active-tab">{activeTab}</div>
      <button type="button" onClick={() => setActiveTab("currentOrders")}>set current</button>
    </>
  );
}

test("portfolio tabs include current orders and parse the search param", async () => {
  expect(PORTFOLIO_TABS.map((tab) => tab.key)).toContain("currentOrders");
  render(
    <MemoryRouter basename="/invest" initialEntries={["/invest/my?tab=currentOrders"]}>
      <TabProbe />
    </MemoryRouter>,
  );
  expect(screen.getByTestId("active-tab")).toHaveTextContent("currentOrders");
});
```

- [ ] **Step 2: Run test to verify it fails**

Run:

```bash
cd frontend/invest && npm test -- CurrentOrdersPanel.test.tsx
```

Expected: FAIL because `currentOrders` is not in `PortfolioTab`.

- [ ] **Step 3: Update shared tab model**

Modify `frontend/invest/src/components/my/portfolioTabs.ts`:

```typescript
export type PortfolioTab = "holdings" | "signals" | "sellHistory" | "currentOrders";

export const PORTFOLIO_TABS: { key: PortfolioTab; label: string }[] = [
  { key: "holdings", label: "보유 현황" },
  { key: "signals", label: "시그널" },
  { key: "sellHistory", label: "매도 이력" },
  { key: "currentOrders", label: "현재 주문" },
];

function parsePortfolioTab(value: string | null): PortfolioTab {
  return value === "signals" || value === "sellHistory" || value === "currentOrders"
    ? value
    : "holdings";
}
```

- [ ] **Step 4: Wire desktop page**

Modify imports in `frontend/invest/src/pages/desktop/DesktopPortfolioPage.tsx`:

```typescript
import { CurrentOrdersPanel } from "../../components/my/CurrentOrdersPanel";
```

Replace title ternary with a helper near `DesktopPortfolioPage`:

```typescript
function portfolioTitle(tab: PortfolioTab): string {
  if (tab === "holdings") return "통합 보유 현황";
  if (tab === "signals") return "내 투자 시그널";
  if (tab === "currentOrders") return "현재 주문";
  return "매도 이력";
}

function portfolioDescription(tab: PortfolioTab): string {
  if (tab === "holdings") return "KIS, Toss/manual, 모의/수동 계좌를 한 화면에서 비교하고 종목별 출처를 확인합니다.";
  if (tab === "signals") return "보유·관심 종목과 시장별 AI 분석 시그널을 내 투자 화면에서 함께 확인합니다.";
  if (tab === "currentOrders") return "KIS/Toss/Upbit 실계좌의 현재 미체결·대기 주문을 읽기 전용으로 확인합니다.";
  return "KIS/Upbit 체결 보정 ledger 기준 최근 매도 체결을 별도 화면에서 확인합니다.";
}
```

Use the helpers:

```tsx
<h1 style={{ margin: 0, fontSize: 26, lineHeight: 1.2, letterSpacing: "-0.03em" }}>
  {portfolioTitle(activeTab)}
</h1>
<p style={{ margin: 0, color: "var(--fg-3)", fontSize: 13 }}>
  {portfolioDescription(activeTab)}
</p>
```

Update the render branch:

```tsx
{activeTab === "holdings" ? (
  <>
    ...
  </>
) : activeTab === "signals" ? (
  <SignalsPanel />
) : activeTab === "currentOrders" ? (
  <CurrentOrdersPanel />
) : (
  <SellHistoryPanel />
)}
```

- [ ] **Step 5: Wire mobile page**

Modify imports in `frontend/invest/src/pages/mobile/MobilePortfolioPage.tsx`:

```typescript
import { CurrentOrdersPanel } from "../../components/my/CurrentOrdersPanel";
```

Update the branch near the existing `SellHistoryPanel compact` branch:

```tsx
) : activeTab === "signals" ? (
  <section style={{ padding: "0 16px" }}>
    <SignalsPanel compact />
  </section>
) : activeTab === "currentOrders" ? (
  <section style={{ padding: "0 16px" }}>
    <CurrentOrdersPanel compact />
  </section>
) : (
  <section style={{ padding: "0 16px" }}>
    <SellHistoryPanel compact />
  </section>
)}
```

- [ ] **Step 6: Run frontend focused tests**

Run:

```bash
cd frontend/invest && npm test -- CurrentOrdersPanel.test.tsx SellHistoryPanel.test.tsx DesktopSignalsPage.test.tsx
```

Expected: PASS.

- [ ] **Step 7: Commit**

```bash
git add frontend/invest/src/components/my/portfolioTabs.ts frontend/invest/src/pages/desktop/DesktopPortfolioPage.tsx frontend/invest/src/pages/mobile/MobilePortfolioPage.tsx frontend/invest/src/__tests__/CurrentOrdersPanel.test.tsx
git commit -m "test: wire current orders into my portfolio tabs"
```

---

### Task 9: Verification and Cleanup

**Files:**
- Modify only if failures require narrow fixes.

- [ ] **Step 1: Run backend focused tests**

Run:

```bash
uv run pytest tests/test_current_orders_service.py tests/routers/test_invest_open_orders_router.py -q
```

Expected: PASS.

- [ ] **Step 2: Run frontend focused tests**

Run:

```bash
cd frontend/invest && npm test -- currentOrders.api.test.ts CurrentOrdersPanel.test.tsx SellHistoryPanel.test.tsx
```

Expected: PASS.

- [ ] **Step 3: Run lint/type checks for touched surfaces**

Run:

```bash
uv run ruff check app/schemas/open_orders.py app/services/current_orders_service.py app/routers/invest_open_orders.py tests/test_current_orders_service.py tests/routers/test_invest_open_orders_router.py
cd frontend/invest && npm run typecheck
```

Expected: PASS.

- [ ] **Step 4: Manual smoke with stubbed or configured credentials**

If broker credentials are available, run:

```bash
make dev
```

Then visit:

```text
http://localhost:8000/invest/my?tab=currentOrders
```

Expected:
- Page loads without a full-page error.
- "현재 주문" tab is selected.
- With no open orders, the panel shows the empty reason.
- If a broker is disabled/unavailable, the panel shows a degraded/unavailable warning while still rendering other broker rows.
- No cancel/modify/order CTA is present.

- [ ] **Step 5: Final commit**

If any verification fixes were needed:

```bash
git add <fixed-files>
git commit -m "fix: verify current orders view"
```

---

## Self-Review

- Spec coverage: S1 Upbit, S2 KIS KR/US, S3 Toss KR/US, S4 all tab and per-source degradation are covered. S5 NXT fallback remains intentionally deferred.
- Placeholder scan: no unresolved placeholder markers or unspecified validation/error-handling steps. The only deferred item is the Linear-approved S5 follow-up.
- Type consistency: backend uses `data_state` snake_case like `fills`; frontend DTOs mirror it. `market="all"` is response-level only; rows use `kr|us|crypto`. `LinkedOrderRow` is not reused directly because `ledgerId` is not available for live open orders; status maps are reused instead.
