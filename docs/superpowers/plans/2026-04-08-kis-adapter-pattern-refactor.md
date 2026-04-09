# KIS Domestic/Overseas Mirroring Removal via Adapter Pattern

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Eliminate ~1,080 lines of duplicated domestic/overseas code across kis_trading_service.py, kis_market_adapters.py, and kis_trading.py by extracting shared logic and delegating market-specific differences to adapters.

**Architecture:** Protocol-based OrderOps for order execution (kis_trading_service.py), BaseAutomationAdapter with hook methods for per-stock automation (kis_market_adapters.py), and MarketHoldingsConfig for bulk/task operations (kis_trading.py). All 18 public function signatures preserved (14 in kis_trading.py + 4 in kis_trading_service.py). Zero logic changes — pure structural refactoring.

**Tech Stack:** Python 3.13, dataclass(slots=True), Protocol (typing), pytest, asyncio

---

## File Map

| File | Action | Responsibility |
|------|--------|---------------|
| `app/jobs/kis_market_adapters.py` | Modify | Add `StockContext`, `BaseAutomationAdapter`, shared `extract_*`/`match_*` functions. Rewrite subclasses as thin overrides. |
| `app/services/kis_trading_service.py` | Modify | Add `SupportsOrderExecution` Protocol, `DomesticOrderOps`/`OverseasOrderOps`. Replace 4 impl functions with 2 unified. |
| `app/jobs/kis_trading.py` | Modify | Add `MarketHoldingsConfig`, handler wrappers. Replace 8 pairs with unified functions + 14 public wrappers. Remove cancel functions (moved to adapter). |
| `tests/test_kis_tasks.py` | Modify | Update adapter constructor calls (remove `cancel_pending_orders` and overseas-specific injected callables). |
| `tests/_kis_tasks_support.py` | No change | Monkeypatch patterns still work because `_domestic_buy`/`_overseas_buy` wrappers delegate via global name lookup. |

---

## Dependency Order

```
Task 1 (shared types) ──> Task 2 (OrderOps) ──> Task 3 (unified buy impl) ──> Task 4 (unified sell impl)
                                                                                         │
Task 5 (BaseAdapter + subclasses + cancel migration) <──────────────────────────────────┘
         │
Task 6 (handler wrappers + MarketHoldingsConfig + unified bulk/task functions)
         │
Task 7 (test updates)
         │
Task 8 (final validation)
```

---

### Task 1: Shared data types and module-level helpers

**Files:**
- Modify: `app/jobs/kis_market_adapters.py` (add at top, before existing classes)

- [ ] **Step 1: Add StockContext dataclass and extract/match functions**

Add these after the existing type aliases (`AutomationResult`, `StepResults`) and before the `SupportsMarketAutomation` Protocol:

```python
# app/jobs/kis_market_adapters.py — add after line 10 (after StepResults)

from app.core.symbol import to_db_symbol


@dataclass(slots=True)
class StockContext:
    """Per-stock context for automation workflows."""

    symbol: str
    name: str
    avg_price: float
    current_price: float
    qty: int
    is_manual: bool
    exchange_code: str | None  # None for domestic


def extract_domestic_stock_info(stock: dict[str, Any]) -> StockContext:
    return StockContext(
        symbol=stock.get("pdno", ""),
        name=stock.get("prdt_name", ""),
        avg_price=float(stock.get("pchs_avg_pric", 0)),
        current_price=float(stock.get("prpr", 0)),
        qty=int(float(stock.get("ord_psbl_qty", stock.get("hldg_qty", 0)))),
        is_manual=stock.get("_is_manual", False),
        exchange_code=None,
    )


def extract_overseas_stock_info(stock: dict[str, Any]) -> StockContext:
    return StockContext(
        symbol=stock.get("ovrs_pdno", ""),
        name=stock.get("ovrs_item_name", ""),
        avg_price=float(stock.get("pchs_avg_pric", 0)),
        current_price=float(stock.get("now_pric2", 0)),
        qty=int(float(stock.get("ord_psbl_qty", stock.get("ovrs_cblc_qty", 0)))),
        is_manual=stock.get("_is_manual", False),
        exchange_code=stock.get("ovrs_excg_cd"),  # raw, resolved later
    )


def match_domestic_stock(
    stocks: list[dict[str, Any]], symbol: str
) -> dict[str, Any] | None:
    return next((s for s in stocks if s.get("pdno") == symbol), None)


def match_overseas_stock(
    stocks: list[dict[str, Any]], symbol: str
) -> dict[str, Any] | None:
    normalized = to_db_symbol(symbol)
    return next(
        (s for s in stocks if to_db_symbol(s.get("ovrs_pdno", "")) == normalized),
        None,
    )
```

- [ ] **Step 2: Write tests for extract and match functions**

```python
# tests/test_kis_market_adapters_helpers.py
import pytest

from app.jobs.kis_market_adapters import (
    StockContext,
    extract_domestic_stock_info,
    extract_overseas_stock_info,
    match_domestic_stock,
    match_overseas_stock,
)


class TestExtractDomesticStockInfo:
    def test_extracts_all_fields(self):
        stock = {
            "pdno": "005930",
            "prdt_name": "삼성전자",
            "pchs_avg_pric": "50000",
            "prpr": "51000",
            "hldg_qty": "10",
        }
        ctx = extract_domestic_stock_info(stock)
        assert ctx.symbol == "005930"
        assert ctx.name == "삼성전자"
        assert ctx.avg_price == 50000.0
        assert ctx.current_price == 51000.0
        assert ctx.qty == 10
        assert ctx.is_manual is False
        assert ctx.exchange_code is None

    def test_prefers_ord_psbl_qty_over_hldg_qty(self):
        stock = {
            "pdno": "005935",
            "prdt_name": "삼성전자우",
            "pchs_avg_pric": "76300",
            "prpr": "77500",
            "hldg_qty": "8",
            "ord_psbl_qty": "5",
        }
        ctx = extract_domestic_stock_info(stock)
        assert ctx.qty == 5

    def test_manual_flag(self):
        stock = {
            "pdno": "005935",
            "prdt_name": "삼성전자우",
            "pchs_avg_pric": "73800",
            "prpr": "73800",
            "hldg_qty": "5",
            "_is_manual": True,
        }
        ctx = extract_domestic_stock_info(stock)
        assert ctx.is_manual is True


class TestExtractOverseasStockInfo:
    def test_extracts_all_fields(self):
        stock = {
            "ovrs_pdno": "AAPL",
            "ovrs_item_name": "애플",
            "pchs_avg_pric": "170.00",
            "now_pric2": "175.00",
            "ovrs_cblc_qty": "10",
            "ovrs_excg_cd": "NASD",
        }
        ctx = extract_overseas_stock_info(stock)
        assert ctx.symbol == "AAPL"
        assert ctx.name == "애플"
        assert ctx.avg_price == 170.0
        assert ctx.current_price == 175.0
        assert ctx.qty == 10
        assert ctx.is_manual is False
        assert ctx.exchange_code == "NASD"

    def test_prefers_ord_psbl_qty(self):
        stock = {
            "ovrs_pdno": "AAPL",
            "ovrs_item_name": "애플",
            "pchs_avg_pric": "170.00",
            "now_pric2": "175.00",
            "ovrs_cblc_qty": "10",
            "ord_psbl_qty": "7",
            "ovrs_excg_cd": "NASD",
        }
        ctx = extract_overseas_stock_info(stock)
        assert ctx.qty == 7


class TestMatchStock:
    def test_match_domestic_found(self):
        stocks = [{"pdno": "005930"}, {"pdno": "005935"}]
        assert match_domestic_stock(stocks, "005935") == {"pdno": "005935"}

    def test_match_domestic_not_found(self):
        stocks = [{"pdno": "005930"}]
        assert match_domestic_stock(stocks, "XXXX") is None

    def test_match_overseas_normalizes_symbol(self):
        stocks = [{"ovrs_pdno": "BRK/B"}, {"ovrs_pdno": "AAPL"}]
        # to_db_symbol("BRK/B") == "BRK.B", to_db_symbol("BRK.B") == "BRK.B"
        result = match_overseas_stock(stocks, "BRK.B")
        assert result is not None
        assert result["ovrs_pdno"] == "BRK/B"

    def test_match_overseas_not_found(self):
        stocks = [{"ovrs_pdno": "AAPL"}]
        assert match_overseas_stock(stocks, "TSLA") is None
```

- [ ] **Step 3: Run tests to verify they pass**

Run: `uv run pytest tests/test_kis_market_adapters_helpers.py -v`
Expected: All tests PASS

- [ ] **Step 4: Verify existing tests still pass**

Run: `uv run pytest tests/test_kis_tasks.py tests/test_kis_trading_service.py tests/test_kis_trading_service_exception_handling.py -q`
Expected: 54 passed

- [ ] **Step 5: Commit**

```bash
git add app/jobs/kis_market_adapters.py tests/test_kis_market_adapters_helpers.py
git commit -m "refactor(kis): add StockContext and shared extract/match helpers"
```

---

### Task 2: SupportsOrderExecution Protocol and OrderOps implementations

**Files:**
- Modify: `app/services/kis_trading_service.py` (add Protocol + implementations after helpers, before DOMESTIC BUY section)

- [ ] **Step 1: Add Protocol and two OrderOps dataclasses**

Add after the `_present_prices` function (line 73) and before the `# DOMESTIC BUY` section (line 75):

```python
# app/services/kis_trading_service.py — add after _present_prices function

from dataclasses import dataclass
from typing import Protocol


class SupportsOrderExecution(Protocol):
    """Market-specific order execution abstraction."""

    market: str

    async def place_order(
        self,
        kis: KISClient,
        symbol: str,
        order_type: str,
        quantity: int,
        price: float,
        *,
        exchange_code: str | None = None,
    ) -> dict[str, Any]: ...

    async def adjust_sell_qty(
        self, kis: KISClient, symbol: str, balance_qty: int
    ) -> int: ...

    def resolve_exchange_code(
        self, settings: Any, fallback: str | None
    ) -> str | None: ...


@dataclass(frozen=True, slots=True)
class DomesticOrderOps:
    market: str = "domestic"

    async def place_order(
        self, kis, symbol, order_type, quantity, price, *, exchange_code=None
    ):
        return await kis.order_korea_stock(
            stock_code=symbol,
            order_type=order_type,
            quantity=quantity,
            price=int(price),
        )

    async def adjust_sell_qty(self, kis, symbol, balance_qty):
        return balance_qty

    def resolve_exchange_code(self, settings, fallback):
        return None


@dataclass(frozen=True, slots=True)
class OverseasOrderOps:
    market: str = "overseas"

    async def place_order(
        self, kis, symbol, order_type, quantity, price, *, exchange_code=None
    ):
        return await kis.order_overseas_stock(
            symbol=symbol,
            exchange_code=exchange_code,
            order_type=order_type,
            quantity=quantity,
            price=price,
        )

    async def adjust_sell_qty(self, kis, symbol, balance_qty):
        my_stocks = await kis.fetch_my_overseas_stocks()
        normalized = to_db_symbol(symbol)
        target = next(
            (
                s
                for s in my_stocks
                if to_db_symbol(s.get("ovrs_pdno", "")) == normalized
            ),
            None,
        )
        if target:
            actual = _coerce_positive_int(
                target.get("ord_psbl_qty", target.get("ovrs_cblc_qty", 0))
            )
            if actual < balance_qty:
                logger.info(
                    "[%s] 주문가능수량 조정: %s -> %s (KIS 계좌 기준)",
                    symbol,
                    balance_qty,
                    actual,
                )
                balance_qty = actual
        return balance_qty

    def resolve_exchange_code(self, settings, fallback):
        if settings and settings.exchange_code:
            return settings.exchange_code
        return fallback


_DOMESTIC_OPS = DomesticOrderOps()
_OVERSEAS_OPS = OverseasOrderOps()
```

Note: Add `from dataclasses import dataclass` and `from typing import Protocol` to the existing imports in `kis_trading_service.py`.

- [ ] **Step 2: Write tests for OrderOps**

```python
# tests/test_kis_order_ops.py
import asyncio
from unittest.mock import AsyncMock

import pytest

from app.services.kis_trading_service import (
    DomesticOrderOps,
    OverseasOrderOps,
    _DOMESTIC_OPS,
    _OVERSEAS_OPS,
)


class TestDomesticOrderOps:
    def test_singleton_exists(self):
        assert _DOMESTIC_OPS.market == "domestic"

    def test_place_order_calls_korea_stock(self):
        kis = AsyncMock()
        kis.order_korea_stock.return_value = {"odno": "123"}

        result = asyncio.run(
            _DOMESTIC_OPS.place_order(
                kis, "005930", "buy", 10, 50000.0, exchange_code=None
            )
        )

        kis.order_korea_stock.assert_called_once_with(
            stock_code="005930", order_type="buy", quantity=10, price=50000
        )
        assert result == {"odno": "123"}

    def test_place_order_casts_price_to_int(self):
        kis = AsyncMock()
        kis.order_korea_stock.return_value = {"odno": "123"}

        asyncio.run(
            _DOMESTIC_OPS.place_order(kis, "005930", "buy", 1, 73800.5)
        )
        call_args = kis.order_korea_stock.call_args
        assert call_args.kwargs["price"] == 73800

    def test_adjust_sell_qty_returns_unchanged(self):
        kis = AsyncMock()
        result = asyncio.run(_DOMESTIC_OPS.adjust_sell_qty(kis, "005930", 10))
        assert result == 10

    def test_resolve_exchange_code_returns_none(self):
        assert _DOMESTIC_OPS.resolve_exchange_code(None, "NASD") is None


class TestOverseasOrderOps:
    def test_singleton_exists(self):
        assert _OVERSEAS_OPS.market == "overseas"

    def test_place_order_calls_overseas_stock(self):
        kis = AsyncMock()
        kis.order_overseas_stock.return_value = {"odno": "456"}

        result = asyncio.run(
            _OVERSEAS_OPS.place_order(
                kis, "AAPL", "buy", 5, 175.50, exchange_code="NASD"
            )
        )

        kis.order_overseas_stock.assert_called_once_with(
            symbol="AAPL",
            exchange_code="NASD",
            order_type="buy",
            quantity=5,
            price=175.50,
        )
        assert result == {"odno": "456"}

    def test_place_order_keeps_float_price(self):
        kis = AsyncMock()
        kis.order_overseas_stock.return_value = {"odno": "456"}

        asyncio.run(
            _OVERSEAS_OPS.place_order(
                kis, "AAPL", "buy", 1, 175.99, exchange_code="NASD"
            )
        )
        call_args = kis.order_overseas_stock.call_args
        assert call_args.kwargs["price"] == 175.99

    def test_adjust_sell_qty_reduces_when_account_has_less(self):
        kis = AsyncMock()
        kis.fetch_my_overseas_stocks.return_value = [
            {"ovrs_pdno": "AAPL", "ord_psbl_qty": "7", "ovrs_cblc_qty": "10"}
        ]

        result = asyncio.run(_OVERSEAS_OPS.adjust_sell_qty(kis, "AAPL", 10))
        assert result == 7

    def test_adjust_sell_qty_unchanged_when_account_has_more(self):
        kis = AsyncMock()
        kis.fetch_my_overseas_stocks.return_value = [
            {"ovrs_pdno": "AAPL", "ord_psbl_qty": "15", "ovrs_cblc_qty": "15"}
        ]

        result = asyncio.run(_OVERSEAS_OPS.adjust_sell_qty(kis, "AAPL", 10))
        assert result == 10

    def test_resolve_exchange_code_from_settings(self):
        from unittest.mock import MagicMock

        settings = MagicMock()
        settings.exchange_code = "NYSE"
        assert _OVERSEAS_OPS.resolve_exchange_code(settings, "NASD") == "NYSE"

    def test_resolve_exchange_code_fallback(self):
        from unittest.mock import MagicMock

        settings = MagicMock()
        settings.exchange_code = None
        assert _OVERSEAS_OPS.resolve_exchange_code(settings, "NASD") == "NASD"

    def test_resolve_exchange_code_no_settings(self):
        assert _OVERSEAS_OPS.resolve_exchange_code(None, "AMEX") == "AMEX"
```

- [ ] **Step 3: Run new tests**

Run: `uv run pytest tests/test_kis_order_ops.py -v`
Expected: All tests PASS

- [ ] **Step 4: Verify existing tests still pass**

Run: `uv run pytest tests/test_kis_trading_service.py tests/test_kis_trading_service_exception_handling.py -q`
Expected: Same pass count as before (no regression)

- [ ] **Step 5: Commit**

```bash
git add app/services/kis_trading_service.py tests/test_kis_order_ops.py
git commit -m "refactor(kis): add SupportsOrderExecution protocol and OrderOps implementations"
```

---

### Task 3: Unified _process_buy_orders_impl

**Files:**
- Modify: `app/services/kis_trading_service.py:97-383` (replace two buy impl functions with one unified)

- [ ] **Step 1: Write unified _process_buy_orders_impl**

Replace `_process_kis_domestic_buy_orders_impl` (lines 97-228) and `_process_kis_overseas_buy_orders_impl` (lines 257-383) with a single function. Keep both old functions temporarily commented or removed.

```python
async def _process_buy_orders_impl(
    ops: SupportsOrderExecution,
    kis_client: KISClient,
    symbol: str,
    current_price: float,
    avg_buy_price: float,
    exchange_code: str | None = None,
) -> OrderStepResult:
    """Unified buy order implementation. Market-specific behavior via ops."""
    from app.services.stock_info_service import StockAnalysisService
    from app.services.symbol_trade_settings_service import SymbolTradeSettingsService

    async with _open_async_session() as db:
        service = StockAnalysisService(db)
        analysis = await service.get_latest_analysis_by_symbol(symbol)

        # 1. 기본 조건: 현재가가 평균 매수가보다 1% 낮아야 함
        if avg_buy_price > 0:
            target_price = avg_buy_price * 0.99
            if current_price >= target_price:
                return OrderStepResult(
                    success=False,
                    message=f"1% 매수 조건 미충족: 현재가 {current_price} >= 목표가 {target_price}"
                    if ops.market == "domestic"
                    else "1% 매수 조건 미충족",
                )

        # 2. 분석 결과 확인
        if not analysis:
            return OrderStepResult(success=False, message="분석 결과 없음")

        # 2.5 종목 설정 확인
        settings_service = SymbolTradeSettingsService(db)
        settings = await settings_service.get_by_symbol(symbol)
        if not settings or not settings.is_active:
            logger.info("[%s] %s", symbol, MSG_NO_SETTINGS)
            return OrderStepResult(success=False, message=MSG_NO_SETTINGS)

        buy_price_levels = settings.buy_price_levels
        resolved_exchange = ops.resolve_exchange_code(settings, exchange_code)

        appropriate_buy_min = _coerce_optional_float(
            getattr(analysis, "appropriate_buy_min", None)
        )
        appropriate_buy_max = _coerce_optional_float(
            getattr(analysis, "appropriate_buy_max", None)
        )
        buy_hope_min = _coerce_optional_float(
            getattr(analysis, "buy_hope_min", None)
        )
        buy_hope_max = _coerce_optional_float(
            getattr(analysis, "buy_hope_max", None)
        )

        # 3. 가격 정보 확인 (스마트 선택 로직)
        if buy_price_levels == 1:
            use_lower_price = (
                appropriate_buy_max is not None
                and avg_buy_price > 0
                and appropriate_buy_max < avg_buy_price
            )
            if use_lower_price:
                if appropriate_buy_min is not None:
                    buy_prices = [appropriate_buy_min]
                elif buy_hope_min is not None:
                    buy_prices = [buy_hope_min]
                else:
                    buy_prices = []
            else:
                if appropriate_buy_max is not None:
                    buy_prices = [appropriate_buy_max]
                elif appropriate_buy_min is not None:
                    buy_prices = [appropriate_buy_min]
                else:
                    buy_prices = []
        else:
            buy_prices = _present_prices(
                appropriate_buy_min,
                appropriate_buy_max,
                buy_hope_min,
                buy_hope_max,
            )
            buy_prices = buy_prices[:buy_price_levels]

        if not buy_prices:
            return OrderStepResult(
                success=False,
                message="분석 결과에 매수 가격 정보 없음",
            )

        # 4. 조건에 맞는 가격 필터링
        threshold_price = avg_buy_price * 0.99 if avg_buy_price > 0 else float("inf")
        valid_prices = [
            p for p in buy_prices if p < threshold_price and p < current_price
        ]

        if not valid_prices:
            return OrderStepResult(
                success=False,
                message=f"조건에 맞는 매수 가격 없음 ({buy_price_levels}개 가격대 중 유효 없음)",
            )

        # 5. 수량 확인
        quantity = _coerce_positive_int(settings.buy_quantity_per_order)
        if quantity < 1:
            return OrderStepResult(success=False, message="설정된 수량이 1 미만")

        # 6. 주문 실행
        success_count = 0
        ordered_prices: list[float] = []
        ordered_quantities: list[int] = []
        total_amount = 0.0

        for price in valid_prices:
            res = await ops.place_order(
                kis_client,
                symbol,
                "buy",
                quantity,
                price,
                exchange_code=resolved_exchange,
            )
            if res and res.get("odno"):
                success_count += 1
                ordered_prices.append(price)
                ordered_quantities.append(quantity)
                total_amount += price * quantity

            await asyncio.sleep(0.2)

        return OrderStepResult(
            success=success_count > 0,
            message=f"{success_count}개 주문 성공 (설정: {buy_price_levels}개 가격대)",
            orders_placed=success_count,
            prices=ordered_prices,
            quantities=ordered_quantities,
            total_amount=total_amount,
        )
```

**Note on buy_prices type unification:** The domestic version previously used `list[tuple[str, float]]` (with price level names). The name was never logged, displayed, or returned — it was only used as `_name` in a `for _name, price in valid_prices` loop. The unified version uses `list[float]` everywhere. This is a safe change with zero information loss (verified in conversation).

- [ ] **Step 2: Update public wrappers to use unified impl**

Replace the 2 public buy wrappers (keep exact same signatures):

```python
async def process_kis_domestic_buy_orders_with_analysis(
    kis_client: KISClient, symbol: str, current_price: float, avg_buy_price: float
) -> dict[str, Any]:
    """분석 결과를 기반으로 KIS 국내 주식 매수 주문 처리."""
    try:
        result = await _process_buy_orders_impl(
            _DOMESTIC_OPS, kis_client, symbol, current_price, avg_buy_price
        )
        return result.to_payload()
    except Exception as exc:
        logger.exception(f"[{symbol}] Domestic buy order failed: {exc}")
        return _map_exception_to_result(exc, f"domestic buy for {symbol}").to_payload()


async def process_kis_overseas_buy_orders_with_analysis(
    kis_client: KISClient,
    symbol: str,
    current_price: float,
    avg_buy_price: float,
    exchange_code: str = "NASD",
) -> dict[str, Any]:
    """분석 결과를 기반으로 KIS 해외 주식 매수 주문 처리."""
    try:
        result = await _process_buy_orders_impl(
            _OVERSEAS_OPS, kis_client, symbol, current_price, avg_buy_price, exchange_code
        )
        return result.to_payload()
    except Exception as exc:
        logger.exception(f"[{symbol}] Overseas buy order failed: {exc}")
        return _map_exception_to_result(exc, f"overseas buy for {symbol}").to_payload()
```

Delete the old `_process_kis_domestic_buy_orders_impl` and `_process_kis_overseas_buy_orders_impl` functions.

- [ ] **Step 3: Run existing buy tests to verify no regression**

Run: `uv run pytest tests/test_kis_trading_service.py -v -k "buy" && uv run pytest tests/test_kis_trading_service_exception_handling.py -v -k "buy"`
Expected: All buy-related tests PASS

- [ ] **Step 4: Run full test suite for these files**

Run: `uv run pytest tests/test_kis_trading_service.py tests/test_kis_trading_service_exception_handling.py tests/test_kis_order_ops.py -q`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/kis_trading_service.py
git commit -m "refactor(kis): unify buy order impl with SupportsOrderExecution"
```

---

### Task 4: Unified _process_sell_orders_impl

**Files:**
- Modify: `app/services/kis_trading_service.py:386-724` (replace two sell impl functions with one unified)

- [ ] **Step 1: Write unified _process_sell_orders_impl**

Replace `_process_kis_domestic_sell_orders_impl` and `_process_kis_overseas_sell_orders_impl` with:

```python
async def _process_sell_orders_impl(
    ops: SupportsOrderExecution,
    kis_client: KISClient,
    symbol: str,
    current_price: float,
    avg_buy_price: float,
    balance_qty: int,
    exchange_code: str | None = None,
) -> OrderStepResult:
    """Unified sell order implementation. Market-specific behavior via ops."""
    from app.services.stock_info_service import StockAnalysisService
    from app.services.symbol_trade_settings_service import SymbolTradeSettingsService

    async with _open_async_session() as db:
        service = StockAnalysisService(db)
        analysis = await service.get_latest_analysis_by_symbol(symbol)

        if not analysis:
            return OrderStepResult(success=False, message="분석 결과 없음")

        # exchange_code resolution (overseas only — domestic returns None)
        resolved_exchange = exchange_code
        if exchange_code is not None:
            settings_service = SymbolTradeSettingsService(db)
            settings = await settings_service.get_by_symbol(symbol)
            resolved_exchange = ops.resolve_exchange_code(settings, exchange_code)

        # Overseas: verify actual orderable qty from KIS account
        balance_qty = await ops.adjust_sell_qty(kis_client, symbol, balance_qty)
        if balance_qty <= 0:
            return OrderStepResult(success=False, message="주문가능수량 없음")

        sell_prices = _present_prices(
            getattr(analysis, "appropriate_sell_min", None),
            getattr(analysis, "appropriate_sell_max", None),
            getattr(analysis, "sell_target_min", None),
            getattr(analysis, "sell_target_max", None),
        )

        if not sell_prices:
            return OrderStepResult(success=False, message="매도 가격 정보 없음")

        min_sell_price = avg_buy_price * 1.01
        valid_prices = [
            p for p in sell_prices if p >= min_sell_price and p >= current_price
        ]
        valid_prices.sort()

        if not valid_prices:
            if current_price >= min_sell_price:
                res = await ops.place_order(
                    kis_client,
                    symbol,
                    "sell",
                    balance_qty,
                    current_price,
                    exchange_code=resolved_exchange,
                )
                if res and res.get("odno"):
                    return OrderStepResult(
                        success=True,
                        message="목표가 도달로 전량 매도",
                        orders_placed=1,
                        prices=[current_price],
                        quantities=[balance_qty],
                        total_volume=balance_qty,
                        expected_amount=current_price * balance_qty,
                    )
                else:
                    return OrderStepResult(success=False, message="매도 주문 실패")
            return OrderStepResult(success=False, message="매도 조건 미충족")

        split_count = len(valid_prices)
        qty_per_order = balance_qty // split_count

        if qty_per_order < 1:
            target_price = valid_prices[0]
            res = await ops.place_order(
                kis_client,
                symbol,
                "sell",
                balance_qty,
                target_price,
                exchange_code=resolved_exchange,
            )
            if res and res.get("odno"):
                return OrderStepResult(
                    success=True,
                    message="전량 매도 주문 (분할 불가)"
                    if ops.market == "domestic"
                    else "전량 매도 주문",
                    orders_placed=1,
                    prices=[target_price],
                    quantities=[balance_qty],
                    total_volume=balance_qty,
                    expected_amount=target_price * balance_qty,
                )
            return OrderStepResult(success=False, message="매도 주문 실패")

        success_count = 0
        remaining_qty = balance_qty
        ordered_prices: list[float] = []
        ordered_quantities: list[int] = []
        total_volume = 0
        expected_amount = 0.0

        for i, price in enumerate(valid_prices):
            is_last = i == len(valid_prices) - 1
            qty = remaining_qty if is_last else qty_per_order

            if qty < 1:
                continue

            res = await ops.place_order(
                kis_client,
                symbol,
                "sell",
                qty,
                price,
                exchange_code=resolved_exchange,
            )
            if res and res.get("odno"):
                success_count += 1
                remaining_qty -= qty
                ordered_prices.append(price)
                ordered_quantities.append(qty)
                total_volume += qty
                expected_amount += price * qty

            await asyncio.sleep(0.2)

        return OrderStepResult(
            success=success_count > 0,
            message=f"{success_count}건 분할 매도 주문 완료",
            orders_placed=success_count,
            prices=ordered_prices,
            quantities=ordered_quantities,
            total_volume=total_volume,
            expected_amount=expected_amount,
        )
```

**Key design note:** `exchange_code is not None` guards the settings lookup — domestic passes `None` so `SymbolTradeSettingsService` is never queried for exchange_code in the domestic path (matching current behavior where domestic sell never imported SymbolTradeSettingsService).

- [ ] **Step 2: Update public sell wrappers**

```python
async def process_kis_domestic_sell_orders_with_analysis(
    kis_client: KISClient,
    symbol: str,
    current_price: float,
    avg_buy_price: float,
    balance_qty: int,
) -> dict[str, Any]:
    """분석 결과를 기반으로 KIS 국내 주식 매도 주문 처리."""
    try:
        result = await _process_sell_orders_impl(
            _DOMESTIC_OPS, kis_client, symbol, current_price, avg_buy_price, balance_qty
        )
        return result.to_payload()
    except Exception as exc:
        logger.exception(f"[{symbol}] Domestic sell order failed: {exc}")
        return _map_exception_to_result(exc, f"domestic sell for {symbol}").to_payload()


async def process_kis_overseas_sell_orders_with_analysis(
    kis_client: KISClient,
    symbol: str,
    current_price: float,
    avg_buy_price: float,
    balance_qty: int,
    exchange_code: str = "NASD",
) -> dict[str, Any]:
    """분석 결과를 기반으로 KIS 해외 주식 매도 주문 처리."""
    try:
        result = await _process_sell_orders_impl(
            _OVERSEAS_OPS,
            kis_client,
            symbol,
            current_price,
            avg_buy_price,
            balance_qty,
            exchange_code,
        )
        return result.to_payload()
    except Exception as exc:
        logger.exception(f"[{symbol}] Overseas sell order failed: {exc}")
        return _map_exception_to_result(exc, f"overseas sell for {symbol}").to_payload()
```

Delete the old `_process_kis_domestic_sell_orders_impl` and `_process_kis_overseas_sell_orders_impl`.

- [ ] **Step 3: Run all trading service tests**

Run: `uv run pytest tests/test_kis_trading_service.py tests/test_kis_trading_service_exception_handling.py -v`
Expected: All tests PASS (same count as baseline)

- [ ] **Step 4: Run full related test suite**

Run: `uv run pytest tests/test_kis_trading_service.py tests/test_kis_trading_service_exception_handling.py tests/test_kis_order_ops.py tests/test_kis_tasks.py -q`
Expected: 54+ tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/services/kis_trading_service.py
git commit -m "refactor(kis): unify sell order impl with SupportsOrderExecution"
```

---

### Task 5: BaseAutomationAdapter + subclass rewrites + cancel migration

This is the largest task. It replaces the two 350+ line adapter classes with BaseAutomationAdapter (shared execute()) and thin subclasses.

**Files:**
- Modify: `app/jobs/kis_market_adapters.py` (full rewrite of adapter classes)
- Modify: `app/jobs/kis_trading.py` (remove cancel functions, update adapter construction)

- [ ] **Step 1: Write BaseAutomationAdapter**

Replace both `DomesticAutomationAdapter` and `OverseasAutomationAdapter` with the following class hierarchy. Keep `SupportsMarketAutomation` Protocol unchanged. The full code for BaseAutomationAdapter.execute() follows the common flow discovered during design: fetch holdings → merge manual → iterate stocks → (cancel buy → buy → refresh → toss or cancel sell → sell).

The `execute()` method uses `self.extract_stock_info()`, `self.fetch_holdings()`, etc. as hook methods. The full implementation of `execute()` is the common logic from the current domestic adapter with hook calls replacing hardcoded field names and API calls.

Key integration points in execute():
- `ctx = self.extract_stock_info(stock)` immediately followed by `ctx.exchange_code = await self.resolve_exchange(ctx.symbol, stock)` — ensures exchange_code is never None when it shouldn't be.
- `await self.cancel_pending(kis, ctx.symbol, "buy", all_open_orders, exchange_code=ctx.exchange_code)` — unified cancel signature.
- `await self.buy_handler(kis, ctx.symbol, ctx.current_price, ctx.avg_price, exchange_code=ctx.exchange_code)` — unified handler signature.
- `await self.sell_handler(kis, ctx.symbol, ctx.current_price, ctx.avg_price, ctx.qty, exchange_code=ctx.exchange_code)` — unified handler signature.

The execute() method body is ~200 lines (down from ~350 per adapter). Do not copy-paste from this plan — transcribe from the current `DomesticAutomationAdapter.execute()` while replacing market-specific code with hook calls. Reference the 15-point difference table from the design conversation for every substitution point.

```python
@dataclass(slots=True)
class BaseAutomationAdapter:
    """Common per-stock automation workflow. Market-specific behavior via hook methods."""

    # Injected dependencies
    kis_client_factory: Callable[[], Any]
    async_session_factory: Callable[[], Any]
    manual_holdings_service_factory: Callable[[Any], Any]
    manual_market_type: Any
    buy_handler: Callable[..., Awaitable[dict[str, Any]]]
    sell_handler: Callable[..., Awaitable[dict[str, Any]]]
    send_toss_recommendation: Callable[..., Awaitable[None]]
    notifier_factory: Callable[[], Any]
    no_stocks_message: str

    # Market attributes (subclass sets defaults)
    market: str = ""
    market_type_label: str = ""
    result_symbol_key: str = ""
    toss_market_type: str = ""
    toss_currency: str = ""
    refresh_holdings_after_sell_cancel: bool = False

    # --- Hook methods: subclass MUST override ---

    async def fetch_holdings(self, kis: Any) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def fetch_open_orders(self, kis: Any) -> list[dict[str, Any]]:
        raise NotImplementedError

    def extract_stock_info(self, stock: dict[str, Any]) -> StockContext:
        raise NotImplementedError

    def build_manual_entry(self, holding: Any) -> dict[str, Any]:
        raise NotImplementedError

    def is_same_symbol(self, stock: dict[str, Any], ticker: str) -> bool:
        raise NotImplementedError

    async def fetch_manual_price(self, kis: Any, symbol: str) -> float:
        raise NotImplementedError

    async def cancel_pending(
        self,
        kis: Any,
        symbol: str,
        order_type: str,
        all_open_orders: list[dict[str, Any]],
        *,
        exchange_code: str | None = None,
    ) -> dict[str, Any]:
        raise NotImplementedError

    # --- Hook methods: subclass MAY override (have defaults) ---

    async def resolve_exchange(
        self, symbol: str, stock: dict[str, Any]
    ) -> str | None:
        return None

    async def refresh_after_buy(
        self,
        kis: Any,
        symbol: str,
        qty: int,
        avg_price: float,
        current_price: float,
    ) -> tuple[int, float, float]:
        return qty, avg_price, current_price

    async def refresh_after_sell_cancel(
        self, kis: Any, symbol: str, qty: int, current_price: float
    ) -> tuple[int, float]:
        return qty, current_price

    async def on_buy_error_result(
        self, name: str, symbol: str, result: dict[str, Any]
    ) -> None:
        pass

    async def on_trade_exception(
        self, symbol: str, name: str, exc: Exception, trade_type: str
    ) -> None:
        pass

    def analysis_target(self, *, name: str | None, symbol: str | None) -> str:
        raise NotImplementedError

    def build_result_entry(
        self, *, name: str | None, symbol: str | None, steps: StepResults
    ) -> AutomationResult:
        resolved_name = name or symbol or ""
        resolved_symbol = symbol or ""
        return {
            "name": resolved_name,
            self.result_symbol_key: resolved_symbol,
            "steps": steps,
        }

    # --- Main workflow ---

    async def execute(self) -> AutomationResult:
        """Unified per-stock automation: cancel → buy → refresh → sell."""
        kis = self.kis_client_factory()

        try:
            my_stocks = await self.fetch_holdings(kis)

            async with self.async_session_factory() as db:
                manual_service = self.manual_holdings_service_factory(db)
                manual_holdings = await manual_service.get_holdings_by_user(
                    user_id=1,
                    market_type=self.manual_market_type,
                )

            for holding in manual_holdings:
                ticker = holding.ticker
                if any(self.is_same_symbol(stock, ticker) for stock in my_stocks):
                    continue
                my_stocks.append(self.build_manual_entry(holding))

            if not my_stocks:
                return {
                    "status": "completed",
                    "message": self.no_stocks_message,
                    "results": [],
                }

            results: list[AutomationResult] = []
            all_open_orders = await self.fetch_open_orders(kis)
            logger.info("%s 미체결 주문 조회 완료: %s건", self.market_type_label, len(all_open_orders))

            for stock in my_stocks:
                ctx = self.extract_stock_info(stock)
                ctx.exchange_code = await self.resolve_exchange(ctx.symbol, stock)

                if ctx.is_manual:
                    try:
                        ctx.current_price = await self.fetch_manual_price(kis, ctx.symbol)
                        logger.info(
                            "[수동잔고] %s(%s) 현재가 조회: %s",
                            ctx.name, ctx.symbol, ctx.current_price,
                        )
                    except Exception as exc:
                        logger.warning(
                            "[수동잔고] %s(%s) 현재가 조회 실패, 평단가 사용: %s",
                            ctx.name, ctx.symbol, exc,
                        )
                        ctx.current_price = ctx.avg_price

                stock_steps: StepResults = []

                # Analysis step skipped
                stock_steps.append({
                    "step": "분석",
                    "result": {"success": True, "message": "분석 스킵 (대체 분석기 준비 중)"},
                })

                # --- Cancel pending buy orders ---
                try:
                    cancel_result = await self.cancel_pending(
                        kis, ctx.symbol, "buy", all_open_orders,
                        exchange_code=ctx.exchange_code,
                    )
                    if cancel_result["total"] > 0:
                        logger.info(
                            "%s 미체결 매수 주문 취소: %s/%s건",
                            ctx.name or ctx.symbol,
                            cancel_result["cancelled"],
                            cancel_result["total"],
                        )
                        stock_steps.append({
                            "step": "매수취소",
                            "result": {"success": True, **cancel_result},
                        })
                        await asyncio.sleep(0.5)
                except Exception as exc:
                    logger.warning(
                        "%s 미체결 매수 주문 취소 실패: %s", ctx.name or ctx.symbol, exc
                    )
                    stock_steps.append({
                        "step": "매수취소",
                        "result": {"success": False, "error": str(exc)},
                    })

                # --- Buy ---
                try:
                    buy_result = await self.buy_handler(
                        kis, ctx.symbol, ctx.current_price, ctx.avg_price,
                        exchange_code=ctx.exchange_code,
                    )
                    stock_steps.append({"step": "매수", "result": buy_result})
                    await self.on_buy_error_result(ctx.name, ctx.symbol, buy_result)
                    if (
                        buy_result.get("success")
                        and buy_result.get("orders_placed", 0) > 0
                    ):
                        try:
                            notifier = self.notifier_factory()
                            await notifier.notify_buy_order(
                                symbol=ctx.symbol,
                                korean_name=ctx.name or ctx.symbol,
                                order_count=buy_result.get("orders_placed", 0),
                                total_amount=buy_result.get("total_amount", 0.0),
                                prices=buy_result.get("prices", []),
                                volumes=buy_result.get("quantities", []),
                                market_type=self.market_type_label,
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
                except Exception as exc:
                    error_msg = str(exc)
                    stock_steps.append({
                        "step": "매수",
                        "result": {"success": False, "error": error_msg},
                    })
                    logger.error(
                        "[매수 실패] %s(%s): %s", ctx.name, ctx.symbol, error_msg,
                    )
                    await self.on_trade_exception(ctx.symbol, ctx.name, exc, "매수")

                # --- Refresh after buy ---
                ctx.qty, ctx.avg_price, ctx.current_price = await self.refresh_after_buy(
                    kis, ctx.symbol, ctx.qty, ctx.avg_price, ctx.current_price,
                )

                # --- Manual holdings: toss recommendation, then continue ---
                if ctx.is_manual:
                    logger.info(
                        "[수동잔고] %s(%s) - KIS 매도 불가, 토스 추천 알림 발송",
                        ctx.name, ctx.symbol,
                    )
                    try:
                        await self.send_toss_recommendation(
                            code=ctx.symbol,
                            name=ctx.name,
                            current_price=ctx.current_price,
                            toss_quantity=ctx.qty,
                            toss_avg_price=ctx.avg_price,
                            market_type=self.toss_market_type,
                            currency=self.toss_currency,
                        )
                        stock_steps.append({
                            "step": "매도",
                            "result": {
                                "success": True,
                                "message": "수동잔고 - 토스 추천 알림 발송",
                                "orders_placed": 0,
                            },
                        })
                    except Exception as exc:
                        logger.warning(
                            "[수동잔고] %s(%s) 토스 추천 알림 발송 실패: %s",
                            ctx.name, ctx.symbol, exc,
                        )
                        stock_steps.append({
                            "step": "매도",
                            "result": {
                                "success": True,
                                "message": "수동잔고 - 매도 스킵",
                                "orders_placed": 0,
                            },
                        })
                    results.append(
                        self.build_result_entry(
                            name=ctx.name, symbol=ctx.symbol, steps=stock_steps,
                        )
                    )
                    continue

                # --- Cancel pending sell orders ---
                sell_orders_cancelled = False
                try:
                    cancel_result = await self.cancel_pending(
                        kis, ctx.symbol, "sell", all_open_orders,
                        exchange_code=ctx.exchange_code,
                    )
                    if cancel_result["total"] > 0:
                        logger.info(
                            "%s 미체결 매도 주문 취소: %s/%s건",
                            ctx.name or ctx.symbol,
                            cancel_result["cancelled"],
                            cancel_result["total"],
                        )
                        stock_steps.append({
                            "step": "매도취소",
                            "result": {"success": True, **cancel_result},
                        })
                        sell_orders_cancelled = cancel_result["cancelled"] > 0
                        await asyncio.sleep(0.5)
                except Exception as exc:
                    logger.warning(
                        "%s 미체결 매도 주문 취소 실패: %s", ctx.name or ctx.symbol, exc
                    )
                    stock_steps.append({
                        "step": "매도취소",
                        "result": {"success": False, "error": str(exc)},
                    })

                # --- Refresh after sell cancel ---
                if sell_orders_cancelled and self.refresh_holdings_after_sell_cancel:
                    ctx.qty, ctx.current_price = await self.refresh_after_sell_cancel(
                        kis, ctx.symbol, ctx.qty, ctx.current_price,
                    )

                # --- Sell ---
                try:
                    sell_result = await self.sell_handler(
                        kis, ctx.symbol, ctx.current_price, ctx.avg_price, ctx.qty,
                        exchange_code=ctx.exchange_code,
                    )
                    stock_steps.append({"step": "매도", "result": sell_result})
                    if (
                        sell_result.get("success")
                        and sell_result.get("orders_placed", 0) > 0
                    ):
                        try:
                            notifier = self.notifier_factory()
                            await notifier.notify_sell_order(
                                symbol=ctx.symbol,
                                korean_name=ctx.name or ctx.symbol,
                                order_count=sell_result.get("orders_placed", 0),
                                total_volume=sell_result.get("total_volume", 0),
                                prices=sell_result.get("prices", []),
                                volumes=sell_result.get("quantities", []),
                                expected_amount=sell_result.get("expected_amount", 0.0),
                                market_type=self.market_type_label,
                            )
                        except Exception as notify_error:
                            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
                except Exception as exc:
                    error_msg = str(exc)
                    stock_steps.append({
                        "step": "매도",
                        "result": {"success": False, "error": error_msg},
                    })
                    logger.error(
                        "[매도 실패] %s(%s): %s", ctx.name, ctx.symbol, error_msg,
                    )
                    await self.on_trade_exception(ctx.symbol, ctx.name, exc, "매도")

                results.append(
                    self.build_result_entry(
                        name=ctx.name, symbol=ctx.symbol, steps=stock_steps,
                    )
                )

            return {
                "status": "completed",
                "message": "종목별 자동 실행 완료",
                "results": results,
            }
        except Exception as exc:
            logger.error(
                "[태스크 실패] %s: %s", self.market_type_label, exc, exc_info=True,
            )
            return {"status": "failed", "error": str(exc)}
```

- [ ] **Step 2: Write DomesticAutomationAdapter subclass**

```python
@dataclass(slots=True)
class DomesticAutomationAdapter(BaseAutomationAdapter):
    market: str = "domestic"
    market_type_label: str = "국내주식"
    result_symbol_key: str = "code"
    toss_market_type: str = "kr"
    toss_currency: str = "원"
    refresh_holdings_after_sell_cancel: bool = True

    async def fetch_holdings(self, kis):
        return await kis.fetch_my_stocks()

    async def fetch_open_orders(self, kis):
        return await kis.inquire_korea_orders(is_mock=False)

    def extract_stock_info(self, stock):
        return extract_domestic_stock_info(stock)

    def build_manual_entry(self, holding):
        qty_str = str(holding.quantity)
        return {
            "pdno": holding.ticker,
            "prdt_name": holding.display_name or holding.ticker,
            "hldg_qty": qty_str,
            "ord_psbl_qty": qty_str,
            "pchs_avg_pric": str(holding.avg_price),
            "prpr": str(holding.avg_price),
            "_is_manual": True,
        }

    def is_same_symbol(self, stock, ticker):
        return stock.get("pdno") == ticker

    async def fetch_manual_price(self, kis, symbol):
        info = await kis.fetch_fundamental_info(symbol)
        return float(info.get("현재가", 0))

    async def cancel_pending(self, kis, symbol, order_type, all_open_orders, *, exchange_code=None):
        # Moved from kis_trading._cancel_domestic_pending_orders (lines 303-385)
        target_code = "02" if order_type == "buy" else "01"
        target_orders = [
            order
            for order in all_open_orders
            if (order.get("pdno") or order.get("PDNO")) == symbol
            and (order.get("sll_buy_dvsn_cd") or order.get("SLL_BUY_DVSN_CD")) == target_code
        ]
        if not target_orders:
            return {"cancelled": 0, "failed": 0, "total": 0}

        cancelled = 0
        failed = 0
        for order in target_orders:
            order_number = None
            try:
                order_number = (
                    order.get("odno") or order.get("ODNO")
                    or order.get("ord_no") or order.get("ORD_NO")
                )
                order_qty = int(order.get("ord_qty") or order.get("ORD_QTY") or 0)
                order_price = int(float(order.get("ord_unpr") or order.get("ORD_UNPR") or 0))
                order_orgno = (
                    order.get("ord_gno_brno") or order.get("ORD_GNO_BRNO")
                    or order.get("krx_fwdg_ord_orgno") or order.get("KRX_FWDG_ORD_ORGNO")
                )
                if not order_number:
                    logger.warning("주문번호 없음 (%s): order=%s", symbol, order)
                    failed += 1
                    continue
                await kis.cancel_korea_order(
                    order_number=order_number,
                    stock_code=symbol,
                    quantity=order_qty,
                    price=order_price,
                    order_type=order_type,
                    is_mock=False,
                    krx_fwdg_ord_orgno=str(order_orgno).strip() if order_orgno else None,
                )
                cancelled += 1
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.warning("주문 취소 실패 (%s, %s): %s", symbol, order_number or "unknown", e)
                failed += 1
        return {"cancelled": cancelled, "failed": failed, "total": len(target_orders)}

    def analysis_target(self, *, name=None, symbol=None):
        return name or symbol or ""

    async def refresh_after_buy(self, kis, symbol, qty, avg_price, current_price):
        try:
            latest = await kis.fetch_my_stocks()
            target = next((s for s in latest if s.get("pdno") == symbol), None)
            if target:
                return (
                    int(target.get("ord_psbl_qty", target.get("hldg_qty", qty))),
                    float(target.get("pchs_avg_pric", avg_price)),
                    float(target.get("prpr", current_price)),
                )
        except Exception:
            pass
        return qty, avg_price, current_price

    async def refresh_after_sell_cancel(self, kis, symbol, qty, current_price):
        try:
            latest = await kis.fetch_my_stocks()
            target = next((s for s in latest if s.get("pdno") == symbol), None)
            if target:
                return (
                    int(target.get("ord_psbl_qty", target.get("hldg_qty", qty))),
                    float(target.get("prpr", current_price)),
                )
        except Exception:
            pass
        return qty, current_price

    async def on_buy_error_result(self, name, symbol, result):
        if result.get("error"):
            logger.error(
                "[매수 에러] %s(%s): %s",
                name, symbol, result["error"],
                extra={"task": "kis.run_per_domestic_stock_automation"},
            )
```

- [ ] **Step 3: Write OverseasAutomationAdapter subclass**

```python
@dataclass(slots=True)
class OverseasAutomationAdapter(BaseAutomationAdapter):
    market: str = "overseas"
    market_type_label: str = "해외주식"
    result_symbol_key: str = "symbol"
    toss_market_type: str = "us"
    toss_currency: str = "$"
    refresh_holdings_after_sell_cancel: bool = False

    async def fetch_holdings(self, kis):
        return await kis.fetch_my_overseas_stocks()

    async def fetch_open_orders(self, kis):
        orders_by_id: dict[str, dict] = {}
        anonymous: list[dict] = []
        for exchange in ("NASD", "NYSE", "AMEX"):
            try:
                open_orders = await kis.inquire_overseas_orders(
                    exchange_code=exchange, is_mock=False,
                )
            except Exception as exc:
                logger.warning("미체결 주문 조회 실패 (exchange=%s): %s", exchange, exc)
                continue
            for order in open_orders:
                oid = self._extract_order_id(order)
                if oid:
                    orders_by_id[oid] = order
                else:
                    anonymous.append(order)
        return list(orders_by_id.values()) + anonymous

    @staticmethod
    def _extract_order_id(order: dict) -> str:
        for key in ("odno", "ODNO", "ord_no", "ORD_NO"):
            if (v := order.get(key)):
                return str(v).strip()
        return ""

    def extract_stock_info(self, stock):
        return extract_overseas_stock_info(stock)

    def build_manual_entry(self, holding):
        qty_str = str(holding.quantity)
        return {
            "ovrs_pdno": holding.ticker,
            "ovrs_item_name": holding.display_name or holding.ticker,
            "ovrs_cblc_qty": qty_str,
            "ord_psbl_qty": qty_str,
            "pchs_avg_pric": str(holding.avg_price),
            "now_pric2": "0",
            "_is_manual": True,
        }

    def is_same_symbol(self, stock, ticker):
        return to_db_symbol(stock.get("ovrs_pdno", "")) == to_db_symbol(ticker)

    async def fetch_manual_price(self, kis, symbol):
        df = await kis.inquire_overseas_price(symbol)
        if not df.empty:
            return float(df.iloc[0]["close"])
        return 0.0

    async def resolve_exchange(self, symbol, stock):
        from app.services.us_symbol_universe_service import get_us_exchange_by_symbol

        preferred = stock.get("ovrs_excg_cd") if isinstance(stock, dict) else None
        normalized = str(preferred or "").strip().upper()
        if normalized:
            return normalized
        return await get_us_exchange_by_symbol(symbol)

    async def cancel_pending(self, kis, symbol, order_type, all_open_orders, *, exchange_code=None):
        # Moved from kis_trading._cancel_overseas_pending_orders (lines 782-857)
        target_code = "02" if order_type == "buy" else "01"
        normalized_symbol = to_db_symbol(symbol)
        target_orders = [
            order
            for order in all_open_orders
            if to_db_symbol(order.get("pdno") or order.get("PDNO") or "") == normalized_symbol
            and (order.get("sll_buy_dvsn_cd") or order.get("SLL_BUY_DVSN_CD")) == target_code
        ]
        if not target_orders:
            return {"cancelled": 0, "failed": 0, "total": 0}

        cancelled = 0
        failed = 0
        for order in target_orders:
            order_number = None
            try:
                order_number = (
                    order.get("odno") or order.get("ODNO")
                    or order.get("ord_no") or order.get("ORD_NO")
                )
                order_qty = int(order.get("ft_ord_qty") or order.get("FT_ORD_QTY") or 0)
                if not order_number:
                    logger.warning("주문번호 없음 (%s): order=%s", symbol, order)
                    failed += 1
                    continue
                await kis.cancel_overseas_order(
                    order_number=order_number,
                    symbol=symbol,
                    exchange_code=exchange_code or "NASD",
                    quantity=order_qty,
                    is_mock=False,
                )
                cancelled += 1
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.warning("주문 취소 실패 (%s, %s): %s", symbol, order_number or "unknown", e)
                failed += 1
        return {"cancelled": cancelled, "failed": failed, "total": len(target_orders)}

    def analysis_target(self, *, name=None, symbol=None):
        return symbol or name or ""

    async def on_trade_exception(self, symbol, name, exc, trade_type):
        try:
            notifier = self.notifier_factory()
            await notifier.notify_trade_failure(
                symbol=symbol,
                korean_name=name or symbol,
                reason=f"{trade_type} 주문 실패: {exc}",
                market_type=self.market_type_label,
            )
        except Exception as notify_error:
            logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
```

- [ ] **Step 4: Remove cancel functions and old helpers from kis_trading.py**

In `app/jobs/kis_trading.py`, remove:
- `_cancel_domestic_pending_orders` (lines 303-385)
- `_cancel_overseas_pending_orders` (lines 782-857)
- `_extract_overseas_order_id` (lines 37-42)
- `_load_overseas_open_orders_all_exchanges` (lines 45-69)

Update adapter construction in `run_per_domestic_stock_automation` and `run_per_overseas_stock_automation` — remove `cancel_pending_orders`, `resolve_exchange_code`, `load_open_orders`, `normalize_symbol` fields. (Full update in Task 6.)

- [ ] **Step 5: Run tests (expect some adapter constructor tests to fail)**

Run: `uv run pytest tests/test_kis_tasks.py -v 2>&1 | head -60`
Expected: Constructor tests (`test_domestic_adapter_uses_name_*`, `test_overseas_adapter_uses_symbol_*`) FAIL due to removed fields. Other tests may fail too. This is expected — Task 7 fixes the tests.

- [ ] **Step 6: Commit (work-in-progress, tests will be fixed in Task 7)**

```bash
git add app/jobs/kis_market_adapters.py app/jobs/kis_trading.py
git commit -m "refactor(kis): extract BaseAutomationAdapter, migrate cancel to adapter methods

WIP: test_kis_tasks.py needs constructor updates (next commit)"
```

---

### Task 6: Handler wrappers, MarketHoldingsConfig, and unified bulk/task functions

**Files:**
- Modify: `app/jobs/kis_trading.py` (major rewrite — add config, unify functions, keep 16 wrappers)

- [ ] **Step 1: Add handler wrappers and MarketHoldingsConfig**

Add after the import section and constants:

```python
# app/jobs/kis_trading.py — after constants (NO_DOMESTIC_STOCKS_MESSAGE etc.)

from app.jobs.kis_market_adapters import (
    DomesticAutomationAdapter,
    OverseasAutomationAdapter,
    StockContext,
    extract_domestic_stock_info,
    extract_overseas_stock_info,
    match_domestic_stock,
    match_overseas_stock,
)


# === Handler wrappers (unified signature for adapter + config) ===

async def _domestic_buy(kis, symbol, price, avg, *, exchange_code=None):
    return await process_kis_domestic_buy_orders_with_analysis(kis, symbol, price, avg)


async def _domestic_sell(kis, symbol, price, avg, qty, *, exchange_code=None):
    return await process_kis_domestic_sell_orders_with_analysis(
        kis, symbol, price, avg, qty
    )


async def _overseas_buy(kis, symbol, price, avg, *, exchange_code=None):
    return await process_kis_overseas_buy_orders_with_analysis(
        kis, symbol, price, avg, exchange_code or "NASD"
    )


async def _overseas_sell(kis, symbol, price, avg, qty, *, exchange_code=None):
    return await process_kis_overseas_sell_orders_with_analysis(
        kis, symbol, price, avg, qty, exchange_code or "NASD"
    )


# === Price fetch helpers ===

async def _fetch_domestic_new_price(kis: KISClient, symbol: str) -> float:
    info = await kis.fetch_price(symbol)
    return float(info["output"]["stck_prpr"])


async def _fetch_overseas_new_price(kis: KISClient, symbol: str) -> float:
    return await kis.fetch_overseas_price(symbol)


# === MarketHoldingsConfig ===

@dataclass(frozen=True, slots=True)
class MarketHoldingsConfig:
    """Market-specific configuration for bulk and single-stock operations."""

    fetch_holdings: Callable[..., Awaitable[list[dict[str, Any]]]]
    extract_info: Callable[[dict[str, Any]], StockContext]
    match_stock: Callable[[list[dict[str, Any]], str], dict[str, Any] | None]
    resolve_exchange: Callable[..., Awaitable[str]] | None
    process_buy: Callable[..., Awaitable[dict[str, Any]]]
    process_sell: Callable[..., Awaitable[dict[str, Any]]]
    fetch_new_price: Callable[..., Awaitable[float]]
    no_stocks_message: str
    result_symbol_key: str
    market_type_label: str | None  # None = no Telegram notifications


_DOMESTIC_CFG = MarketHoldingsConfig(
    fetch_holdings=lambda kis: kis.fetch_my_stocks(),
    extract_info=extract_domestic_stock_info,
    match_stock=match_domestic_stock,
    resolve_exchange=None,
    process_buy=_domestic_buy,
    process_sell=_domestic_sell,
    fetch_new_price=_fetch_domestic_new_price,
    no_stocks_message=NO_DOMESTIC_STOCKS_MESSAGE,
    result_symbol_key="code",
    market_type_label=None,
)

_OVERSEAS_CFG = MarketHoldingsConfig(
    fetch_holdings=lambda kis: kis.fetch_my_overseas_stocks(),
    extract_info=extract_overseas_stock_info,
    match_stock=match_overseas_stock,
    resolve_exchange=_resolve_overseas_exchange_code,
    process_buy=_overseas_buy,
    process_sell=_overseas_sell,
    fetch_new_price=_fetch_overseas_new_price,
    no_stocks_message=NO_OVERSEAS_STOCKS_MESSAGE,
    result_symbol_key="symbol",
    market_type_label="해외주식",
)
```

Note: Add `from dataclasses import dataclass` and `from collections.abc import Awaitable, Callable` to imports if not already present. Also `from typing import Any`.

- [ ] **Step 2: Write unified bulk and task functions**

```python
async def _execute_bulk_buy_orders(cfg: MarketHoldingsConfig) -> dict:
    kis = KISClient()
    try:
        my_stocks = await cfg.fetch_holdings(kis)
        if not my_stocks:
            return {
                "status": "completed",
                "success_count": 0,
                "total_count": 0,
                "message": cfg.no_stocks_message,
                "results": [],
            }

        results = []
        for stock in my_stocks:
            ctx = cfg.extract_info(stock)
            if cfg.resolve_exchange:
                ctx.exchange_code = await cfg.resolve_exchange(
                    ctx.symbol, ctx.exchange_code
                )

            try:
                res = await cfg.process_buy(
                    kis, ctx.symbol, ctx.current_price, ctx.avg_price,
                    exchange_code=ctx.exchange_code,
                )
                results.append({
                    "name": ctx.name,
                    cfg.result_symbol_key: ctx.symbol,
                    "success": res["success"],
                    "message": res["message"],
                })
                if (
                    cfg.market_type_label
                    and res.get("success")
                    and res.get("orders_placed", 0) > 0
                ):
                    try:
                        notifier = get_trade_notifier()
                        await notifier.notify_buy_order(
                            symbol=ctx.symbol,
                            korean_name=ctx.name or ctx.symbol,
                            order_count=res.get("orders_placed", 0),
                            total_amount=res.get("total_amount", 0.0),
                            prices=res.get("prices", []),
                            volumes=res.get("quantities", []),
                            market_type=cfg.market_type_label,
                        )
                    except Exception as notify_error:
                        logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
            except Exception as e:
                results.append({
                    "name": ctx.name,
                    cfg.result_symbol_key: ctx.symbol,
                    "success": False,
                    "error": str(e),
                })
                if cfg.market_type_label:
                    try:
                        notifier = get_trade_notifier()
                        await notifier.notify_trade_failure(
                            symbol=ctx.symbol,
                            korean_name=ctx.name or ctx.symbol,
                            reason=f"매수 주문 실패: {e}",
                            market_type=cfg.market_type_label,
                        )
                    except Exception as notify_error:
                        logger.warning("텔레그램 알림 전송 실패: %s", notify_error)

        success_count = sum(1 for r in results if r.get("success"))
        return {
            "status": "completed",
            "success_count": success_count,
            "total_count": len(my_stocks),
            "message": f"{success_count}/{len(my_stocks)}개 종목 매수 주문 완료",
            "results": results,
        }
    except Exception as e:
        return {"status": "failed", "error": str(e)}


async def _execute_bulk_sell_orders(cfg: MarketHoldingsConfig) -> dict:
    kis = KISClient()
    try:
        my_stocks = await cfg.fetch_holdings(kis)
        if not my_stocks:
            return {
                "status": "completed",
                "success_count": 0,
                "total_count": 0,
                "message": cfg.no_stocks_message,
                "results": [],
            }

        results = []
        for stock in my_stocks:
            ctx = cfg.extract_info(stock)
            if cfg.resolve_exchange:
                ctx.exchange_code = await cfg.resolve_exchange(
                    ctx.symbol, ctx.exchange_code
                )

            try:
                res = await cfg.process_sell(
                    kis, ctx.symbol, ctx.current_price, ctx.avg_price, ctx.qty,
                    exchange_code=ctx.exchange_code,
                )
                results.append({
                    "name": ctx.name,
                    cfg.result_symbol_key: ctx.symbol,
                    "success": res["success"],
                    "message": res["message"],
                })
                if (
                    cfg.market_type_label
                    and res.get("success")
                    and res.get("orders_placed", 0) > 0
                ):
                    try:
                        notifier = get_trade_notifier()
                        await notifier.notify_sell_order(
                            symbol=ctx.symbol,
                            korean_name=ctx.name or ctx.symbol,
                            order_count=res.get("orders_placed", 0),
                            total_volume=res.get("total_volume", 0),
                            prices=res.get("prices", []),
                            volumes=res.get("quantities", []),
                            expected_amount=res.get("expected_amount", 0.0),
                            market_type=cfg.market_type_label,
                        )
                    except Exception as notify_error:
                        logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
            except Exception as e:
                results.append({
                    "name": ctx.name,
                    cfg.result_symbol_key: ctx.symbol,
                    "success": False,
                    "error": str(e),
                })
                if cfg.market_type_label:
                    try:
                        notifier = get_trade_notifier()
                        await notifier.notify_trade_failure(
                            symbol=ctx.symbol,
                            korean_name=ctx.name or ctx.symbol,
                            reason=f"매도 주문 실패: {e}",
                            market_type=cfg.market_type_label,
                        )
                    except Exception as notify_error:
                        logger.warning("텔레그램 알림 전송 실패: %s", notify_error)

        success_count = sum(1 for r in results if r.get("success"))
        return {
            "status": "completed",
            "success_count": success_count,
            "total_count": len(my_stocks),
            "message": f"{success_count}/{len(my_stocks)}개 종목 매도 주문 완료",
            "results": results,
        }
    except Exception as e:
        return {"status": "failed", "error": str(e)}


async def _execute_single_buy_task(cfg: MarketHoldingsConfig, symbol: str) -> dict:
    kis = KISClient()
    try:
        my_stocks = await cfg.fetch_holdings(kis)
        target = cfg.match_stock(my_stocks, symbol)

        if target:
            ctx = cfg.extract_info(target)
            if cfg.resolve_exchange:
                ctx.exchange_code = await cfg.resolve_exchange(
                    ctx.symbol, ctx.exchange_code
                )
        else:
            try:
                current_price = await cfg.fetch_new_price(kis, symbol)
            except Exception as price_error:
                return {"success": False, "message": f"현재가 조회 실패: {price_error}"}
            exchange_code = (
                await cfg.resolve_exchange(symbol, None) if cfg.resolve_exchange else None
            )
            ctx = StockContext(
                symbol=symbol, name="", avg_price=0.0,
                current_price=current_price, qty=0,
                is_manual=False, exchange_code=exchange_code,
            )

        return await cfg.process_buy(
            kis, ctx.symbol, ctx.current_price, ctx.avg_price,
            exchange_code=ctx.exchange_code,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}


async def _execute_single_sell_task(cfg: MarketHoldingsConfig, symbol: str) -> dict:
    kis = KISClient()
    try:
        my_stocks = await cfg.fetch_holdings(kis)
        target = cfg.match_stock(my_stocks, symbol)

        if not target:
            return {"success": False, "message": "보유 중인 주식이 아닙니다."}

        ctx = cfg.extract_info(target)
        if cfg.resolve_exchange:
            ctx.exchange_code = await cfg.resolve_exchange(
                ctx.symbol, ctx.exchange_code
            )

        return await cfg.process_sell(
            kis, ctx.symbol, ctx.current_price, ctx.avg_price, ctx.qty,
            exchange_code=ctx.exchange_code,
        )
    except Exception as e:
        return {"success": False, "error": str(e)}
```

- [ ] **Step 3: Replace all old functions with thin public wrappers**

Delete the old function bodies for all 8 pairs. Replace with:

```python
# === Analysis (dead code) ===

def _analyze_stock_ignored(symbol: str) -> dict:
    if not symbol:
        return {"status": "failed", "error": "종목 코드/심볼이 필요합니다."}
    return {
        "status": "ignored",
        "symbol": symbol,
        "message": "Gemini analyzer removed. OpenClaw-based analysis coming soon.",
    }


async def run_analysis_for_my_domestic_stocks() -> dict:
    return {"status": "ignored", "message": "Gemini analyzer removed. OpenClaw-based analysis coming soon.", "results": []}


async def run_analysis_for_my_overseas_stocks() -> dict:
    return {"status": "ignored", "message": "Gemini analyzer removed. OpenClaw-based analysis coming soon.", "results": []}


async def analyze_domestic_stock_task(symbol: str) -> dict:
    return _analyze_stock_ignored(symbol)


async def analyze_overseas_stock_task(symbol: str) -> dict:
    return _analyze_stock_ignored(symbol)


# === Bulk orders ===

async def execute_domestic_buy_orders() -> dict:
    return await _execute_bulk_buy_orders(_DOMESTIC_CFG)


async def execute_overseas_buy_orders() -> dict:
    return await _execute_bulk_buy_orders(_OVERSEAS_CFG)


async def execute_domestic_sell_orders() -> dict:
    return await _execute_bulk_sell_orders(_DOMESTIC_CFG)


async def execute_overseas_sell_orders() -> dict:
    return await _execute_bulk_sell_orders(_OVERSEAS_CFG)


# === Single stock tasks ===

async def execute_domestic_buy_order_task(symbol: str) -> dict:
    return await _execute_single_buy_task(_DOMESTIC_CFG, symbol)


async def execute_overseas_buy_order_task(symbol: str) -> dict:
    return await _execute_single_buy_task(_OVERSEAS_CFG, symbol)


async def execute_domestic_sell_order_task(symbol: str) -> dict:
    return await _execute_single_sell_task(_DOMESTIC_CFG, symbol)


async def execute_overseas_sell_order_task(symbol: str) -> dict:
    return await _execute_single_sell_task(_OVERSEAS_CFG, symbol)


# === Per-stock automation ===

async def run_per_domestic_stock_automation() -> dict:
    from app.core.db import AsyncSessionLocal
    from app.models.manual_holdings import MarketType
    from app.services.manual_holdings_service import ManualHoldingsService

    adapter = DomesticAutomationAdapter(
        kis_client_factory=KISClient,
        async_session_factory=AsyncSessionLocal,
        manual_holdings_service_factory=ManualHoldingsService,
        manual_market_type=MarketType.KR,
        buy_handler=_domestic_buy,
        sell_handler=_domestic_sell,
        send_toss_recommendation=_send_toss_recommendation_async,
        notifier_factory=get_trade_notifier,
        no_stocks_message=NO_DOMESTIC_STOCKS_MESSAGE,
    )
    return await kis_automation_runner.run_market_automation(adapter=adapter)


async def run_per_overseas_stock_automation() -> dict:
    from app.core.db import AsyncSessionLocal
    from app.models.manual_holdings import MarketType
    from app.services.manual_holdings_service import ManualHoldingsService

    adapter = OverseasAutomationAdapter(
        kis_client_factory=KISClient,
        async_session_factory=AsyncSessionLocal,
        manual_holdings_service_factory=ManualHoldingsService,
        manual_market_type=MarketType.US,
        buy_handler=_overseas_buy,
        sell_handler=_overseas_sell,
        send_toss_recommendation=_send_toss_recommendation_async,
        notifier_factory=get_trade_notifier,
        no_stocks_message=NO_OVERSEAS_STOCKS_MESSAGE,
    )
    return await kis_automation_runner.run_market_automation(adapter=adapter)
```

Also remove the now-unused imports: `from app.services.us_symbol_universe_service import get_us_exchange_by_symbol` is still needed by `_resolve_overseas_exchange_code`. Keep `_resolve_overseas_exchange_code` and `_send_toss_recommendation_async` as module-level helpers.

- [ ] **Step 4: Commit (tests still need fixing)**

```bash
git add app/jobs/kis_trading.py
git commit -m "refactor(kis): unify bulk/task functions with MarketHoldingsConfig

WIP: test_kis_tasks.py adapter constructor tests need updates"
```

---

### Task 7: Update tests for new adapter interface

**Files:**
- Modify: `tests/test_kis_tasks.py`

- [ ] **Step 1: Fix adapter constructor tests**

Update `test_domestic_adapter_uses_name_and_refreshes_after_sell_cancel`:

```python
def test_domestic_adapter_uses_name_and_refreshes_after_sell_cancel():
    from app.jobs.kis_market_adapters import DomesticAutomationAdapter

    async def fake_buy(*args, **kwargs):
        return {"success": True}

    async def fake_sell(*args, **kwargs):
        return {"success": True}

    async def fake_toss(*args, **kwargs):
        return None

    adapter = DomesticAutomationAdapter(
        kis_client_factory=lambda: None,
        async_session_factory=lambda: None,
        manual_holdings_service_factory=lambda db: None,
        manual_market_type="KR",
        buy_handler=fake_buy,
        sell_handler=fake_sell,
        send_toss_recommendation=fake_toss,
        notifier_factory=lambda: None,
        no_stocks_message="none",
    )

    assert adapter.analysis_target(name="삼성전자우", symbol="005935") == "삼성전자우"
    assert adapter.refresh_holdings_after_sell_cancel is True
    assert adapter.build_result_entry(name="삼성전자우", symbol="005935", steps=[]) == {
        "name": "삼성전자우",
        "code": "005935",
        "steps": [],
    }
```

Update `test_overseas_adapter_uses_symbol_without_sell_cancel_refresh`:

```python
def test_overseas_adapter_uses_symbol_without_sell_cancel_refresh():
    from app.jobs.kis_market_adapters import OverseasAutomationAdapter

    async def fake_buy(*args, **kwargs):
        return {"success": True}

    async def fake_sell(*args, **kwargs):
        return {"success": True}

    async def fake_toss(*args, **kwargs):
        return None

    adapter = OverseasAutomationAdapter(
        kis_client_factory=lambda: None,
        async_session_factory=lambda: None,
        manual_holdings_service_factory=lambda db: None,
        manual_market_type="US",
        buy_handler=fake_buy,
        sell_handler=fake_sell,
        send_toss_recommendation=fake_toss,
        notifier_factory=lambda: None,
        no_stocks_message="none",
    )

    assert adapter.analysis_target(name="Apple", symbol="AAPL") == "AAPL"
    assert adapter.refresh_holdings_after_sell_cancel is False
    assert adapter.build_result_entry(name="Apple", symbol="AAPL", steps=[]) == {
        "name": "Apple",
        "symbol": "AAPL",
        "steps": [],
    }
```

- [ ] **Step 2: Update handler monkeypatch targets if needed**

The existing monkeypatch pattern (`monkeypatch.setattr(kis_tasks, "process_kis_domestic_buy_orders_with_analysis", fake_buy)`) still works because `_domestic_buy` wrapper calls this function via global name lookup. Verify this by running the integration tests.

For tests that directly create adapters with handler arguments (like `test_run_per_domestic_stock_automation_executes_all_steps`), the monkeypatch on the module-level function name flows through `_domestic_buy` → monkeypatched function.

- [ ] **Step 3: Run all tests**

Run: `uv run pytest tests/test_kis_tasks.py tests/test_kis_trading_service.py tests/test_kis_trading_service_exception_handling.py tests/test_kis_order_ops.py tests/test_kis_market_adapters_helpers.py -v`
Expected: ALL tests PASS

If any test fails, debug by checking:
1. Adapter constructor argument mismatch → remove old fields
2. Handler signature mismatch → ensure mock handlers accept `**kwargs`
3. Cancel behavior → adapter methods now call KIS directly, DummyKIS must provide the methods

- [ ] **Step 4: Commit**

```bash
git add tests/test_kis_tasks.py
git commit -m "test(kis): update adapter constructor tests for new interface"
```

---

### Task 8: Final validation and cleanup

**Files:**
- All modified files

- [ ] **Step 1: Run full unit test suite**

Run: `make test-unit`
Expected: ALL tests PASS

- [ ] **Step 2: Run linter**

Run: `make lint`
Expected: No errors (fix any formatting issues with `make format`)

- [ ] **Step 3: Verify line count reduction**

Run:
```bash
wc -l app/services/kis_trading_service.py app/jobs/kis_market_adapters.py app/jobs/kis_trading.py
```
Expected: Total ~1,300 lines (down from ~2,380)

- [ ] **Step 4: Verify all 16 public functions are still importable**

```bash
uv run python -c "
from app.jobs.kis_trading import (
    run_analysis_for_my_domestic_stocks,
    run_analysis_for_my_overseas_stocks,
    execute_domestic_buy_orders,
    execute_overseas_buy_orders,
    execute_domestic_sell_orders,
    execute_overseas_sell_orders,
    run_per_domestic_stock_automation,
    run_per_overseas_stock_automation,
    analyze_domestic_stock_task,
    analyze_overseas_stock_task,
    execute_domestic_buy_order_task,
    execute_overseas_buy_order_task,
    execute_domestic_sell_order_task,
    execute_overseas_sell_order_task,
)
from app.services.kis_trading_service import (
    process_kis_domestic_buy_orders_with_analysis,
    process_kis_overseas_buy_orders_with_analysis,
    process_kis_domestic_sell_orders_with_analysis,
    process_kis_overseas_sell_orders_with_analysis,
)
print('All 18 public functions importable')
"
```
Expected: "All 18 public functions importable"

- [ ] **Step 5: Final commit**

```bash
git add -A
git commit -m "refactor(kis): complete domestic/overseas mirroring removal via adapter pattern

- kis_trading_service.py: unified buy/sell impl via SupportsOrderExecution protocol
- kis_market_adapters.py: BaseAutomationAdapter with shared execute(), thin subclasses
- kis_trading.py: MarketHoldingsConfig for bulk/task, 16 public wrappers preserved
- ~1,080 lines removed (~45% reduction across 3 files)"
```

---

## Verification Checklist

- [ ] `make test-unit` — all tests pass
- [ ] `make lint` — no errors
- [ ] All 16 public function signatures unchanged
- [ ] All 4 `kis_trading_service.py` public function signatures unchanged
- [ ] `SupportsMarketAutomation` Protocol unchanged
- [ ] `kis_automation_runner.py` — no changes needed
- [ ] No logic changes — same behavior before and after
