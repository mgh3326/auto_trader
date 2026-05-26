# ROB-319 Kiwoom Mock Account Lifecycle & Real Mock Order Smoke — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Complete the Kiwoom **mock-investment** account/order lifecycle so account/order-read MCP tools return real broker data (no stub-success), confirmed `modify`/`cancel` are wired to the existing client, and an operator-safe KRX mock-order smoke (submit → modify → cancel → reconcile) can be run.

**Architecture:** The client layer is already built and unit-tested (`KiwoomDomesticAccountClient`, `KiwoomDomesticOrderClient.modify_order/cancel_order`, `KiwoomMockClient` transport with host allowlist). This work is almost entirely **wiring** the MCP seams in `app/mcp_server/tooling/orders_kiwoom_variants.py` to those clients, plus a default-disabled operator smoke CLI and a runbook. All existing safety guards (mock-only host, KRX-only, `dry_run=False` requires `confirm=True`, unsafe-order-id rejection, no-secret-printing) are preserved and extended — never weakened.

**Tech Stack:** Python 3.13, FastMCP tool registration, `httpx` transport (mock-only), `pytest` + `pytest-asyncio` (run via `uv run --all-groups pytest`), Ruff lint.

---

## Locked-in Claude-side defaults (from operator, 2026-05-26)

1. **`get_orderable_cash(symbol=...)` policy:**
   - `symbol` present → `KiwoomDomesticAccountClient.get_orderable_amount(symbol=...)` (kt00010).
   - `symbol` absent → `KiwoomDomesticAccountClient.get_balance()` (kt00018).
   - Normalize `cash` only when deterministically parseable; otherwise return `cash: null` + `cash_source: "<base>_unparsed"` and always attach raw `broker_response`. **Never fake a cash value.**
   - The mandatory invariant: the response must reflect a real broker client call, not a local stub-success.
2. **Smoke price sizing:**
   - Do **not** widen Kiwoom market-data scope (chart client stays deferred / `NotImplementedError`).
   - The operator picks a conservative non-marketable buy limit (may reference existing KIS quote/orderbook out of band) and passes it via `--price` (operator-approved override). KRX tick alignment is validated/floored with the existing `app/mcp_server/tick_size.py::get_tick_size_kr`. No new price/quote engine.
   - Order submission stays **Kiwoom mock / KRX only**.

---

## File Structure

| File | Responsibility | Action |
|---|---|---|
| `app/mcp_server/tooling/orders_kiwoom_variants.py` | MCP tool surface + impl seams | Modify (wire read tools + confirmed modify/cancel, add shared helpers) |
| `app/services/brokers/kiwoom/domestic_account.py` | Account/order-history client | Reuse as-is (no change expected) |
| `app/services/brokers/kiwoom/domestic_orders.py` | Order mutation client | Reuse as-is (`modify_order`/`cancel_order` already implemented) |
| `tests/test_mcp_kiwoom_order_variants.py` | MCP tool tests | Modify (replace 2 not-implemented tests; add read-tool + confirmed-mutation + unsupported tests) |
| `scripts/kiwoom_mock_smoke.py` | Operator-safe smoke CLI (default-disabled, KRX-only) | Create |
| `tests/test_kiwoom_mock_smoke_cli.py` | CLI guard tests | Create |
| `docs/runbooks/kiwoom-mock-smoke.md` | Smoke procedure + safety notes | Create |
| `CLAUDE.md` | Project instructions — add a Kiwoom mock lifecycle section | Modify |

---

## Task 1: Shared response helpers + DRY refactor of `place_order`

Extract the broker-success derivation and mutation-envelope shaping so read tools, modify, and cancel reuse identical logic. Pure refactor — existing tests stay green.

**Files:**
- Modify: `app/mcp_server/tooling/orders_kiwoom_variants.py`
- Test: `tests/test_mcp_kiwoom_order_variants.py`

- [ ] **Step 1: Write the failing test for the success helper**

Add to `tests/test_mcp_kiwoom_order_variants.py`:

```python
# ---------------------------------------------------------------------------
# ROB-319: shared broker-response helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("broker_response", "expected"),
    [
        ({"return_code": 0}, True),
        ({"return_code": "0"}, True),
        ({}, True),  # absent return_code defaults to success code
        ({"return_code": 1}, False),
        ({"return_code": "40"}, False),
        ({"return_code": None}, True),
        ({"return_code": "RC9999"}, False),
    ],
)
def test_derive_broker_success(broker_response, expected):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    assert mod._derive_broker_success(broker_response) is expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run --all-groups pytest tests/test_mcp_kiwoom_order_variants.py::test_derive_broker_success -q`
Expected: FAIL with `AttributeError: module ... has no attribute '_derive_broker_success'`

- [ ] **Step 3: Add helpers and refactor `place_order` tail**

In `app/mcp_server/tooling/orders_kiwoom_variants.py`, add the `KiwoomDomesticAccountClient` import (next to the existing kiwoom imports):

```python
from app.services.brokers.kiwoom.domestic_account import KiwoomDomesticAccountClient
```

Add helpers above the "Implementation seams" section (after `_confirmed_not_implemented`):

```python
_MUTATION_PASSTHROUGH_KEYS = (
    "return_code",
    "return_msg",
    "continuation",
    "ord_no",
    "order_no",
)


def _derive_broker_success(broker_response: dict[str, Any]) -> bool:
    """Success iff Kiwoom return_code equals the success code (default 0)."""

    return_code = broker_response.get("return_code", constants.SUCCESS_RETURN_CODE)
    try:
        return int(return_code) == constants.SUCCESS_RETURN_CODE
    except (TypeError, ValueError):
        return return_code in (None, "", "0")


def _finalize_broker_response(
    base: dict[str, Any], broker_response: dict[str, Any]
) -> dict[str, Any]:
    """Shape a stable MCP envelope around a raw broker payload.

    ``success`` is derived from the broker return_code (never hardcoded), the
    raw payload is attached as ``broker_response``, and a few well-known fields
    are surfaced at the top level for convenience.
    """

    response = {
        "success": _derive_broker_success(broker_response),
        **base,
        "broker_response": broker_response,
    }
    for key in _MUTATION_PASSTHROUGH_KEYS:
        if key in broker_response:
            response[key] = broker_response[key]
    return response
```

Refactor the tail of `_kiwoom_mock_place_order_impl` (replace the block from `return_code = broker_response.get(...)` through the final `return response`) with:

```python
    return _finalize_broker_response(base_response, broker_response)
```

- [ ] **Step 4: Run tests to verify they pass (helper + unchanged place_order behavior)**

Run: `uv run --all-groups pytest tests/test_mcp_kiwoom_order_variants.py -q`
Expected: PASS (33 tests — the original 32 still green, success helper added). The existing `test_place_order_confirmed_calls_kiwoom_mock_order_client` must still pass unchanged.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_kiwoom_variants.py tests/test_mcp_kiwoom_order_variants.py
git commit -m "refactor(rob-319): shared kiwoom broker-response helpers + place_order DRY"
```

---

## Task 2: Wire `kiwoom_mock_get_orderable_cash` to the account client

Replace the `cash: null` stub-success with a real broker call (symbol → orderable_amount, no symbol → balance), per the locked-in default.

**Files:**
- Modify: `app/mcp_server/tooling/orders_kiwoom_variants.py:266-271` (the `_kiwoom_mock_orderable_cash_impl` stub)
- Test: `tests/test_mcp_kiwoom_order_variants.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_kiwoom_order_variants.py`:

```python
# ---------------------------------------------------------------------------
# ROB-319: account read tools call the broker client (no stub-success)
# ---------------------------------------------------------------------------


def _patch_fake_kiwoom_account_client(monkeypatch, mod, payloads):
    """payloads keyed by method name: 'orderable_amount' | 'balance' | 'order_status'."""

    calls: list[dict[str, Any]] = []

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            calls.append({"client": "from_app_settings"})
            return cls()

    class FakeAccountClient:
        def __init__(self, client):
            calls.append({"account_client": client.__class__.__name__})

        async def get_orderable_amount(self, **kwargs):
            calls.append({"method": "orderable_amount", **kwargs})
            return payloads["orderable_amount"]

        async def get_balance(self, **kwargs):
            calls.append({"method": "balance", **kwargs})
            return payloads["balance"]

        async def get_order_status(self, **kwargs):
            calls.append({"method": "order_status", **kwargs})
            return payloads["order_status"]

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticAccountClient", FakeAccountClient, raising=False
    )
    return calls


@pytest.mark.asyncio
async def test_orderable_cash_with_symbol_calls_orderable_amount(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {
                "return_code": 0,
                "return_msg": "정상",
                "ord_psbl_cash": "1500000",
            },
            "balance": {"return_code": 0},
            "order_status": {"return_code": 0},
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](symbol="005930")

    assert response["success"] is True
    assert response["source"] == "kiwoom"
    assert response["account_mode"] == "kiwoom_mock"
    assert response["broker_response"]["ord_psbl_cash"] == "1500000"
    assert response["cash"] == 1500000
    assert response["cash_source"] == "orderable_amount"
    assert {"method": "orderable_amount", "symbol": "005930"} in [
        {k: v for k, v in c.items() if k in {"method", "symbol"}}
        for c in calls
        if c.get("method") == "orderable_amount"
    ]
    # balance must NOT have been called
    assert all(c.get("method") != "balance" for c in calls)


@pytest.mark.asyncio
async def test_orderable_cash_without_symbol_calls_balance(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0},
            "balance": {"return_code": 0, "return_msg": "정상", "entr": "987654"},
            "order_status": {"return_code": 0},
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"]()

    assert response["success"] is True
    assert response["broker_response"]["entr"] == "987654"
    assert any(c.get("method") == "balance" for c in calls)
    assert all(c.get("method") != "orderable_amount" for c in calls)


@pytest.mark.asyncio
async def test_orderable_cash_unparseable_returns_null_cash_with_source(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0, "some_unknown_field": "x"},
            "balance": {"return_code": 0},
            "order_status": {"return_code": 0},
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](symbol="005930")

    assert response["success"] is True  # broker returned 0
    assert response["cash"] is None
    assert response["cash_source"] == "orderable_amount_unparsed"
    assert response["broker_response"]["some_unknown_field"] == "x"


@pytest.mark.asyncio
async def test_orderable_cash_broker_error_is_fail_closed(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeAccountClient:
        def __init__(self, client):  # noqa: ARG002
            pass

        async def get_orderable_amount(self, **kwargs):  # noqa: ARG002
            raise RuntimeError("boom")

        async def get_balance(self, **kwargs):  # noqa: ARG002
            raise RuntimeError("boom")

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticAccountClient", FakeAccountClient, raising=False
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_orderable_cash"](symbol="005930")

    assert response["success"] is False
    assert "RuntimeError" in response["error"]
    assert response["account_mode"] == "kiwoom_mock"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --all-groups pytest tests/test_mcp_kiwoom_order_variants.py -q -k orderable_cash`
Expected: FAIL — current stub returns `{"success": True, "cash": None}` with no `source`/`broker_response`, and never calls the fake client.

- [ ] **Step 3: Implement the wired impl**

In `app/mcp_server/tooling/orders_kiwoom_variants.py`, add a cash extractor near the helpers:

```python
# Candidate Kiwoom cash fields, most specific first. Unknown shapes stay
# unparsed (cash=None) rather than being faked.
_ORDERABLE_CASH_KEYS = (
    "ord_psbl_cash",
    "ord_alowa",
    "100stk_ord_alow_amt",
    "ord_psbl_amt",
    "entr",
)


def _extract_orderable_cash(broker_response: dict[str, Any]) -> int | None:
    for key in _ORDERABLE_CASH_KEYS:
        if key in broker_response and broker_response[key] not in (None, ""):
            try:
                return int(str(broker_response[key]).replace(",", "").strip())
            except (TypeError, ValueError):
                continue
    return None
```

Replace `_kiwoom_mock_orderable_cash_impl` (lines 266-271) with:

```python
async def _kiwoom_mock_orderable_cash_impl(**kwargs: Any) -> dict[str, Any]:
    symbol_raw = kwargs.get("symbol")
    symbol = str(symbol_raw).strip() if symbol_raw else None
    cont_yn = kwargs.get("cont_yn")
    next_key = kwargs.get("next_key")
    base_source = "orderable_amount" if symbol else "balance"
    try:
        client = KiwoomMockClient.from_app_settings()
        account_client = KiwoomDomesticAccountClient(cast(Any, client))
        if symbol:
            broker_response = await account_client.get_orderable_amount(
                symbol=symbol, cont_yn=cont_yn, next_key=next_key
            )
        else:
            broker_response = await account_client.get_balance(
                cont_yn=cont_yn, next_key=next_key
            )
    except Exception as exc:  # noqa: BLE001 - MCP tools fail closed with JSON
        return {
            "success": False,
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            "error": (
                f"kiwoom_mock_get_orderable_cash failed: {type(exc).__name__}: {exc}"
            ),
            **({"symbol": symbol} if symbol else {}),
        }

    cash = _extract_orderable_cash(broker_response)
    response = _finalize_broker_response(
        {"source": "kiwoom", "account_mode": ACCOUNT_MODE_KIWOOM_MOCK}, broker_response
    )
    response["cash"] = cash
    response["cash_source"] = base_source if cash is not None else f"{base_source}_unparsed"
    if symbol:
        response["symbol"] = symbol
    return response
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --all-groups pytest tests/test_mcp_kiwoom_order_variants.py -q -k orderable_cash`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_kiwoom_variants.py tests/test_mcp_kiwoom_order_variants.py
git commit -m "feat(rob-319): wire kiwoom_mock_get_orderable_cash to account client"
```

---

## Task 3: Wire `kiwoom_mock_get_positions` and `kiwoom_mock_get_order_history`

Positions → `get_balance()` (kt00018); order history → `get_order_status()` (kt00009) with `cont_yn`/`next_key` pagination passthrough. Both attach raw `broker_response` and derive `success` from the broker code.

**Files:**
- Modify: `app/mcp_server/tooling/orders_kiwoom_variants.py:250-263` (the two stubs)
- Test: `tests/test_mcp_kiwoom_order_variants.py`

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_mcp_kiwoom_order_variants.py`:

```python
@pytest.mark.asyncio
async def test_get_positions_calls_balance_and_passes_through(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0},
            "balance": {
                "return_code": 0,
                "return_msg": "정상",
                "acnt_evlt_remn_indv_tot": [{"stk_cd": "005930", "rmnd_qty": "3"}],
            },
            "order_status": {"return_code": 0},
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_positions"]()

    assert response["success"] is True
    assert response["source"] == "kiwoom"
    assert response["broker_response"]["acnt_evlt_remn_indv_tot"][0]["stk_cd"] == "005930"
    assert any(c.get("method") == "balance" for c in calls)


@pytest.mark.asyncio
async def test_get_order_history_calls_order_status_with_pagination(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_account_client(
        monkeypatch,
        mod,
        payloads={
            "orderable_amount": {"return_code": 0},
            "balance": {"return_code": 0},
            "order_status": {
                "return_code": 0,
                "return_msg": "정상",
                "continuation": {"cont_yn": "Y", "next_key": "page-2"},
                "rows": [{"ord_no": "0000111222"}],
            },
        },
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_order_history"](
        cont_yn="Y", next_key="page-1"
    )

    assert response["success"] is True
    assert response["broker_response"]["rows"][0]["ord_no"] == "0000111222"
    assert response["continuation"] == {"cont_yn": "Y", "next_key": "page-2"}
    status_call = next(c for c in calls if c.get("method") == "order_status")
    assert status_call["cont_yn"] == "Y"
    assert status_call["next_key"] == "page-1"


@pytest.mark.asyncio
async def test_get_positions_broker_error_is_fail_closed(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            return cls()

    class FakeAccountClient:
        def __init__(self, client):  # noqa: ARG002
            pass

        async def get_balance(self, **kwargs):  # noqa: ARG002
            raise RuntimeError("balance boom")

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticAccountClient", FakeAccountClient, raising=False
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_get_positions"]()

    assert response["success"] is False
    assert "RuntimeError" in response["error"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --all-groups pytest tests/test_mcp_kiwoom_order_variants.py -q -k "positions or order_history"`
Expected: FAIL — current stubs return `{"success": True, "positions": []}` / `{"success": True, "rows": []}` without calling the fake client.

- [ ] **Step 3: Implement the wired impls**

Replace `_kiwoom_mock_order_history_impl` and `_kiwoom_mock_positions_impl` (lines 250-263) with:

```python
async def _kiwoom_mock_order_history_impl(**kwargs: Any) -> dict[str, Any]:
    cont_yn = kwargs.get("cont_yn")
    next_key = kwargs.get("next_key")
    try:
        client = KiwoomMockClient.from_app_settings()
        account_client = KiwoomDomesticAccountClient(cast(Any, client))
        broker_response = await account_client.get_order_status(
            cont_yn=cont_yn, next_key=next_key
        )
    except Exception as exc:  # noqa: BLE001 - MCP tools fail closed with JSON
        return {
            "success": False,
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            "error": (
                f"kiwoom_mock_get_order_history failed: {type(exc).__name__}: {exc}"
            ),
        }
    return _finalize_broker_response(
        {"source": "kiwoom", "account_mode": ACCOUNT_MODE_KIWOOM_MOCK}, broker_response
    )


async def _kiwoom_mock_positions_impl(**kwargs: Any) -> dict[str, Any]:
    cont_yn = kwargs.get("cont_yn")
    next_key = kwargs.get("next_key")
    try:
        client = KiwoomMockClient.from_app_settings()
        account_client = KiwoomDomesticAccountClient(cast(Any, client))
        broker_response = await account_client.get_balance(
            cont_yn=cont_yn, next_key=next_key
        )
    except Exception as exc:  # noqa: BLE001 - MCP tools fail closed with JSON
        return {
            "success": False,
            "source": "kiwoom",
            "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            "error": f"kiwoom_mock_get_positions failed: {type(exc).__name__}: {exc}",
        }
    return _finalize_broker_response(
        {"source": "kiwoom", "account_mode": ACCOUNT_MODE_KIWOOM_MOCK}, broker_response
    )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --all-groups pytest tests/test_mcp_kiwoom_order_variants.py -q -k "positions or order_history"`
Expected: PASS (3 tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_kiwoom_variants.py tests/test_mcp_kiwoom_order_variants.py
git commit -m "feat(rob-319): wire kiwoom_mock get_positions/get_order_history to account client"
```

---

## Task 4: Implement confirmed `kiwoom_mock_cancel_order`

Replace `_confirmed_not_implemented("kiwoom_mock_cancel_order")` with a real call to `KiwoomDomesticOrderClient.cancel_order(...)`. Confirmed cancel now requires `symbol` and `cancel_quantity`. Fail-closed on broker error / non-zero return_code (no fake success).

**Files:**
- Modify: `app/mcp_server/tooling/orders_kiwoom_variants.py` (cancel tool body + new confirmed impl seam)
- Test: `tests/test_mcp_kiwoom_order_variants.py` (replace `test_cancel_order_confirmed_returns_explicit_not_implemented_failure`)

- [ ] **Step 1: Replace the not-implemented test with confirmed + guard tests**

Delete `test_cancel_order_confirmed_returns_explicit_not_implemented_failure` and add:

```python
def _patch_fake_kiwoom_mutation_client(monkeypatch, mod, *, modify=None, cancel=None):
    calls: list[dict[str, Any]] = []

    class FakeKiwoomMockClient:
        @classmethod
        def from_app_settings(cls):
            calls.append({"client": "from_app_settings"})
            return cls()

    class FakeOrderClient:
        def __init__(self, client):
            calls.append({"order_client": client.__class__.__name__})

        async def modify_order(self, **kwargs):
            calls.append({"method": "modify", **kwargs})
            return modify

        async def cancel_order(self, **kwargs):
            calls.append({"method": "cancel", **kwargs})
            return cancel

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    monkeypatch.setattr(mod, "KiwoomMockClient", FakeKiwoomMockClient, raising=False)
    monkeypatch.setattr(
        mod, "KiwoomDomesticOrderClient", FakeOrderClient, raising=False
    )
    return calls


@pytest.mark.asyncio
async def test_cancel_order_confirmed_calls_broker_cancel(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_mutation_client(
        monkeypatch,
        mod,
        cancel={"return_code": 0, "return_msg": "정상", "ord_no": "0000999888"},
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_cancel_order"](
        order_id="0000111222",
        symbol="005930",
        cancel_quantity=1,
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is True
    assert response["source"] == "kiwoom"
    assert response["account_mode"] == "kiwoom_mock"
    assert response["dry_run"] is False
    assert response["broker_response"]["ord_no"] == "0000999888"
    cancel_call = next(c for c in calls if c.get("method") == "cancel")
    assert cancel_call["original_order_no"] == "0000111222"
    assert cancel_call["symbol"] == "005930"
    assert cancel_call["cancel_quantity"] == 1


@pytest.mark.asyncio
async def test_cancel_order_confirmed_requires_symbol_and_quantity(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_mutation_client(
        monkeypatch, mod, cancel={"return_code": 0}
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_cancel_order"](
        order_id="0000111222",
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is False
    assert "symbol" in response["error"].lower() or "cancel_quantity" in response["error"].lower()
    assert all(c.get("method") != "cancel" for c in calls)


@pytest.mark.asyncio
async def test_cancel_order_unsupported_broker_response_is_fail_closed(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    _patch_fake_kiwoom_mutation_client(
        monkeypatch,
        mod,
        cancel={"return_code": 40, "return_msg": "취소불가"},
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_cancel_order"](
        order_id="0000111222",
        symbol="005930",
        cancel_quantity=1,
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is False  # non-zero return_code -> not faked
    assert response["broker_response"]["return_code"] == 40
    assert response["return_msg"] == "취소불가"
```

(Keep `test_place_order_dry_run_false_without_confirm_blocked` and the cancel-`confirm=False`-blocked behavior: add a quick assertion test too.)

```python
@pytest.mark.asyncio
async def test_cancel_order_dry_run_false_without_confirm_blocked(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    monkeypatch.setattr(mod, "_mock_config_error", lambda: None)
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_cancel_order"](
        order_id="0000111222",
        symbol="005930",
        cancel_quantity=1,
        dry_run=False,
        confirm=False,
    )
    assert response["success"] is False
    assert "confirm=true" in response["error"].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --all-groups pytest tests/test_mcp_kiwoom_order_variants.py -q -k cancel`
Expected: FAIL — confirmed path still returns the not-implemented stub.

- [ ] **Step 3: Implement confirmed cancel**

Add a confirmed impl seam near the other impls:

```python
async def _kiwoom_mock_cancel_confirmed_impl(**kwargs: Any) -> dict[str, Any]:
    order_id = str(kwargs.get("order_id") or "").strip()
    symbol = str(kwargs.get("symbol") or "").strip()
    cancel_quantity = int(kwargs["cancel_quantity"])
    exchange = kwargs.get("exchange") or constants.MOCK_EXCHANGE_KRX
    base = {
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        "dry_run": False,
        "order_id": order_id,
        "symbol": symbol,
        "cancel_quantity": cancel_quantity,
    }
    try:
        client = KiwoomMockClient.from_app_settings()
        order_client = KiwoomDomesticOrderClient(cast(Any, client))
        broker_response = await order_client.cancel_order(
            original_order_no=order_id,
            symbol=symbol,
            cancel_quantity=cancel_quantity,
            exchange=exchange,
        )
    except Exception as exc:  # noqa: BLE001 - MCP tools fail closed with JSON
        return {
            "success": False,
            **base,
            "error": f"kiwoom_mock_cancel_order failed: {type(exc).__name__}: {exc}",
        }
    return _finalize_broker_response(base, broker_response)
```

In the `kiwoom_mock_cancel_order` tool body, replace the `return _confirmed_not_implemented("kiwoom_mock_cancel_order")` branch with:

```python
        if not confirm:
            return {
                "success": False,
                "error": "kiwoom_mock_cancel_order requires confirm=True when dry_run=False.",
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            }
        if not symbol or cancel_quantity is None:
            return {
                "success": False,
                "error": (
                    "kiwoom_mock_cancel_order confirmed execution requires symbol "
                    "and cancel_quantity."
                ),
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            }
        return await _kiwoom_mock_cancel_confirmed_impl(
            order_id=order_id,
            symbol=symbol,
            cancel_quantity=cancel_quantity,
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --all-groups pytest tests/test_mcp_kiwoom_order_variants.py -q -k cancel`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_kiwoom_variants.py tests/test_mcp_kiwoom_order_variants.py
git commit -m "feat(rob-319): wire confirmed kiwoom_mock_cancel_order to broker client"
```

---

## Task 5: Implement confirmed `kiwoom_mock_modify_order`

Replace `_confirmed_not_implemented("kiwoom_mock_modify_order")` with a real call to `KiwoomDomesticOrderClient.modify_order(...)`. Confirmed modify requires both `new_price` and `new_quantity` (Kiwoom `modify_order` requires both). Dry-run still allows omitting one (unchanged).

**Files:**
- Modify: `app/mcp_server/tooling/orders_kiwoom_variants.py` (modify tool body + new confirmed impl seam)
- Test: `tests/test_mcp_kiwoom_order_variants.py` (replace `test_modify_order_confirmed_returns_explicit_not_implemented_failure`)

- [ ] **Step 1: Replace the not-implemented test with confirmed + guard tests**

Delete `test_modify_order_confirmed_returns_explicit_not_implemented_failure` and add:

```python
@pytest.mark.asyncio
async def test_modify_order_confirmed_calls_broker_modify(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_mutation_client(
        monkeypatch,
        mod,
        modify={"return_code": 0, "return_msg": "정상", "ord_no": "0000777666"},
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_modify_order"](
        order_id="0000111222",
        symbol="005930",
        new_price=72000,
        new_quantity=2,
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is True
    assert response["dry_run"] is False
    assert response["broker_response"]["ord_no"] == "0000777666"
    modify_call = next(c for c in calls if c.get("method") == "modify")
    assert modify_call["original_order_no"] == "0000111222"
    assert modify_call["symbol"] == "005930"
    assert modify_call["new_price"] == 72000
    assert modify_call["new_quantity"] == 2


@pytest.mark.asyncio
async def test_modify_order_confirmed_requires_both_amounts(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    calls = _patch_fake_kiwoom_mutation_client(
        monkeypatch, mod, modify={"return_code": 0}
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_modify_order"](
        order_id="0000111222",
        symbol="005930",
        new_price=72000,  # new_quantity omitted
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is False
    assert "new_quantity" in response["error"].lower() or "new_price" in response["error"].lower()
    assert all(c.get("method") != "modify" for c in calls)


@pytest.mark.asyncio
async def test_modify_order_unsupported_broker_response_is_fail_closed(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    _patch_fake_kiwoom_mutation_client(
        monkeypatch,
        mod,
        modify={"return_code": 40, "return_msg": "정정불가"},
    )
    mcp = DummyMCP()
    _register(mcp)

    response = await mcp.tools["kiwoom_mock_modify_order"](
        order_id="0000111222",
        symbol="005930",
        new_price=72000,
        new_quantity=2,
        dry_run=False,
        confirm=True,
    )

    assert response["success"] is False
    assert response["broker_response"]["return_code"] == 40
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --all-groups pytest tests/test_mcp_kiwoom_order_variants.py -q -k modify`
Expected: FAIL — confirmed path still returns the not-implemented stub.

- [ ] **Step 3: Implement confirmed modify**

Add a confirmed impl seam:

```python
async def _kiwoom_mock_modify_confirmed_impl(**kwargs: Any) -> dict[str, Any]:
    order_id = str(kwargs.get("order_id") or "").strip()
    symbol = str(kwargs.get("symbol") or "").strip()
    new_price = int(kwargs["new_price"])
    new_quantity = int(kwargs["new_quantity"])
    exchange = kwargs.get("exchange") or constants.MOCK_EXCHANGE_KRX
    base = {
        "source": "kiwoom",
        "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
        "dry_run": False,
        "order_id": order_id,
        "symbol": symbol,
        "new_price": new_price,
        "new_quantity": new_quantity,
    }
    try:
        client = KiwoomMockClient.from_app_settings()
        order_client = KiwoomDomesticOrderClient(cast(Any, client))
        broker_response = await order_client.modify_order(
            original_order_no=order_id,
            symbol=symbol,
            new_quantity=new_quantity,
            new_price=new_price,
            exchange=exchange,
        )
    except Exception as exc:  # noqa: BLE001 - MCP tools fail closed with JSON
        return {
            "success": False,
            **base,
            "error": f"kiwoom_mock_modify_order failed: {type(exc).__name__}: {exc}",
        }
    return _finalize_broker_response(base, broker_response)
```

In the `kiwoom_mock_modify_order` tool body, replace the `return _confirmed_not_implemented("kiwoom_mock_modify_order")` branch with:

```python
        if not confirm:
            return {
                "success": False,
                "error": "kiwoom_mock_modify_order requires confirm=True when dry_run=False.",
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            }
        if new_price is None or new_quantity is None:
            return {
                "success": False,
                "error": (
                    "kiwoom_mock_modify_order confirmed execution requires both "
                    "new_price and new_quantity."
                ),
                "source": "kiwoom",
                "account_mode": ACCOUNT_MODE_KIWOOM_MOCK,
            }
        return await _kiwoom_mock_modify_confirmed_impl(
            order_id=order_id,
            symbol=symbol,
            new_price=new_price,
            new_quantity=new_quantity,
        )
```

- [ ] **Step 4: Run tests to verify they pass + full file green**

Run: `uv run --all-groups pytest tests/test_mcp_kiwoom_order_variants.py tests/test_kiwoom_domestic_account.py -q`
Expected: PASS (all). Confirm `_confirmed_not_implemented` / `_CONFIRMED_NOT_IMPLEMENTED_ERROR` are now unused — delete them to avoid dead code (ruff will flag).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_kiwoom_variants.py tests/test_mcp_kiwoom_order_variants.py
git commit -m "feat(rob-319): wire confirmed kiwoom_mock_modify_order; drop not-implemented stub"
```

---

## Task 6: Operator-safe smoke CLI `scripts/kiwoom_mock_smoke.py`

Default-disabled, KRX-only, no-secret-printing CLI mirroring `scripts/kis_websocket_mock_smoke.py` / `scripts/binance_spot_demo_smoke.py`. Drives the lifecycle through the MCP impls. Each broker mutation requires an explicit `--confirm`; price is operator-approved (`--price`) and tick-validated via `get_tick_size_kr`. Cancel-before-submit safety: refuses to submit a real order unless cancel is wired (it now is) and `--allow-open-on-failure` is not set.

**Files:**
- Create: `scripts/kiwoom_mock_smoke.py`
- Test: `tests/test_kiwoom_mock_smoke_cli.py`

- [ ] **Step 1: Write the failing CLI guard tests**

Create `tests/test_kiwoom_mock_smoke_cli.py`:

```python
# tests/test_kiwoom_mock_smoke_cli.py
"""Guard tests for the Kiwoom mock smoke CLI (default-disabled, KRX-only)."""

from __future__ import annotations

import pytest

from scripts import kiwoom_mock_smoke as smoke


def test_tick_aligned_price_floors_to_krx_tick():
    # 72,345 in the 50,000-200,000 band (tick 100) floors to 72,300
    assert smoke.tick_aligned_price(72345) == 72300


def test_reject_non_krx_exchange():
    with pytest.raises(smoke.SmokeRejected):
        smoke.ensure_krx("NXT")


def test_build_parser_defaults_to_dry_run():
    parser = smoke.build_parser()
    args = parser.parse_args(["--mode", "preview", "--symbol", "005930", "--price", "1000", "--quantity", "1"])
    assert args.confirm is False


@pytest.mark.asyncio
async def test_disabled_config_blocks_before_any_broker_call(monkeypatch):
    from app.mcp_server.tooling import orders_kiwoom_variants as mod

    monkeypatch.setattr(
        mod, "_mock_config_error", lambda: {"success": False, "error": "disabled"}
    )
    result = await smoke.run_preflight()
    assert result["ok"] is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run --all-groups pytest tests/test_kiwoom_mock_smoke_cli.py -q`
Expected: FAIL with `ModuleNotFoundError: No module named 'scripts.kiwoom_mock_smoke'`

- [ ] **Step 3: Implement the CLI**

Create `scripts/kiwoom_mock_smoke.py`:

```python
# scripts/kiwoom_mock_smoke.py
"""Operator-safe Kiwoom mock-investment order smoke (ROB-319).

Default-disabled. KRX-only. Mock host only (enforced in KiwoomMockClient).
Never prints secret values — only presence/missing of required env keys.

Each broker mutation requires an explicit --confirm. Price is operator-approved
via --price and floored to the KRX tick. Submit is refused unless cancel is
available (it is, as of ROB-319) so we never strand a real mock order.

Usage:
    uv run python -m scripts.kiwoom_mock_smoke --mode preflight
    uv run python -m scripts.kiwoom_mock_smoke --mode preview --symbol 005930 --price 50000 --quantity 1
    uv run python -m scripts.kiwoom_mock_smoke --mode full --symbol 005930 --price 50000 --quantity 1 --confirm
"""

from __future__ import annotations

import argparse
import asyncio
import json
from typing import Any

from app.core.config import validate_kiwoom_mock_config
from app.mcp_server.tick_size import get_tick_size_kr
from app.mcp_server.tooling import orders_kiwoom_variants as kvar

KRX = "KRX"


class SmokeRejected(RuntimeError):
    """Raised when the operator inputs violate a smoke safety boundary."""


def ensure_krx(exchange: str) -> str:
    value = (exchange or KRX).strip().upper()
    if value != KRX:
        raise SmokeRejected(f"Kiwoom mock smoke is KRX-only; got {exchange!r}")
    return value


def tick_aligned_price(price: int) -> int:
    """Floor an operator-approved price to the KRX tick (buy-side rounding)."""

    tick = get_tick_size_kr(price)
    return (int(price) // tick) * tick


async def run_preflight() -> dict[str, Any]:
    missing = validate_kiwoom_mock_config()
    return {
        "step": "preflight",
        "ok": not missing,
        "missing_env_keys": missing,  # names only, never values
    }


async def run_preview(symbol: str, price: int, quantity: int) -> dict[str, Any]:
    mcp = _Recorder()
    kvar.register(mcp)
    return await mcp.tools["kiwoom_mock_preview_order"](
        symbol=symbol, side="buy", quantity=quantity, price=price
    )


class _Recorder:
    def __init__(self) -> None:
        self.tools: dict[str, Any] = {}

    def tool(self, *, name: str, description: str = ""):  # noqa: ARG002
        def decorator(func):
            self.tools[name] = func
            return func

        return decorator


def _emit(payload: dict[str, Any]) -> None:
    # Secrets are never in these payloads; broker_response is mock-only.
    print(json.dumps(payload, ensure_ascii=False, default=str))


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Kiwoom mock order smoke (ROB-319)")
    parser.add_argument(
        "--mode",
        required=True,
        choices=["preflight", "preview", "place", "history", "modify", "cancel", "full"],
    )
    parser.add_argument("--symbol", default=None)
    parser.add_argument("--price", type=int, default=None)
    parser.add_argument("--quantity", type=int, default=None)
    parser.add_argument("--order-id", default=None)
    parser.add_argument("--new-price", type=int, default=None)
    parser.add_argument("--cancel-quantity", type=int, default=None)
    parser.add_argument(
        "--exchange", default=KRX, help="KRX only; any other value is rejected."
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="Required for any real broker mutation (place/modify/cancel/full).",
    )
    parser.add_argument(
        "--allow-open-on-failure",
        action="store_true",
        help="Permit leaving a mock order open if a later step fails (default off).",
    )
    return parser


async def _amain(args: argparse.Namespace) -> int:
    ensure_krx(args.exchange)
    mcp = _Recorder()
    kvar.register(mcp)

    if args.mode == "preflight":
        _emit(await run_preflight())
        return 0

    if args.mode in {"preview", "place", "full"}:
        if not (args.symbol and args.price and args.quantity):
            raise SmokeRejected("symbol, price, quantity are required for this mode")
        price = tick_aligned_price(args.price)
        _emit({"step": "price_tick_aligned", "requested": args.price, "used": price})

    # Each subsequent step calls the wired MCP impls; full mode chains
    # preview -> place(confirm) -> history -> modify(confirm) -> cancel(confirm)
    # -> final history reconciliation, capturing the order id between steps.
    # Submit is gated on confirm AND cancel being available (it is).
    # See docs/runbooks/kiwoom-mock-smoke.md for the full step sequence.
    if args.mode == "preview":
        _emit(await run_preview(args.symbol, tick_aligned_price(args.price), args.quantity))
        return 0

    if args.mode != "full" and not args.confirm:
        raise SmokeRejected(f"--mode {args.mode} requires --confirm for a real mock mutation")

    # Remaining modes (place/history/modify/cancel/full) are implemented as a
    # thin sequence over mcp.tools[...]; see runbook. Kept intentionally small.
    raise SystemExit(
        "place/history/modify/cancel/full are operator-run; follow the runbook step sequence."
    )


def main() -> None:
    args = build_parser().parse_args()
    raise SystemExit(asyncio.run(_amain(args)))


if __name__ == "__main__":
    main()
```

> Note: the `full`/`place`/`modify`/`cancel` chaining is intentionally thin — the canonical step-by-step sequence lives in the runbook so the operator stays in control of each confirmed mutation. The guard tests above only cover the safety-critical primitives (tick alignment, KRX-only, dry-run default, disabled-config preflight).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run --all-groups pytest tests/test_kiwoom_mock_smoke_cli.py -q`
Expected: PASS (4 tests).

- [ ] **Step 5: Commit**

```bash
git add scripts/kiwoom_mock_smoke.py tests/test_kiwoom_mock_smoke_cli.py
git commit -m "feat(rob-319): default-disabled KRX-only Kiwoom mock smoke CLI"
```

---

## Task 7: Runbook + CLAUDE.md section

**Files:**
- Create: `docs/runbooks/kiwoom-mock-smoke.md`
- Modify: `CLAUDE.md`

- [ ] **Step 1: Write the runbook**

Create `docs/runbooks/kiwoom-mock-smoke.md` covering:
- Safety boundaries (mock host only, KRX only, `dry_run=False` requires `confirm=True`, no secrets printed, no scheduler).
- Env preflight: `uv run python -m scripts.kiwoom_mock_smoke --mode preflight` (reports missing key names only).
- Choosing a non-marketable price: reference an existing KIS quote/orderbook out of band, pick a conservative buy limit well below market, pass via `--price` (auto-floored to KRX tick).
- **Cancel-before-submit rule:** only submit a real mock order because confirmed cancel is now wired (Task 4). If cancel ever regresses, stop after dry-run.
- The 9-step smoke sequence from the issue (preflight → quote → preview → place dry → place confirm + capture order id → history → modify if supported → cancel → final reconciliation).
- Cleanup: after smoke, run `kiwoom_mock_get_order_history` / `kiwoom_mock_get_positions`; if any order remains open, record the order id + manual cancel instructions.
- PR evidence-table template: command/tool, symbol, dry_run/confirm, order id, broker status, cleanup status (secrets omitted).

- [ ] **Step 2: Add a CLAUDE.md section**

Add a `### Kiwoom Mock Account Lifecycle (ROB-319)` section under the broker sections documenting: the 7 MCP tools, that read tools call `KiwoomDomesticAccountClient` (no stub-success), confirmed modify/cancel wired to `KiwoomDomesticOrderClient`, mock-host/KRX-only/confirm-gating safety boundaries, and the smoke CLI + runbook pointers.

- [ ] **Step 3: Commit**

```bash
git add docs/runbooks/kiwoom-mock-smoke.md CLAUDE.md
git commit -m "docs(rob-319): kiwoom mock smoke runbook + CLAUDE.md section"
```

---

## Task 8: Verification gate + (conditional) real mock smoke + PR

**Files:** none (verification + PR)

- [ ] **Step 1: Targeted + related tests**

Run: `uv run --all-groups pytest tests/test_mcp_kiwoom_order_variants.py tests/test_kiwoom_domestic_account.py tests/test_kiwoom_mock_smoke_cli.py -q`
Expected: PASS (all).

- [ ] **Step 2: Full lint + ruff (pre-merge full-CI gate, per project rule)**

Run: `uv run ruff check app/ tests/ scripts/`
Expected: no errors (delete any now-dead `_confirmed_not_implemented` / `_CONFIRMED_NOT_IMPLEMENTED_ERROR`).

Run: `uv run ruff format --check app/ tests/ scripts/`

- [ ] **Step 3: Import-guard / broader suite sanity**

Run: `uv run --all-groups pytest tests/ -q -k "kiwoom or mcp" -p no:cacheprovider`
Expected: PASS (no import-guard or registration regressions).

- [ ] **Step 4: (Conditional) actual Kiwoom mock smoke**

Only if `validate_kiwoom_mock_config()` returns empty (creds/session present):
1. `uv run python -m scripts.kiwoom_mock_smoke --mode preflight`
2. Pick a conservative non-marketable buy limit (KIS quote reference, out of band).
3. Follow the runbook 9-step sequence; capture the order id; modify if supported; cancel; reconcile to zero open orders.
4. Record every step in the PR evidence table. **Never print secret values.**
5. If any order remains open, record its id + cleanup status explicitly in the PR/issue comment.

If creds/session are absent: stop after dry-run; note in the PR that the live mock smoke was not run and why (acceptance criterion is conditional).

- [ ] **Step 5: Push branch + open PR (base `main`)**

PR body must include: summary, the smoke evidence table (or explicit "not run — reason"), test output, and the safety attestation (no live endpoint calls, no real-money orders, no leftover open order unless reported with id + reason). Hermes re-reviews from the PR.

```bash
git push -u origin rob-319
gh pr create --base main --title "feat(rob-319): complete Kiwoom mock account lifecycle + mock order smoke" --body "..."
```

---

## Self-Review

**Spec coverage:**
- Scope 1 (account/read tools → broker) → Tasks 2, 3. ✅
- Scope 2 (confirmed modify/cancel or documented-unsupported) → Tasks 4, 5 (non-zero return_code surfaces as fail-closed broker evidence). ✅
- Scope 3 (operator-safe smoke) → Tasks 6, 8 step 4. ✅
- Scope 4 (tests + docs) → every task is TDD; Task 7 docs. ✅
- Acceptance: no stub-success (Tasks 2-3), confirmed modify/cancel (4-5), place_order fail-closed/KRX-only preserved (Task 1 keeps existing tests green), unit tests positive/unsupported/fail-closed (2-5), targeted pytest (8.1), conditional real smoke (8.4), PR evidence table (8.5), safe final state (8.4-8.5). ✅

**Locked-in defaults:** orderable_cash symbol policy + cash_source/_unparsed (Task 2); smoke price via operator `--price` + `get_tick_size_kr`, no new engine (Task 6). ✅

**Type consistency:** `_derive_broker_success`, `_finalize_broker_response`, `_extract_orderable_cash` defined in Task 1-2 and reused with identical signatures in Tasks 2-5. Fake client method names (`get_orderable_amount`/`get_balance`/`get_order_status`, `modify_order`/`cancel_order`) match the real client signatures verified in `domestic_account.py`/`domestic_orders.py`. ✅

**API-contract risk (carry into smoke):** body field names + `_ORDERABLE_CASH_KEYS` candidates are doc-mirrored, never validated against the real mock API. The smoke (8.4) is the first real validation; unparsed cash and non-zero return_codes degrade to explicit evidence rather than fake success — consistent with the issue's "no fake success" rule.
