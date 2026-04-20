# Split Symbol Settings Router Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 989-line `app/routers/symbol_settings.py` into 3 domain-specific routers and extract duplicated cost estimation logic into a dedicated service.

**Architecture:** Extract common buy-price-extraction and cost-calculation into `order_estimation_service.py`. Split the router into `symbol_settings.py` (CRUD), `user_defaults.py` (user settings), `order_estimation.py` (cost estimation). All routers share the same `/api/symbol-settings` prefix to preserve API URLs. Shared `get_user_from_request()` moves to the existing `dependencies.py`.

**Tech Stack:** FastAPI, Pydantic, SQLAlchemy (async), pytest

---

## File Structure

### New Files
| File | Responsibility |
|------|---------------|
| `app/services/order_estimation_service.py` | `extract_buy_prices_from_analysis()`, `calculate_estimated_order_cost()` (moved from symbol_trade_settings_service + extended with `amount_based` for crypto), 3 pending-order fetch helpers |
| `app/routers/user_defaults.py` | GET/PUT `/user-defaults` (2 endpoints) |
| `app/routers/order_estimation.py` | 5 `estimated-cost` endpoints |
| `tests/test_order_estimation_service.py` | Tests for new service |

### Modified Files
| File | Change |
|------|--------|
| `app/routers/dependencies.py` | Add `get_user_from_request()` (moved from symbol_settings.py) |
| `app/routers/symbol_settings.py` | Remove moved endpoints/schemas/constants, keep 5 CRUD endpoints only |
| `app/services/symbol_trade_settings_service.py` | Remove `calculate_estimated_order_cost` |
| `app/main.py:35,150` | Import + register `user_defaults` and `order_estimation` routers |
| `tests/test_symbol_trade_settings.py` | Update `calculate_estimated_order_cost` import, fix router endpoint test |

### Endpoint Distribution After Split

| Router File | Endpoints | Count |
|-------------|-----------|-------|
| `user_defaults.py` | GET/PUT `/user-defaults` | 2 |
| `order_estimation.py` | GET `/symbols/domestic/estimated-cost`, `/symbols/overseas/estimated-cost`, `/symbols/all/estimated-cost`, `/symbols/crypto/estimated-cost`, `/symbols/{symbol}/estimated-cost` | 5 |
| `symbol_settings.py` | GET `/symbols`, GET/POST/PUT/DELETE `/symbols/{symbol}` | 5 |

### Critical: Router Registration Order in main.py

`order_estimation.router` **MUST** be registered BEFORE `symbol_settings.router`. Fixed paths like `/symbols/domestic/estimated-cost` must match before the `{symbol}` path parameter catches `"domestic"` as a symbol.

```python
app.include_router(user_defaults.router)        # no path conflicts
app.include_router(order_estimation.router)      # fixed paths: /symbols/domestic/... etc.
app.include_router(symbol_settings.router)       # path param: /symbols/{symbol} — LAST
```

---

## Task 1: Verify Green Baseline

**Files:** (none modified)

- [ ] **Step 1: Run existing tests**

```bash
uv run pytest tests/ -v -k "symbol_settings or user_default or order_estimation"
```

Expected: All tests in `test_symbol_trade_settings.py` PASS.

- [ ] **Step 2: Run lint**

```bash
make lint
```

Expected: PASS (no errors).

- [ ] **Step 3: Record test count for later comparison**

Note the number of passing tests (should be ~17 tests in `TestCalculateEstimatedOrderCost`, `TestSymbolTradeSettingsService`, `TestGetBuyQuantityFunctions`, `TestSymbolTradeSettingsModel`, `TestSymbolSettingsRouter`).

---

## Task 2: Create `order_estimation_service.py` with Tests (TDD)

**Files:**
- Create: `app/services/order_estimation_service.py`
- Create: `tests/test_order_estimation_service.py`

- [ ] **Step 1: Write failing tests for `extract_buy_prices_from_analysis`**

Create `tests/test_order_estimation_service.py`:

```python
"""Tests for Order Estimation Service"""

from decimal import Decimal
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from app.services.order_estimation_service import (
    calculate_estimated_order_cost,
    extract_buy_prices_from_analysis,
    fetch_pending_crypto_buy_cost,
    fetch_pending_domestic_buy_cost,
    fetch_pending_overseas_buy_cost,
)


class TestExtractBuyPrices:
    """extract_buy_prices_from_analysis 테스트"""

    def test_extract_all_four_prices(self):
        """4개 매수 가격 모두 추출"""
        analysis = MagicMock()
        analysis.appropriate_buy_min = Decimal("50000")
        analysis.appropriate_buy_max = Decimal("52000")
        analysis.buy_hope_min = Decimal("48000")
        analysis.buy_hope_max = Decimal("49000")

        result = extract_buy_prices_from_analysis(analysis)

        assert len(result) == 4
        assert result[0] == {"price_name": "appropriate_buy_min", "price": 50000.0}
        assert result[1] == {"price_name": "appropriate_buy_max", "price": 52000.0}
        assert result[2] == {"price_name": "buy_hope_min", "price": 48000.0}
        assert result[3] == {"price_name": "buy_hope_max", "price": 49000.0}

    def test_extract_partial_prices(self):
        """일부 가격만 존재할 때"""
        analysis = MagicMock()
        analysis.appropriate_buy_min = Decimal("50000")
        analysis.appropriate_buy_max = None
        analysis.buy_hope_min = None
        analysis.buy_hope_max = Decimal("49000")

        result = extract_buy_prices_from_analysis(analysis)

        assert len(result) == 2
        assert result[0]["price_name"] == "appropriate_buy_min"
        assert result[1]["price_name"] == "buy_hope_max"

    def test_extract_no_prices(self):
        """가격이 전혀 없을 때"""
        analysis = MagicMock()
        analysis.appropriate_buy_min = None
        analysis.appropriate_buy_max = None
        analysis.buy_hope_min = None
        analysis.buy_hope_max = None

        result = extract_buy_prices_from_analysis(analysis)

        assert result == []


class TestCalculateEstimatedOrderCost:
    """calculate_estimated_order_cost (moved + extended) 테스트"""

    def test_krw_integer_quantity(self):
        """KRW 통화에서 정수 수량"""
        buy_prices = [
            {"price_name": "appropriate_buy_min", "price": 50000},
            {"price_name": "buy_hope_min", "price": 48000},
        ]
        result = calculate_estimated_order_cost(
            symbol="005930",
            buy_prices=buy_prices,
            quantity_per_order=2,
            currency="KRW",
        )
        assert result["symbol"] == "005930"
        assert result["total_orders"] == 2
        assert result["total_quantity"] == 4
        assert result["total_cost"] == (50000 * 2) + (48000 * 2)
        assert result["currency"] == "KRW"
        assert result["buy_prices"][0]["quantity"] == 2

    def test_usd_decimal_quantity(self):
        """USD 통화에서 소수점 수량 유지"""
        buy_prices = [{"price_name": "appropriate_buy_min", "price": 150}]
        result = calculate_estimated_order_cost(
            symbol="AAPL",
            buy_prices=buy_prices,
            quantity_per_order=2.5,
            currency="USD",
        )
        assert result["buy_prices"][0]["quantity"] == 2.5
        assert result["total_cost"] == 375.0

    def test_empty_prices(self):
        """빈 가격 목록"""
        result = calculate_estimated_order_cost(
            symbol="BTC", buy_prices=[], quantity_per_order=1, currency="KRW"
        )
        assert result["total_orders"] == 0
        assert result["total_quantity"] == 0
        assert result["total_cost"] == 0

    def test_amount_based_crypto(self):
        """금액 기반 계산 (암호화폐)"""
        buy_prices = [
            {"price_name": "appropriate_buy_min", "price": 50_000_000},
            {"price_name": "buy_hope_min", "price": 48_000_000},
        ]
        result = calculate_estimated_order_cost(
            symbol="KRW-BTC",
            buy_prices=buy_prices,
            quantity_per_order=10000,  # 10,000 KRW per order
            currency="KRW",
            amount_based=True,
        )
        assert result["total_orders"] == 2
        assert result["total_cost"] == 20000  # 10000 * 2 prices
        assert result["buy_prices"][0]["cost"] == 10000
        assert result["buy_prices"][0]["quantity"] == pytest.approx(
            10000 / 50_000_000
        )

    def test_amount_based_zero_price(self):
        """금액 기반에서 가격이 0일 때 수량 0"""
        buy_prices = [{"price_name": "appropriate_buy_min", "price": 0}]
        result = calculate_estimated_order_cost(
            symbol="KRW-X",
            buy_prices=buy_prices,
            quantity_per_order=10000,
            currency="KRW",
            amount_based=True,
        )
        assert result["buy_prices"][0]["quantity"] == 0
        assert result["buy_prices"][0]["cost"] == 10000


class TestFetchPendingBuyCost:
    """미체결 주문 금액 조회 테스트"""

    @pytest.mark.asyncio
    async def test_fetch_pending_domestic_buy_cost(self):
        """국내 미체결 매수 주문 금액"""
        mock_orders = [
            {"sll_buy_dvsn_cd": "02", "ord_qty": "10", "ord_unpr": "50000"},
            {"sll_buy_dvsn_cd": "01", "ord_qty": "5", "ord_unpr": "60000"},
            {"sll_buy_dvsn_cd": "02", "ord_qty": "3", "ord_unpr": "48000"},
        ]
        with patch(
            "app.services.brokers.kis.client.KISClient"
        ) as MockKIS:
            mock_instance = AsyncMock()
            mock_instance.inquire_korea_orders.return_value = mock_orders
            MockKIS.return_value = mock_instance

            result = await fetch_pending_domestic_buy_cost()

            assert result == (10 * 50000) + (3 * 48000)

    @pytest.mark.asyncio
    async def test_fetch_pending_domestic_buy_cost_error(self):
        """국내 미체결 조회 실패 시 0 반환"""
        with patch(
            "app.services.brokers.kis.client.KISClient"
        ) as MockKIS:
            mock_instance = AsyncMock()
            mock_instance.inquire_korea_orders.side_effect = Exception("API Error")
            MockKIS.return_value = mock_instance

            result = await fetch_pending_domestic_buy_cost()

            assert result == 0.0

    @pytest.mark.asyncio
    async def test_fetch_pending_overseas_buy_cost(self):
        """해외 미체결 매수 주문 금액"""
        mock_orders = [
            {"sll_buy_dvsn_cd": "02", "ft_ord_qty": "5", "ft_ord_unpr3": "150.50"},
            {"sll_buy_dvsn_cd": "01", "ft_ord_qty": "3", "ft_ord_unpr3": "200.00"},
        ]
        with patch(
            "app.services.brokers.kis.client.KISClient"
        ) as MockKIS:
            mock_instance = AsyncMock()
            mock_instance.inquire_overseas_orders.return_value = mock_orders
            MockKIS.return_value = mock_instance

            result = await fetch_pending_overseas_buy_cost()

            assert result == 5 * 150.50

    @pytest.mark.asyncio
    async def test_fetch_pending_crypto_buy_cost_limit_order(self):
        """암호화폐 미체결 지정가 매수 주문"""
        mock_orders = [
            {
                "side": "bid",
                "ord_type": "limit",
                "price": "50000000",
                "remaining_volume": "0.001",
            },
        ]
        with patch(
            "app.services.brokers.upbit.client.fetch_open_orders",
            new_callable=AsyncMock,
            return_value=mock_orders,
        ):
            result = await fetch_pending_crypto_buy_cost()

            assert result == 50000000 * 0.001

    @pytest.mark.asyncio
    async def test_fetch_pending_crypto_buy_cost_market_order(self):
        """암호화폐 미체결 시장가 매수 주문"""
        mock_orders = [
            {"side": "bid", "ord_type": "price", "price": "100000"},
        ]
        with patch(
            "app.services.brokers.upbit.client.fetch_open_orders",
            new_callable=AsyncMock,
            return_value=mock_orders,
        ):
            result = await fetch_pending_crypto_buy_cost()

            assert result == 100000.0
```

- [ ] **Step 2: Run tests to confirm they fail**

```bash
uv run pytest tests/test_order_estimation_service.py -v
```

Expected: `ModuleNotFoundError: No module named 'app.services.order_estimation_service'`

- [ ] **Step 3: Implement `order_estimation_service.py`**

Create `app/services/order_estimation_service.py`:

```python
"""Order Estimation Service — 주문 비용 추정 공통 로직"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_BUY_PRICE_FIELDS = [
    "appropriate_buy_min",
    "appropriate_buy_max",
    "buy_hope_min",
    "buy_hope_max",
]


def extract_buy_prices_from_analysis(analysis: Any) -> list[dict[str, Any]]:
    """분석 결과에서 매수 가격 목록 추출

    Args:
        analysis: StockAnalysisResult 객체 (appropriate_buy_min/max, buy_hope_min/max 속성)

    Returns:
        [{"price_name": "appropriate_buy_min", "price": 50000.0}, ...]
    """
    buy_prices: list[dict[str, Any]] = []
    for field in _BUY_PRICE_FIELDS:
        value = getattr(analysis, field, None)
        if value is not None:
            buy_prices.append({"price_name": field, "price": float(value)})
    return buy_prices


def calculate_estimated_order_cost(
    symbol: str,
    buy_prices: list[dict[str, float]],
    quantity_per_order: float,
    currency: str = "KRW",
    *,
    amount_based: bool = False,
) -> dict[str, Any]:
    """예상 주문 비용 계산

    Args:
        symbol: 종목 코드
        buy_prices: 매수 가격 목록 [{"price_name": "...", "price": 50000}, ...]
        quantity_per_order: 주문당 수량 (amount_based=True일 때는 주문당 금액)
        currency: 통화 (KRW, USD)
        amount_based: True이면 금액 기반 계산 (암호화폐용).
            각 가격대마다 동일 금액(quantity_per_order)을 매수하고,
            수량은 금액/가격으로 역산.

    Returns:
        {
            "symbol": "005930",
            "quantity_per_order": 2,
            "buy_prices": [{"price_name": ..., "price": ..., "quantity": ..., "cost": ...}],
            "total_orders": 2,
            "total_quantity": 4,
            "total_cost": 196000,
            "currency": "KRW"
        }
    """
    result_prices = []
    total_quantity = 0.0
    total_cost = 0.0

    for price_info in buy_prices:
        price = price_info["price"]
        price_name = price_info["price_name"]

        if amount_based:
            qty = quantity_per_order / price if price > 0 else 0
            cost = quantity_per_order
        elif currency == "KRW":
            qty = int(quantity_per_order)
            cost = price * qty
        else:
            qty = quantity_per_order
            cost = price * qty

        result_prices.append(
            {
                "price_name": price_name,
                "price": price,
                "quantity": qty,
                "cost": cost,
            }
        )

        total_quantity += qty
        total_cost += cost

    return {
        "symbol": symbol,
        "quantity_per_order": quantity_per_order,
        "buy_prices": result_prices,
        "total_orders": len(buy_prices),
        "total_quantity": total_quantity,
        "total_cost": total_cost,
        "currency": currency,
    }


async def fetch_pending_domestic_buy_cost() -> float:
    """미체결 국내 매수 주문 총액 조회

    KIS API를 호출하여 국내 미체결 매수 주문의 총 금액을 반환.
    실패 시 0.0 반환 (warning 로그).
    """
    from app.services.brokers.kis.client import KISClient

    try:
        kis = KISClient()
        pending_orders = await kis.inquire_korea_orders()
        cost = 0.0
        for order in pending_orders:
            if order.get("sll_buy_dvsn_cd") == "02":
                qty = int(order.get("ord_qty", 0))
                price = int(order.get("ord_unpr", 0))
                cost += qty * price
        return cost
    except Exception as e:
        logger.warning(f"미체결 주문 조회 실패 (계속 진행): {e}")
        return 0.0


async def fetch_pending_overseas_buy_cost() -> float:
    """미체결 해외 매수 주문 총액 조회

    KIS API를 호출하여 해외(NASD) 미체결 매수 주문의 총 금액을 반환.
    실패 시 0.0 반환 (warning 로그).
    """
    from app.services.brokers.kis.client import KISClient

    try:
        kis = KISClient()
        pending_orders = await kis.inquire_overseas_orders(exchange_code="NASD")
        cost = 0.0
        for order in pending_orders:
            if order.get("sll_buy_dvsn_cd") == "02":
                qty = float(order.get("ft_ord_qty", 0))
                price = float(order.get("ft_ord_unpr3", 0))
                cost += qty * price
        return cost
    except Exception as e:
        logger.warning(f"해외 미체결 주문 조회 실패 (계속 진행): {e}")
        return 0.0


async def fetch_pending_crypto_buy_cost() -> float:
    """미체결 암호화폐 매수 주문 총액 조회

    Upbit API를 호출하여 미체결 매수 주문의 총 금액을 반환.
    시장가(price) 주문: price가 주문 금액.
    지정가(limit) 주문: price * remaining_volume.
    실패 시 0.0 반환 (warning 로그).
    """
    import app.services.brokers.upbit.client as upbit

    try:
        pending_orders = await upbit.fetch_open_orders()
        cost = 0.0
        for order in pending_orders:
            if order.get("side") == "bid":
                ord_type = order.get("ord_type", "")
                if ord_type == "price":
                    cost += float(order.get("price", 0))
                else:
                    price_val = float(order.get("price", 0))
                    remaining = float(order.get("remaining_volume", 0))
                    cost += price_val * remaining
        return cost
    except Exception as e:
        logger.warning(f"Upbit 미체결 주문 조회 실패 (계속 진행): {e}")
        return 0.0
```

- [ ] **Step 4: Run tests to confirm they pass**

```bash
uv run pytest tests/test_order_estimation_service.py -v
```

Expected: All 13 tests PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/order_estimation_service.py tests/test_order_estimation_service.py
git commit -m "feat: create order_estimation_service with extracted common logic

Extract duplicated buy-price extraction and cost calculation into
a single service. Add amount_based mode for crypto support."
```

---

## Task 3: Move `get_user_from_request` to Shared Dependencies

**Files:**
- Modify: `app/routers/dependencies.py:1-30`

- [ ] **Step 1: Add `get_user_from_request` to `dependencies.py`**

Append the following function to `app/routers/dependencies.py` (after `get_authenticated_user`):

```python


async def get_user_from_request(
    request: Request,
    db: AsyncSession = Depends(get_db),
) -> User:
    """웹 세션 또는 API 토큰에서 사용자 조회 (symbol-settings 전용)"""
    if hasattr(request.state, "user") and request.state.user:
        return request.state.user

    user = await get_current_user_from_session(request, db)
    if user:
        return user

    raise HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Authentication required",
    )
```

- [ ] **Step 2: Run lint to verify**

```bash
make lint
```

Expected: PASS.

- [ ] **Step 3: Commit**

```bash
git add app/routers/dependencies.py
git commit -m "refactor: move get_user_from_request to shared dependencies"
```

---

## Task 4: Create `user_defaults.py` Router

**Files:**
- Create: `app/routers/user_defaults.py`

This router handles GET/PUT `/api/symbol-settings/user-defaults`. Code is moved from `symbol_settings.py:143-262` with schemas from lines 143-179.

- [ ] **Step 1: Create `app/routers/user_defaults.py`**

```python
"""User Trade Defaults Router — 사용자 기본 거래 설정 API"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.routers.dependencies import get_user_from_request
from app.services.symbol_trade_settings_service import UserTradeDefaultsService

router = APIRouter(prefix="/api/symbol-settings", tags=["symbol-settings"])

USER_DEFAULTS_UPDATABLE_FIELDS = {
    "crypto_default_buy_amount",
    "crypto_min_order_amount",
    "equity_kr_default_buy_quantity",
    "equity_us_default_buy_quantity",
    "equity_us_default_buy_amount",
}


class UserTradeDefaultsUpdate(BaseModel):
    """사용자 기본 설정 업데이트 요청"""

    crypto_default_buy_amount: float | None = Field(
        None, gt=0, description="암호화폐 기본 매수 금액 (KRW)"
    )
    crypto_min_order_amount: float | None = Field(
        None, gt=0, description="암호화폐 최소 주문 금액 (KRW)"
    )
    equity_kr_default_buy_quantity: float | None = Field(
        None, ge=0, description="국내주식 기본 매수 수량 (0이면 매수 안함)"
    )
    equity_us_default_buy_quantity: float | None = Field(
        None, ge=0, description="해외주식 기본 매수 수량 (0이면 매수 안함)"
    )
    equity_us_default_buy_amount: float | None = Field(
        None, ge=0, description="해외주식 기본 매수 금액 (USD, 0이면 매수 안함)"
    )


class UserTradeDefaultsResponse(BaseModel):
    """사용자 기본 설정 응답"""

    id: int
    user_id: int
    crypto_default_buy_amount: float
    crypto_min_order_amount: float
    equity_kr_default_buy_quantity: float | None
    equity_us_default_buy_quantity: float | None
    equity_us_default_buy_amount: float | None
    is_active: bool
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


def _build_defaults_response(defaults) -> UserTradeDefaultsResponse:
    """UserTradeDefaults 모델 → 응답 변환"""
    return UserTradeDefaultsResponse(
        id=defaults.id,
        user_id=defaults.user_id,
        crypto_default_buy_amount=float(defaults.crypto_default_buy_amount),
        crypto_min_order_amount=float(defaults.crypto_min_order_amount),
        equity_kr_default_buy_quantity=float(defaults.equity_kr_default_buy_quantity)
        if defaults.equity_kr_default_buy_quantity
        else None,
        equity_us_default_buy_quantity=float(defaults.equity_us_default_buy_quantity)
        if defaults.equity_us_default_buy_quantity
        else None,
        equity_us_default_buy_amount=float(defaults.equity_us_default_buy_amount)
        if defaults.equity_us_default_buy_amount
        else None,
        is_active=defaults.is_active,
        created_at=str(defaults.created_at),
        updated_at=str(defaults.updated_at),
    )


@router.get("/user-defaults", response_model=UserTradeDefaultsResponse)
async def get_user_defaults(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 기본 거래 설정 조회"""
    user = await get_user_from_request(request, db)
    service = UserTradeDefaultsService(db)
    defaults = await service.get_or_create(user.id)
    return _build_defaults_response(defaults)


@router.put("/user-defaults", response_model=UserTradeDefaultsResponse)
async def update_user_defaults(
    request_data: UserTradeDefaultsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 기본 거래 설정 업데이트"""
    user = await get_user_from_request(request, db)
    service = UserTradeDefaultsService(db)

    update_data = {k: v for k, v in request_data.model_dump().items() if v is not None}

    invalid_fields = set(update_data) - USER_DEFAULTS_UPDATABLE_FIELDS
    if invalid_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid fields for update: {', '.join(sorted(invalid_fields))}",
        )

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    defaults = await service.update_settings(user.id, update_data)
    return _build_defaults_response(defaults)
```

Note: The duplicated `UserTradeDefaultsResponse(...)` construction in the original (lines 197-214 and 245-262) is DRY-ed into `_build_defaults_response()`.

- [ ] **Step 2: Verify file parses correctly**

```bash
uv run python -c "from app.routers.user_defaults import router; print(f'Routes: {len(router.routes)}')"
```

Expected: `Routes: 2`

- [ ] **Step 3: Commit**

```bash
git add app/routers/user_defaults.py
git commit -m "feat: create user_defaults router (not yet registered)"
```

---

## Task 5: Create `order_estimation.py` Router

**Files:**
- Create: `app/routers/order_estimation.py`

This router handles all 5 estimated-cost endpoints (moved from `symbol_settings.py:301-401, 403-503, 506-583, 586-749, 915-989`). Uses `extract_buy_prices_from_analysis` and `calculate_estimated_order_cost` from the new service instead of duplicated inline logic.

- [ ] **Step 1: Create `app/routers/order_estimation.py`**

```python
"""Order Estimation Router — 주문 비용 추정 API"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import InstrumentType
from app.routers.dependencies import get_user_from_request
from app.services.order_estimation_service import (
    calculate_estimated_order_cost,
    extract_buy_prices_from_analysis,
    fetch_pending_crypto_buy_cost,
    fetch_pending_domestic_buy_cost,
    fetch_pending_overseas_buy_cost,
)
from app.services.stock_info_service import StockAnalysisService
from app.services.symbol_trade_settings_service import (
    SymbolTradeSettingsService,
    UserTradeDefaultsService,
)
from app.services.upbit_symbol_universe_service import get_active_upbit_base_currencies

router = APIRouter(prefix="/api/symbol-settings", tags=["symbol-settings"])


class EstimatedCostResponse(BaseModel):
    """예상 비용 응답"""

    symbol: str
    quantity_per_order: float
    buy_prices: list[dict]
    total_orders: int
    total_quantity: float
    total_cost: float
    currency: str


class AllEstimatedCostResponse(BaseModel):
    """전체 예상 비용 응답"""

    symbols: list[EstimatedCostResponse]
    grand_total_cost: float
    total_symbols: int
    pending_buy_orders_cost: float = 0.0
    net_estimated_cost: float = 0.0


async def _estimate_costs_for_settings(
    settings_list,
    analysis_service: StockAnalysisService,
    currency: str,
) -> tuple[list[EstimatedCostResponse], float]:
    """설정 목록에 대한 비용 추정 공통 루프

    Returns:
        (results, grand_total)
    """
    results = []
    grand_total = 0.0

    for settings_obj in settings_list:
        analysis = await analysis_service.get_latest_analysis_by_symbol(
            settings_obj.symbol
        )
        if not analysis:
            continue

        buy_prices = extract_buy_prices_from_analysis(analysis)
        if not buy_prices:
            continue

        limited_buy_prices = buy_prices[: settings_obj.buy_price_levels]

        result = calculate_estimated_order_cost(
            symbol=settings_obj.symbol,
            buy_prices=limited_buy_prices,
            quantity_per_order=float(settings_obj.buy_quantity_per_order),
            currency=currency,
        )

        results.append(EstimatedCostResponse(**result))
        grand_total += result["total_cost"]

    return results, grand_total


# NOTE: 고정 경로는 반드시 경로 파라미터({symbol}) 라우트보다 먼저 정의해야 함
@router.get("/symbols/domestic/estimated-cost", response_model=AllEstimatedCostResponse)
async def get_domestic_estimated_costs(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """국내 주식 예상 매수 비용 합계 (미체결 매수 주문 금액 차감)

    설정된 국내 주식 종목에 대해 예상 비용을 계산하고,
    기존 미체결 매수 주문 금액을 차감한 순 비용을 반환합니다.
    """
    user = await get_user_from_request(request, db)
    settings_service = SymbolTradeSettingsService(db)
    analysis_service = StockAnalysisService(db)

    all_settings = await settings_service.get_all(user.id, active_only=True)
    domestic_settings = [
        s for s in all_settings if s.instrument_type == InstrumentType.equity_kr
    ]

    results, grand_total = await _estimate_costs_for_settings(
        domestic_settings, analysis_service, currency="KRW"
    )

    pending_buy_cost = await fetch_pending_domestic_buy_cost()
    net_cost = max(0.0, grand_total - pending_buy_cost)

    return AllEstimatedCostResponse(
        symbols=results,
        grand_total_cost=grand_total,
        total_symbols=len(results),
        pending_buy_orders_cost=pending_buy_cost,
        net_estimated_cost=net_cost,
    )


@router.get(
    "/symbols/overseas/estimated-cost", response_model=AllEstimatedCostResponse
)
async def get_overseas_estimated_costs(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """해외 주식 예상 매수 비용 합계 (미체결 매수 주문 금액 차감)

    설정된 해외 주식 종목에 대해 예상 비용을 계산하고,
    기존 미체결 매수 주문 금액을 차감한 순 비용을 반환합니다.
    """
    user = await get_user_from_request(request, db)
    settings_service = SymbolTradeSettingsService(db)
    analysis_service = StockAnalysisService(db)

    all_settings = await settings_service.get_all(user.id, active_only=True)
    overseas_settings = [
        s for s in all_settings if s.instrument_type == InstrumentType.equity_us
    ]

    results, grand_total = await _estimate_costs_for_settings(
        overseas_settings, analysis_service, currency="USD"
    )

    pending_buy_cost = await fetch_pending_overseas_buy_cost()
    net_cost = max(0.0, grand_total - pending_buy_cost)

    return AllEstimatedCostResponse(
        symbols=results,
        grand_total_cost=grand_total,
        total_symbols=len(results),
        pending_buy_orders_cost=pending_buy_cost,
        net_estimated_cost=net_cost,
    )


@router.get("/symbols/all/estimated-cost", response_model=AllEstimatedCostResponse)
async def get_all_estimated_costs(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 모든 활성 종목 예상 매수 비용 합계

    설정된 모든 종목에 대해 예상 비용을 계산하고 합계를 반환합니다.
    """
    user = await get_user_from_request(request, db)
    settings_service = SymbolTradeSettingsService(db)
    analysis_service = StockAnalysisService(db)

    all_settings = await settings_service.get_all(user.id, active_only=True)

    results = []
    grand_total = 0.0

    for settings_obj in all_settings:
        analysis = await analysis_service.get_latest_analysis_by_symbol(
            settings_obj.symbol
        )
        if not analysis:
            continue

        buy_prices = extract_buy_prices_from_analysis(analysis)
        if not buy_prices:
            continue

        currency = (
            "USD" if settings_obj.instrument_type == InstrumentType.equity_us else "KRW"
        )

        result = calculate_estimated_order_cost(
            symbol=settings_obj.symbol,
            buy_prices=buy_prices,
            quantity_per_order=float(settings_obj.buy_quantity_per_order),
            currency=currency,
        )

        results.append(EstimatedCostResponse(**result))
        grand_total += result["total_cost"]

    return AllEstimatedCostResponse(
        symbols=results,
        grand_total_cost=grand_total,
        total_symbols=len(results),
        pending_buy_orders_cost=0.0,
        net_estimated_cost=grand_total,
    )


@router.get("/symbols/crypto/estimated-cost", response_model=AllEstimatedCostResponse)
async def get_crypto_estimated_costs(
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """암호화폐 예상 매수 비용 합계 (미체결 매수 주문 금액 차감)

    보유 코인 전체에 대해 예상 비용을 계산합니다.
    - 종목 설정이 있으면 설정된 금액 사용
    - 종목 설정이 없으면 사용자 기본 설정(crypto_default_buy_amount) 또는 10,000원 사용
    기존 미체결 매수 주문 금액을 차감한 순 비용을 반환합니다.
    """
    import app.services.brokers.upbit.client as upbit

    user = await get_user_from_request(request, db)
    settings_service = SymbolTradeSettingsService(db)
    analysis_service = StockAnalysisService(db)
    defaults_service = UserTradeDefaultsService(db)

    # 사용자 기본 설정에서 기본 매수 금액 조회
    user_defaults = await defaults_service.get_or_create(user.id)
    default_buy_amount = (
        float(user_defaults.crypto_default_buy_amount) if user_defaults else 10000.0
    )

    # 보유 코인 조회
    my_coins = await upbit.fetch_my_coins()
    tradable_currencies = await get_active_upbit_base_currencies(
        quote_currency="KRW",
        db=db,
    )

    # 거래 가능한 코인만 필터링
    MIN_TRADE_THRESHOLD = 1000
    tradable_coins = [
        coin
        for coin in my_coins
        if str(coin.get("currency") or "").upper() != "KRW"
        and (
            (float(coin.get("balance", 0)) + float(coin.get("locked", 0)))
            * float(coin.get("avg_buy_price", 0))
        )
        >= MIN_TRADE_THRESHOLD
        and str(coin.get("currency") or "").upper() in tradable_currencies
    ]

    # 종목별 설정 조회
    all_settings = await settings_service.get_all(user.id, active_only=True)
    settings_map = {
        s.symbol: s for s in all_settings if s.instrument_type == InstrumentType.crypto
    }

    results = []
    grand_total = 0.0

    for coin in tradable_coins:
        currency = coin.get("currency")
        market = f"KRW-{currency}"

        analysis = await analysis_service.get_latest_analysis_by_symbol(market)
        if not analysis:
            continue

        buy_prices = extract_buy_prices_from_analysis(analysis)
        if not buy_prices:
            continue

        # 설정 조회 (없으면 기본값 사용)
        settings_obj = settings_map.get(market)
        if settings_obj:
            buy_amount = float(settings_obj.buy_quantity_per_order)
            buy_price_levels = settings_obj.buy_price_levels
        else:
            buy_amount = default_buy_amount
            buy_price_levels = 4

        limited_buy_prices = buy_prices[:buy_price_levels]

        # 암호화폐는 금액 기반 매수 → amount_based=True
        result = calculate_estimated_order_cost(
            symbol=market,
            buy_prices=limited_buy_prices,
            quantity_per_order=buy_amount,
            currency="KRW",
            amount_based=True,
        )

        results.append(EstimatedCostResponse(**result))
        grand_total += result["total_cost"]

    pending_buy_cost = await fetch_pending_crypto_buy_cost()
    net_cost = max(0.0, grand_total - pending_buy_cost)

    return AllEstimatedCostResponse(
        symbols=results,
        grand_total_cost=grand_total,
        total_symbols=len(results),
        pending_buy_orders_cost=pending_buy_cost,
        net_estimated_cost=net_cost,
    )


@router.get(
    "/symbols/{symbol}/estimated-cost", response_model=EstimatedCostResponse
)
async def get_estimated_cost(
    symbol: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """특정 종목의 예상 매수 비용 계산

    AI 분석 결과의 4개 매수 가격을 기반으로 예상 비용을 계산합니다.
    """
    user = await get_user_from_request(request, db)
    settings_service = SymbolTradeSettingsService(db)
    analysis_service = StockAnalysisService(db)

    settings_obj = await settings_service.get_by_symbol(symbol, user.id)
    if not settings_obj or not settings_obj.is_active:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Active settings not found for symbol: {symbol}",
        )

    analysis = await analysis_service.get_latest_analysis_by_symbol(symbol)
    if not analysis:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No analysis found for symbol: {symbol}",
        )

    buy_prices = extract_buy_prices_from_analysis(analysis)
    if not buy_prices:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"No buy prices in analysis for symbol: {symbol}",
        )

    currency = (
        "USD" if settings_obj.instrument_type == InstrumentType.equity_us else "KRW"
    )

    result = calculate_estimated_order_cost(
        symbol=symbol,
        buy_prices=buy_prices,
        quantity_per_order=float(settings_obj.buy_quantity_per_order),
        currency=currency,
    )

    return EstimatedCostResponse(**result)
```

Key changes from original:
- `extract_buy_prices_from_analysis(analysis)` replaces 5x duplicated buy price extraction (25 lines → 1 line each)
- `_estimate_costs_for_settings()` DRYs the domestic/overseas loop (used by 2 endpoints)
- `fetch_pending_*_buy_cost()` replaces inline try/except blocks
- Crypto endpoint now uses `calculate_estimated_order_cost(amount_based=True)` instead of manual dict construction
- `get_all_estimated_costs` doesn't use `buy_price_levels` limiting (matches original: line 569 uses `buy_prices` not `limited_buy_prices`)

- [ ] **Step 2: Verify file parses correctly**

```bash
uv run python -c "from app.routers.order_estimation import router; print(f'Routes: {len(router.routes)}')"
```

Expected: `Routes: 5`

- [ ] **Step 3: Commit**

```bash
git add app/routers/order_estimation.py
git commit -m "feat: create order_estimation router (not yet registered)"
```

---

## Task 6: Switchover — Trim `symbol_settings.py` + Update `main.py`

**Files:**
- Modify: `app/routers/symbol_settings.py` (remove moved code)
- Modify: `app/main.py:35,150` (register new routers)

- [ ] **Step 1: Rewrite `symbol_settings.py` to CRUD-only**

Replace the entire content of `app/routers/symbol_settings.py` with:

```python
"""
Symbol Trade Settings Router

종목별 분할 매수 수량 설정 CRUD API
"""

from fastapi import APIRouter, Depends, HTTPException, Request, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.db import get_db
from app.models.trading import InstrumentType
from app.routers.dependencies import get_user_from_request
from app.services.symbol_trade_settings_service import SymbolTradeSettingsService
from app.services.us_symbol_universe_service import (
    USSymbolUniverseLookupError,
    get_us_exchange_by_symbol,
)

router = APIRouter(prefix="/api/symbol-settings", tags=["symbol-settings"])

SYMBOL_SETTINGS_UPDATABLE_FIELDS = {
    "buy_quantity_per_order",
    "buy_price_levels",
    "exchange_code",
    "is_active",
    "note",
}


# Pydantic 모델
class SymbolSettingsCreate(BaseModel):
    """설정 생성 요청"""

    symbol: str = Field(..., description="종목 코드 (005930, AAPL, BTC 등)")
    instrument_type: InstrumentType = Field(
        ..., description="상품 타입 (equity_kr, equity_us, crypto)"
    )
    buy_quantity_per_order: float = Field(..., gt=0, description="주문당 매수 수량")
    buy_price_levels: int = Field(
        default=4,
        ge=1,
        le=4,
        description="주문할 가격대 수 (1~4). 1: appropriate_buy_min만, 4: 전체 4개",
    )
    exchange_code: str | None = Field(
        None, description="해외주식 거래소 코드 (NASD, NYSE 등)"
    )
    note: str | None = Field(None, description="메모")


class SymbolSettingsUpdate(BaseModel):
    """설정 업데이트 요청"""

    buy_quantity_per_order: float | None = Field(
        None, gt=0, description="주문당 매수 수량"
    )
    buy_price_levels: int | None = Field(
        None, ge=1, le=4, description="주문할 가격대 수 (1~4)"
    )
    exchange_code: str | None = Field(None, description="해외주식 거래소 코드")
    is_active: bool | None = Field(None, description="활성화 여부")
    note: str | None = Field(None, description="메모")


class SymbolSettingsResponse(BaseModel):
    """설정 응답"""

    id: int
    symbol: str
    instrument_type: str
    buy_quantity_per_order: float
    buy_price_levels: int
    exchange_code: str | None
    is_active: bool
    note: str | None
    created_at: str
    updated_at: str

    class Config:
        from_attributes = True


def _build_settings_response(s) -> SymbolSettingsResponse:
    """SymbolTradeSettings 모델 → 응답 변환"""
    return SymbolSettingsResponse(
        id=s.id,
        symbol=s.symbol,
        instrument_type=s.instrument_type.value,
        buy_quantity_per_order=float(s.buy_quantity_per_order),
        buy_price_levels=s.buy_price_levels,
        exchange_code=s.exchange_code,
        is_active=s.is_active,
        note=s.note,
        created_at=str(s.created_at),
        updated_at=str(s.updated_at),
    )


@router.get("/symbols", response_model=list[SymbolSettingsResponse])
async def get_all_settings(
    request: Request,
    active_only: bool = True,
    instrument_type: InstrumentType | None = None,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 모든 종목 설정 조회"""
    user = await get_user_from_request(request, db)
    service = SymbolTradeSettingsService(db)

    if instrument_type:
        settings_list = await service.get_by_type(instrument_type, user.id, active_only)
    else:
        settings_list = await service.get_all(user.id, active_only)

    return [_build_settings_response(s) for s in settings_list]


@router.get("/symbols/{symbol}", response_model=SymbolSettingsResponse)
async def get_settings_by_symbol(
    symbol: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 특정 종목 설정 조회"""
    user = await get_user_from_request(request, db)
    service = SymbolTradeSettingsService(db)
    settings_obj = await service.get_by_symbol(symbol, user.id)

    if not settings_obj:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Settings not found for symbol: {symbol}",
        )

    return _build_settings_response(settings_obj)


@router.post(
    "/symbols",
    response_model=SymbolSettingsResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_settings(
    request_data: SymbolSettingsCreate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 종목 설정 생성"""
    user = await get_user_from_request(request, db)
    service = SymbolTradeSettingsService(db)

    existing = await service.get_by_symbol(request_data.symbol, user.id)
    if existing:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Settings already exist for symbol: {request_data.symbol}",
        )

    exchange_code = request_data.exchange_code
    if request_data.instrument_type == InstrumentType.equity_us and not exchange_code:
        try:
            exchange_code = await get_us_exchange_by_symbol(request_data.symbol)
        except USSymbolUniverseLookupError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=str(exc),
            ) from exc

    settings_obj = await service.create(
        user_id=user.id,
        symbol=request_data.symbol,
        instrument_type=request_data.instrument_type,
        buy_quantity_per_order=request_data.buy_quantity_per_order,
        buy_price_levels=request_data.buy_price_levels,
        exchange_code=exchange_code,
        note=request_data.note,
    )

    return _build_settings_response(settings_obj)


@router.put("/symbols/{symbol}", response_model=SymbolSettingsResponse)
async def update_settings(
    symbol: str,
    request_data: SymbolSettingsUpdate,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 종목 설정 업데이트"""
    user = await get_user_from_request(request, db)
    service = SymbolTradeSettingsService(db)

    existing = await service.get_by_symbol(symbol, user.id)
    if not existing:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Settings not found for symbol: {symbol}",
        )

    update_data = {k: v for k, v in request_data.model_dump().items() if v is not None}

    invalid_fields = set(update_data) - SYMBOL_SETTINGS_UPDATABLE_FIELDS
    if invalid_fields:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Invalid fields for update: {', '.join(sorted(invalid_fields))}",
        )

    if not update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="No fields to update",
        )

    settings_obj = await service.update_settings(symbol, update_data, user.id)

    if settings_obj is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Settings for symbol '{symbol}' not found",
        )

    return _build_settings_response(settings_obj)


@router.delete("/symbols/{symbol}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_settings(
    symbol: str,
    request: Request,
    db: AsyncSession = Depends(get_db),
):
    """현재 사용자의 종목 설정 삭제"""
    user = await get_user_from_request(request, db)
    service = SymbolTradeSettingsService(db)

    deleted = await service.delete_settings(symbol, user.id)
    if not deleted:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"Settings not found for symbol: {symbol}",
        )
```

Key changes:
- Removed: `get_user_from_request()` (now in `dependencies.py`)
- Removed: `USER_DEFAULTS_UPDATABLE_FIELDS` (moved to `user_defaults.py`)
- Removed: `UserTradeDefaultsUpdate`, `UserTradeDefaultsResponse` schemas (moved)
- Removed: `EstimatedCostResponse`, `AllEstimatedCostResponse` schemas (moved)
- Removed: `get_user_defaults`, `update_user_defaults` endpoints (moved)
- Removed: All 5 `estimated-cost` endpoints (moved)
- Removed: Imports for `StockAnalysisService`, `calculate_estimated_order_cost`, `UserTradeDefaultsService`, `get_active_upbit_base_currencies`
- Added: `_build_settings_response()` helper to DRY 5x repeated response construction
- Added: Import of `get_user_from_request` from `dependencies`

- [ ] **Step 2: Update `app/main.py` — add imports**

In `app/main.py`, add `order_estimation` and `user_defaults` to the router imports block (around line 35):

Change:
```python
from app.routers import (
    ...
    symbol_settings,
    ...
)
```

To:
```python
from app.routers import (
    ...
    order_estimation,
    symbol_settings,
    user_defaults,
    ...
)
```

The exact position in the alphabetically-sorted import list:
- `order_estimation` goes after `openclaw_callback`
- `user_defaults` goes after `trading`

- [ ] **Step 3: Update `app/main.py` — register routers**

At the router registration section (around line 150), replace the single `symbol_settings` line:

Change:
```python
    app.include_router(symbol_settings.router)
```

To (order matters — see File Structure section):
```python
    app.include_router(user_defaults.router)
    app.include_router(order_estimation.router)
    app.include_router(symbol_settings.router)
```

- [ ] **Step 4: Verify app starts without errors**

```bash
uv run python -c "from app.main import api; print('OK')"
```

Expected: `OK` (no import errors or duplicate route warnings).

- [ ] **Step 5: Commit**

```bash
git add app/routers/symbol_settings.py app/main.py
git commit -m "refactor: split symbol_settings into 3 routers, register in main

- symbol_settings.py: CRUD only (5 endpoints)
- user_defaults.py: user settings (2 endpoints)
- order_estimation.py: cost estimation (5 endpoints)

Router registration order ensures fixed paths match before {symbol}."
```

---

## Task 7: Clean Up Old Service + Update Tests

**Files:**
- Modify: `app/services/symbol_trade_settings_service.py:204-268` (remove `calculate_estimated_order_cost`)
- Modify: `tests/test_symbol_trade_settings.py` (update imports, fix router test)

- [ ] **Step 1: Remove `calculate_estimated_order_cost` from `symbol_trade_settings_service.py`**

Delete the `calculate_estimated_order_cost` function (lines 204-268) from `app/services/symbol_trade_settings_service.py`. Also remove the `Any` import if it's no longer used.

The function is now in `app/services/order_estimation_service.py`.

- [ ] **Step 2: Update test imports in `test_symbol_trade_settings.py`**

Change the import at the top of `tests/test_symbol_trade_settings.py`:

From:
```python
from app.services.symbol_trade_settings_service import (
    SymbolTradeSettingsService,
    calculate_estimated_order_cost,
    get_buy_quantity_for_crypto,
    get_buy_quantity_for_symbol,
)
```

To:
```python
from app.services.order_estimation_service import calculate_estimated_order_cost
from app.services.symbol_trade_settings_service import (
    SymbolTradeSettingsService,
    get_buy_quantity_for_crypto,
    get_buy_quantity_for_symbol,
)
```

- [ ] **Step 3: Update `test_router_endpoint_exists` in `test_symbol_trade_settings.py`**

Replace the `test_router_endpoint_exists` method (around line 397) with:

```python
    def test_router_endpoint_exists(self):
        """라우터 엔드포인트 존재 확인"""
        from app.routers.order_estimation import router as estimation_router
        from app.routers.symbol_settings import router as settings_router
        from app.routers.user_defaults import router as defaults_router

        settings_routes = [route.path for route in settings_router.routes]
        estimation_routes = [route.path for route in estimation_router.routes]
        defaults_routes = [route.path for route in defaults_router.routes]

        # symbol_settings: CRUD 엔드포인트
        assert any("/symbols/{symbol}" in r for r in settings_routes)

        # order_estimation: 비용 추정 엔드포인트
        assert any("estimated-cost" in r for r in estimation_routes)

        # user_defaults: 사용자 기본 설정 엔드포인트
        assert any("user-defaults" in r for r in defaults_routes)
```

- [ ] **Step 4: Run all relevant tests**

```bash
uv run pytest tests/ -v -k "symbol_settings or user_default or order_estimation"
```

Expected: All tests PASS (original tests + new service tests). Total count should be original count + 13 new service tests.

- [ ] **Step 5: Commit**

```bash
git add app/services/symbol_trade_settings_service.py tests/test_symbol_trade_settings.py
git commit -m "refactor: remove calculate_estimated_order_cost from old service, update test imports"
```

---

## Task 8: Final Verification

**Files:** (none modified)

- [ ] **Step 1: Run full lint**

```bash
make lint
```

Expected: PASS (no errors).

- [ ] **Step 2: Run full test suite**

```bash
uv run pytest tests/ -v -k "symbol_settings or user_default or order_estimation"
```

Expected: All tests PASS.

- [ ] **Step 3: Verify API paths haven't changed**

```bash
uv run python -c "
from app.routers.symbol_settings import router as s
from app.routers.user_defaults import router as u
from app.routers.order_estimation import router as o

all_routes = sorted(set(
    r.path for r in list(s.routes) + list(u.routes) + list(o.routes)
    if hasattr(r, 'path')
))
for path in all_routes:
    print(path)
"
```

Expected output (all 12 original paths preserved):
```
/api/symbol-settings/symbols
/api/symbol-settings/symbols/all/estimated-cost
/api/symbol-settings/symbols/crypto/estimated-cost
/api/symbol-settings/symbols/domestic/estimated-cost
/api/symbol-settings/symbols/overseas/estimated-cost
/api/symbol-settings/symbols/{symbol}
/api/symbol-settings/symbols/{symbol}/estimated-cost
/api/symbol-settings/user-defaults
```

Note: `/symbols` and `/symbols/{symbol}` appear once each even though they serve GET+POST and GET+PUT+DELETE respectively — FastAPI merges them by path.

- [ ] **Step 4: Review line counts**

```bash
wc -l app/routers/symbol_settings.py app/routers/user_defaults.py app/routers/order_estimation.py app/services/order_estimation_service.py
```

Expected approximate counts:
- `symbol_settings.py`: ~210 lines (was 989)
- `user_defaults.py`: ~115 lines
- `order_estimation.py`: ~260 lines
- `order_estimation_service.py`: ~150 lines
