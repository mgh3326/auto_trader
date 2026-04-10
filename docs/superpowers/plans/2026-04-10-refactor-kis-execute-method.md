# Refactor `BaseAutomationAdapter.execute()` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Split the 327-line `execute()` method and 58-line `cancel_pending` methods in `app/jobs/kis_market_adapters.py` into focused private methods (each under 50 lines), and pull up duplicated `cancel_pending` logic from both subclasses into the base class.

**Architecture:** Extract Method refactoring on `BaseAutomationAdapter.execute()` into 6 private methods (`_prepare_holdings`, `_resolve_manual_price`, `_execute_buy_orders`, `_execute_sell_orders`, `_handle_manual_sell`, `_aggregate_results`). Pull up `cancel_pending` loop from both subclasses into base class with two new hooks (`_filter_pending_orders`, `_cancel_single_order`). Existing tests remain unchanged — they validate behavioral equivalence.

**Tech Stack:** Python 3.13, dataclasses, asyncio

**Constraints:**
- `app/mcp_server/tooling/` is off-limits
- `AutomationResult`, `SupportsMarketAutomation`, and external import paths must not change
- `kis_automation_runner.py` must work without modification

---

## File Structure

Only one file is modified:

- **Modify:** `app/jobs/kis_market_adapters.py`
  - `BaseAutomationAdapter`: add 7 new private methods, rewrite `execute()`, make `cancel_pending` concrete
  - `DomesticAutomationAdapter`: replace `cancel_pending` with `_filter_pending_orders` + `_cancel_single_order`
  - `OverseasAutomationAdapter`: replace `cancel_pending` with `_filter_pending_orders` + `_cancel_single_order`
- **Test:** `tests/test_kis_tasks.py` (existing, no changes)
- **Test:** `tests/test_kis_market_adapters_helpers.py` (existing, no changes)

---

### Task 1: Baseline Verification

**Files:**
- Read: `app/jobs/kis_market_adapters.py`

- [ ] **Step 1: Run existing tests to establish baseline**

Run: `uv run pytest tests/test_kis_tasks.py tests/test_kis_market_adapters_helpers.py -v`
Expected: All tests PASS

- [ ] **Step 2: Run lint to establish baseline**

Run: `make lint`
Expected: No errors

---

### Task 2: Extract `_prepare_holdings()` and `_resolve_manual_price()`

**Files:**
- Modify: `app/jobs/kis_market_adapters.py:76-501` (BaseAutomationAdapter)

- [ ] **Step 1: Add `_prepare_holdings` method to `BaseAutomationAdapter`**

Insert this method right before `execute()` (after the `build_result_entry` method, around line 172):

```python
    async def _prepare_holdings(
        self, kis: Any
    ) -> tuple[list[dict[str, Any]] | None, list[dict[str, Any]]]:
        """Fetch KIS + manual holdings, merge, and fetch open orders.

        Returns (None, []) when no stocks are held.
        """
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
            return None, []

        all_open_orders = await self.fetch_open_orders(kis)
        logger.info(
            "%s 미체결 주문 조회 완료: %s건",
            self.market_type_label,
            len(all_open_orders),
        )
        return my_stocks, all_open_orders
```

- [ ] **Step 2: Add `_resolve_manual_price` method to `BaseAutomationAdapter`**

Insert right after `_prepare_holdings`:

```python
    async def _resolve_manual_price(self, kis: Any, ctx: StockContext) -> None:
        """Fetch live price for manual holdings; fall back to avg_price on failure."""
        try:
            ctx.current_price = await self.fetch_manual_price(kis, ctx.symbol)
            logger.info(
                "[수동잔고] %s(%s) 현재가 조회: %s",
                ctx.name,
                ctx.symbol,
                ctx.current_price,
            )
        except Exception as exc:
            logger.warning(
                "[수동잔고] %s(%s) 현재가 조회 실패, 평단가 사용: %s",
                ctx.name,
                ctx.symbol,
                exc,
            )
            ctx.current_price = ctx.avg_price
```

- [ ] **Step 3: Replace the top of `execute()` to call `_prepare_holdings` and `_resolve_manual_price`**

Replace lines 175-234 of `execute()` (from `async def execute` through the manual price fetch block) with:

```python
    async def execute(self) -> AutomationResult:
        """Unified per-stock automation: cancel -> buy -> refresh -> sell."""
        kis = self.kis_client_factory()

        try:
            my_stocks, all_open_orders = await self._prepare_holdings(kis)
            if my_stocks is None:
                return {
                    "status": "completed",
                    "message": self.no_stocks_message,
                    "results": [],
                }

            results: list[AutomationResult] = []

            for stock in my_stocks:
                ctx = self.extract_stock_info(stock)
                ctx.exchange_code = await self.resolve_exchange(ctx.symbol, stock)

                if ctx.is_manual:
                    await self._resolve_manual_price(kis, ctx)

                stock_steps: StepResults = []

                # Analysis step skipped
                stock_steps.append(
                    {
                        "step": "분석",
                        "result": {
                            "success": True,
                            "message": "분석 스킵 (대체 분석기 준비 중)",
                        },
                    }
                )
```

Keep the rest of the per-stock loop (cancel buy, buy, refresh, sell, etc.) unchanged for now.

- [ ] **Step 4: Run tests to verify**

Run: `uv run pytest tests/test_kis_tasks.py tests/test_kis_market_adapters_helpers.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/jobs/kis_market_adapters.py
git commit -m "refactor(kis): extract _prepare_holdings and _resolve_manual_price from execute()"
```

---

### Task 3: Extract `_execute_buy_orders()`

**Files:**
- Modify: `app/jobs/kis_market_adapters.py` (BaseAutomationAdapter)

- [ ] **Step 1: Add `_execute_buy_orders` method to `BaseAutomationAdapter`**

Insert after `_resolve_manual_price`:

```python
    async def _execute_buy_orders(
        self,
        kis: Any,
        ctx: StockContext,
        all_open_orders: list[dict[str, Any]],
        stock_steps: StepResults,
    ) -> None:
        """Cancel pending buy orders, execute buy, notify, and refresh holdings."""
        # --- Cancel pending buy orders ---
        try:
            cancel_result = await self.cancel_pending(
                kis,
                ctx.symbol,
                "buy",
                all_open_orders,
                exchange_code=ctx.exchange_code,
            )
            if cancel_result["total"] > 0:
                logger.info(
                    "%s 미체결 매수 주문 취소: %s/%s건",
                    ctx.name or ctx.symbol,
                    cancel_result["cancelled"],
                    cancel_result["total"],
                )
                stock_steps.append(
                    {
                        "step": "매수취소",
                        "result": {"success": True, **cancel_result},
                    }
                )
                await asyncio.sleep(0.5)
        except Exception as exc:
            logger.warning(
                "%s 미체결 매수 주문 취소 실패: %s", ctx.name or ctx.symbol, exc
            )
            stock_steps.append(
                {
                    "step": "매수취소",
                    "result": {"success": False, "error": str(exc)},
                }
            )

        # --- Buy ---
        try:
            buy_result = await self.buy_handler(
                kis,
                ctx.symbol,
                ctx.current_price,
                ctx.avg_price,
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
            stock_steps.append(
                {
                    "step": "매수",
                    "result": {"success": False, "error": error_msg},
                }
            )
            logger.error(
                "[매수 실패] %s(%s): %s",
                ctx.name,
                ctx.symbol,
                error_msg,
            )
            await self.on_trade_exception(ctx.symbol, ctx.name, exc, "매수")

        # --- Refresh after buy ---
        (
            ctx.qty,
            ctx.avg_price,
            ctx.current_price,
        ) = await self.refresh_after_buy(
            kis,
            ctx.symbol,
            ctx.qty,
            ctx.avg_price,
            ctx.current_price,
        )
```

- [ ] **Step 2: Replace inline buy code in `execute()` with method call**

In `execute()`, replace the cancel-buy / buy / refresh-after-buy block (the section after the analysis step append through `refresh_after_buy`) with:

```python
                await self._execute_buy_orders(
                    kis, ctx, all_open_orders, stock_steps
                )
```

Keep the manual sell / regular sell / results.append code unchanged for now.

- [ ] **Step 3: Run tests to verify**

Run: `uv run pytest tests/test_kis_tasks.py tests/test_kis_market_adapters_helpers.py -v`
Expected: All tests PASS

- [ ] **Step 4: Commit**

```bash
git add app/jobs/kis_market_adapters.py
git commit -m "refactor(kis): extract _execute_buy_orders from execute()"
```

---

### Task 4: Extract `_execute_sell_orders()`, `_handle_manual_sell()`, `_aggregate_results()`

**Files:**
- Modify: `app/jobs/kis_market_adapters.py` (BaseAutomationAdapter)

- [ ] **Step 1: Add `_handle_manual_sell` method**

Insert after `_execute_buy_orders`:

```python
    async def _handle_manual_sell(
        self, kis: Any, ctx: StockContext, stock_steps: StepResults
    ) -> None:
        """Send toss recommendation for manual holdings instead of KIS sell."""
        logger.info(
            "[수동잔고] %s(%s) - KIS 매도 불가, 토스 추천 알림 발송",
            ctx.name,
            ctx.symbol,
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
            stock_steps.append(
                {
                    "step": "매도",
                    "result": {
                        "success": True,
                        "message": "수동잔고 - 토스 추천 알림 발송",
                        "orders_placed": 0,
                    },
                }
            )
        except Exception as exc:
            logger.warning(
                "[수동잔고] %s(%s) 토스 추천 알림 발송 실패: %s",
                ctx.name,
                ctx.symbol,
                exc,
            )
            stock_steps.append(
                {
                    "step": "매도",
                    "result": {
                        "success": True,
                        "message": "수동잔고 - 매도 스킵",
                        "orders_placed": 0,
                    },
                }
            )
```

- [ ] **Step 2: Add `_execute_sell_orders` method**

Insert after `_handle_manual_sell`:

```python
    async def _execute_sell_orders(
        self,
        kis: Any,
        ctx: StockContext,
        all_open_orders: list[dict[str, Any]],
        stock_steps: StepResults,
    ) -> None:
        """Cancel pending sell orders, refresh, and execute sell.

        For manual holdings, delegates to toss recommendation instead.
        """
        if ctx.is_manual:
            await self._handle_manual_sell(kis, ctx, stock_steps)
            return

        # --- Cancel pending sell orders ---
        sell_orders_cancelled = False
        try:
            cancel_result = await self.cancel_pending(
                kis,
                ctx.symbol,
                "sell",
                all_open_orders,
                exchange_code=ctx.exchange_code,
            )
            if cancel_result["total"] > 0:
                logger.info(
                    "%s 미체결 매도 주문 취소: %s/%s건",
                    ctx.name or ctx.symbol,
                    cancel_result["cancelled"],
                    cancel_result["total"],
                )
                stock_steps.append(
                    {
                        "step": "매도취소",
                        "result": {"success": True, **cancel_result},
                    }
                )
                sell_orders_cancelled = cancel_result["cancelled"] > 0
                await asyncio.sleep(0.5)
        except Exception as exc:
            logger.warning(
                "%s 미체결 매도 주문 취소 실패: %s", ctx.name or ctx.symbol, exc
            )
            stock_steps.append(
                {
                    "step": "매도취소",
                    "result": {"success": False, "error": str(exc)},
                }
            )

        # --- Refresh after sell cancel ---
        if sell_orders_cancelled and self.refresh_holdings_after_sell_cancel:
            ctx.qty, ctx.current_price = await self.refresh_after_sell_cancel(
                kis,
                ctx.symbol,
                ctx.qty,
                ctx.current_price,
            )

        # --- Sell ---
        try:
            sell_result = await self.sell_handler(
                kis,
                ctx.symbol,
                ctx.current_price,
                ctx.avg_price,
                ctx.qty,
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
                        expected_amount=sell_result.get(
                            "expected_amount", 0.0
                        ),
                        market_type=self.market_type_label,
                    )
                except Exception as notify_error:
                    logger.warning("텔레그램 알림 전송 실패: %s", notify_error)
        except Exception as exc:
            error_msg = str(exc)
            stock_steps.append(
                {
                    "step": "매도",
                    "result": {"success": False, "error": error_msg},
                }
            )
            logger.error(
                "[매도 실패] %s(%s): %s",
                ctx.name,
                ctx.symbol,
                error_msg,
            )
            await self.on_trade_exception(ctx.symbol, ctx.name, exc, "매도")
```

- [ ] **Step 3: Add `_aggregate_results` method**

Insert after `_execute_sell_orders`:

```python
    def _aggregate_results(
        self, results: list[AutomationResult]
    ) -> AutomationResult:
        return {
            "status": "completed",
            "message": "종목별 자동 실행 완료",
            "results": results,
        }
```

- [ ] **Step 4: Rewrite `execute()` to use all extracted methods**

Replace the entire `execute()` method with:

```python
    async def execute(self) -> AutomationResult:
        """Unified per-stock automation: cancel -> buy -> refresh -> sell."""
        kis = self.kis_client_factory()

        try:
            my_stocks, all_open_orders = await self._prepare_holdings(kis)
            if my_stocks is None:
                return {
                    "status": "completed",
                    "message": self.no_stocks_message,
                    "results": [],
                }

            results: list[AutomationResult] = []

            for stock in my_stocks:
                ctx = self.extract_stock_info(stock)
                ctx.exchange_code = await self.resolve_exchange(
                    ctx.symbol, stock
                )

                if ctx.is_manual:
                    await self._resolve_manual_price(kis, ctx)

                stock_steps: StepResults = [
                    {
                        "step": "분석",
                        "result": {
                            "success": True,
                            "message": "분석 스킵 (대체 분석기 준비 중)",
                        },
                    },
                ]

                await self._execute_buy_orders(
                    kis, ctx, all_open_orders, stock_steps
                )
                await self._execute_sell_orders(
                    kis, ctx, all_open_orders, stock_steps
                )

                results.append(
                    self.build_result_entry(
                        name=ctx.name,
                        symbol=ctx.symbol,
                        steps=stock_steps,
                    )
                )

            return self._aggregate_results(results)
        except Exception as exc:
            logger.error(
                "[태스크 실패] %s: %s",
                self.market_type_label,
                exc,
                exc_info=True,
            )
            return {"status": "failed", "error": str(exc)}
```

- [ ] **Step 5: Run tests to verify**

Run: `uv run pytest tests/test_kis_tasks.py tests/test_kis_market_adapters_helpers.py -v`
Expected: All tests PASS

- [ ] **Step 6: Commit**

```bash
git add app/jobs/kis_market_adapters.py
git commit -m "refactor(kis): extract _execute_sell_orders, _handle_manual_sell, _aggregate_results and rewrite execute()"
```

---

### Task 5: Pull Up `cancel_pending` to Base Class

Both `DomesticAutomationAdapter.cancel_pending` (58 lines) and `OverseasAutomationAdapter.cancel_pending` (47 lines) share identical loop structure. The only differences are symbol matching and the cancel API call.

**Files:**
- Modify: `app/jobs/kis_market_adapters.py:76-775`

- [ ] **Step 1: Replace base class `cancel_pending` with concrete implementation**

Replace the `cancel_pending` method in `BaseAutomationAdapter` (currently `raise NotImplementedError` at line ~118-127) with the shared loop logic, and add two new hook methods:

```python
    async def cancel_pending(
        self,
        kis: Any,
        symbol: str,
        order_type: str,
        all_open_orders: list[dict[str, Any]],
        *,
        exchange_code: str | None = None,
    ) -> dict[str, Any]:
        """Cancel pending orders matching symbol and type."""
        target_code = "02" if order_type == "buy" else "01"
        target_orders = self._filter_pending_orders(
            all_open_orders, symbol, target_code
        )
        if not target_orders:
            return {"cancelled": 0, "failed": 0, "total": 0}

        cancelled = 0
        failed = 0
        for order in target_orders:
            order_number = self._extract_order_number(order)
            if not order_number:
                logger.warning(
                    "주문번호 없음 (%s): order=%s", symbol, order
                )
                failed += 1
                continue
            try:
                await self._cancel_single_order(
                    kis,
                    symbol,
                    order,
                    order_number,
                    order_type,
                    exchange_code=exchange_code,
                )
                cancelled += 1
                await asyncio.sleep(0.2)
            except Exception as e:
                logger.warning(
                    "주문 취소 실패 (%s, %s): %s",
                    symbol,
                    order_number,
                    e,
                )
                failed += 1
        return {
            "cancelled": cancelled,
            "failed": failed,
            "total": len(target_orders),
        }

    @staticmethod
    def _extract_order_number(order: dict[str, Any]) -> str | None:
        return (
            order.get("odno")
            or order.get("ODNO")
            or order.get("ord_no")
            or order.get("ORD_NO")
        )

    def _filter_pending_orders(
        self,
        orders: list[dict[str, Any]],
        symbol: str,
        target_code: str,
    ) -> list[dict[str, Any]]:
        raise NotImplementedError

    async def _cancel_single_order(
        self,
        kis: Any,
        symbol: str,
        order: dict[str, Any],
        order_number: str,
        order_type: str,
        *,
        exchange_code: str | None = None,
    ) -> None:
        raise NotImplementedError
```

- [ ] **Step 2: Replace `DomesticAutomationAdapter.cancel_pending` with hook implementations**

Delete the entire `cancel_pending` method from `DomesticAutomationAdapter` and replace with:

```python
    def _filter_pending_orders(self, orders, symbol, target_code):
        return [
            order
            for order in orders
            if (order.get("pdno") or order.get("PDNO")) == symbol
            and (order.get("sll_buy_dvsn_cd") or order.get("SLL_BUY_DVSN_CD"))
            == target_code
        ]

    async def _cancel_single_order(
        self, kis, symbol, order, order_number, order_type, *, exchange_code=None
    ):
        order_qty = int(order.get("ord_qty") or order.get("ORD_QTY") or 0)
        order_price = int(
            float(order.get("ord_unpr") or order.get("ORD_UNPR") or 0)
        )
        order_orgno = (
            order.get("ord_gno_brno")
            or order.get("ORD_GNO_BRNO")
            or order.get("krx_fwdg_ord_orgno")
            or order.get("KRX_FWDG_ORD_ORGNO")
        )
        await kis.cancel_korea_order(
            order_number=order_number,
            stock_code=symbol,
            quantity=order_qty,
            price=order_price,
            order_type=order_type,
            is_mock=False,
            krx_fwdg_ord_orgno=str(order_orgno).strip()
            if order_orgno
            else None,
        )
```

- [ ] **Step 3: Replace `OverseasAutomationAdapter.cancel_pending` with hook implementations**

Delete the entire `cancel_pending` method from `OverseasAutomationAdapter` and replace with:

```python
    def _filter_pending_orders(self, orders, symbol, target_code):
        normalized_symbol = to_db_symbol(symbol)
        return [
            order
            for order in orders
            if to_db_symbol(order.get("pdno") or order.get("PDNO") or "")
            == normalized_symbol
            and (order.get("sll_buy_dvsn_cd") or order.get("SLL_BUY_DVSN_CD"))
            == target_code
        ]

    async def _cancel_single_order(
        self, kis, symbol, order, order_number, order_type, *, exchange_code=None
    ):
        order_qty = int(
            order.get("ft_ord_qty") or order.get("FT_ORD_QTY") or 0
        )
        await kis.cancel_overseas_order(
            order_number=order_number,
            symbol=symbol,
            exchange_code=exchange_code or "NASD",
            quantity=order_qty,
            is_mock=False,
        )
```

- [ ] **Step 4: Run tests to verify**

Run: `uv run pytest tests/test_kis_tasks.py tests/test_kis_market_adapters_helpers.py -v`
Expected: All tests PASS

- [ ] **Step 5: Commit**

```bash
git add app/jobs/kis_market_adapters.py
git commit -m "refactor(kis): pull up cancel_pending loop to base class with hook methods"
```

---

### Task 6: Final Verification

- [ ] **Step 1: Run lint**

Run: `make lint`
Expected: No errors

- [ ] **Step 2: Run full test suite**

Run: `uv run pytest tests/test_kis_tasks.py tests/test_kis_market_adapters_helpers.py -v`
Expected: All tests PASS

- [ ] **Step 3: Verify method sizes**

Run: `grep -n 'async def \|def ' app/jobs/kis_market_adapters.py` and manually verify each new method is under 50 lines.

Expected method line counts (approximate):
| Method | Lines |
|--------|-------|
| `execute()` | ~25 |
| `_prepare_holdings()` | ~25 |
| `_resolve_manual_price()` | ~14 |
| `_execute_buy_orders()` | ~48 |
| `_execute_sell_orders()` | ~50 |
| `_handle_manual_sell()` | ~30 |
| `_aggregate_results()` | ~5 |
| `cancel_pending()` (base) | ~30 |

- [ ] **Step 4: Commit (if any formatting fixes were needed)**

```bash
git add app/jobs/kis_market_adapters.py
git commit -m "style: fix formatting after execute() refactor"
```
