# Crypto Phase 2 Live Strategy Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** MCP crypto screening and trading flows reflect the validated Phase 2 live strategy baseline: 4.5% stop-loss, 8-day stop-loss cooldown, and RSI 46 mean-reversion exit.

**Architecture:** Add a small Redis-backed cooldown service that owns stop-loss TTL state for crypto symbols. Reuse that service from both the crypto screening path and the order execution path so `screen_stocks` hides cooled-down re-entry candidates and `place_order` rejects cooled-down crypto buys. Expose sell-side Phase 2 signals from `get_holdings` by evaluating each crypto holding's current PnL plus realtime RSI, without changing the existing public fields.

**Tech Stack:** Python 3.13, FastMCP tooling, Redis async client, Upbit quote/holding APIs, pytest, Ruff.

---

### Task 1: Add a Redis-backed crypto stop-loss cooldown service

**Files:**
- Create: `app/services/crypto_trade_cooldown_service.py`
- Test: `tests/test_crypto_trade_cooldown_service.py`

**Step 1: Write the failing tests**

Add tests for three behaviors:
- `record_stop_loss(symbol)` stores a TTL-backed key for `8 * 24 * 60 * 60` seconds.
- `is_in_cooldown(symbol)` returns `True` when Redis has the key, `False` when absent.
- Redis failures degrade safely: reads return `False`, writes do not raise.

```python
@pytest.mark.asyncio
async def test_record_stop_loss_sets_ttl(monkeypatch):
    fake_redis = AsyncMock()
    fake_redis.set = AsyncMock(return_value=True)
    monkeypatch.setattr(cooldown_service.redis_async, "from_url", AsyncMock(return_value=fake_redis))

    service = cooldown_service.CryptoTradeCooldownService()
    await service.record_stop_loss("KRW-BTC")

    fake_redis.set.assert_awaited_once_with(
        "crypto:stop_loss_cooldown:KRW-BTC",
        "1",
        ex=8 * 24 * 60 * 60,
    )
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_crypto_trade_cooldown_service.py -q`

Expected: FAIL with import/module errors because `app/services/crypto_trade_cooldown_service.py` does not exist yet.

**Step 3: Write the minimal implementation**

Implement a focused service with:
- constants:
  - `STOP_LOSS_COOLDOWN_DAYS = 8`
  - `STOP_LOSS_COOLDOWN_TTL_SECONDS = STOP_LOSS_COOLDOWN_DAYS * 24 * 60 * 60`
- helper key format:
  - `crypto:stop_loss_cooldown:{SYMBOL}`
- methods:
  - `async def is_in_cooldown(self, symbol: str) -> bool`
  - `async def record_stop_loss(self, symbol: str) -> None`
  - optional `async def get_remaining_ttl_seconds(self, symbol: str) -> int | None`
- Redis initialization via `redis.asyncio.from_url(settings.get_redis_url(), ...)`
- symbol normalization to uppercase `KRW-*`
- defensive error handling with logger warnings, not hard failures

```python
class CryptoTradeCooldownService:
    async def is_in_cooldown(self, symbol: str) -> bool:
        try:
            redis_client = await self._get_redis()
            return bool(await redis_client.get(self._key(symbol)))
        except Exception:
            logger.warning("crypto stop-loss cooldown read failed", exc_info=True)
            return False
```

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_crypto_trade_cooldown_service.py -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add app/services/crypto_trade_cooldown_service.py tests/test_crypto_trade_cooldown_service.py
git commit -m "feat: add crypto stop-loss cooldown service"
```

### Task 2: Filter cooled-down symbols out of crypto `screen_stocks`

**Files:**
- Modify: `app/mcp_server/tooling/screening/crypto.py`
- Modify: `tests/test_mcp_screen_stocks_crypto.py`
- Modify: `app/mcp_server/README.md`

**Step 1: Write the failing tests**

Extend crypto screening tests to lock these behaviors:
- cooled-down symbols are removed before final ranking/limit slicing
- response metadata exposes how many symbols were filtered by cooldown
- screening degradation on cooldown lookup failure is non-fatal and returns results

```python
@pytest.mark.asyncio
async def test_screen_stocks_crypto_filters_stop_loss_cooldown_symbols(monkeypatch):
    monkeypatch.setattr(
        screening_crypto,
        "_get_crypto_trade_cooldown_service",
        lambda: FakeCooldownService(blocked={"KRW-BTC"}),
    )

    result = await tools["screen_stocks"](market="crypto", limit=5)

    symbols = [item["symbol"] for item in result["results"]]
    assert "KRW-BTC" not in symbols
    assert result["meta"]["filtered_by_stop_loss_cooldown"] == 1
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks_crypto.py -k "cooldown" -q`

Expected: FAIL because the cooldown filter/meta fields do not exist yet.

**Step 3: Write the minimal implementation**

In `app/mcp_server/tooling/screening/crypto.py`:
- add a tiny module-level accessor, for example `_get_crypto_trade_cooldown_service()`
- in both `_screen_crypto()` and `_screen_crypto_via_tvscreener()`, check cooldown after symbol normalization and before final candidate list append
- count blocked symbols in `filtered_by_stop_loss_cooldown`
- append a warning only if cooldown lookup itself fails, not when a symbol is merely blocked
- preserve existing result ordering and limit behavior for remaining symbols
- pass the new count into `finalize_crypto_screen(...)` metadata, or enrich the returned payload immediately after finalization if changing the finalizer is smaller

```python
cooldown_service = _get_crypto_trade_cooldown_service()
filtered_by_stop_loss_cooldown = 0

for raw_item in top_candidates:
    market_code = str(raw_item.get("market") or "").strip().upper()
    if await cooldown_service.is_in_cooldown(market_code):
        filtered_by_stop_loss_cooldown += 1
        continue
```

Update `app/mcp_server/README.md`:
- document that crypto `screen_stocks` excludes symbols in an 8-day stop-loss cooldown window
- note the new `meta.filtered_by_stop_loss_cooldown` field

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_mcp_screen_stocks_crypto.py -k "cooldown" -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/screening/crypto.py tests/test_mcp_screen_stocks_crypto.py app/mcp_server/README.md
git commit -m "feat: filter crypto screen results by stop-loss cooldown"
```

### Task 3: Reject cooled-down crypto buys and record cooldown after stop-loss sells

**Files:**
- Modify: `app/mcp_server/tooling/order_execution.py`
- Modify: `tests/test_mcp_place_order.py`
- Possibly modify: `app/mcp_server/README.md`

**Step 1: Write the failing tests**

Add tests for:
- crypto buy order returns `success: false` when symbol is in cooldown
- market crypto sell below `avg_buy_price * (1 - 0.045)` records cooldown after successful non-dry-run execution
- dry-run stop-loss sells do not record cooldown
- profitable sells above the stop-loss threshold do not record cooldown

```python
@pytest.mark.asyncio
async def test_place_order_crypto_buy_blocked_by_stop_loss_cooldown(monkeypatch):
    _patch_runtime_attr(
        monkeypatch,
        "_get_crypto_trade_cooldown_service",
        lambda: FakeCooldownService(in_cooldown=True),
    )

    result = await tools["place_order"](
        symbol="KRW-BTC",
        side="buy",
        order_type="market",
        amount=100000.0,
        dry_run=True,
    )

    assert result["success"] is False
    assert "cooldown" in result["error"].lower()
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_mcp_place_order.py -k "cooldown or stop_loss" -q`

Expected: FAIL because `_place_order_impl()` does not check or record cooldown state.

**Step 3: Write the minimal implementation**

In `app/mcp_server/tooling/order_execution.py`:
- add local constants:
  - `CRYPTO_STOP_LOSS_PCT = 0.045`
- add `_get_crypto_trade_cooldown_service()` accessor
- before buy preview/balance logic:
  - if `market_type == "crypto"` and cooldown active, return `_order_error(...)`
- after successful non-dry-run execution:
  - only for `market_type == "crypto"` and `side == "sell"`
  - inspect holdings captured before execution or dry-run preview values
  - if current market sell price is `<= avg_price * (1 - CRYPTO_STOP_LOSS_PCT)`, call `record_stop_loss(normalized_symbol)`
- do not change existing limit-sell minimum-profit guard

Recommended shape:

```python
if side_lower == "buy" and market_type == "crypto":
    cooldown_service = _get_crypto_trade_cooldown_service()
    if await cooldown_service.is_in_cooldown(normalized_symbol):
        return _order_error(
            "Symbol is in stop-loss cooldown until re-entry window expires"
        )
```

For the post-execution write:

```python
if (
    market_type == "crypto"
    and side_lower == "sell"
    and not dry_run
    and avg_price > 0
    and current_price <= avg_price * (1 - CRYPTO_STOP_LOSS_PCT)
):
    await cooldown_service.record_stop_loss(normalized_symbol)
```

Update `app/mcp_server/README.md` if needed:
- `place_order(..., side="buy", market="crypto")` may reject buys while a stop-loss cooldown is active

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_mcp_place_order.py -k "cooldown or stop_loss" -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/order_execution.py tests/test_mcp_place_order.py app/mcp_server/README.md
git commit -m "feat: enforce crypto stop-loss cooldown in order flow"
```

### Task 4: Add stop-loss and mean-reversion sell signals to `get_holdings`

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_holdings.py`
- Modify: `app/mcp_server/tooling/shared.py`
- Modify: `tests/test_mcp_portfolio_tools.py`
- Modify: `app/mcp_server/README.md`

**Step 1: Write the failing tests**

Add tests for crypto holdings only:
- when `profit_rate <= -4.5`, the position includes a stop-loss sell signal
- when `profit_rate > 0` and realtime RSI 14 is `> 46`, the position includes a mean-reversion sell signal
- non-crypto positions remain unchanged
- positions without current price or RSI do not emit a false signal

Use a non-breaking output field set such as:
- `strategy_signal.action` (`"sell"`)
- `strategy_signal.reason` (`"stop_loss"` or `"mean_reversion_exit"`)
- `strategy_signal.threshold`
- `strategy_signal.rsi_14` when available

```python
@pytest.mark.asyncio
async def test_get_holdings_crypto_stop_loss_signal(monkeypatch):
    _patch_runtime_attr(
        monkeypatch,
        "_get_indicators_impl",
        AsyncMock(
            return_value={
                "symbol": "KRW-BTC",
                "indicators": {"rsi": {"14": 35.0}},
            }
        ),
    )

    result = await tools["get_holdings"](account="upbit", market="crypto")
    btc = result["accounts"][0]["positions"][0]

    assert btc["strategy_signal"]["action"] == "sell"
    assert btc["strategy_signal"]["reason"] == "stop_loss"
```

**Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_mcp_portfolio_tools.py -k "strategy_signal or mean_reversion or stop_loss" -q`

Expected: FAIL because holdings output does not contain strategy signal fields.

**Step 3: Write the minimal implementation**

In `app/mcp_server/tooling/portfolio_holdings.py`:
- add constants:
  - `CRYPTO_STOP_LOSS_PCT = -4.5`
  - `CRYPTO_MEAN_REVERSION_RSI_EXIT = 46.0`
- add a helper that evaluates a crypto position after current price recalculation:
  - stop-loss first when `profit_rate <= -4.5`
  - else mean-reversion exit when `profit_rate > 0` and realtime RSI 14 `> 46`
- fetch RSI only for crypto positions that have valid `current_price`
  - use existing `_get_indicators_impl(symbol, ["rsi"], market="crypto")`
  - batch with `asyncio.gather`
- store optional fields directly on internal position dict so `position_to_output()` can pass them through

In `app/mcp_server/tooling/shared.py`:
- extend `position_to_output()` to include:
  - `strategy_signal`
  - `rsi_14` only if you want the signal payload lighter, or keep RSI nested inside `strategy_signal`

Recommended helper shape:

```python
def _build_crypto_strategy_signal(
    position: dict[str, Any],
    *,
    rsi_14: float | None,
) -> dict[str, Any] | None:
    profit_rate = _to_optional_float(position.get("profit_rate"))
    if profit_rate is None:
        return None
    if profit_rate <= -4.5:
        return {"action": "sell", "reason": "stop_loss", "threshold_pct": -4.5}
    if profit_rate > 0 and rsi_14 is not None and rsi_14 > 46.0:
        return {"action": "sell", "reason": "mean_reversion_exit", "rsi_14": rsi_14}
    return None
```

Update `app/mcp_server/README.md`:
- `get_holdings` crypto positions may include optional `strategy_signal` when the Phase 2 exit logic triggers

**Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_mcp_portfolio_tools.py -k "strategy_signal or mean_reversion or stop_loss" -q`

Expected: PASS.

**Step 5: Commit**

```bash
git add app/mcp_server/tooling/portfolio_holdings.py app/mcp_server/tooling/shared.py tests/test_mcp_portfolio_tools.py app/mcp_server/README.md
git commit -m "feat: expose crypto phase2 exit signals in holdings"
```

### Task 5: Run focused verification and create the final feature commit

**Files:**
- Verify only

**Step 1: Run the targeted MCP regression suite**

Run: `uv run pytest tests/ -v -k "screen or holdings or order"`

Expected: PASS for the touched screening, holdings, and order flows.

**Step 2: Run lint on application code**

Run: `uv run ruff check app/`

Expected: PASS with no new Ruff violations.

**Step 3: Inspect the diff for contract drift**

Run: `git diff -- app/mcp_server/tooling/screening/crypto.py app/mcp_server/tooling/order_execution.py app/mcp_server/tooling/portfolio_holdings.py app/mcp_server/tooling/shared.py app/services/crypto_trade_cooldown_service.py app/mcp_server/README.md tests/test_crypto_trade_cooldown_service.py tests/test_mcp_screen_stocks_crypto.py tests/test_mcp_place_order.py tests/test_mcp_portfolio_tools.py`

Expected:
- cooldown service is the only new module
- public MCP contract changes are additive only (`meta.filtered_by_stop_loss_cooldown`, optional `strategy_signal`)
- no unrelated files changed

**Step 4: Create the final commit**

```bash
git add app/services/crypto_trade_cooldown_service.py app/mcp_server/tooling/screening/crypto.py app/mcp_server/tooling/order_execution.py app/mcp_server/tooling/portfolio_holdings.py app/mcp_server/tooling/shared.py app/mcp_server/README.md tests/test_crypto_trade_cooldown_service.py tests/test_mcp_screen_stocks_crypto.py tests/test_mcp_place_order.py tests/test_mcp_portfolio_tools.py
git commit -m "feat: add stop-loss, cooldown, and mean-reversion exit to crypto strategy"
```

**Step 5: Record residual checks**

Manually confirm after test/lint:
- buy cooldown is enforced for both `screen_stocks` and direct `place_order`
- cooldown is written only after an actual stop-loss sell execution
- `get_holdings` emits stop-loss before mean-reversion when both could appear true
