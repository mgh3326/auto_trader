# ROB-531 Toss Live Order MCP Tools Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add seven default-profile `toss_live_*` MCP tools for Toss Securities live KR/US order preview, placement, modification, cancellation, history, positions, and orderable cash.

**Architecture:** Extend the ROB-530 Toss client with explicit order mutation methods, then keep all live-order safety policy in a new MCP wrapper module `orders_toss_variants.py`. The tools are registered only in `McpProfile.DEFAULT`, fail closed when Toss is disabled or credentials are missing, default all mutations to `dry_run=True`, and require `confirm=True` before any POST. No DB migration, ledger write, reconcile, auto-trading adapter, or ladder support is included.

**Tech Stack:** Python 3.13, FastMCP, httpx, dataclasses, Decimal, pytest, pytest-asyncio, Ruff, ty.

---

## Scope Lock

Implement these public tools:

- `toss_preview_order`
- `toss_place_order`
- `toss_modify_order`
- `toss_cancel_order`
- `toss_get_order_history`
- `toss_get_positions`
- `toss_get_orderable_cash`

Register them in the `default` MCP profile alongside existing KIS typed tools. Do not create a new `toss` profile in this issue.

Hard safety requirements:

- Use `account_mode="toss_live"` metadata and reject any mismatched `account_mode` / `account_type` argument.
- Check `validate_toss_api_config()` at every tool entry before broker reads or writes.
- Mutation tools keep `dry_run=True` by default.
- Mutation tools call Toss only when `dry_run=False and confirm=True`.
- `confirmHighValueOrder` is never inferred automatically. Pass it only when the operator supplies `confirm_high_value_order=True`.
- KR high-value orders with computable notional >= 100,000,000 KRW fail locally unless `confirm_high_value_order=True`.
- For sell orders and sell reprices, validate holdings cost basis and block if execution price or current market sell proxy is below `average_purchase_price * 1.01`.
- For live sells, fail closed if holding/cost basis cannot be resolved.
- Before non-dry-run place, read `OPEN` orders for the symbol and block opposite-side pending orders locally.
- KR modify requires both `new_price` and `new_quantity`.
- US modify requires `new_price` and rejects any `new_quantity`.
- Toss cancel/modify responses must state that the returned `orderId` is a new replacement order id.
- Surface Toss error envelope `code`, `requestId`, `message`, and `data` hints such as `tickSize` / `nearestPrices`.

Out of scope:

- Ledger rows and reconcile. ROB-538 owns accepted-only ledger and fill evidence.
- Portfolio/manual_holdings source switch. ROB-532 owns it.
- Operational activation and live smoke. ROB-539 owns it.
- Buy/sell ladder support. Opposite-pending constraints require separate design.
- DB migration.

## File Structure

- Modify: `app/services/brokers/toss/dto.py`
  - Add DTOs/parsers for order mutation responses.
- Modify: `app/services/brokers/toss/client.py`
  - Add JSON-body support to `_request()`.
  - Add `place_order()`, `modify_order()`, and `cancel_order()` methods.
- Modify: `app/services/brokers/toss/__init__.py`
  - Export any new stable DTOs if needed.
- Modify: `app/mcp_server/tooling/account_modes.py`
  - Add `ACCOUNT_MODE_TOSS_LIVE = "toss_live"` and matching routing metadata support.
- Create: `app/mcp_server/tooling/orders_toss_variants.py`
  - Own all Toss MCP tool registration, guards, response shaping, and Toss client lifecycle.
- Modify: `app/mcp_server/tooling/registry.py`
  - Register Toss tools in `McpProfile.DEFAULT` only.
- Modify: `app/mcp_server/README.md`
  - Document default-profile Toss tools, gates, KR/US modify semantics, high-value confirmation, and hold-for-review status.
- Modify: `tests/services/brokers/toss/test_client.py`
  - Add client mutation transport tests.
- Replace/modify: `tests/services/brokers/toss/test_no_mutation_surface.py`
  - ROB-530's no-mutation assertion is obsolete; replace with tests proving mutations are explicit and account-scoped.
- Create: `tests/test_mcp_toss_order_variants.py`
  - Tool registration, fail-closed config, dry-run/confirm, guard, and response-contract tests.
- Modify or create registry/doc tests if existing coverage expects exact profile tool sets.

## Task 1: Add Toss Client Mutation Methods

**Files:**
- Modify: `app/services/brokers/toss/dto.py`
- Modify: `app/services/brokers/toss/client.py`
- Modify: `tests/services/brokers/toss/test_client.py`
- Modify: `tests/services/brokers/toss/test_no_mutation_surface.py`

- [ ] **Step 1: Write failing client tests**

Add tests that prove:

```python
async def test_place_order_posts_json_with_account_header_and_client_order_id():
    # MockTransport captures method, path, headers, and json body.
    # Call client.place_order(... client_order_id="abc123" ...).
    # Assert POST /api/v1/orders, X-Tossinvest-Account, order fields, and parsed order_id.

async def test_modify_order_posts_to_modify_path_and_parses_new_order_id():
    # Assert POST /api/v1/orders/{orderId}/modify and parsed replacement order_id.

async def test_cancel_order_posts_to_cancel_path_and_parses_new_order_id():
    # Assert POST /api/v1/orders/{orderId}/cancel and parsed replacement order_id.
```

Update `test_no_mutation_surface.py` so it no longer forbids Toss order POSTs. Replace it with assertions that mutation methods are exactly the explicit methods and still use `account_required=True`.

- [ ] **Step 2: Run failing tests**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_client.py tests/services/brokers/toss/test_no_mutation_surface.py -q
```

Expected: FAIL because mutation DTOs/client methods do not exist yet.

- [ ] **Step 3: Implement DTOs and client methods**

In `dto.py`, add:

```python
@dataclass(frozen=True)
class TossOrderPlacementResult:
    order_id: str
    client_order_id: str | None


@dataclass(frozen=True)
class TossOrderOperationResult:
    order_id: str


def parse_order_placement_result(raw: dict[str, Any]) -> TossOrderPlacementResult:
    return TossOrderPlacementResult(
        order_id=str(raw["orderId"]),
        client_order_id=(
            str(raw["clientOrderId"]) if raw.get("clientOrderId") is not None else None
        ),
    )


def parse_order_operation_result(raw: dict[str, Any]) -> TossOrderOperationResult:
    return TossOrderOperationResult(order_id=str(raw["orderId"]))
```

In `client.py`, extend `_request()` with `json: dict[str, Any] | None = None` and pass it to `self._client.request(...)` on the original request, 429 retry, and token retry.

Add methods:

```python
async def place_order(self, payload: dict[str, Any]) -> TossOrderPlacementResult:
    return parse_order_placement_result(
        await self._request(
            "POST",
            "/api/v1/orders",
            group=TossApiGroup.ORDER,
            json=payload,
            account_required=True,
        )
    )


async def modify_order(
    self, order_id: str, payload: dict[str, Any]
) -> TossOrderOperationResult:
    return parse_order_operation_result(
        await self._request(
            "POST",
            f"/api/v1/orders/{order_id}/modify",
            group=TossApiGroup.ORDER,
            json=payload,
            account_required=True,
        )
    )


async def cancel_order(self, order_id: str) -> TossOrderOperationResult:
    return parse_order_operation_result(
        await self._request(
            "POST",
            f"/api/v1/orders/{order_id}/cancel",
            group=TossApiGroup.ORDER,
            json={},
            account_required=True,
        )
    )
```

- [ ] **Step 4: Verify Task 1**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_client.py tests/services/brokers/toss/test_no_mutation_surface.py -q
uv run ruff check app/services/brokers/toss tests/services/brokers/toss
```

Expected: PASS.

- [ ] **Step 5: Commit Task 1**

```bash
git add app/services/brokers/toss tests/services/brokers/toss
git commit -m "feat(ROB-531): add Toss order mutation client methods"
```

## Task 2: Add Account Mode And Toss Tool Shell

**Files:**
- Modify: `app/mcp_server/tooling/account_modes.py`
- Create: `app/mcp_server/tooling/orders_toss_variants.py`
- Modify: `app/mcp_server/tooling/registry.py`
- Create: `tests/test_mcp_toss_order_variants.py`

- [ ] **Step 1: Write failing registration/fail-closed tests**

Create tests with `tests._mcp_tooling_support.DummyMCP`:

```python
EXPECTED_TOOL_NAMES = {
    "toss_preview_order",
    "toss_place_order",
    "toss_modify_order",
    "toss_cancel_order",
    "toss_get_order_history",
    "toss_get_positions",
    "toss_get_orderable_cash",
}


def test_all_seven_toss_tools_register():
    # register_toss_live_order_tools(DummyMCP())
    # Assert EXPECTED_TOOL_NAMES are present.


async def test_place_order_fails_closed_when_toss_disabled(monkeypatch):
    # monkeypatch validate_toss_api_config to return ["TOSS_API_ENABLED"].
    # Call toss_place_order with minimum args.
    # Assert success False, account_mode toss_live, no client call.


async def test_toss_tools_reject_wrong_account_mode(monkeypatch):
    # config OK, account_mode="kis_live" -> success False.
```

- [ ] **Step 2: Run failing tests**

```bash
uv run pytest tests/test_mcp_toss_order_variants.py -q
```

Expected: FAIL because `orders_toss_variants.py` does not exist.

- [ ] **Step 3: Implement account mode and shell registration**

In `account_modes.py`, add `ACCOUNT_MODE_TOSS_LIVE = "toss_live"`, include `"toss_live"` in `_ACCOUNT_MODE_ALIASES` and `_ACCOUNT_TYPE_ALIASES`, add `is_toss_live`, and export the constant.

Create `orders_toss_variants.py` with:

```python
TOSS_LIVE_ORDER_TOOL_NAMES = {
    "toss_preview_order",
    "toss_place_order",
    "toss_modify_order",
    "toss_cancel_order",
    "toss_get_order_history",
    "toss_get_positions",
    "toss_get_orderable_cash",
}
```

Add `_config_error()`, `_check_mode_arg()`, `_prepare_call()`, `_safe_order_id_error()`, `_client_context()`, and `register_toss_live_order_tools(mcp)`.

Initial tool bodies may return guarded dry-run/read stubs only until later tasks, but they must apply config and account-mode checks before any Toss client construction.

In `registry.py`, import and call `register_toss_live_order_tools(mcp)` inside `if profile is McpProfile.DEFAULT:`.

- [ ] **Step 4: Verify Task 2**

```bash
uv run pytest tests/test_mcp_toss_order_variants.py -q
uv run ruff check app/mcp_server/tooling/account_modes.py app/mcp_server/tooling/orders_toss_variants.py app/mcp_server/tooling/registry.py tests/test_mcp_toss_order_variants.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 2**

```bash
git add app/mcp_server/tooling/account_modes.py app/mcp_server/tooling/orders_toss_variants.py app/mcp_server/tooling/registry.py tests/test_mcp_toss_order_variants.py
git commit -m "feat(ROB-531): register Toss live MCP order tools"
```

## Task 3: Implement Preview And Place Safety Gates

**Files:**
- Modify: `app/mcp_server/tooling/orders_toss_variants.py`
- Modify: `tests/test_mcp_toss_order_variants.py`

- [ ] **Step 1: Write failing guard tests**

Add tests for:

```python
async def test_place_order_defaults_to_dry_run_and_does_not_call_broker(monkeypatch):
    # Patch client factory to raise if POST is called.
    # Call toss_place_order without dry_run.
    # Assert success True, dry_run True, mutation_sent False.


async def test_place_order_requires_confirm_when_dry_run_false(monkeypatch):
    # dry_run=False, confirm=False -> success False, no POST.


async def test_high_value_kr_order_requires_explicit_confirm_high_value(monkeypatch):
    # KR buy 2000 * 50000 = 100,000,000.
    # confirm=True but confirm_high_value_order=False -> local failure.


async def test_place_sell_blocks_below_avg_floor_for_limit_and_market(monkeypatch):
    # holdings avg 100, current 100.
    # LIMIT sell price 100 and MARKET sell current 100 -> blocked below 101.


async def test_place_order_blocks_opposite_pending_before_post(monkeypatch):
    # list_orders(status="OPEN", symbol="AAPL") returns SELL order.
    # BUY order dry_run=False confirm=True -> success False, no POST.
```

- [ ] **Step 2: Run failing guard tests**

```bash
uv run pytest tests/test_mcp_toss_order_variants.py -q
```

Expected: FAIL because the shell does not enforce all guards yet.

- [ ] **Step 3: Implement shared helpers**

In `orders_toss_variants.py`, add helpers:

- `_infer_market(symbol, market)` -> `"kr"` for six digits, `"us"` otherwise; reject anything except `kr` / `us`.
- `_decimal_string(value, name)` -> `Decimal` parse without float math for Toss payloads.
- `_stringify_decimal(value)` -> stable Toss decimal string.
- `_new_client_order_id()` -> `uuid.uuid4().hex`.
- `_estimate_krw_notional(market, quantity, price, order_amount)` -> Decimal or None.
- `_high_value_error(...)` -> local failure when KR notional >= 100,000,000 and flag is false.
- `_find_holding(client, symbol)` -> Toss holding item or None.
- `_latest_price(client, symbol)` -> Toss price Decimal.
- `_sell_loss_guard(client, symbol, order_type, price)` -> fail closed when no holding or avg <= 0; for limit compare `price`; for market compare latest price.
- `_opposite_pending_error(client, symbol, side)` -> reads `list_orders(status="OPEN", symbol=symbol)` and blocks opposite side.
- `_toss_error_response(exc, base)` -> include `status_code`, `code`, `request_id`, `message`, and `data`.

- [ ] **Step 4: Implement `toss_preview_order` and `toss_place_order`**

Tool signature:

```python
async def toss_place_order(
    symbol: str,
    side: Literal["buy", "sell"],
    order_type: Literal["limit", "market"] = "limit",
    quantity: str | int | None = None,
    price: str | int | None = None,
    order_amount: str | int | None = None,
    market: Literal["kr", "us"] | None = None,
    time_in_force: Literal["DAY", "CLS"] = "DAY",
    dry_run: bool = True,
    confirm: bool = False,
    confirm_high_value_order: bool = False,
    account_mode: str | None = None,
    account_type: str | None = None,
) -> dict[str, Any]:
```

Build Toss payload keys in official casing: `clientOrderId`, `symbol`, `side`, `orderType`, `timeInForce`, `quantity`, `price`, `orderAmount`, `confirmHighValueOrder`.

Only include `confirmHighValueOrder` when `confirm_high_value_order=True`.

Dry-run response must include:

- `success: True`
- `dry_run: True`
- `mutation_sent: False`
- `payload_preview`
- `account_mode: "toss_live"`

Confirmed response must include:

- `success: True`
- `dry_run: False`
- `mutation_sent: True`
- `order_id`
- `client_order_id`
- `source: "toss"`
- `account_mode: "toss_live"`

- [ ] **Step 5: Verify Task 3**

```bash
uv run pytest tests/test_mcp_toss_order_variants.py -q
uv run ruff check app/mcp_server/tooling/orders_toss_variants.py tests/test_mcp_toss_order_variants.py
```

Expected: PASS.

- [ ] **Step 6: Commit Task 3**

```bash
git add app/mcp_server/tooling/orders_toss_variants.py tests/test_mcp_toss_order_variants.py
git commit -m "feat(ROB-531): guard Toss live order preview and placement"
```

## Task 4: Implement Modify And Cancel

**Files:**
- Modify: `app/mcp_server/tooling/orders_toss_variants.py`
- Modify: `tests/test_mcp_toss_order_variants.py`

- [ ] **Step 1: Write failing modify/cancel tests**

Add tests for:

```python
async def test_modify_kr_requires_price_and_quantity(monkeypatch):
    # market="kr", new_price set, new_quantity None -> local failure.


async def test_modify_us_rejects_quantity(monkeypatch):
    # market="us", new_price set, new_quantity set -> local failure.


async def test_modify_sell_reprice_blocks_below_avg_floor(monkeypatch):
    # Existing OPEN order side SELL from get_order(), avg 100, new_price 100 -> block.


async def test_modify_requires_confirm_when_dry_run_false(monkeypatch):
    # no POST.


async def test_cancel_requires_confirm_when_dry_run_false(monkeypatch):
    # no POST.


async def test_modify_and_cancel_surface_replacement_order_id(monkeypatch):
    # Confirmed call returns replacement order id and operation_semantics text.
```

- [ ] **Step 2: Run failing tests**

```bash
uv run pytest tests/test_mcp_toss_order_variants.py -q
```

Expected: FAIL until modify/cancel are implemented.

- [ ] **Step 3: Implement modify**

Use `client.get_order(order_id)` before confirmed modify and dry-run modify. Infer side/symbol if not supplied by the original order.

Payload rules:

- Always include `orderType`.
- For KR: require `new_price` and `new_quantity`; include `price` and `quantity`.
- For US: require `new_price`; reject `new_quantity`; include `price`.
- Include `confirmHighValueOrder` only when `confirm_high_value_order=True`.

Apply sell reprice floor when the original order side is `SELL` and target order type is `LIMIT`.

- [ ] **Step 4: Implement cancel**

Validate safe order id. For dry-run, return no mutation. For confirmed execution, call `client.cancel_order(order_id)`.

Confirmed response includes:

```python
{
    "success": True,
    "dry_run": False,
    "mutation_sent": True,
    "original_order_id": order_id,
    "replacement_order_id": result.order_id,
    "operation_semantics": "Toss cancel returns a newly issued orderId; it is not the original order id.",
}
```

Use equivalent semantics text for modify.

- [ ] **Step 5: Verify Task 4**

```bash
uv run pytest tests/test_mcp_toss_order_variants.py -q
uv run ruff check app/mcp_server/tooling/orders_toss_variants.py tests/test_mcp_toss_order_variants.py
```

Expected: PASS.

- [ ] **Step 6: Commit Task 4**

```bash
git add app/mcp_server/tooling/orders_toss_variants.py tests/test_mcp_toss_order_variants.py
git commit -m "feat(ROB-531): guard Toss live modify and cancel"
```

## Task 5: Implement Read Tools

**Files:**
- Modify: `app/mcp_server/tooling/orders_toss_variants.py`
- Modify: `tests/test_mcp_toss_order_variants.py`

- [ ] **Step 1: Write failing read-tool tests**

Add tests for:

```python
async def test_get_order_history_uses_closed_cursor_pagination_args(monkeypatch):
    # status="closed", cursor and limit pass through to client.list_orders(status="CLOSED").


async def test_get_positions_shapes_holdings(monkeypatch):
    # holdings() items surface symbol, quantity, avg price, last price, currency.


async def test_get_orderable_cash_reads_currency(monkeypatch):
    # currency="KRW" -> client.buying_power(currency="KRW").
```

- [ ] **Step 2: Run failing tests**

```bash
uv run pytest tests/test_mcp_toss_order_variants.py -q
```

Expected: FAIL until read tools are implemented.

- [ ] **Step 3: Implement read tools**

`toss_get_order_history`:

- Accept `status: Literal["open", "closed"] = "closed"`.
- Send Toss status `"OPEN"` or `"CLOSED"`.
- Accept `symbol`, `from_date`, `to_date`, `cursor`, `limit`.
- Preserve `next_cursor` and `has_next`.

`toss_get_positions`:

- Accept optional `symbol`.
- Call `client.holdings(symbol=symbol)`.
- Return `items` with decimal values stringified.
- Include raw overview under `overview`.

`toss_get_orderable_cash`:

- Accept `currency: Literal["KRW", "USD"] = "KRW"`.
- Call `client.buying_power(currency=currency)`.
- Return `cash_buying_power` as string and `currency`.

- [ ] **Step 4: Verify Task 5**

```bash
uv run pytest tests/test_mcp_toss_order_variants.py -q
uv run ruff check app/mcp_server/tooling/orders_toss_variants.py tests/test_mcp_toss_order_variants.py
```

Expected: PASS.

- [ ] **Step 5: Commit Task 5**

```bash
git add app/mcp_server/tooling/orders_toss_variants.py tests/test_mcp_toss_order_variants.py
git commit -m "feat(ROB-531): add Toss live order read tools"
```

## Task 6: Default Profile Registration And Documentation

**Files:**
- Modify: `app/mcp_server/tooling/registry.py`
- Modify: `app/mcp_server/README.md`
- Modify or create tests that assert default profile tool presence.

- [ ] **Step 1: Write failing default-profile test**

Add or update a registry test:

```python
def test_default_profile_registers_toss_live_tools(monkeypatch):
    # Build DummyMCP through register_all_tools(profile=McpProfile.DEFAULT).
    # Disable optional snapshot/report flags if needed.
    # Assert all TOSS_LIVE_ORDER_TOOL_NAMES are present.
```

- [ ] **Step 2: Run failing registry/doc tests**

```bash
uv run pytest tests/test_mcp_server_main.py tests/test_mcp_toss_order_variants.py -q
```

Expected: FAIL if registry/docs are not complete.

- [ ] **Step 3: Update README**

Document:

- `default` profile now includes `toss_live_*`.
- Toss is live-only and default-disabled by `TOSS_API_ENABLED`.
- Mutations require `dry_run=False` and `confirm=True`.
- 100M KRW+ orders require explicit `confirm_high_value_order=True`.
- KR modify needs price and quantity; US modify rejects quantity.
- Cancel/modify return new replacement order ids.
- ROB-531 is under `hold_for_final_review`; no merge/deploy/live trading use until stronger review clears it.

- [ ] **Step 4: Verify Task 6**

```bash
uv run pytest tests/test_mcp_toss_order_variants.py tests/test_mcp_server_main.py -q
uv run ruff check app/mcp_server/tooling app/mcp_server/README.md tests/test_mcp_toss_order_variants.py
```

Expected: PASS or README ignored by Ruff with no Python lint errors.

- [ ] **Step 5: Commit Task 6**

```bash
git add app/mcp_server/tooling/registry.py app/mcp_server/README.md tests
git commit -m "docs(ROB-531): document Toss live MCP order surface"
```

## Task 7: Final Verification And Hold Comment

**Files:**
- No production file changes unless verification exposes a bug.

- [ ] **Step 1: Run focused tests**

```bash
uv run pytest tests/services/brokers/toss tests/test_mcp_toss_order_variants.py -q
```

Expected: PASS.

- [ ] **Step 2: Run broader MCP order regression tests**

```bash
uv run pytest tests/test_mcp_kis_order_variants.py tests/test_mcp_kiwoom_order_variants.py tests/test_mcp_order_tools.py -q
```

Expected: PASS.

- [ ] **Step 3: Run lint/type gate**

```bash
make lint
```

Expected: PASS.

- [ ] **Step 4: Confirm no migration**

```bash
git diff --name-only | rg 'alembic|migrations' || true
```

Expected: no migration files.

- [ ] **Step 5: Post Linear implementation hold comment**

Add a Linear comment:

```text
Implementation is ready for ROB-531, but hold_for_final_review remains active because this changes live order execution boundaries. Do not merge, deploy, or use for live Toss orders until stronger-model/CTO review clears the dry_run/confirm gates, high-value confirmation handling, opposite-pending precheck, loss-sell guard mirror, and KR/US modify semantics.
```

- [ ] **Step 6: Final commit if needed**

If Task 7 required test/docs fixes:

```bash
git add <changed-files>
git commit -m "test(ROB-531): verify Toss live order MCP guards"
```

## Self-Review

- Spec coverage: covers all seven tools, default profile registration, fail-closed config, dry-run/confirm, loss-sell guard, high-value confirm, opposite-pending precheck, KR/US modify semantics, replacement order id semantics, docs, and migration zero.
- Placeholder scan: no unresolved placeholders remain.
- Type consistency: public account mode is `toss_live`; Toss API payload keys use official camelCase; MCP args use snake_case.
- Risk status: implementation remains under `high_risk_change`, `needs_stronger_model_review`, and `hold_for_final_review`.
