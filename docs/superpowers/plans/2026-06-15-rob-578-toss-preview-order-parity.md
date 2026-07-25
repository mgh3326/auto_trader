# ROB-578 Toss Preview Order Parity Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Enrich `toss_preview_order` with current price, limit fill distance, marketability warnings, and estimated Toss fee/FX costs so the preview is useful without a separate quote lookup.

**Architecture:** Keep the change inside the Toss MCP order tool boundary. Reuse `TossReadClient.prices()` for live market context, `account_costs`/`build_cost_profiles()` for operator-maintained Toss cost metadata, and `get_usd_krw_rate_details()` for US FX-cost conversion. Preserve the existing `warnings` field as Toss stock-warning rows and add `order_warnings` for KIS-style string warnings.

**Tech Stack:** Python 3.13, FastMCP tool handlers, Decimal math, pytest/pytest-asyncio, existing Toss broker DTOs and account-routing cost profiles.

---

## Decisions And Assumptions

- `warnings` remains the existing list of Toss stock-warning objects such as `{"warning_type": "OVERHEATED"}`. Add `order_warnings: list[str]` for price/marketability warnings like `buy_limit_above_market`, `sell_limit_below_market`, and `sell_limit_above_market`.
- Toss decimal response values should continue to be JSON-safe strings via `_stringify_decimal()`. This matches existing `payload_preview`, positions, cash, and order-history Toss MCP responses.
- US `fx_cost_full_conversion` is a full-notional conversion estimate: `notional_usd * usd_krw * fx_spread_bps / 10000`. It is labelled with `fx_assumption="full_notional_krw_conversion"`. Cash-aware FX cost remains the responsibility of ROB-565 `suggest_order_account`.
- Price lookup failures should not block a preview. Return the payload and cost estimate when possible, set `current_price=None`, omit `fill_distance`, add `price_context_unavailable` to `order_warnings`, and include `price_context_message`.
- This is a read-only preview contract change. It must not alter live mutation gates, `dry_run=False` behavior, accepted-order ledger writes, or sell-loss guard behavior.

## File Structure

- Modify: `app/mcp_server/tooling/orders_toss_variants.py`
  - Add preview-only helpers for quote context, limit fill distance, and cost estimates.
  - Enrich `toss_preview_order` response without changing mutation helpers.
- Modify: `tests/test_mcp_toss_order_variants.py`
  - Add focused tests for buy above market, sell below market, and quote-degraded preview behavior.
  - Extend only the local test stubs needed for preview enrichment.
- Modify: `app/mcp_server/README.md`
  - Document the new `toss_preview_order` response fields and the `warnings` vs `order_warnings` split.
- Optional after implementation: update ROB-578 in Linear with a short status comment if the implementation is not immediately shipped.

## Task 1: Add Failing Toss Preview Enrichment Tests

**Files:**
- Modify: `tests/test_mcp_toss_order_variants.py`

- [ ] **Step 1: Add the missing test import**

Add this import near the existing imports:

```python
from types import SimpleNamespace
```

- [ ] **Step 2: Add preview test helpers**

Add these helpers after `MockTossClient`:

```python
def _enable_toss_preview(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])
    return otv


def _stub_toss_costs(commission_bps: float = 10.0, fx_spread_bps: float = 1.7):
    return {
        "version": 1,
        "accounts": {
            "toss": {
                "broker": "toss",
                "markets": {
                    "kr": {"commission_bps": 0.0, "fx_spread_bps": 0.0},
                    "us": {
                        "commission_bps": commission_bps,
                        "fx_spread_bps": fx_spread_bps,
                    },
                },
            }
        },
    }


def _stub_usd_krw_quote(rate: float = 1360.0):
    return SimpleNamespace(
        rate=rate,
        mid_rate=rate,
        default_rate=rate,
        source="toss",
        valid_from=None,
        valid_until=None,
        basis_point=None,
        rate_change_type=None,
    )
```

- [ ] **Step 3: Add buy-limit above-market test**

Add this test near `test_preview_order_shapes_payload_and_rejects_invalid_inputs`:

```python
@pytest.mark.asyncio
async def test_toss_preview_buy_limit_above_market_returns_price_distance_and_costs(
    monkeypatch,
):
    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "AVGO", "last_price": Decimal("390"), "currency": "USD"}
    ]
    monkeypatch.setattr(
        otv,
        "get_account_costs_setting",
        AsyncMock(return_value=_stub_toss_costs()),
        raising=False,
    )
    monkeypatch.setattr(
        otv,
        "get_usd_krw_rate_details",
        AsyncMock(return_value=_stub_usd_krw_quote()),
        raising=False,
    )

    res = await toss_preview_order(
        symbol="AVGO",
        side="buy",
        order_type="limit",
        quantity="1",
        price="394",
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert res["current_price"] == "390"
    assert res["current_price_currency"] == "USD"
    assert res["order_warnings"] == ["buy_limit_above_market"]
    assert res["fill_distance"] == {
        "distance_usd": "4",
        "distance_pct": "1.0256",
        "currency": "USD",
        "marketable": True,
        "direction": "above_market",
    }
    assert res["estimated_value"] == "394"
    assert res["estimated_value_currency"] == "USD"
    assert res["fee"] == "0.394"
    assert res["fee_currency"] == "USD"
    assert res["fx_cost_full_conversion"] == "91.0928"
    assert res["fx_cost_full_conversion_currency"] == "KRW"
    assert res["estimated_costs"] == {
        "notional": "394",
        "notional_currency": "USD",
        "fee": "0.394",
        "fee_currency": "USD",
        "commission_bps": 10.0,
        "fx_spread_bps": 1.7,
        "fx_cost_full_conversion": "91.0928",
        "fx_cost_full_conversion_currency": "KRW",
        "fx_rate_usd_krw": "1360",
        "fx_rate_source": "toss",
        "fx_assumption": "full_notional_krw_conversion",
        "cost_profile_source": "user_setting",
        "cost_profile_review_required": False,
    }
```

- [ ] **Step 4: Add sell-limit below-market test**

```python
@pytest.mark.asyncio
async def test_toss_preview_sell_limit_below_market_returns_marketable_warning(
    monkeypatch,
):
    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = [
        {"symbol": "AVGO", "last_price": Decimal("390"), "currency": "USD"}
    ]
    monkeypatch.setattr(
        otv,
        "get_account_costs_setting",
        AsyncMock(return_value=_stub_toss_costs()),
        raising=False,
    )
    monkeypatch.setattr(
        otv,
        "get_usd_krw_rate_details",
        AsyncMock(return_value=_stub_usd_krw_quote()),
        raising=False,
    )

    res = await toss_preview_order(
        symbol="AVGO",
        side="sell",
        order_type="limit",
        quantity="1",
        price="380",
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert res["current_price"] == "390"
    assert res["order_warnings"] == ["sell_limit_below_market"]
    assert res["fill_distance"] == {
        "distance_usd": "10",
        "distance_pct": "2.5641",
        "currency": "USD",
        "marketable": True,
        "direction": "below_market",
    }
```

- [ ] **Step 5: Add quote-degraded preview test**

```python
@pytest.mark.asyncio
async def test_toss_preview_order_degrades_when_price_context_unavailable(monkeypatch):
    otv = _enable_toss_preview(monkeypatch)
    mock_client = MockTossClient(monkeypatch)
    mock_client.prices_list = []
    monkeypatch.setattr(
        otv,
        "get_account_costs_setting",
        AsyncMock(return_value=_stub_toss_costs()),
        raising=False,
    )
    monkeypatch.setattr(
        otv,
        "get_usd_krw_rate_details",
        AsyncMock(return_value=_stub_usd_krw_quote()),
        raising=False,
    )

    res = await toss_preview_order(
        symbol="AVGO",
        side="buy",
        order_type="limit",
        quantity="1",
        price="394",
        account_mode="toss_live",
    )

    assert res["success"] is True
    assert res["current_price"] is None
    assert res["current_price_currency"] is None
    assert "price_context_unavailable" in res["order_warnings"]
    assert "Could not resolve latest price for symbol: AVGO" in res[
        "price_context_message"
    ]
    assert "fill_distance" not in res
    assert res["estimated_value"] == "394"
    assert res["fee"] == "0.394"
    assert res["warnings"] == []
```

- [ ] **Step 6: Run the focused tests and verify they fail for the expected missing fields**

Run:

```bash
uv run pytest \
  tests/test_mcp_toss_order_variants.py::test_toss_preview_buy_limit_above_market_returns_price_distance_and_costs \
  tests/test_mcp_toss_order_variants.py::test_toss_preview_sell_limit_below_market_returns_marketable_warning \
  tests/test_mcp_toss_order_variants.py::test_toss_preview_order_degrades_when_price_context_unavailable \
  -q
```

Expected: FAIL because `toss_preview_order` does not yet return `current_price`, `order_warnings`, `fill_distance`, `estimated_value`, `fee`, or `fx_cost_full_conversion`.

- [ ] **Step 7: Commit failing tests**

```bash
git add tests/test_mcp_toss_order_variants.py
git commit -m "test(ROB-578): cover enriched Toss preview context"
```

## Task 2: Implement Preview Quote, Fill-Distance, And Cost Helpers

**Files:**
- Modify: `app/mcp_server/tooling/orders_toss_variants.py`

- [ ] **Step 1: Add imports**

Add these imports near the existing service imports:

```python
from app.mcp_server.tooling.portfolio_cash import get_account_costs_setting
from app.services.account_routing import build_cost_profiles
from app.services.exchange_rate_service import get_usd_krw_rate_details
```

- [ ] **Step 2: Add preview constants**

Add these constants after `TOSS_LIVE_ORDER_TOOL_NAMES`:

```python
_BPS = Decimal("10000")
_PRICE_CONTEXT_UNAVAILABLE = "price_context_unavailable"
```

- [ ] **Step 3: Add Decimal helper utilities**

Add these helpers after `_stringify_decimal`:

```python
def _decimal_bps(value: float) -> Decimal:
    return Decimal(str(value)) / _BPS


def _quantize_bps_pct(value: Decimal) -> Decimal:
    return value.quantize(Decimal("0.0001"))


def _currency_for_market(market: Literal["kr", "us"]) -> str:
    return "KRW" if market == "kr" else "USD"


def _distance_key_for_market(market: Literal["kr", "us"]) -> str:
    return "distance_krw" if market == "kr" else "distance_usd"
```

- [ ] **Step 4: Add price-context lookup helper**

Add this helper near `_latest_price`:

```python
async def _preview_price_context(
    client: TossReadClient, symbol: str
) -> tuple[Decimal | None, str | None, str | None]:
    try:
        prices = await client.prices([symbol])
        for item in prices:
            if item.symbol == symbol:
                return item.last_price, item.currency, None
        return None, None, f"Could not resolve latest price for symbol: {symbol}"
    except Exception as exc:  # noqa: BLE001
        return None, None, f"Failed to retrieve current price for {symbol}: {exc}"
```

- [ ] **Step 5: Add limit fill-distance helper**

Add this helper after `_preview_price_context`:

```python
def _limit_fill_context(
    *,
    market: Literal["kr", "us"],
    side: Literal["buy", "sell"],
    order_type: Literal["limit", "market"],
    price: Decimal | None,
    current_price: Decimal | None,
) -> tuple[list[str], dict[str, Any] | None]:
    if (
        order_type != "limit"
        or price is None
        or current_price is None
        or current_price <= Decimal("0")
        or price == current_price
    ):
        return [], None

    direction = "above_market" if price > current_price else "below_market"
    marketable = (side == "buy" and price > current_price) or (
        side == "sell" and price < current_price
    )
    order_warnings: list[str] = []
    if side == "buy" and price > current_price:
        order_warnings.append("buy_limit_above_market")
    elif side == "sell" and price < current_price:
        order_warnings.append("sell_limit_below_market")
    elif side == "sell" and price > current_price:
        order_warnings.append("sell_limit_above_market")

    distance = abs(price - current_price)
    distance_pct = _quantize_bps_pct(distance / current_price * Decimal("100"))
    return order_warnings, {
        _distance_key_for_market(market): _stringify_decimal(distance),
        "distance_pct": _stringify_decimal(distance_pct),
        "currency": _currency_for_market(market),
        "marketable": marketable,
        "direction": direction,
    }
```

- [ ] **Step 6: Add preview notional and cost helper**

Add these helpers after `_limit_fill_context`:

```python
def _preview_notional(
    *,
    quantity: Decimal | None,
    effective_price: Decimal | None,
    order_amount: Decimal | None,
) -> Decimal | None:
    if order_amount is not None:
        return order_amount
    if quantity is not None and effective_price is not None:
        return quantity * effective_price
    return None


async def _preview_cost_context(
    *,
    market: Literal["kr", "us"],
    quantity: Decimal | None,
    effective_price: Decimal | None,
    order_amount: Decimal | None,
) -> dict[str, Any]:
    notional = _preview_notional(
        quantity=quantity,
        effective_price=effective_price,
        order_amount=order_amount,
    )
    if notional is None:
        return {
            "estimated_value": None,
            "estimated_value_currency": _currency_for_market(market),
            "fee": None,
            "fee_currency": _currency_for_market(market),
            "fx_cost_full_conversion": None,
            "fx_cost_full_conversion_currency": "KRW" if market == "us" else None,
            "estimated_costs": {
                "cost_profile_source": None,
                "cost_profile_review_required": True,
                "message": "notional unavailable: quantity/effective price or order_amount required",
            },
        }

    try:
        account_costs = await get_account_costs_setting()
        cost_profile_message = None
    except Exception as exc:  # noqa: BLE001
        account_costs = None
        cost_profile_message = f"account_costs_unavailable: {exc}"

    profiles = build_cost_profiles(account_costs)
    profile = profiles.market_profile("toss", market)
    currency = _currency_for_market(market)
    fee = notional * _decimal_bps(profile.commission_bps)
    estimated_costs: dict[str, Any] = {
        "notional": _stringify_decimal(notional),
        "notional_currency": currency,
        "fee": _stringify_decimal(fee),
        "fee_currency": currency,
        "commission_bps": profile.commission_bps,
        "fx_spread_bps": profile.fx_spread_bps,
        "cost_profile_source": profiles.source,
        "cost_profile_review_required": profiles.review_required,
    }
    if cost_profile_message is not None:
        estimated_costs["cost_profile_message"] = cost_profile_message

    fx_cost_full_conversion: Decimal | None = None
    fx_cost_full_conversion_currency: str | None = None
    if market == "us":
        fx_cost_full_conversion_currency = "KRW"
        try:
            quote = await get_usd_krw_rate_details()
            usd_krw = Decimal(str(quote.default_rate))
            fx_cost_full_conversion = notional * usd_krw * _decimal_bps(profile.fx_spread_bps)
            estimated_costs.update(
                {
                    "fx_cost_full_conversion": _stringify_decimal(fx_cost_full_conversion),
                    "fx_cost_full_conversion_currency": "KRW",
                    "fx_rate_usd_krw": _stringify_decimal(usd_krw),
                    "fx_rate_source": quote.source,
                    "fx_assumption": "full_notional_krw_conversion",
                }
            )
        except Exception as exc:  # noqa: BLE001
            estimated_costs.update(
                {
                    "fx_cost_full_conversion": None,
                    "fx_cost_full_conversion_currency": "KRW",
                    "fx_cost_message": f"fx_rate_unavailable: {exc}",
                    "fx_assumption": "full_notional_krw_conversion",
                }
            )
    else:
        fx_cost_full_conversion = Decimal("0")
        fx_cost_full_conversion_currency = "KRW"
        estimated_costs.update(
            {
                "fx_cost_full_conversion": "0",
                "fx_cost_full_conversion_currency": "KRW",
                "fx_assumption": "not_applicable_kr_order",
            }
        )

    return {
        "estimated_value": _stringify_decimal(notional),
        "estimated_value_currency": currency,
        "fee": _stringify_decimal(fee),
        "fee_currency": currency,
        "fx_cost_full_conversion": _stringify_decimal(fx_cost_full_conversion) if fx_cost_full_conversion is not None else None,
        "fx_cost_full_conversion_currency": fx_cost_full_conversion_currency,
        "estimated_costs": estimated_costs,
    }
```

- [ ] **Step 7: Enrich `toss_preview_order`**

Replace the warnings-only client block and final return with this shape:

```python
    warnings_list = []
    warnings_check_msg = None
    order_warnings: list[str] = []
    current_price_dec: Decimal | None = None
    current_price_currency: str | None = None
    price_context_message: str | None = None
    try:
        async with _client_context() as client:
            (
                current_price_dec,
                current_price_currency,
                price_context_message,
            ) = await _preview_price_context(client, symbol)
            guard_res = await check_warnings_guard(
                client, symbol, market=mkt, side=side
            )
            warnings_list = [
                {
                    "warning_type": w.warning_type,
                    "exchange": w.exchange,
                    "start_date": w.start_date,
                    "end_date": w.end_date,
                }
                for w in guard_res.warnings
            ]
            if guard_res.error_message:
                warnings_check_msg = guard_res.error_message
    except Exception as exc:
        logger.error("Failed to check warnings in preview: %s", exc, exc_info=True)
        warnings_check_msg = f"Failed to check warnings: {exc}"

    if price_context_message is not None:
        order_warnings.append(_PRICE_CONTEXT_UNAVAILABLE)

    fill_warnings, fill_distance = _limit_fill_context(
        market=mkt,
        side=side,
        order_type=order_type,
        price=price_dec,
        current_price=current_price_dec,
    )
    order_warnings.extend(fill_warnings)

    effective_price = price_dec if price_dec is not None else current_price_dec
    cost_context = await _preview_cost_context(
        market=mkt,
        quantity=quantity_dec,
        effective_price=effective_price,
        order_amount=order_amount_dec,
    )

    response = {
        "success": True,
        "preview": True,
        "market": mkt,
        **tick_meta,
        "current_price": _stringify_decimal(current_price_dec),
        "current_price_currency": current_price_currency,
        "order_warnings": order_warnings,
        "payload_preview": payload,
        "account_mode": ACCOUNT_MODE_TOSS_LIVE,
        "warnings": warnings_list,
        "warnings_check_message": warnings_check_msg,
        **cost_context,
    }
    if price_context_message is not None:
        response["price_context_message"] = price_context_message
    if fill_distance is not None:
        response["fill_distance"] = fill_distance
    return response
```

- [ ] **Step 8: Run focused tests**

Run:

```bash
uv run pytest \
  tests/test_mcp_toss_order_variants.py::test_toss_preview_buy_limit_above_market_returns_price_distance_and_costs \
  tests/test_mcp_toss_order_variants.py::test_toss_preview_sell_limit_below_market_returns_marketable_warning \
  tests/test_mcp_toss_order_variants.py::test_toss_preview_order_degrades_when_price_context_unavailable \
  -q
```

Expected: PASS.

- [ ] **Step 9: Commit implementation**

```bash
git add app/mcp_server/tooling/orders_toss_variants.py tests/test_mcp_toss_order_variants.py
git commit -m "feat(ROB-578): enrich Toss order preview context"
```

## Task 3: Update MCP Contract Documentation

**Files:**
- Modify: `app/mcp_server/README.md`

- [ ] **Step 1: Update the Toss live order safety section**

Under `#### Toss Safety Rules and Gates`, after `KR Stock Warnings`, add:

```markdown
- **Preview Market And Cost Context**: `toss_preview_order` is read-only but enriches the payload preview with Toss quote and cost context. It returns `current_price`, `current_price_currency`, `fill_distance` for off-market limit prices, `order_warnings` for marketability/fill-risk strings, `estimated_value`, `fee`, `fee_currency`, `fx_cost_full_conversion`, `fx_cost_full_conversion_currency`, and `estimated_costs`. The existing `warnings` field remains reserved for Toss stock-warning rows; string order warnings are not mixed into it. US `fx_cost_full_conversion` assumes the full order notional is converted KRW->USD and is labelled `fx_assumption="full_notional_krw_conversion"`; use `suggest_order_account` for cash-aware routing cost comparison.
```

- [ ] **Step 2: Update the tool registration description**

In `app/mcp_server/tooling/orders_toss_variants.py`, extend the `toss_preview_order` tool description to mention the new read-only enrichment:

```python
            "requires TOSS_API_ENABLED and Toss credentials. The response "
            "includes current_price, fill_distance/order_warnings for limit "
            "marketability, and estimated Toss fee/FX costs from account_costs."
```

- [ ] **Step 3: Run Toss tool description test**

Run:

```bash
uv run pytest tests/test_mcp_toss_order_variants.py::test_toss_tool_descriptions_document_live_gates -q
```

Expected: PASS.

- [ ] **Step 4: Commit docs**

```bash
git add app/mcp_server/README.md app/mcp_server/tooling/orders_toss_variants.py
git commit -m "docs(ROB-578): document Toss preview context fields"
```

## Task 4: Regression Verification

**Files:**
- Verify: `tests/test_mcp_toss_order_variants.py`
- Verify: `tests/test_mcp_toss_order_variants_rob561.py`
- Verify: `app/mcp_server/tooling/orders_toss_variants.py`

- [ ] **Step 1: Run full Toss order variant tests**

```bash
uv run pytest tests/test_mcp_toss_order_variants.py tests/test_mcp_toss_order_variants_rob561.py -q
```

Expected: PASS. This catches existing payload shape, warning-row, tick snapping, live gate, modify/cancel, and reconcile expectations.

- [ ] **Step 2: Run lint on touched Python files**

```bash
uv run ruff check app/mcp_server/tooling/orders_toss_variants.py tests/test_mcp_toss_order_variants.py
```

Expected: PASS.

- [ ] **Step 3: Run formatter check on touched Python files**

```bash
uv run ruff format --check app/mcp_server/tooling/orders_toss_variants.py tests/test_mcp_toss_order_variants.py
```

Expected: PASS.

- [ ] **Step 4: Run type check on the touched runtime module**

```bash
uv run ty check app/mcp_server/tooling/orders_toss_variants.py
```

Expected: PASS. If `ty` is too slow or unavailable locally, record the exact failure and run `make lint` as the fallback project command.

- [ ] **Step 5: Commit verification-only fixes if needed**

Only commit if the verification commands required small fixes:

```bash
git add app/mcp_server/tooling/orders_toss_variants.py tests/test_mcp_toss_order_variants.py app/mcp_server/README.md
git commit -m "chore(ROB-578): polish Toss preview verification"
```

## Task 5: Linear Status Update

**Files:**
- No code files.

- [ ] **Step 1: Comment on ROB-578 after implementation is verified**

Post this Linear comment:

```markdown
Implemented ROB-578 Toss preview enrichment.

- `toss_preview_order` now returns `current_price`, `fill_distance`, `order_warnings`, estimated `fee`, and US `fx_cost_full_conversion`.
- Existing Toss stock warning rows stay in `warnings`; KIS-style price warnings are in `order_warnings` to avoid breaking the public Toss warning-row contract.
- US FX cost is labelled as a full-notional KRW->USD estimate. Cash-aware routing remains in `suggest_order_account` / ROB-565.

Verified with:
- `uv run pytest tests/test_mcp_toss_order_variants.py tests/test_mcp_toss_order_variants_rob561.py -q`
- `uv run ruff check app/mcp_server/tooling/orders_toss_variants.py tests/test_mcp_toss_order_variants.py`
- `uv run ruff format --check app/mcp_server/tooling/orders_toss_variants.py tests/test_mcp_toss_order_variants.py`
- `uv run ty check app/mcp_server/tooling/orders_toss_variants.py`
```

- [ ] **Step 2: Apply model-lane labels only if the implementation scope grows**

Current planned scope is `keep_on_gpt54`: read-only MCP preview enrichment, no DB migration, no mutation gate changes, no live order approval-boundary change.

If implementation changes `dry_run=False`, confirmation gates, sell-loss guard, accepted-order ledger behavior, or live mutation activation policy, apply `high_risk_change + needs_stronger_model_review + hold_for_final_review` before merge or operational use.

## Self-Review

- Spec coverage: The plan covers `current_price`, `fill_distance`, marketability warnings, expected fee, expected FX cost, tests for above/below-market limit cases, and docs updates.
- Placeholder scan: No unfinished-marker text, vague "add tests" instruction, or missing commands remain.
- Type consistency: New helper signatures use existing `Decimal`, `Literal["kr", "us"]`, and `Literal["buy", "sell"]` patterns already present in `orders_toss_variants.py`.
- Contract safety: Existing `warnings` semantics remain backward-compatible; string warnings move to `order_warnings`.
