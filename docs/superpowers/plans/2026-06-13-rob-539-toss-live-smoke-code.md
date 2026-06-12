# ROB-539 Toss Live Smoke Code Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add the repository code needed for a default-disabled Toss live smoke CLI that can preview a one-share limit order and, when explicitly confirmed, place, idempotency-retry, cancel, and reconcile the live order lifecycle.

**Architecture:** Keep `scripts/toss_live_smoke.py` as a thin operator CLI. It should call the existing Toss MCP order functions and ROB-538 reconcile kernel instead of duplicating broker rules. Add only a private client-order-id override path inside `orders_toss_variants.py` so the smoke can verify Toss idempotency with the same `clientOrderId` while preserving the public MCP tool contract.

**Tech Stack:** Python 3.13, argparse, asyncio, pytest, Ruff, ty, existing Toss MCP tooling, existing ROB-538 Toss live ledger/reconcile service.

---

## Scope

This is the **code-only** plan.

In scope:
- Fast-forward the branch to include ROB-538.
- Extend `scripts/toss_live_smoke.py` with `--order-test` and `--confirm`.
- Add tests for fail-closed behavior, explicit live-order arguments, cleanup, idempotency retry, and reconcile calls.
- Refactor Toss order tooling only enough to let the smoke reuse a generated `clientOrderId`.
- Run local unit/type/lint checks.

Out of scope for this plan:
- Running `--confirm` against a real Toss account.
- Enabling production env flags.
- Writing the full operator activation checklist.
- Clearing `hold_for_final_review`.
- Moving ROB-539 to Done.

## File Structure

- Modify: `scripts/toss_live_smoke.py`
  - Owns CLI parsing, environment gates, structured stdout events, `--order-test`, and `--confirm` orchestration.
- Modify: `app/mcp_server/tooling/orders_toss_variants.py`
  - Keeps public MCP functions unchanged.
  - Adds a private place-order implementation that accepts a generated `client_order_id_override` for the smoke idempotency retry.
- Modify: `tests/services/brokers/toss/test_smoke_script.py`
  - Tests CLI parsing, disabled gates, order-test preview, confirm lifecycle, cleanup, and no-secret output.
- Modify: `tests/test_mcp_toss_order_variants.py`
  - Tests the private clientOrderId override and verifies public behavior remains unchanged.

---

### Task 1: Bring ROB-538 Into This Branch

**Files:**
- No source files.

- [ ] **Step 1: Confirm clean worktree**

Run:

```bash
git status --short --branch
```

Expected:

```text
## rob-539
```

- [ ] **Step 2: Fast-forward to `origin/main`**

Run:

```bash
git fetch origin
git merge --ff-only origin/main
```

Expected: fast-forward includes ROB-538 files such as:

```text
app/mcp_server/tooling/toss_live_ledger.py
app/services/toss_live_order_ledger_service.py
docs/runbooks/toss-live-order-reconcile.md
```

- [ ] **Step 3: Run baseline Toss smoke tests**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_smoke_script.py tests/test_mcp_toss_order_variants.py -q
```

Expected: PASS before making ROB-539 changes.

- [ ] **Step 4: Commit only if fast-forward created local merge metadata**

Fast-forward normally creates no merge commit. If no files are modified, do not commit.

---

### Task 2: Add CLI Validation Tests for Code-Only Smoke Modes

**Files:**
- Modify: `tests/services/brokers/toss/test_smoke_script.py`

- [ ] **Step 1: Add failing tests for explicit order arguments and disabled gates**

Append these tests after the existing disabled-env test:

```python
def test_order_test_requires_explicit_order_arguments(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TOSS_API_ENABLED", "true")

    code = toss_live_smoke.main(["--order-test", "--symbol", "005930"])

    assert code == 2
    output = capsys.readouterr().out
    assert "--market is required for --order-test" in output
    assert "--quantity is required for --order-test" in output
    assert "--price is required for --order-test" in output


def test_confirm_requires_explicit_order_arguments(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TOSS_API_ENABLED", "true")
    monkeypatch.setenv("TOSS_LIVE_ORDER_MUTATIONS_ENABLED", "true")

    code = toss_live_smoke.main(["--confirm", "--symbol", "005930"])

    assert code == 2
    output = capsys.readouterr().out
    assert "--market is required for --confirm" in output
    assert "--quantity is required for --confirm" in output
    assert "--price is required for --confirm" in output


def test_order_test_disabled_when_toss_api_disabled(monkeypatch, capsys) -> None:
    monkeypatch.delenv("TOSS_API_ENABLED", raising=False)

    code = toss_live_smoke.main(
        [
            "--order-test",
            "--market",
            "kr",
            "--symbol",
            "005930",
            "--quantity",
            "1",
            "--price",
            "50000",
        ]
    )

    assert code == 0
    assert "TOSS_API_ENABLED is not truthy" in capsys.readouterr().out


def test_confirm_disabled_without_mutation_gate(monkeypatch, capsys) -> None:
    monkeypatch.setenv("TOSS_API_ENABLED", "true")
    monkeypatch.delenv("TOSS_LIVE_ORDER_MUTATIONS_ENABLED", raising=False)

    code = toss_live_smoke.main(
        [
            "--confirm",
            "--market",
            "kr",
            "--symbol",
            "005930",
            "--quantity",
            "1",
            "--price",
            "50000",
        ]
    )

    assert code == 0
    output = capsys.readouterr().out
    assert "TOSS_LIVE_ORDER_MUTATIONS_ENABLED is not truthy" in output
```

- [ ] **Step 2: Run tests to verify failure**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_smoke_script.py -q
```

Expected: FAIL because `--order-test`, `--confirm`, and order argument validation do not exist yet.

---

### Task 3: Implement CLI Modes and `--order-test`

**Files:**
- Modify: `scripts/toss_live_smoke.py`

- [ ] **Step 1: Replace parser with mutually exclusive modes**

Implement this shape in `main`:

```python
parser = argparse.ArgumentParser(description="Toss Open API live smoke")
mode = parser.add_mutually_exclusive_group()
mode.add_argument("--preflight", action="store_true")
mode.add_argument("--order-test", action="store_true")
mode.add_argument("--confirm", action="store_true")
parser.add_argument("--market", choices=["kr", "us"])
parser.add_argument("--symbol", action="append")
parser.add_argument("--quantity")
parser.add_argument("--price")
parser.add_argument("--time-in-force", default="DAY", choices=["DAY", "CLS"])
args = parser.parse_args(argv)
```

- [ ] **Step 2: Add validation helper**

Add:

```python
def _validate_order_args(args: argparse.Namespace, mode_name: str) -> list[str]:
    errors: list[str] = []
    if args.market is None:
        errors.append(f"--market is required for {mode_name}")
    if not args.symbol:
        errors.append(f"--symbol is required for {mode_name}")
    elif len(args.symbol) != 1:
        errors.append(f"exactly one --symbol is required for {mode_name}")
    if not args.quantity:
        errors.append(f"--quantity is required for {mode_name}")
    if not args.price:
        errors.append(f"--price is required for {mode_name}")
    return errors
```

- [ ] **Step 3: Add structured print helper**

Add imports:

```python
import json
from typing import Any
```

Add:

```python
def _print_event(event: str, payload: dict[str, Any]) -> None:
    safe = {"event": event, **payload}
    print(json.dumps(safe, ensure_ascii=False, sort_keys=True, default=str))
```

- [ ] **Step 4: Add `run_order_test`**

Import the public Toss place function:

```python
from app.mcp_server.tooling.orders_toss_variants import toss_place_order
```

Add:

```python
async def run_order_test(
    *,
    market: str,
    symbol: str,
    quantity: str,
    price: str,
    time_in_force: str,
) -> int:
    result = await toss_place_order(
        symbol=symbol,
        side="buy",
        order_type="limit",
        quantity=quantity,
        price=price,
        market=market,  # type: ignore[arg-type]
        time_in_force=time_in_force,  # type: ignore[arg-type]
        dry_run=True,
        confirm=False,
        reason="ROB-539 Toss live smoke order-test",
        account_mode="toss_live",
    )
    _print_event("toss_order_test_preview", result)
    return 0 if bool(result.get("success")) else 1
```

- [ ] **Step 5: Wire mode handling**

Use this control flow after parsing:

```python
if not (args.preflight or args.order_test or args.confirm):
    print(
        "Toss live smoke disabled: pass --preflight, --order-test, or --confirm"
    )
    return 0

if args.preflight:
    if not _truthy(os.environ.get("TOSS_API_ENABLED")):
        print("Toss live smoke disabled: TOSS_API_ENABLED is not truthy")
        return 0
    return asyncio.run(run_preflight(args.symbol or ["005930"]))

if args.order_test:
    if not _truthy(os.environ.get("TOSS_API_ENABLED")):
        print("Toss live smoke disabled: TOSS_API_ENABLED is not truthy")
        return 0
    errors = _validate_order_args(args, "--order-test")
    if errors:
        for error in errors:
            print(error)
        return 2
    return asyncio.run(
        run_order_test(
            market=args.market,
            symbol=args.symbol[0],
            quantity=args.quantity,
            price=args.price,
            time_in_force=args.time_in_force,
        )
    )
```

- [ ] **Step 6: Run validation tests**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_smoke_script.py -q
```

Expected: disabled/validation/order-test tests pass; confirm lifecycle tests are not written yet.

- [ ] **Step 7: Commit**

```bash
git add scripts/toss_live_smoke.py tests/services/brokers/toss/test_smoke_script.py
git commit -m "test(ROB-539): add Toss live smoke CLI gates"
```

---

### Task 4: Add Private ClientOrderId Override Tests

**Files:**
- Modify: `tests/test_mcp_toss_order_variants.py`

- [ ] **Step 1: Add failing test for private override**

Add this near existing `toss_place_order` tests:

```python
@pytest.mark.asyncio
async def test_private_place_impl_accepts_client_order_id_override(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)
    recorded: dict[str, object] = {}

    async def fake_record_toss_place_order(**kwargs):
        recorded.update(kwargs)
        return {
            "ledger_id": 777,
            "broker_status": "accepted",
            "fill_recorded": False,
            "journal_created": False,
        }

    monkeypatch.setattr(otv, "record_toss_place_order", fake_record_toss_place_order)

    result = await otv._toss_place_order_impl(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="1",
        price="50000",
        order_amount=None,
        market="kr",
        time_in_force="DAY",
        dry_run=False,
        confirm=True,
        confirm_high_value_order=False,
        reason="ROB-539 smoke",
        exit_reason=None,
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        report_item_uuid=None,
        account_mode="toss_live",
        account_type=None,
        client_order_id_override="abc123def456abc123def456abc123de",
    )

    assert result["success"] is True
    assert mock_client.placed_payloads[0]["clientOrderId"] == "abc123def456abc123def456abc123de"
    assert recorded["client_order_id"] == "abc123def456abc123def456abc123de"
```

- [ ] **Step 2: Add failing test for unsafe override**

Add:

```python
@pytest.mark.asyncio
async def test_private_place_impl_rejects_unsafe_client_order_id_override(monkeypatch):
    import app.mcp_server.tooling.orders_toss_variants as otv
    from app.core.config import settings

    monkeypatch.setattr(settings, "toss_api_enabled", True)
    monkeypatch.setattr(settings, "toss_live_order_mutations_enabled", True)
    monkeypatch.setattr(otv, "validate_toss_api_config", lambda: [])

    mock_client = MockTossClient(monkeypatch)

    result = await otv._toss_place_order_impl(
        symbol="005930",
        side="buy",
        order_type="limit",
        quantity="1",
        price="50000",
        order_amount=None,
        market="kr",
        time_in_force="DAY",
        dry_run=False,
        confirm=True,
        confirm_high_value_order=False,
        reason=None,
        exit_reason=None,
        thesis=None,
        strategy=None,
        target_price=None,
        stop_loss=None,
        min_hold_days=None,
        notes=None,
        indicators_snapshot=None,
        report_item_uuid=None,
        account_mode="toss_live",
        account_type=None,
        client_order_id_override="../bad",
    )

    assert result["success"] is False
    assert "Unsafe client order id rejected" in result["error"]
    assert not mock_client.placed_payloads
```

- [ ] **Step 3: Run tests to verify failure**

Run:

```bash
uv run pytest tests/test_mcp_toss_order_variants.py::test_private_place_impl_accepts_client_order_id_override tests/test_mcp_toss_order_variants.py::test_private_place_impl_rejects_unsafe_client_order_id_override -q
```

Expected: FAIL because `_toss_place_order_impl` does not exist yet.

---

### Task 5: Implement Private ClientOrderId Override

**Files:**
- Modify: `app/mcp_server/tooling/orders_toss_variants.py`

- [ ] **Step 1: Add client order id validator**

Near `_SAFE_ORDER_ID_RE`, add:

```python
_SAFE_CLIENT_ORDER_ID_RE = re.compile(r"^[A-Za-z0-9_\-]{8,64}$")


def _client_order_id_error(
    client_order_id: str | None, base: dict[str, Any]
) -> dict[str, Any] | None:
    if client_order_id is None:
        return None
    candidate = client_order_id.strip()
    if not candidate or not _SAFE_CLIENT_ORDER_ID_RE.fullmatch(candidate):
        return {
            "success": False,
            **base,
            "error": f"Unsafe client order id rejected: {client_order_id!r}",
        }
    return None
```

- [ ] **Step 2: Extract private place implementation**

Move the current body of `toss_place_order` into a new private function with the same parameters plus `client_order_id_override`:

```python
async def _toss_place_order_impl(
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
    reason: str | None = None,
    exit_reason: str | None = None,
    thesis: str | None = None,
    strategy: str | None = None,
    target_price: str | int | None = None,
    stop_loss: str | int | None = None,
    min_hold_days: int | None = None,
    notes: str | None = None,
    indicators_snapshot: dict[str, Any] | None = None,
    report_item_uuid: str | None = None,
    account_mode: str | None = None,
    account_type: str | None = None,
    client_order_id_override: str | None = None,
) -> dict[str, Any]:
    ...
```

Inside payload construction, replace:

```python
"clientOrderId": _new_client_order_id(),
```

with:

```python
"clientOrderId": client_order_id_override or _new_client_order_id(),
```

After `base_response` is created and before any side effect, add:

```python
if (
    id_guard := _client_order_id_error(client_order_id_override, base_response)
) is not None:
    return id_guard
```

- [ ] **Step 3: Make public function delegate to private implementation**

Replace the public `toss_place_order` body with:

```python
async def toss_place_order(...existing signature...) -> dict[str, Any]:
    return await _toss_place_order_impl(
        symbol=symbol,
        side=side,
        order_type=order_type,
        quantity=quantity,
        price=price,
        order_amount=order_amount,
        market=market,
        time_in_force=time_in_force,
        dry_run=dry_run,
        confirm=confirm,
        confirm_high_value_order=confirm_high_value_order,
        reason=reason,
        exit_reason=exit_reason,
        thesis=thesis,
        strategy=strategy,
        target_price=target_price,
        stop_loss=stop_loss,
        min_hold_days=min_hold_days,
        notes=notes,
        indicators_snapshot=indicators_snapshot,
        report_item_uuid=report_item_uuid,
        account_mode=account_mode,
        account_type=account_type,
        client_order_id_override=None,
    )
```

Keep the public function signature unchanged so FastMCP does not expose `client_order_id`.

- [ ] **Step 4: Run focused tests**

Run:

```bash
uv run pytest tests/test_mcp_toss_order_variants.py::test_private_place_impl_accepts_client_order_id_override tests/test_mcp_toss_order_variants.py::test_private_place_impl_rejects_unsafe_client_order_id_override -q
```

Expected: PASS.

- [ ] **Step 5: Run full Toss MCP order tests**

Run:

```bash
uv run pytest tests/test_mcp_toss_order_variants.py -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add app/mcp_server/tooling/orders_toss_variants.py tests/test_mcp_toss_order_variants.py
git commit -m "feat(ROB-539): support private Toss smoke idempotency key"
```

---

### Task 6: Add Confirm Lifecycle Tests

**Files:**
- Modify: `tests/services/brokers/toss/test_smoke_script.py`

- [ ] **Step 1: Add success lifecycle test**

Append:

```python
@pytest.mark.asyncio
async def test_run_confirm_places_retries_cancels_and_reconciles(monkeypatch, capsys) -> None:
    place_calls: list[dict[str, object]] = []
    cancel_calls: list[str] = []
    reconcile_calls: list[dict[str, object]] = []

    async def fake_place_for_smoke(**kwargs):
        place_calls.append(kwargs)
        return {
            "success": True,
            "order_id": "ord-1",
            "client_order_id": kwargs["client_order_id_override"],
            "ledger_id": 123,
            "mutation_sent": True,
        }

    async def fake_cancel_order(*, order_id, dry_run, confirm, account_mode):
        cancel_calls.append(order_id)
        return {
            "success": True,
            "original_order_id": order_id,
            "replacement_order_id": f"cancel-{order_id}",
            "mutation_sent": True,
        }

    async def fake_reconcile(**kwargs):
        reconcile_calls.append(kwargs)
        return {
            "success": True,
            "dry_run": kwargs["dry_run"],
            "counts": {"cancelled": 1},
            "reconciled": [{"order_id": kwargs["order_id"], "verdict": "none"}],
        }

    monkeypatch.setattr(toss_live_smoke, "_place_order_for_smoke", fake_place_for_smoke)
    monkeypatch.setattr(toss_live_smoke, "toss_cancel_order", fake_cancel_order)
    monkeypatch.setattr(toss_live_smoke, "toss_reconcile_orders_impl", fake_reconcile)

    code = await toss_live_smoke.run_confirm(
        market="kr",
        symbol="005930",
        quantity="1",
        price="50000",
        time_in_force="DAY",
    )

    assert code == 0
    assert len(place_calls) == 2
    assert place_calls[0]["client_order_id_override"] == place_calls[1]["client_order_id_override"]
    assert cancel_calls == ["ord-1"]
    assert reconcile_calls == [
        {"order_id": "ord-1", "symbol": "005930", "market": "kr", "dry_run": True, "limit": 10},
        {"order_id": "ord-1", "symbol": "005930", "market": "kr", "dry_run": False, "limit": 10},
    ]
    output = capsys.readouterr().out
    assert "client_secret" not in output.lower()
    assert "toss_confirm_place" in output
    assert "toss_confirm_cancel" in output
    assert "toss_confirm_reconcile_apply" in output
```

- [ ] **Step 2: Add duplicate-id anomaly test**

Append:

```python
@pytest.mark.asyncio
async def test_run_confirm_cancels_duplicate_order_if_idempotency_fails(monkeypatch) -> None:
    order_ids = iter(["ord-1", "ord-2"])
    cancel_calls: list[str] = []

    async def fake_place_for_smoke(**kwargs):
        return {
            "success": True,
            "order_id": next(order_ids),
            "client_order_id": kwargs["client_order_id_override"],
            "ledger_id": 123,
            "mutation_sent": True,
        }

    async def fake_cancel_order(*, order_id, dry_run, confirm, account_mode):
        cancel_calls.append(order_id)
        return {"success": True, "original_order_id": order_id}

    async def fake_reconcile(**kwargs):
        return {"success": True, "counts": {"cancelled": 1}, "reconciled": []}

    monkeypatch.setattr(toss_live_smoke, "_place_order_for_smoke", fake_place_for_smoke)
    monkeypatch.setattr(toss_live_smoke, "toss_cancel_order", fake_cancel_order)
    monkeypatch.setattr(toss_live_smoke, "toss_reconcile_orders_impl", fake_reconcile)

    code = await toss_live_smoke.run_confirm(
        market="kr",
        symbol="005930",
        quantity="1",
        price="50000",
        time_in_force="DAY",
    )

    assert code == 2
    assert cancel_calls == ["ord-1", "ord-2"]
```

- [ ] **Step 3: Add finally-cancel test**

Append:

```python
@pytest.mark.asyncio
async def test_run_confirm_cancels_original_when_idempotency_retry_raises(monkeypatch) -> None:
    calls = 0
    cancel_calls: list[str] = []

    async def fake_place_for_smoke(**kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "success": True,
                "order_id": "ord-1",
                "client_order_id": kwargs["client_order_id_override"],
                "ledger_id": 123,
                "mutation_sent": True,
            }
        raise RuntimeError("retry exploded")

    async def fake_cancel_order(*, order_id, dry_run, confirm, account_mode):
        cancel_calls.append(order_id)
        return {"success": True, "original_order_id": order_id}

    async def fake_reconcile(**kwargs):
        return {"success": True, "counts": {"cancelled": 1}, "reconciled": []}

    monkeypatch.setattr(toss_live_smoke, "_place_order_for_smoke", fake_place_for_smoke)
    monkeypatch.setattr(toss_live_smoke, "toss_cancel_order", fake_cancel_order)
    monkeypatch.setattr(toss_live_smoke, "toss_reconcile_orders_impl", fake_reconcile)

    code = await toss_live_smoke.run_confirm(
        market="kr",
        symbol="005930",
        quantity="1",
        price="50000",
        time_in_force="DAY",
    )

    assert code == 2
    assert cancel_calls == ["ord-1"]
```

- [ ] **Step 4: Run tests to verify failure**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_smoke_script.py::test_run_confirm_places_retries_cancels_and_reconciles tests/services/brokers/toss/test_smoke_script.py::test_run_confirm_cancels_duplicate_order_if_idempotency_fails tests/services/brokers/toss/test_smoke_script.py::test_run_confirm_cancels_original_when_idempotency_retry_raises -q
```

Expected: FAIL because `run_confirm`, `_place_order_for_smoke`, and `toss_reconcile_orders_impl` are not wired yet.

---

### Task 7: Implement Confirm Lifecycle

**Files:**
- Modify: `scripts/toss_live_smoke.py`

- [ ] **Step 1: Add imports**

Add:

```python
import uuid
from app.mcp_server.tooling.orders_toss_variants import (
    _toss_place_order_impl,
    toss_cancel_order,
    toss_place_order,
)
from app.mcp_server.tooling.toss_live_ledger import toss_reconcile_orders_impl
```

- [ ] **Step 2: Add private wrapper for monkeypatch-friendly place calls**

Add:

```python
async def _place_order_for_smoke(**kwargs: Any) -> dict[str, Any]:
    return await _toss_place_order_impl(**kwargs)
```

- [ ] **Step 3: Add reconcile anomaly helper**

Add:

```python
def _has_reconcile_anomaly(result: dict[str, Any]) -> bool:
    counts = result.get("counts")
    if not isinstance(counts, dict):
        return True
    return bool(counts.get("anomaly"))
```

- [ ] **Step 4: Implement `run_confirm`**

Add:

```python
async def run_confirm(
    *,
    market: str,
    symbol: str,
    quantity: str,
    price: str,
    time_in_force: str,
) -> int:
    client_order_id = uuid.uuid4().hex
    opened_order_ids: list[str] = []
    exit_code = 0

    async def place_once(step: str) -> dict[str, Any]:
        result = await _place_order_for_smoke(
            symbol=symbol,
            side="buy",
            order_type="limit",
            quantity=quantity,
            price=price,
            order_amount=None,
            market=market,
            time_in_force=time_in_force,
            dry_run=False,
            confirm=True,
            confirm_high_value_order=False,
            reason="ROB-539 Toss live smoke confirm",
            exit_reason=None,
            thesis=None,
            strategy=None,
            target_price=None,
            stop_loss=None,
            min_hold_days=None,
            notes="ROB-539 live smoke: 1-share limit buy, immediate cancel",
            indicators_snapshot=None,
            report_item_uuid=None,
            account_mode="toss_live",
            account_type=None,
            client_order_id_override=client_order_id,
        )
        _print_event(step, result)
        order_id = result.get("order_id")
        if result.get("success") and isinstance(order_id, str) and order_id not in opened_order_ids:
            opened_order_ids.append(order_id)
        return result

    try:
        first = await place_once("toss_confirm_place")
        if not bool(first.get("success")):
            return 1

        retry = await place_once("toss_confirm_idempotency_retry")
        if not bool(retry.get("success")):
            exit_code = 2
        elif retry.get("order_id") != first.get("order_id"):
            _print_event(
                "toss_confirm_idempotency_anomaly",
                {
                    "success": False,
                    "first_order_id": first.get("order_id"),
                    "retry_order_id": retry.get("order_id"),
                    "message": "Same clientOrderId returned a different order id.",
                },
            )
            exit_code = 2
    except Exception as exc:
        _print_event(
            "toss_confirm_exception",
            {"success": False, "error": f"{type(exc).__name__}: {exc}"},
        )
        exit_code = 2
    finally:
        for order_id in list(opened_order_ids):
            try:
                cancel = await toss_cancel_order(
                    order_id=order_id,
                    dry_run=False,
                    confirm=True,
                    account_mode="toss_live",
                )
                _print_event("toss_confirm_cancel", cancel)
                if not bool(cancel.get("success")):
                    exit_code = 2
            except Exception as exc:
                _print_event(
                    "toss_confirm_cancel_exception",
                    {
                        "success": False,
                        "order_id": order_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                exit_code = 2

        for order_id in list(opened_order_ids):
            try:
                preview = await toss_reconcile_orders_impl(
                    order_id=order_id,
                    symbol=symbol,
                    market=market,
                    dry_run=True,
                    limit=10,
                )
                _print_event("toss_confirm_reconcile_preview", preview)
                if _has_reconcile_anomaly(preview):
                    exit_code = 2
                    continue

                applied = await toss_reconcile_orders_impl(
                    order_id=order_id,
                    symbol=symbol,
                    market=market,
                    dry_run=False,
                    limit=10,
                )
                _print_event("toss_confirm_reconcile_apply", applied)
                if _has_reconcile_anomaly(applied):
                    exit_code = 2
            except Exception as exc:
                _print_event(
                    "toss_confirm_reconcile_exception",
                    {
                        "success": False,
                        "order_id": order_id,
                        "error": f"{type(exc).__name__}: {exc}",
                    },
                )
                exit_code = 2

    return exit_code
```

- [ ] **Step 5: Wire `--confirm` in `main`**

After `--order-test` handling, add:

```python
if args.confirm:
    if not _truthy(os.environ.get("TOSS_API_ENABLED")):
        print("Toss live smoke disabled: TOSS_API_ENABLED is not truthy")
        return 0
    if not _truthy(os.environ.get("TOSS_LIVE_ORDER_MUTATIONS_ENABLED")):
        print(
            "Toss live smoke disabled: "
            "TOSS_LIVE_ORDER_MUTATIONS_ENABLED is not truthy"
        )
        return 0
    errors = _validate_order_args(args, "--confirm")
    if errors:
        for error in errors:
            print(error)
        return 2
    return asyncio.run(
        run_confirm(
            market=args.market,
            symbol=args.symbol[0],
            quantity=args.quantity,
            price=args.price,
            time_in_force=args.time_in_force,
        )
    )
```

- [ ] **Step 6: Run focused confirm tests**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_smoke_script.py::test_run_confirm_places_retries_cancels_and_reconciles tests/services/brokers/toss/test_smoke_script.py::test_run_confirm_cancels_duplicate_order_if_idempotency_fails tests/services/brokers/toss/test_smoke_script.py::test_run_confirm_cancels_original_when_idempotency_retry_raises -q
```

Expected: PASS.

- [ ] **Step 7: Run all smoke script tests**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_smoke_script.py -q
```

Expected: PASS.

- [ ] **Step 8: Commit**

```bash
git add scripts/toss_live_smoke.py tests/services/brokers/toss/test_smoke_script.py
git commit -m "feat(ROB-539): add Toss live confirm smoke lifecycle"
```

---

### Task 8: Code-Facing Verification and Safety Review

**Files:**
- No new files unless a check requires formatting changes.

- [ ] **Step 1: Run targeted tests**

Run:

```bash
uv run pytest tests/services/brokers/toss/test_smoke_script.py tests/test_mcp_toss_order_variants.py tests/mcp_server/tooling/test_toss_live_ledger.py tests/services/test_toss_live_order_ledger_service.py -q
```

Expected: PASS.

- [ ] **Step 2: Run Ruff**

Run:

```bash
uv run ruff check scripts/toss_live_smoke.py app/mcp_server/tooling/orders_toss_variants.py tests/services/brokers/toss/test_smoke_script.py tests/test_mcp_toss_order_variants.py
```

Expected: PASS.

- [ ] **Step 3: Run format check**

Run:

```bash
uv run ruff format --check scripts/toss_live_smoke.py app/mcp_server/tooling/orders_toss_variants.py tests/services/brokers/toss/test_smoke_script.py tests/test_mcp_toss_order_variants.py
```

Expected: PASS. If it fails, run the same command without `--check`, inspect the diff, and rerun the check.

- [ ] **Step 4: Run type check**

Run:

```bash
uv run ty check scripts/toss_live_smoke.py app/mcp_server/tooling/orders_toss_variants.py
```

Expected: PASS or only pre-existing unrelated ty issues. If ty reports new issues in touched files, fix them before continuing.

- [ ] **Step 5: Secret-output audit**

Run:

```bash
rg -n "client_secret|get_secret_value\\(|TOSS_API_CLIENT_SECRET|Authorization|Bearer" scripts/toss_live_smoke.py app/mcp_server/tooling/orders_toss_variants.py tests/services/brokers/toss/test_smoke_script.py tests/test_mcp_toss_order_variants.py
```

Expected: no new secret printing. Imports or config validation references are acceptable only if they do not print values.

- [ ] **Step 6: Confirm no operator execution happened**

Run:

```bash
git diff --stat
```

Expected: only code/tests/plan changes. No `.env`, deployment, production config, database migration execution output, or live evidence files.

- [ ] **Step 7: Final code commit if needed**

If Task 8 produced formatting or small fixes:

```bash
git add scripts/toss_live_smoke.py app/mcp_server/tooling/orders_toss_variants.py tests/services/brokers/toss/test_smoke_script.py tests/test_mcp_toss_order_variants.py
git commit -m "chore(ROB-539): verify Toss live smoke code path"
```

---

## Final Code Handoff

After this plan is implemented, report:
- The smoke CLI modes added.
- The exact test/lint/type commands and results.
- That no live Toss order was submitted locally.
- That `hold_for_final_review` remains in force.
- That operator activation/runbook work is still pending as a separate step.

Do not run:

```bash
uv run python -m scripts.toss_live_smoke --confirm ...
```

unless the user explicitly approves the operator phase and the required live-account conditions are satisfied.

## Self-Review

- Spec coverage: covers branch sync, code-only CLI, dry-run preview, live confirm orchestration, idempotency retry, finally-cancel, ledger/reconcile calls, tests, and no-secret verification.
- Scope check: excludes operator activation and real live execution.
- Placeholder scan: no TBD/TODO placeholders remain.
- Type consistency: `run_confirm`, `_place_order_for_smoke`, `_toss_place_order_impl`, and `toss_reconcile_orders_impl` names are consistent across tasks.
