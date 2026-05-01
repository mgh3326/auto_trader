# ROB-73 — Alpaca Paper Guarded Submit/Cancel MCP Tools and Dev Smoke

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

Status: `plan_ready`
Issue: ROB-73
Branch: `feature/ROB-73-alpaca-paper-submit-cancel-dev-smoke`
Worktree: `/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-73-alpaca-paper-submit-cancel-dev-smoke`
Planner/Reviewer: Claude Opus
Implementer: Claude Sonnet (same AoE session, same worktree)
Production baseline SHA: `12a80b86aa2d639f30985e8a8e91252deabf61ca`

---

## 1. Goal

Implement two adapter-specific, paper-only MCP tools and a dev-owned smoke flow as a single PR slice combining ROB-72.A (submit) and ROB-72.B (cancel):

- `alpaca_paper_submit_order` — explicit-paper submit with default no-side-effect mode.
- `alpaca_paper_cancel_order` — explicit single-`order_id` cancel with default no-side-effect mode.
- `scripts/smoke/alpaca_paper_dev_smoke.py` — dev/operator-only flow that defaults to preview-only and requires both a CLI flag and an environment variable to perform any broker mutation.

The user has approved combining the earlier ROB-72.A and ROB-72.B follow-ups because this is an Alpaca **paper** account. Strict safety gates remain.

## 2. Architecture

- **New tool module** `app/mcp_server/tooling/alpaca_paper_orders.py` houses both new MCP handlers, a `ALPACA_PAPER_MUTATING_TOOL_NAMES` constant, hard-coded smoke caps, and an injectable service factory mirroring the read-only and preview modules.
- **Validation reuse:** `alpaca_paper_submit_order` constructs `PreviewOrderInput` from `app/mcp_server/tooling/alpaca_paper_preview.py` to guarantee preview/submit do not drift, then layers stricter submit-only checks (lower qty/notional caps, asset_class `us_equity` only).
- **Endpoint guard:** Both handlers obtain the service through `_default_service_factory()` which calls `AlpacaPaperBrokerService()`. The service constructor (`app/services/brokers/alpaca/service.py`) already raises `AlpacaPaperEndpointError` on any non-paper base URL. There is no parameter on the new handlers that can switch endpoints.
- **Confirmation model:** `confirm: bool = False` (default). Without `confirm=True`, the handler returns `{"submitted": False, ...}` or `{"cancelled": False, ...}` with the validated request — no broker call. `confirm=True` calls the paper service exactly once.
- **Secret hygiene:** No handler, helper, or smoke script ever reads, prints, logs, or echoes `settings.alpaca_paper_api_key`, `settings.alpaca_paper_api_secret`, the `APCA-API-KEY-ID`/`APCA-API-SECRET-KEY` headers, or any Authorization value. The smoke summary only prints presence/length and tool result classes.
- **Dev smoke gating:** `scripts/smoke/alpaca_paper_dev_smoke.py` defaults to preview-only. Side effects require both `--confirm-paper-side-effect` (CLI flag) **and** `ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS=1` (env var). Either alone is rejected.

## 3. Tech Stack

- Python 3.13, `pydantic` v2 (existing `PreviewOrderInput`).
- `fastmcp` `FastMCP` decorator API (mirrors existing tools).
- `httpx` via `AlpacaPaperBrokerService` (no direct HTTP from this module).
- `pytest` (`@pytest.mark.unit` + `@pytest.mark.asyncio`) with `DummyMCP` and `FakeAlpacaPaperService` already established in `tests/_mcp_tooling_support.py` and `tests/test_mcp_alpaca_paper_tools.py`.
- `argparse` for the dev smoke script.

## 4. Hard Safety Boundaries

These ten constraints come directly from the handoff prompt and ROB-72 contract. The implementer MUST keep all true throughout:

1. Trading endpoint: `https://paper-api.alpaca.markets` only. Any other value → fail closed.
2. Market data endpoint: `https://data.alpaca.markets` only (not used in this issue, but no other data URL is acceptable).
3. Live trading endpoint `https://api.alpaca.markets` is permanently rejected by `AlpacaPaperBrokerService.__init__`.
4. Never print, log, commit, or paste API key/secret/Authorization values. No `print(settings.alpaca_paper_*)`. No `logger.debug` of headers or request bodies.
5. Do NOT route Alpaca paper through generic `place_order`, `cancel_order`, `modify_order` (those tools are KIS-only / legacy and must continue to be Alpaca-free).
6. No runtime parameter on either new tool may change the endpoint, base URL, or environment.
7. Submit defaults to blocked/no-op unless `confirm=True` is supplied.
8. Cancel requires exactly one explicit `order_id`. No cancel-all, by-symbol, by-status, or wildcard cancel — and no helper that iterates `list_orders()` for cancellation in this issue.
9. Automated tests must mock all side effects via `set_alpaca_paper_orders_service_factory(...)`. Only the dev smoke script in side-effect mode may place/cancel one tiny paper-account order.
10. Do NOT introduce `paper_001`, `paper_us_001`, DB registry rows, or strategy profile mapping in this issue.

---

## 5. File Structure

| File | Action | Responsibility |
|------|--------|----------------|
| `app/mcp_server/tooling/alpaca_paper_orders.py` | CREATE | Submit & cancel handlers, mutating-tool name set, smoke caps, service factory, register fn |
| `app/mcp_server/tooling/registry.py` | MODIFY | Register the new mutating tools in both `DEFAULT` and `HERMES_PAPER_KIS` profiles |
| `tests/test_mcp_alpaca_paper_tools.py` | MODIFY | Remove `alpaca_paper_submit_order` / `alpaca_paper_cancel_order` from the existing forbidden-name guards; keep `place`/`replace`/`modify` forbidden |
| `tests/test_alpaca_paper_orders_tools.py` | CREATE | Full unit-test matrix for the two new MCP tools |
| `scripts/smoke/alpaca_paper_dev_smoke.py` | CREATE | Dev smoke runner: preview-only by default; side-effect mode behind dual gate |
| `tests/test_alpaca_paper_dev_smoke_safety.py` | CREATE | Static + behavioural safety tests for the new dev smoke script |
| `docs/runbooks/alpaca-paper-dev-smoke.md` | CREATE | Operator runbook for the dev smoke flow |

No other production files are touched in this PR. `app/services/brokers/alpaca/service.py` already exposes `submit_order(OrderRequest) -> Order` and `cancel_order(order_id) -> None`; we reuse them as-is.

---

## 6. Tool Contracts

### 6.1 `alpaca_paper_submit_order`

```python
async def alpaca_paper_submit_order(
    symbol: str,
    side: str,                          # "buy" | "sell"
    type: str,                          # "market" | "limit"
    qty: Decimal | None = None,         # mutually exclusive with notional
    notional: Decimal | None = None,    # mutually exclusive with qty; market only
    time_in_force: str = "day",         # "day" | "gtc" | "ioc" | "fok"
    limit_price: Decimal | None = None, # required when type == "limit"
    client_order_id: str | None = None, # caller-supplied or auto-generated
    asset_class: str = "us_equity",     # us_equity only
    confirm: bool = False,              # safety gate
) -> dict[str, Any]: ...
```

Behaviour:

1. **Validate** by constructing `PreviewOrderInput(...)` from `alpaca_paper_preview.py`. Any `ValidationError` propagates as `ValueError` like preview does today.
2. **Apply submit-only caps** (stricter than preview):
   - If `qty is not None and qty > SUBMIT_MAX_QTY` → `ValueError("qty exceeds submit cap (5)")`.
   - If `notional is not None and notional > SUBMIT_MAX_NOTIONAL_USD` → `ValueError("notional exceeds submit cap (1000)")`.
   - If `qty is not None and limit_price is not None and qty * limit_price > SUBMIT_MAX_NOTIONAL_USD` → `ValueError("estimated_cost exceeds submit cap (1000)")`.
3. **Generate `client_order_id` if absent**: deterministic shape `rob73-<sha256(canonical_payload)[:16]>` so retries with the same args do not duplicate at the broker.
4. **Confirm gate**: if `confirm is not True`, return:
   ```python
   {
       "success": True,
       "account_mode": "alpaca_paper",
       "source": "alpaca_paper",
       "submitted": False,
       "blocked_reason": "confirmation_required",
       "order_request": {<canonical fields>},
       "client_order_id": <chosen id>,
   }
   ```
   No service call.
5. **Confirmed path** (`confirm is True`):
   - Construct `OrderRequest(symbol=..., side=..., type=..., qty=..., notional=..., time_in_force=..., limit_price=..., client_order_id=...)`.
   - Call `service.submit_order(order_request)` exactly once.
   - Return:
     ```python
     {
         "success": True,
         "account_mode": "alpaca_paper",
         "source": "alpaca_paper",
         "submitted": True,
         "order": _model_to_jsonable(order),     # never includes secrets
         "client_order_id": <chosen id>,
     }
     ```
6. `AlpacaPaperEndpointError` raised by service init must propagate (fail closed). `AlpacaPaperRequestError` propagates to the caller as a raised exception (not swallowed) so the smoke script can classify `[FAIL]`.

### 6.2 `alpaca_paper_cancel_order`

```python
async def alpaca_paper_cancel_order(
    order_id: str,
    confirm: bool = False,
) -> dict[str, Any]: ...
```

Behaviour:

1. **Validate** `order_id`: trim; `if not stripped: raise ValueError("order_id is required")`. No wildcards, no comma-lists, no `*`/`all`/`-`/empty.
2. **Confirm gate**: if `confirm is not True`, return:
   ```python
   {
       "success": True,
       "account_mode": "alpaca_paper",
       "source": "alpaca_paper",
       "cancelled": False,
       "blocked_reason": "confirmation_required",
       "target_order_id": <stripped id>,
   }
   ```
   No service call.
3. **Confirmed path** (`confirm is True`):
   - Call `service.cancel_order(stripped_order_id)` exactly once.
   - Then call `service.get_order(stripped_order_id)` for read-back. If read-back fails, swallow with `read_back_status="unavailable"`; do NOT raise — cancel is idempotent.
   - Return:
     ```python
     {
         "success": True,
         "account_mode": "alpaca_paper",
         "source": "alpaca_paper",
         "cancelled": True,
         "cancelled_order_id": <stripped id>,
         "order": _model_to_jsonable(order_or_None),
         "read_back_status": <"ok" | "unavailable">,
     }
     ```
4. There is no parameter list other than `(order_id, confirm)`. No `status=`, `symbol=`, `all=`, `force=`, `bulk=`, etc.

### 6.3 Module-level constants

```python
SUBMIT_MAX_QTY: Decimal = Decimal("5")
SUBMIT_MAX_NOTIONAL_USD: Decimal = Decimal("1000")

ALPACA_PAPER_MUTATING_TOOL_NAMES: set[str] = {
    "alpaca_paper_submit_order",
    "alpaca_paper_cancel_order",
}
```

Constants are intentionally hard-coded (not env-driven) so a misconfigured environment cannot relax them. A follow-up issue may turn them into `Settings` fields after operator experience.

---

## 7. Validation / Reuse Plan With Preview

- Submit path imports `PreviewOrderInput` from `app/mcp_server/tooling/alpaca_paper_preview.py` (already exported in `__all__`).
- Submit calls `PreviewOrderInput(symbol=..., side=..., type=..., qty=..., notional=..., time_in_force=..., limit_price=..., stop_price=None, client_order_id=..., asset_class=...)` so every preview validator runs (side, type, qty/notional exclusivity, limit→limit_price, market→no limit_price, notional→market only, asset_class==`us_equity`, TIF allowed set, symbol shape).
- Submit then enforces strictly tighter caps via `SUBMIT_MAX_QTY` / `SUBMIT_MAX_NOTIONAL_USD` (preview allows 1,000,000 qty / 10,000,000 notional, both far above smoke needs).
- A unit test (§9.6) constructs the same arguments through `alpaca_paper_preview_order()` (using the preview service factory) and through `alpaca_paper_submit_order(..., confirm=False)` and asserts the `order_request`/`order_request`-equivalent fields agree on the canonical payload.
- The preview module's `_FORBIDDEN_SERVICE_METHODS = ("submit_order", "cancel_order")` constant remains untouched. The preview path must NEVER call these methods.

---

## 8. Endpoint Guard and Secret-Redaction Plan

**Endpoint guard:**

- The new module never reads `settings.alpaca_paper_base_url` directly.
- `_default_service_factory()` returns `AlpacaPaperBrokerService()` whose constructor already enforces `base_url == PAPER_TRADING_BASE_URL` and rejects `LIVE_TRADING_BASE_URL`/`DATA_BASE_URL` with `AlpacaPaperEndpointError`.
- Test `test_submit_fails_closed_on_live_endpoint` (§9.6) monkeypatches `AlpacaPaperSettings.from_app_settings` to return `LIVE_TRADING_BASE_URL` and asserts the handler raises `AlpacaPaperEndpointError` BEFORE the broker is touched (no `submit_order` call on the fake service).
- Same test for cancel: `test_cancel_fails_closed_on_live_endpoint`.

**Secret redaction:**

- New module imports nothing from `app.core.config`. It only ever holds the `AlpacaPaperBrokerService` instance returned by the factory.
- `_model_to_jsonable` (copied or imported from `alpaca_paper.py`) operates on `Order`/`Position`/`Cash` Pydantic models which contain no auth fields.
- The dev smoke script never `print()`s the raw `service`, `request`, or `order` objects; it only prints classification strings, counts, and id-shape (`order_id_len=<n>`) — never raw broker payloads.
- Test `test_dev_smoke_script_no_secret_or_header_strings` (§9.7) reads the script source and asserts these substrings are absent: `APCA-API-KEY-ID`, `APCA-API-SECRET-KEY`, `Authorization`, `api_key`, `api_secret`, `get_secret_value`.
- Test `test_dev_smoke_script_no_raw_payload_print` mirrors the existing `test_smoke_script_no_raw_payload_print` pattern and bans `print(payload|result|order|orders|positions|account|fills|assets)`.

---

## 9. Tasks (TDD, bite-sized)

Execute in order. Commit after each task.

### Task 0: Update existing forbidden-name guards (must run first to keep existing test file consistent with the new tool surface)

**Files:**
- Modify: `tests/test_mcp_alpaca_paper_tools.py`

- [ ] **Step 0.1: Update `forbidden_names` in `test_no_alpaca_live_or_mutating_alpaca_order_tools_registered`**

  Change the set so that `alpaca_paper_submit_order` and `alpaca_paper_cancel_order` are NO LONGER in `forbidden_names`. Keep `alpaca_paper_place_order`, `alpaca_paper_replace_order`, `alpaca_paper_modify_order`, `alpaca_live_get_account`, `alpaca_live_list_orders` as forbidden.

  Replace the existing `forbidden_names = {...}` literal with:

  ```python
  forbidden_names = {
      "alpaca_live_get_account",
      "alpaca_live_list_orders",
      "alpaca_paper_place_order",
      "alpaca_paper_replace_order",
      "alpaca_paper_modify_order",
  }
  ```

  Update the trailing `set comprehension` check to allow only `submit` and `cancel` verbs:

  ```python
  assert {
      name
      for name in mcp.tools
      if name.startswith("alpaca_paper_")
      and any(verb in name for verb in ("place", "replace", "modify"))
  } == set()
  ```

- [ ] **Step 0.2: Update `forbidden` set in `test_no_alpaca_paper_submit_or_cancel_or_modify_tools`**

  Rename the test function to `test_no_alpaca_paper_place_or_replace_or_modify_tools` and replace the `forbidden` set with:

  ```python
  forbidden = {
      "alpaca_paper_preview_submit",
      "alpaca_paper_order_submit",
      "alpaca_paper_replace",
      "alpaca_paper_modify",
      "alpaca_paper_place_order",
      "alpaca_paper_cancel_all_orders",
      "alpaca_paper_cancel_orders",
  }
  ```

- [ ] **Step 0.3: Run tests to confirm they now FAIL (because submit/cancel are not yet registered, but the renamed tests still pass against the unchanged registry)**

  Run: `uv run pytest tests/test_mcp_alpaca_paper_tools.py::test_no_alpaca_live_or_mutating_alpaca_order_tools_registered tests/test_mcp_alpaca_paper_tools.py::test_no_alpaca_paper_place_or_replace_or_modify_tools -v`

  Expected: BOTH pass (registry has not changed yet; the new allowed names just aren't there yet).

- [ ] **Step 0.4: Commit**

  ```bash
  git add tests/test_mcp_alpaca_paper_tools.py
  git commit -m "test(ROB-73): widen Alpaca paper guards to allow submit/cancel tools"
  ```

---

### Task 1: Create the new orders module with submit handler (TDD)

**Files:**
- Create: `app/mcp_server/tooling/alpaca_paper_orders.py`
- Create: `tests/test_alpaca_paper_orders_tools.py`

- [ ] **Step 1.1: Write the failing first test — service factory + module surface**

  Create `tests/test_alpaca_paper_orders_tools.py`:

  ```python
  from __future__ import annotations

  from decimal import Decimal
  from typing import Any

  import pytest

  from app.mcp_server.profiles import McpProfile
  from app.mcp_server.tooling.registry import register_all_tools
  from tests._mcp_tooling_support import DummyMCP
  from tests.test_mcp_alpaca_paper_tools import FakeAlpacaPaperService


  def test_module_exposes_expected_surface() -> None:
      from app.mcp_server.tooling import alpaca_paper_orders as mod

      assert mod.ALPACA_PAPER_MUTATING_TOOL_NAMES == {
          "alpaca_paper_submit_order",
          "alpaca_paper_cancel_order",
      }
      assert callable(mod.alpaca_paper_submit_order)
      assert callable(mod.alpaca_paper_cancel_order)
      assert callable(mod.set_alpaca_paper_orders_service_factory)
      assert callable(mod.reset_alpaca_paper_orders_service_factory)
      assert callable(mod.register_alpaca_paper_orders_tools)
      assert mod.SUBMIT_MAX_QTY == Decimal("5")
      assert mod.SUBMIT_MAX_NOTIONAL_USD == Decimal("1000")
  ```

- [ ] **Step 1.2: Run test, expect ImportError / module-not-found**

  Run: `uv run pytest tests/test_alpaca_paper_orders_tools.py::test_module_exposes_expected_surface -v`
  Expected: FAIL with `ModuleNotFoundError: No module named 'app.mcp_server.tooling.alpaca_paper_orders'`.

- [ ] **Step 1.3: Create the module skeleton**

  Create `app/mcp_server/tooling/alpaca_paper_orders.py`:

  ```python
  """Guarded Alpaca paper submit/cancel MCP tools (ROB-73).

  Adapter-specific paper-only side-effect tools.  Both default to a
  no-broker-call state and require an explicit ``confirm=True`` flag to
  invoke ``AlpacaPaperBrokerService.submit_order`` / ``cancel_order``.

  These tools are NOT generic.  They never route through ``place_order`` /
  ``cancel_order`` / ``modify_order``.  There is no parameter that can
  switch the underlying service to the live endpoint.
  """

  from __future__ import annotations

  import hashlib
  import json
  from collections.abc import Callable
  from decimal import Decimal
  from typing import TYPE_CHECKING, Any

  from pydantic import BaseModel

  from app.mcp_server.tooling.alpaca_paper_preview import PreviewOrderInput
  from app.services.brokers.alpaca.schemas import OrderRequest
  from app.services.brokers.alpaca.service import AlpacaPaperBrokerService

  if TYPE_CHECKING:
      from fastmcp import FastMCP


  ALPACA_PAPER_MUTATING_TOOL_NAMES: set[str] = {
      "alpaca_paper_submit_order",
      "alpaca_paper_cancel_order",
  }

  SUBMIT_MAX_QTY: Decimal = Decimal("5")
  SUBMIT_MAX_NOTIONAL_USD: Decimal = Decimal("1000")

  ServiceFactory = Callable[[], AlpacaPaperBrokerService]


  def _default_service_factory() -> AlpacaPaperBrokerService:
      return AlpacaPaperBrokerService()


  _service_factory: ServiceFactory = _default_service_factory


  def set_alpaca_paper_orders_service_factory(factory: ServiceFactory) -> None:
      global _service_factory
      _service_factory = factory


  def reset_alpaca_paper_orders_service_factory() -> None:
      global _service_factory
      _service_factory = _default_service_factory


  def _model_to_jsonable(value: Any) -> Any:
      if isinstance(value, BaseModel):
          return value.model_dump(mode="json", by_alias=True)
      if isinstance(value, list | tuple):
          return [_model_to_jsonable(item) for item in value]
      if isinstance(value, dict):
          return {k: _model_to_jsonable(v) for k, v in value.items()}
      return value


  def _canonical_payload(validated: PreviewOrderInput) -> dict[str, Any]:
      return {
          "symbol": validated.symbol,
          "side": validated.side,
          "type": validated.type,
          "time_in_force": validated.time_in_force,
          "qty": str(validated.qty) if validated.qty is not None else None,
          "notional": str(validated.notional) if validated.notional is not None else None,
          "limit_price": str(validated.limit_price) if validated.limit_price is not None else None,
          "asset_class": validated.asset_class,
      }


  def _derive_client_order_id(payload: dict[str, Any]) -> str:
      blob = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
      digest = hashlib.sha256(blob).hexdigest()[:16]
      return f"rob73-{digest}"


  async def alpaca_paper_submit_order(
      symbol: str,
      side: str,
      type: str,  # noqa: A002
      qty: Decimal | None = None,
      notional: Decimal | None = None,
      time_in_force: str = "day",
      limit_price: Decimal | None = None,
      client_order_id: str | None = None,
      asset_class: str = "us_equity",
      confirm: bool = False,
  ) -> dict[str, Any]:
      """Submit a single Alpaca PAPER order (us_equity only).

      Defaults to ``confirm=False`` which performs no broker call.
      """
      validated = PreviewOrderInput(
          symbol=symbol,
          side=side,
          type=type,
          qty=qty,
          notional=notional,
          time_in_force=time_in_force,
          limit_price=limit_price,
          stop_price=None,
          client_order_id=client_order_id,
          asset_class=asset_class,
      )

      if validated.qty is not None and validated.qty > SUBMIT_MAX_QTY:
          raise ValueError(
              f"qty {validated.qty} exceeds submit cap ({SUBMIT_MAX_QTY})"
          )
      if validated.notional is not None and validated.notional > SUBMIT_MAX_NOTIONAL_USD:
          raise ValueError(
              f"notional {validated.notional} exceeds submit cap ({SUBMIT_MAX_NOTIONAL_USD})"
          )
      if (
          validated.qty is not None
          and validated.limit_price is not None
          and validated.qty * validated.limit_price > SUBMIT_MAX_NOTIONAL_USD
      ):
          raise ValueError(
              f"estimated_cost {validated.qty * validated.limit_price} "
              f"exceeds submit cap ({SUBMIT_MAX_NOTIONAL_USD})"
          )

      canonical = _canonical_payload(validated)
      coid = validated.client_order_id or _derive_client_order_id(canonical)

      if confirm is not True:
          return {
              "success": True,
              "account_mode": "alpaca_paper",
              "source": "alpaca_paper",
              "submitted": False,
              "blocked_reason": "confirmation_required",
              "order_request": canonical,
              "client_order_id": coid,
          }

      request = OrderRequest(
          symbol=validated.symbol,
          side=validated.side,
          type=validated.type,
          qty=validated.qty,
          notional=validated.notional,
          time_in_force=validated.time_in_force,
          limit_price=validated.limit_price,
          stop_price=None,
          client_order_id=coid,
      )
      order = await _service_factory().submit_order(request)
      return {
          "success": True,
          "account_mode": "alpaca_paper",
          "source": "alpaca_paper",
          "submitted": True,
          "order": _model_to_jsonable(order),
          "client_order_id": coid,
      }


  async def alpaca_paper_cancel_order(
      order_id: str,
      confirm: bool = False,
  ) -> dict[str, Any]:
      """Cancel exactly one Alpaca PAPER order by id."""
      stripped = (order_id or "").strip()
      if not stripped:
          raise ValueError("order_id is required")

      if confirm is not True:
          return {
              "success": True,
              "account_mode": "alpaca_paper",
              "source": "alpaca_paper",
              "cancelled": False,
              "blocked_reason": "confirmation_required",
              "target_order_id": stripped,
          }

      service = _service_factory()
      await service.cancel_order(stripped)

      order_payload: Any = None
      read_back_status = "ok"
      try:
          order = await service.get_order(stripped)
          order_payload = _model_to_jsonable(order)
      except Exception:  # noqa: BLE001 — read-back is best-effort
          read_back_status = "unavailable"

      return {
          "success": True,
          "account_mode": "alpaca_paper",
          "source": "alpaca_paper",
          "cancelled": True,
          "cancelled_order_id": stripped,
          "order": order_payload,
          "read_back_status": read_back_status,
      }


  def register_alpaca_paper_orders_tools(mcp: FastMCP) -> None:
      _ = mcp.tool(
          name="alpaca_paper_submit_order",
          description=(
              "Submit a single Alpaca PAPER us_equity order. "
              "Defaults to confirm=False which validates and returns the request "
              "WITHOUT calling the broker. Use confirm=True to actually submit. "
              "Paper endpoint only; live endpoint cannot be selected. "
              "Strict caps: qty<=5, notional<=$1000, qty*limit_price<=$1000."
          ),
      )(alpaca_paper_submit_order)
      _ = mcp.tool(
          name="alpaca_paper_cancel_order",
          description=(
              "Cancel exactly ONE Alpaca PAPER order by order_id. "
              "Defaults to confirm=False which returns the target order_id WITHOUT "
              "calling the broker. Use confirm=True to actually cancel. "
              "No bulk/all/by-symbol/by-status options. Paper endpoint only."
          ),
      )(alpaca_paper_cancel_order)


  __all__ = [
      "ALPACA_PAPER_MUTATING_TOOL_NAMES",
      "SUBMIT_MAX_NOTIONAL_USD",
      "SUBMIT_MAX_QTY",
      "alpaca_paper_cancel_order",
      "alpaca_paper_submit_order",
      "register_alpaca_paper_orders_tools",
      "reset_alpaca_paper_orders_service_factory",
      "set_alpaca_paper_orders_service_factory",
  ]
  ```

- [ ] **Step 1.4: Run the surface test, expect PASS**

  Run: `uv run pytest tests/test_alpaca_paper_orders_tools.py::test_module_exposes_expected_surface -v`
  Expected: PASS.

- [ ] **Step 1.5: Commit**

  ```bash
  git add app/mcp_server/tooling/alpaca_paper_orders.py tests/test_alpaca_paper_orders_tools.py
  git commit -m "feat(ROB-73): add alpaca_paper_orders module with submit/cancel handlers"
  ```

---

### Task 2: Submit handler — validation + confirm-gate tests

**Files:**
- Modify: `tests/test_alpaca_paper_orders_tools.py`

- [ ] **Step 2.1: Add the shared fixture to the test file**

  Append after the existing imports/test in `tests/test_alpaca_paper_orders_tools.py`:

  ```python
  from app.mcp_server.tooling.alpaca_paper_orders import (
      ALPACA_PAPER_MUTATING_TOOL_NAMES,
      SUBMIT_MAX_NOTIONAL_USD,
      SUBMIT_MAX_QTY,
      alpaca_paper_cancel_order,
      alpaca_paper_submit_order,
      reset_alpaca_paper_orders_service_factory,
      set_alpaca_paper_orders_service_factory,
  )
  from app.services.brokers.alpaca.schemas import Order


  class FakeOrdersService(FakeAlpacaPaperService):
      """Fake service that records submit/cancel calls without raising."""

      async def submit_order(self, request: Any) -> Order:  # type: ignore[override]
          self.calls.append(("submit_order", {"request": request}))
          return Order(
              id="paper-order-123",
              client_order_id=getattr(request, "client_order_id", None),
              symbol=getattr(request, "symbol", "AAPL"),
              qty=getattr(request, "qty", None),
              filled_qty=Decimal("0"),
              side=getattr(request, "side", "buy"),
              type=getattr(request, "type", "limit"),
              time_in_force=getattr(request, "time_in_force", "day"),
              status="accepted",
              limit_price=getattr(request, "limit_price", None),
          )

      async def cancel_order(self, order_id: str) -> None:  # type: ignore[override]
          self.calls.append(("cancel_order", {"order_id": order_id}))

      async def get_order(self, order_id: str) -> Order:  # type: ignore[override]
          self.calls.append(("get_order", {"order_id": order_id}))
          return Order(
              id=order_id,
              symbol="AAPL",
              qty=Decimal("1"),
              filled_qty=Decimal("0"),
              side="buy",
              type="limit",
              time_in_force="day",
              status="canceled",
              limit_price=Decimal("1.00"),
          )


  @pytest.fixture
  def fake_orders_service() -> FakeOrdersService:
      service = FakeOrdersService()
      set_alpaca_paper_orders_service_factory(lambda: service)  # type: ignore[arg-type]
      yield service
      reset_alpaca_paper_orders_service_factory()
  ```

- [ ] **Step 2.2: Add submit confirm-gate + validation tests**

  ```python
  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_submit_without_confirm_is_blocked_no_op(
      fake_orders_service: FakeOrdersService,
  ) -> None:
      payload = await alpaca_paper_submit_order(
          symbol="AAPL", side="buy", type="limit",
          qty=Decimal("1"), limit_price=Decimal("1.00"),
      )
      assert payload["submitted"] is False
      assert payload["blocked_reason"] == "confirmation_required"
      assert payload["order_request"]["symbol"] == "AAPL"
      assert payload["client_order_id"].startswith("rob73-")
      assert fake_orders_service.calls == []


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_submit_with_confirm_calls_service_once(
      fake_orders_service: FakeOrdersService,
  ) -> None:
      payload = await alpaca_paper_submit_order(
          symbol="AAPL", side="buy", type="limit",
          qty=Decimal("1"), limit_price=Decimal("1.00"),
          confirm=True,
      )
      assert payload["submitted"] is True
      assert payload["order"]["id"] == "paper-order-123"
      submit_calls = [c for c in fake_orders_service.calls if c[0] == "submit_order"]
      assert len(submit_calls) == 1
      sent = submit_calls[0][1]["request"]
      assert sent.symbol == "AAPL"
      assert sent.qty == Decimal("1")
      assert sent.limit_price == Decimal("1.00")
      assert sent.client_order_id.startswith("rob73-")


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_submit_caller_client_order_id_passes_through(
      fake_orders_service: FakeOrdersService,
  ) -> None:
      payload = await alpaca_paper_submit_order(
          symbol="AAPL", side="buy", type="limit",
          qty=Decimal("1"), limit_price=Decimal("1.00"),
          client_order_id="dev-smoke-001", confirm=True,
      )
      assert payload["client_order_id"] == "dev-smoke-001"
      sent = [c for c in fake_orders_service.calls if c[0] == "submit_order"][0][1]["request"]
      assert sent.client_order_id == "dev-smoke-001"


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_submit_rejects_qty_exceeding_cap(
      fake_orders_service: FakeOrdersService,
  ) -> None:
      with pytest.raises(ValueError, match="exceeds submit cap"):
          await alpaca_paper_submit_order(
              symbol="AAPL", side="buy", type="limit",
              qty=SUBMIT_MAX_QTY + Decimal("1"),
              limit_price=Decimal("1.00"),
              confirm=True,
          )
      assert [c for c in fake_orders_service.calls if c[0] == "submit_order"] == []


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_submit_rejects_notional_exceeding_cap(
      fake_orders_service: FakeOrdersService,
  ) -> None:
      with pytest.raises(ValueError, match="exceeds submit cap"):
          await alpaca_paper_submit_order(
              symbol="AAPL", side="buy", type="market",
              notional=SUBMIT_MAX_NOTIONAL_USD + Decimal("1"),
              confirm=True,
          )
      assert [c for c in fake_orders_service.calls if c[0] == "submit_order"] == []


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_submit_rejects_estimated_cost_exceeding_cap(
      fake_orders_service: FakeOrdersService,
  ) -> None:
      with pytest.raises(ValueError, match="estimated_cost"):
          await alpaca_paper_submit_order(
              symbol="AAPL", side="buy", type="limit",
              qty=Decimal("5"), limit_price=Decimal("250"),  # 5 * 250 = 1250 > 1000
              confirm=True,
          )
      assert [c for c in fake_orders_service.calls if c[0] == "submit_order"] == []


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_submit_propagates_preview_validation_errors(
      fake_orders_service: FakeOrdersService,
  ) -> None:
      with pytest.raises(ValueError):
          await alpaca_paper_submit_order(
              symbol="AAPL", side="hold", type="limit",
              qty=Decimal("1"), limit_price=Decimal("1.00"),
          )
      with pytest.raises(ValueError, match="limit_price is required"):
          await alpaca_paper_submit_order(
              symbol="AAPL", side="buy", type="limit", qty=Decimal("1"),
          )
      with pytest.raises(ValueError, match="exactly one"):
          await alpaca_paper_submit_order(
              symbol="AAPL", side="buy", type="market",
              qty=Decimal("1"), notional=Decimal("100"),
          )
      with pytest.raises(ValueError, match="us_equity only"):
          await alpaca_paper_submit_order(
              symbol="BTC", side="buy", type="market",
              qty=Decimal("1"), asset_class="crypto",
          )
      assert fake_orders_service.calls == []


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_submit_client_order_id_is_deterministic(
      fake_orders_service: FakeOrdersService,
  ) -> None:
      a = await alpaca_paper_submit_order(
          symbol="AAPL", side="buy", type="limit",
          qty=Decimal("1"), limit_price=Decimal("1.00"),
      )
      b = await alpaca_paper_submit_order(
          symbol="AAPL", side="buy", type="limit",
          qty=Decimal("1"), limit_price=Decimal("1.00"),
      )
      assert a["client_order_id"] == b["client_order_id"]
      c = await alpaca_paper_submit_order(
          symbol="MSFT", side="buy", type="limit",
          qty=Decimal("1"), limit_price=Decimal("1.00"),
      )
      assert c["client_order_id"] != a["client_order_id"]
  ```

- [ ] **Step 2.3: Run the new tests**

  Run: `uv run pytest tests/test_alpaca_paper_orders_tools.py -v -k "submit"`
  Expected: all PASS.

- [ ] **Step 2.4: Commit**

  ```bash
  git add tests/test_alpaca_paper_orders_tools.py
  git commit -m "test(ROB-73): cover alpaca_paper_submit_order validation and confirm gate"
  ```

---

### Task 3: Cancel handler tests

**Files:**
- Modify: `tests/test_alpaca_paper_orders_tools.py`

- [ ] **Step 3.1: Append cancel tests**

  ```python
  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_cancel_without_confirm_is_blocked_no_op(
      fake_orders_service: FakeOrdersService,
  ) -> None:
      payload = await alpaca_paper_cancel_order(order_id="paper-order-123")
      assert payload["cancelled"] is False
      assert payload["blocked_reason"] == "confirmation_required"
      assert payload["target_order_id"] == "paper-order-123"
      assert fake_orders_service.calls == []


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_cancel_with_confirm_calls_service_once_and_reads_back(
      fake_orders_service: FakeOrdersService,
  ) -> None:
      payload = await alpaca_paper_cancel_order(
          order_id="paper-order-123", confirm=True,
      )
      assert payload["cancelled"] is True
      assert payload["cancelled_order_id"] == "paper-order-123"
      assert payload["read_back_status"] == "ok"
      assert payload["order"]["status"] == "canceled"
      cancel_calls = [c for c in fake_orders_service.calls if c[0] == "cancel_order"]
      assert cancel_calls == [("cancel_order", {"order_id": "paper-order-123"})]


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_cancel_strips_whitespace_from_order_id(
      fake_orders_service: FakeOrdersService,
  ) -> None:
      payload = await alpaca_paper_cancel_order(
          order_id="  paper-order-123  ", confirm=True,
      )
      assert payload["cancelled_order_id"] == "paper-order-123"


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_cancel_rejects_blank_order_id(
      fake_orders_service: FakeOrdersService,
  ) -> None:
      for bad in ("", "   ", "\t\n"):
          with pytest.raises(ValueError, match="order_id is required"):
              await alpaca_paper_cancel_order(order_id=bad, confirm=True)
      assert fake_orders_service.calls == []


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_cancel_signature_has_no_bulk_or_filter_params() -> None:
      import inspect
      sig = inspect.signature(alpaca_paper_cancel_order)
      assert set(sig.parameters.keys()) == {"order_id", "confirm"}


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_cancel_read_back_failure_marks_unavailable_but_succeeds(
      fake_orders_service: FakeOrdersService,
  ) -> None:
      from app.services.brokers.alpaca.exceptions import AlpacaPaperRequestError

      async def _raise(_id: str) -> Order:
          raise AlpacaPaperRequestError("not found", status_code=404)

      fake_orders_service.get_order = _raise  # type: ignore[assignment]

      payload = await alpaca_paper_cancel_order(
          order_id="paper-order-123", confirm=True,
      )
      assert payload["cancelled"] is True
      assert payload["read_back_status"] == "unavailable"
      assert payload["order"] is None
  ```

- [ ] **Step 3.2: Run tests**

  Run: `uv run pytest tests/test_alpaca_paper_orders_tools.py -v -k "cancel"`
  Expected: all PASS.

- [ ] **Step 3.3: Commit**

  ```bash
  git add tests/test_alpaca_paper_orders_tools.py
  git commit -m "test(ROB-73): cover alpaca_paper_cancel_order confirm gate and read-back"
  ```

---

### Task 4: Endpoint-guard fail-closed tests

**Files:**
- Modify: `tests/test_alpaca_paper_orders_tools.py`

- [ ] **Step 4.1: Append live-endpoint guard tests**

  ```python
  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_submit_fails_closed_on_live_endpoint(
      monkeypatch: pytest.MonkeyPatch,
  ) -> None:
      from app.mcp_server.tooling import alpaca_paper_orders as mod
      from app.services.brokers.alpaca.config import AlpacaPaperSettings
      from app.services.brokers.alpaca.endpoints import LIVE_TRADING_BASE_URL
      from app.services.brokers.alpaca.exceptions import AlpacaPaperEndpointError

      def fake_from_app_settings() -> AlpacaPaperSettings:
          return AlpacaPaperSettings(
              api_key="pk-test", api_secret="sk-test",
              base_url=LIVE_TRADING_BASE_URL,
          )

      monkeypatch.setattr(
          AlpacaPaperSettings, "from_app_settings", fake_from_app_settings
      )
      mod.reset_alpaca_paper_orders_service_factory()

      with pytest.raises(AlpacaPaperEndpointError):
          await alpaca_paper_submit_order(
              symbol="AAPL", side="buy", type="limit",
              qty=Decimal("1"), limit_price=Decimal("1.00"),
              confirm=True,
          )


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_cancel_fails_closed_on_live_endpoint(
      monkeypatch: pytest.MonkeyPatch,
  ) -> None:
      from app.mcp_server.tooling import alpaca_paper_orders as mod
      from app.services.brokers.alpaca.config import AlpacaPaperSettings
      from app.services.brokers.alpaca.endpoints import LIVE_TRADING_BASE_URL
      from app.services.brokers.alpaca.exceptions import AlpacaPaperEndpointError

      def fake_from_app_settings() -> AlpacaPaperSettings:
          return AlpacaPaperSettings(
              api_key="pk-test", api_secret="sk-test",
              base_url=LIVE_TRADING_BASE_URL,
          )

      monkeypatch.setattr(
          AlpacaPaperSettings, "from_app_settings", fake_from_app_settings
      )
      mod.reset_alpaca_paper_orders_service_factory()

      with pytest.raises(AlpacaPaperEndpointError):
          await alpaca_paper_cancel_order(order_id="paper-order-123", confirm=True)


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_submit_signature_has_no_endpoint_or_base_url_param() -> None:
      import inspect
      sig = inspect.signature(alpaca_paper_submit_order)
      param_names = set(sig.parameters.keys())
      forbidden = {"endpoint", "base_url", "live", "url", "host", "env"}
      assert forbidden.isdisjoint(param_names)
  ```

- [ ] **Step 4.2: Run guard tests**

  Run: `uv run pytest tests/test_alpaca_paper_orders_tools.py -v -k "fails_closed or endpoint"`
  Expected: all PASS.

- [ ] **Step 4.3: Commit**

  ```bash
  git add tests/test_alpaca_paper_orders_tools.py
  git commit -m "test(ROB-73): assert submit/cancel fail closed on live endpoint"
  ```

---

### Task 5: Register the tools in both profiles

**Files:**
- Modify: `app/mcp_server/tooling/registry.py`
- Modify: `tests/test_alpaca_paper_orders_tools.py`

- [ ] **Step 5.1: Add registration tests (must fail first)**

  Append to `tests/test_alpaca_paper_orders_tools.py`:

  ```python
  @pytest.mark.unit
  def test_registers_alpaca_paper_orders_tools_default_profile() -> None:
      mcp = DummyMCP()
      register_all_tools(mcp, profile=McpProfile.DEFAULT)  # type: ignore[arg-type]
      assert ALPACA_PAPER_MUTATING_TOOL_NAMES <= mcp.tools.keys()


  @pytest.mark.unit
  def test_registers_alpaca_paper_orders_tools_paper_profile() -> None:
      mcp = DummyMCP()
      register_all_tools(mcp, profile=McpProfile.HERMES_PAPER_KIS)  # type: ignore[arg-type]
      assert ALPACA_PAPER_MUTATING_TOOL_NAMES <= mcp.tools.keys()


  @pytest.mark.unit
  def test_no_alpaca_paper_place_replace_modify_or_bulk_cancel_tools() -> None:
      mcp = DummyMCP()
      register_all_tools(mcp, profile=McpProfile.DEFAULT)  # type: ignore[arg-type]
      forbidden = {
          "alpaca_paper_place_order",
          "alpaca_paper_replace_order",
          "alpaca_paper_modify_order",
          "alpaca_paper_cancel_all_orders",
          "alpaca_paper_cancel_orders",
          "alpaca_paper_cancel_by_symbol",
      }
      assert forbidden.isdisjoint(mcp.tools.keys())
  ```

- [ ] **Step 5.2: Run, expect FAIL**

  Run: `uv run pytest tests/test_alpaca_paper_orders_tools.py -v -k "registers_alpaca_paper_orders or no_alpaca_paper_place"`
  Expected: registration tests FAIL (`alpaca_paper_submit_order` not yet in `mcp.tools`).

- [ ] **Step 5.3: Wire registration in `app/mcp_server/tooling/registry.py`**

  Add the import (alphabetised group near existing alpaca imports):

  ```python
  from app.mcp_server.tooling.alpaca_paper_orders import (
      register_alpaca_paper_orders_tools,
  )
  ```

  In `register_all_tools()`, after the existing
  `register_alpaca_paper_preview_tools(mcp)` line, add:

  ```python
      register_alpaca_paper_orders_tools(mcp)
  ```

  This runs in the always-registered block (above the profile-gated section), so both `DEFAULT` and `HERMES_PAPER_KIS` get the tools.

- [ ] **Step 5.4: Run all alpaca tests**

  Run: `uv run pytest tests/test_mcp_alpaca_paper_tools.py tests/test_alpaca_paper_orders_tools.py tests/test_alpaca_paper_smoke_safety.py tests/test_alpaca_paper_isolation.py -v`
  Expected: all PASS.

- [ ] **Step 5.5: Commit**

  ```bash
  git add app/mcp_server/tooling/registry.py tests/test_alpaca_paper_orders_tools.py
  git commit -m "feat(ROB-73): register alpaca_paper submit/cancel MCP tools in all profiles"
  ```

---

### Task 6: Validation reuse drift test (preview ↔ submit canonical payload)

**Files:**
- Modify: `tests/test_alpaca_paper_orders_tools.py`

- [ ] **Step 6.1: Add the parity test**

  ```python
  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_submit_canonical_payload_matches_preview_order_request(
      fake_orders_service: FakeOrdersService,
      monkeypatch: pytest.MonkeyPatch,
  ) -> None:
      """Submit's order_request must agree with preview's order_request on shared fields."""
      from app.mcp_server.tooling.alpaca_paper_preview import (
          alpaca_paper_preview_order,
          reset_alpaca_paper_preview_service_factory,
          set_alpaca_paper_preview_service_factory,
      )

      set_alpaca_paper_preview_service_factory(lambda: fake_orders_service)  # type: ignore[arg-type]
      try:
          preview = await alpaca_paper_preview_order(
              symbol="AAPL", side="buy", type="limit",
              qty=Decimal("1"), limit_price=Decimal("1.00"),
          )
      finally:
          reset_alpaca_paper_preview_service_factory()

      submit_blocked = await alpaca_paper_submit_order(
          symbol="AAPL", side="buy", type="limit",
          qty=Decimal("1"), limit_price=Decimal("1.00"),
      )

      shared = ("symbol", "side", "type", "time_in_force", "qty",
                "notional", "limit_price", "asset_class")
      for key in shared:
          assert preview["order_request"][key] == submit_blocked["order_request"][key], (
              f"preview/submit drift on '{key}'"
          )
  ```

- [ ] **Step 6.2: Run the parity test**

  Run: `uv run pytest tests/test_alpaca_paper_orders_tools.py::test_submit_canonical_payload_matches_preview_order_request -v`
  Expected: PASS.

- [ ] **Step 6.3: Commit**

  ```bash
  git add tests/test_alpaca_paper_orders_tools.py
  git commit -m "test(ROB-73): assert preview/submit canonical payload do not drift"
  ```

---

### Task 7: Dev smoke script

**Files:**
- Create: `scripts/smoke/alpaca_paper_dev_smoke.py`
- Create: `tests/test_alpaca_paper_dev_smoke_safety.py`

- [ ] **Step 7.1: Write the dev smoke script**

  Create `scripts/smoke/alpaca_paper_dev_smoke.py`:

  ```python
  """Dev/operator-only Alpaca PAPER submit→cancel smoke (ROB-73).

  Modes:
    Preview-only (default):
        uv run python scripts/smoke/alpaca_paper_dev_smoke.py
      Calls account/cash + alpaca_paper_submit_order(confirm=False) +
      alpaca_paper_cancel_order(order_id='dummy', confirm=False).
      No broker mutations.

    Side-effect mode (BOTH gates required):
        ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS=1 \\
            uv run python scripts/smoke/alpaca_paper_dev_smoke.py \\
            --confirm-paper-side-effect
      Submits one tiny PAPER limit order (AAPL buy 1 share @ $1.00),
      captures its id, cancels it, reads back final status, prints a
      redacted summary.

  This script never prints API keys, secrets, headers, or raw broker payloads.
  Either gate alone is rejected.
  """
  from __future__ import annotations

  import argparse
  import asyncio
  import os
  import sys
  from decimal import Decimal

  from app.mcp_server.tooling.alpaca_paper import (
      alpaca_paper_get_account,
      alpaca_paper_get_cash,
  )
  from app.mcp_server.tooling.alpaca_paper_orders import (
      alpaca_paper_cancel_order,
      alpaca_paper_submit_order,
  )

  SMOKE_SYMBOL = "AAPL"
  SMOKE_QTY = Decimal("1")
  SMOKE_LIMIT_PRICE = Decimal("1.00")  # far below market — should not fill
  ENV_GATE = "ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS"


  def _both_gates_set(args: argparse.Namespace) -> bool:
      return bool(args.confirm_paper_side_effect) and os.environ.get(ENV_GATE) == "1"


  async def _preview_only() -> int:
      lines: list[tuple[str, bool, str]] = []
      try:
          acct = await alpaca_paper_get_account()
          lines.append(("get_account", True, f"status={acct['account'].get('status', '?')}"))
      except Exception as exc:  # noqa: BLE001
          lines.append(("get_account", False, f"ERROR: {type(exc).__name__}"))

      try:
          cash = await alpaca_paper_get_cash()
          lines.append(("get_cash", True, f"cash_set={cash['cash'].get('cash') is not None}"))
      except Exception as exc:  # noqa: BLE001
          lines.append(("get_cash", False, f"ERROR: {type(exc).__name__}"))

      try:
          submit = await alpaca_paper_submit_order(
              symbol=SMOKE_SYMBOL, side="buy", type="limit",
              qty=SMOKE_QTY, limit_price=SMOKE_LIMIT_PRICE,
          )
          lines.append((
              "submit_order(confirm=False)", submit["submitted"] is False,
              f"blocked_reason={submit.get('blocked_reason')}",
          ))
      except Exception as exc:  # noqa: BLE001
          lines.append(("submit_order(confirm=False)", False, f"ERROR: {type(exc).__name__}"))

      try:
          cancel = await alpaca_paper_cancel_order(order_id="dummy-no-op")
          lines.append((
              "cancel_order(confirm=False)", cancel["cancelled"] is False,
              f"blocked_reason={cancel.get('blocked_reason')}",
          ))
      except Exception as exc:  # noqa: BLE001
          lines.append(("cancel_order(confirm=False)", False, f"ERROR: {type(exc).__name__}"))

      ok = all(success for _, success, _ in lines)
      for name, success, note in lines:
          print(f"  [{'OK' if success else 'FAIL'}] {name}: {note}")
      print(f"summary: {'PASS' if ok else 'FAIL'} mode=preview_only")
      return 0 if ok else 1


  async def _side_effect_smoke() -> int:
      lines: list[tuple[str, bool, str]] = []
      submitted_id: str | None = None
      cancelled = False
      readback_status = "unknown"

      try:
          acct = await alpaca_paper_get_account()
          lines.append(("get_account", True, f"status={acct['account'].get('status', '?')}"))
      except Exception as exc:  # noqa: BLE001
          lines.append(("get_account", False, f"ERROR: {type(exc).__name__}"))
          ok = all(s for _, s, _ in lines)
          for name, s, note in lines:
              print(f"  [{'OK' if s else 'FAIL'}] {name}: {note}")
          print("summary: BLOCKED mode=side_effects reason=account_unreachable")
          return 1

      try:
          submit = await alpaca_paper_submit_order(
              symbol=SMOKE_SYMBOL, side="buy", type="limit",
              qty=SMOKE_QTY, limit_price=SMOKE_LIMIT_PRICE,
              confirm=True,
          )
          submitted_id = submit["order"]["id"]
          lines.append((
              "submit_order(confirm=True)", submit["submitted"] is True,
              f"order_id_len={len(submitted_id)} status={submit['order'].get('status', '?')}",
          ))
      except Exception as exc:  # noqa: BLE001
          lines.append(("submit_order(confirm=True)", False, f"ERROR: {type(exc).__name__}"))

      if submitted_id:
          try:
              cancel = await alpaca_paper_cancel_order(
                  order_id=submitted_id, confirm=True,
              )
              cancelled = bool(cancel.get("cancelled"))
              readback_status = cancel.get("read_back_status", "unknown")
              order = cancel.get("order") or {}
              lines.append((
                  "cancel_order(confirm=True)", cancelled,
                  f"read_back={readback_status} final_status={order.get('status', '?')}",
              ))
          except Exception as exc:  # noqa: BLE001
              lines.append(("cancel_order(confirm=True)", False, f"ERROR: {type(exc).__name__}"))

      ok = all(success for _, success, _ in lines)
      for name, success, note in lines:
          print(f"  [{'OK' if success else 'FAIL'}] {name}: {note}")
      classification = "PASS" if ok and cancelled else "PARTIAL"
      print(f"summary: {classification} mode=side_effects")
      return 0 if classification == "PASS" else 1


  async def _async_main(args: argparse.Namespace) -> int:
      if args.confirm_paper_side_effect and os.environ.get(ENV_GATE) != "1":
          print(
              f"BLOCKED: --confirm-paper-side-effect requires {ENV_GATE}=1; "
              "either gate alone is rejected.",
              file=sys.stderr,
          )
          return 2
      if not args.confirm_paper_side_effect and os.environ.get(ENV_GATE) == "1":
          print(
              f"BLOCKED: {ENV_GATE}=1 requires --confirm-paper-side-effect; "
              "either gate alone is rejected.",
              file=sys.stderr,
          )
          return 2

      if _both_gates_set(args):
          return await _side_effect_smoke()
      return await _preview_only()


  def build_parser() -> argparse.ArgumentParser:
      parser = argparse.ArgumentParser(
          description="Dev-owned Alpaca PAPER submit/cancel smoke runner",
      )
      parser.add_argument(
          "--confirm-paper-side-effect",
          action="store_true",
          help=f"Required (with {ENV_GATE}=1) to enable broker mutations",
      )
      return parser


  def main() -> None:
      args = build_parser().parse_args()
      sys.exit(asyncio.run(_async_main(args)))


  if __name__ == "__main__":
      main()
  ```

- [ ] **Step 7.2: Write the dev smoke safety tests**

  Create `tests/test_alpaca_paper_dev_smoke_safety.py`:

  ```python
  """Safety tests for scripts/smoke/alpaca_paper_dev_smoke.py (ROB-73)."""
  from __future__ import annotations

  import ast
  import importlib.util
  import os
  from pathlib import Path

  import pytest

  from app.mcp_server.tooling.alpaca_paper import (
      reset_alpaca_paper_service_factory,
      set_alpaca_paper_service_factory,
  )
  from app.mcp_server.tooling.alpaca_paper_orders import (
      reset_alpaca_paper_orders_service_factory,
      set_alpaca_paper_orders_service_factory,
  )

  SCRIPT_PATH = (
      Path(__file__).resolve().parents[1]
      / "scripts" / "smoke" / "alpaca_paper_dev_smoke.py"
  )

  FORBIDDEN_SECRET_STRINGS = (
      "APCA-API-KEY-ID",
      "APCA-API-SECRET-KEY",
      "Authorization",
      "get_secret_value",
      "api_secret",
  )


  @pytest.mark.unit
  def test_dev_smoke_script_exists() -> None:
      assert SCRIPT_PATH.exists()


  @pytest.mark.unit
  def test_dev_smoke_script_has_no_secret_or_header_strings() -> None:
      text = SCRIPT_PATH.read_text(encoding="utf-8")
      hits = [s for s in FORBIDDEN_SECRET_STRINGS if s in text]
      assert not hits, f"dev smoke script references secret strings: {hits}"


  @pytest.mark.unit
  def test_dev_smoke_script_no_raw_payload_print() -> None:
      source = SCRIPT_PATH.read_text(encoding="utf-8")
      tree = ast.parse(source)
      raw_names = {"payload", "result", "orders", "positions", "account",
                   "fills", "assets", "order", "submit", "cancel", "cash"}
      for node in ast.walk(tree):
          if isinstance(node, ast.Call):
              func = node.func
              if isinstance(func, ast.Name) and func.id == "print":
                  for arg in node.args:
                      if isinstance(arg, ast.Name) and arg.id in raw_names:
                          pytest.fail(
                              f"smoke script calls print({arg.id}) "
                              "which would dump a raw broker payload"
                          )


  @pytest.mark.unit
  def test_dev_smoke_script_does_not_route_through_legacy_order_tools() -> None:
      text = SCRIPT_PATH.read_text(encoding="utf-8")
      forbidden = ("place_order", "modify_order", "replace_order",
                   "cancel_all", "cancel_by_symbol")
      hits = [s for s in forbidden if s in text]
      assert not hits, f"dev smoke script references forbidden order routes: {hits}"


  def _load_module():
      spec = importlib.util.spec_from_file_location("_alpaca_dev_smoke", SCRIPT_PATH)
      assert spec is not None and spec.loader is not None
      module = importlib.util.module_from_spec(spec)
      spec.loader.exec_module(module)  # type: ignore[union-attr]
      return module


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_dev_smoke_default_mode_no_broker_calls(
      capsys: pytest.CaptureFixture[str],
      monkeypatch: pytest.MonkeyPatch,
  ) -> None:
      from tests.test_mcp_alpaca_paper_tools import FakeAlpacaPaperService
      from tests.test_alpaca_paper_orders_tools import FakeOrdersService

      ro = FakeAlpacaPaperService()
      orders = FakeOrdersService()
      set_alpaca_paper_service_factory(lambda: ro)  # type: ignore[arg-type]
      set_alpaca_paper_orders_service_factory(lambda: orders)  # type: ignore[arg-type]
      monkeypatch.delenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", raising=False)
      try:
          module = _load_module()
          args = module.build_parser().parse_args([])
          rc = await module._async_main(args)
      finally:
          reset_alpaca_paper_service_factory()
          reset_alpaca_paper_orders_service_factory()

      captured = capsys.readouterr()
      assert rc == 0
      assert "mode=preview_only" in captured.out
      submit_calls = [c for c in orders.calls if c[0] == "submit_order"]
      cancel_calls = [c for c in orders.calls if c[0] == "cancel_order"]
      assert submit_calls == []
      assert cancel_calls == []


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_dev_smoke_flag_without_env_is_blocked(
      capsys: pytest.CaptureFixture[str],
      monkeypatch: pytest.MonkeyPatch,
  ) -> None:
      from tests.test_alpaca_paper_orders_tools import FakeOrdersService

      orders = FakeOrdersService()
      set_alpaca_paper_orders_service_factory(lambda: orders)  # type: ignore[arg-type]
      monkeypatch.delenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", raising=False)
      try:
          module = _load_module()
          args = module.build_parser().parse_args(["--confirm-paper-side-effect"])
          rc = await module._async_main(args)
      finally:
          reset_alpaca_paper_orders_service_factory()

      assert rc == 2
      assert [c for c in orders.calls if c[0] in ("submit_order", "cancel_order")] == []


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_dev_smoke_env_without_flag_is_blocked(
      monkeypatch: pytest.MonkeyPatch,
  ) -> None:
      from tests.test_alpaca_paper_orders_tools import FakeOrdersService

      orders = FakeOrdersService()
      set_alpaca_paper_orders_service_factory(lambda: orders)  # type: ignore[arg-type]
      monkeypatch.setenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", "1")
      try:
          module = _load_module()
          args = module.build_parser().parse_args([])
          rc = await module._async_main(args)
      finally:
          reset_alpaca_paper_orders_service_factory()
          monkeypatch.delenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", raising=False)

      assert rc == 2


  @pytest.mark.unit
  @pytest.mark.asyncio
  async def test_dev_smoke_both_gates_runs_submit_then_cancel(
      capsys: pytest.CaptureFixture[str],
      monkeypatch: pytest.MonkeyPatch,
  ) -> None:
      from tests.test_mcp_alpaca_paper_tools import FakeAlpacaPaperService
      from tests.test_alpaca_paper_orders_tools import FakeOrdersService

      ro = FakeAlpacaPaperService()
      orders = FakeOrdersService()
      set_alpaca_paper_service_factory(lambda: ro)  # type: ignore[arg-type]
      set_alpaca_paper_orders_service_factory(lambda: orders)  # type: ignore[arg-type]
      monkeypatch.setenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", "1")
      try:
          module = _load_module()
          args = module.build_parser().parse_args(["--confirm-paper-side-effect"])
          rc = await module._async_main(args)
      finally:
          reset_alpaca_paper_service_factory()
          reset_alpaca_paper_orders_service_factory()
          monkeypatch.delenv("ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS", raising=False)

      out = capsys.readouterr().out
      assert rc == 0
      assert "mode=side_effects" in out
      submit_calls = [c for c in orders.calls if c[0] == "submit_order"]
      cancel_calls = [c for c in orders.calls if c[0] == "cancel_order"]
      assert len(submit_calls) == 1
      assert len(cancel_calls) == 1
      assert cancel_calls[0][1]["order_id"] == "paper-order-123"
  ```

- [ ] **Step 7.3: Run the dev smoke tests**

  Run: `uv run pytest tests/test_alpaca_paper_dev_smoke_safety.py -v`
  Expected: all PASS.

- [ ] **Step 7.4: Verify the existing read-only smoke safety still passes**

  Run: `uv run pytest tests/test_alpaca_paper_smoke_safety.py -v`
  Expected: all PASS — the read-only smoke script is untouched and remains free of submit/cancel verbs.

- [ ] **Step 7.5: Commit**

  ```bash
  git add scripts/smoke/alpaca_paper_dev_smoke.py tests/test_alpaca_paper_dev_smoke_safety.py
  git commit -m "feat(ROB-73): add dual-gated dev smoke for Alpaca paper submit/cancel"
  ```

---

### Task 8: Operator runbook for the dev smoke

**Files:**
- Create: `docs/runbooks/alpaca-paper-dev-smoke.md`

- [ ] **Step 8.1: Write the runbook**

  Create `docs/runbooks/alpaca-paper-dev-smoke.md` with the following content:

  ```markdown
  # Alpaca Paper Dev Smoke (Submit → Cancel) — Operator Runbook

  Owner: Dev (NOT production ops)
  Related issues: ROB-73 / ROB-72 / ROB-71 / ROB-70 / ROB-69

  This runbook covers the dev-owned smoke for the two new MCP tools
  `alpaca_paper_submit_order` and `alpaca_paper_cancel_order`. The smoke is
  intentionally hard to run with side effects by accident.

  ## Scope and safety boundary

  - Adapter-specific paper-only tools. No live endpoint, no data endpoint as trading base, no generic order route, no bulk cancel.
  - Default mode: preview only. No broker mutations, no `submit_order` / `cancel_order` HTTP calls.
  - Side-effect mode requires BOTH a CLI flag (`--confirm-paper-side-effect`) AND an env var (`ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS=1`). Either alone exits with code 2 and zero broker calls.
  - Side-effect smoke places ONE tiny PAPER order: `AAPL` buy `1` share `limit @ $1.00`. The price is far below market so the order should not fill before cancel. If market behaviour fills it, mark the result PARTIAL and document in the report.
  - Never run this from production hosts. This issue is dev-owned smoke.
  - Never paste API keys, secrets, Authorization headers, or raw broker payloads.

  ## Step 1 — Verify environment without printing secrets

  ```bash
  python - <<'PY'
  import os
  for k in ('ALPACA_PAPER_API_KEY', 'ALPACA_PAPER_API_SECRET'):
      v = os.environ.get(k, '')
      print(f'{k}: present={bool(v)} len={len(v)}')
  print('ALPACA_PAPER_BASE_URL=', os.environ.get('ALPACA_PAPER_BASE_URL', '<unset>'))
  PY
  ```

  Expected: keys present, base URL unset or exactly `https://paper-api.alpaca.markets`.

  ## Step 2 — Preview-only smoke (default)

  ```bash
  uv run python scripts/smoke/alpaca_paper_dev_smoke.py
  ```

  Expected output shape:

  ```text
    [OK] get_account: status=ACTIVE
    [OK] get_cash: cash_set=True
    [OK] submit_order(confirm=False): blocked_reason=confirmation_required
    [OK] cancel_order(confirm=False): blocked_reason=confirmation_required
  summary: PASS mode=preview_only
  ```

  Exit code 0 = PASS. Any FAIL line → BLOCKED.

  ## Step 3 — Side-effect smoke (BOTH gates required)

  Only run when explicitly authorised on a dev host.

  ```bash
  ALPACA_PAPER_SMOKE_ALLOW_SIDE_EFFECTS=1 \
      uv run python scripts/smoke/alpaca_paper_dev_smoke.py \
      --confirm-paper-side-effect
  ```

  Expected:

  ```text
    [OK] get_account: status=ACTIVE
    [OK] submit_order(confirm=True): order_id_len=36 status=accepted
    [OK] cancel_order(confirm=True): read_back=ok final_status=canceled
  summary: PASS mode=side_effects
  ```

  - `summary: PARTIAL mode=side_effects` → cancel did not confirm or read-back was unavailable. Investigate but do not retry without re-checking gates.
  - `summary: BLOCKED ...` → either gate missing. Re-read Step 3.

  ## Step 4 — Verify post-smoke state

  ```bash
  uv run python scripts/smoke/alpaca_paper_readonly_smoke.py
  ```

  Expected: open orders count back to baseline (typically 0). The cancelled order may still appear under non-open statuses for a short time.

  ## Step 5 — Report template (dev → Linear)

  ```text
  ROB-73 dev smoke: PASS|PARTIAL|BLOCKED
  mode: preview_only|side_effects
  preview_only_exit: <0|1>
  side_effect_exit: <0|1|2|skipped>
  notes: <redacted exception class only, if any>
  safety: paper endpoint only; both gates required for side effects; no secrets printed; no bulk cancel.
  ```
  ```

- [ ] **Step 8.2: Commit**

  ```bash
  git add docs/runbooks/alpaca-paper-dev-smoke.md
  git commit -m "docs(ROB-73): operator runbook for Alpaca paper dev smoke"
  ```

---

### Task 9: Final verification (full alpaca test slice + lint + format)

- [ ] **Step 9.1: Run the full alpaca test slice**

  Run:
  ```bash
  uv run pytest tests/test_mcp_alpaca_paper_tools.py \
                tests/test_alpaca_paper_orders_tools.py \
                tests/test_alpaca_paper_smoke_safety.py \
                tests/test_alpaca_paper_dev_smoke_safety.py \
                tests/test_alpaca_paper_isolation.py \
                tests/test_alpaca_paper_service_endpoint_guard.py \
                tests/test_alpaca_paper_service_methods.py \
                tests/test_alpaca_paper_config.py -q
  ```
  Expected: all PASS.

- [ ] **Step 9.2: Run lint and format check**

  Run:
  ```bash
  uv run ruff check app tests scripts docs || uv run ruff check app tests scripts
  uv run ruff format --check app tests scripts
  ```
  Expected: clean. Fix any issues with `uv run ruff check --fix app tests scripts` and `uv run ruff format app tests scripts`.

- [ ] **Step 9.3: Optional — typecheck (per repo convention)**

  Run: `make typecheck` (or `uv run ty check app` if `ty` is the repo's convention).
  If failures are unrelated to this PR, document them and proceed.

- [ ] **Step 9.4: Optional commit if formatting changes**

  If lint/format produced edits:
  ```bash
  git add -p
  git commit -m "chore(ROB-73): apply ruff format/lint fixes"
  ```

---

## 10. Test Matrix (mapped to handoff prompt §4)

| Handoff requirement | Test |
|---------------------|------|
| MCP registry includes the two explicit tools | `test_registers_alpaca_paper_orders_tools_default_profile`, `test_registers_alpaca_paper_orders_tools_paper_profile` |
| Existing read-only and preview tools remain registered | Existing `test_registers_explicit_alpaca_paper_readonly_tools_*`, `test_registers_alpaca_paper_preview_tool_*` (untouched) |
| No generic Alpaca paper write path via place/cancel/modify | `test_no_alpaca_paper_place_replace_modify_or_bulk_cancel_tools`, updated `test_no_alpaca_live_or_mutating_alpaca_order_tools_registered`, `test_existing_generic_order_tools_are_not_alpaca_tools` (existing) |
| Live Alpaca base URL fails closed | `test_submit_fails_closed_on_live_endpoint`, `test_cancel_fails_closed_on_live_endpoint`, plus existing `test_service_init_rejects_live_endpoint` |
| Submit without confirmation is blocked/no-op | `test_submit_without_confirm_is_blocked_no_op` |
| Submit with confirmation calls service with validated payload (mocked) | `test_submit_with_confirm_calls_service_once`, `test_submit_caller_client_order_id_passes_through` |
| Submit caps reject oversized qty/notional | `test_submit_rejects_qty_exceeding_cap`, `test_submit_rejects_notional_exceeding_cap`, `test_submit_rejects_estimated_cost_exceeding_cap` |
| Cancel without confirmation is blocked/no-op | `test_cancel_without_confirm_is_blocked_no_op` |
| Cancel requires exact order id | `test_cancel_rejects_blank_order_id`, `test_cancel_signature_has_no_bulk_or_filter_params` |
| Cancel with confirmation calls exact paper service method (mocked) | `test_cancel_with_confirm_calls_service_once_and_reads_back`, `test_cancel_strips_whitespace_from_order_id` |
| Dev smoke script defaults to no side effects | `test_dev_smoke_default_mode_no_broker_calls` |
| Either gate alone is rejected | `test_dev_smoke_flag_without_env_is_blocked`, `test_dev_smoke_env_without_flag_is_blocked` |
| Side-effect mode runs submit then cancel | `test_dev_smoke_both_gates_runs_submit_then_cancel` |
| Smoke script has no secret/header strings | `test_dev_smoke_script_has_no_secret_or_header_strings`, `test_dev_smoke_script_no_raw_payload_print` |
| Smoke script does not route through legacy generic tools | `test_dev_smoke_script_does_not_route_through_legacy_order_tools` |
| Preview/submit canonical payload do not drift | `test_submit_canonical_payload_matches_preview_order_request` |
| Submit signature has no endpoint-switching parameter | `test_submit_signature_has_no_endpoint_or_base_url_param` |

## 11. Local Validation Commands

Run after every task and once at the end:

```bash
uv run pytest tests/test_mcp_alpaca_paper_tools.py \
              tests/test_alpaca_paper_smoke_safety.py \
              tests/test_alpaca_paper_orders_tools.py \
              tests/test_alpaca_paper_dev_smoke_safety.py -q

uv run ruff check app tests scripts docs || uv run ruff check app tests scripts
uv run ruff format --check app tests scripts
```

Optional broader checks before PR:

```bash
uv run pytest tests/test_alpaca_paper_isolation.py \
              tests/test_alpaca_paper_service_endpoint_guard.py \
              tests/test_alpaca_paper_service_methods.py \
              tests/test_alpaca_paper_config.py -q

uv run python scripts/smoke/alpaca_paper_dev_smoke.py   # preview-only — should print "summary: PASS mode=preview_only"
```

## 12. Handoff to Implementer

The implementer (Sonnet, same AoE session, same worktree) executes Tasks 0–9 in order, committing per-task. After Task 9, the implementer emits:

```text
AOE_STATUS: implementation_done
AOE_ISSUE: ROB-73
AOE_ROLE: implementer
AOE_AGENT: sonnet
AOE_TESTS: <commands and result summary>
AOE_NEXT: request_planner_review
```

Reviewer (Opus) then runs the full slice in §11, checks each safety boundary in §4, audits the diff against §5/§6/§9, and emits either `review_must_fix` or `review_passed`.
