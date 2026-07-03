# ROB-657 — `kis_live_get_order_history` expired-status + `is_live` Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Stop `kis_live_get_order_history` from reporting a dead KR order (`filled==0 && remaining==0`) as `status="pending"`; map it to `expired` and add an explicit `is_live` boolean.

**Architecture:** The shared `_map_kis_status` helper gains an `ordered` argument and a terminal-death rule (`ordered>0 && filled==0 && remaining<=0 → "expired"`), ordered so explicit cancel evidence still wins. Both KIS normalizers pass `ordered` and emit `is_live = status in ("pending","partial")`. The order-history summary gains an `expired` count. Existing status-filtering already excludes non-`pending`/`partial` from `status="pending"` queries, so the dead order drops out of the live view for free. No DB migration (response-shape additive).

**Tech Stack:** Python 3.13, pytest (`@pytest.mark.unit`), ruff + ty, uv.

## Global Constraints

- Migration: **none** — additive response fields only.
- Do not change the tool query `Literal` `["all","pending","filled","cancelled"]`; `expired` surfaces under `status="all"`.
- Follow existing KIS field-access idiom: `_get_kis_field(order, "snake", "UPPER", ...)`.
- KR domestic is the only path that can hit the bug; overseas shares the helper and must keep passing tests (its `remaining = ordered - filled` never triggers the death rule).
- Commit message trailer: `Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>`.
- Spec: `docs/superpowers/specs/2026-07-03-rob-657-kis-order-history-expired-status-design.md`.

---

## File Structure

- `app/mcp_server/tooling/orders_modify_cancel.py` — `_map_kis_status` (signature + death rule), `_normalize_kis_domestic_order` (pass `ordered`, add `is_live`), `_normalize_kis_overseas_order` (pass `ordered`, add `is_live`).
- `app/mcp_server/tooling/orders_history.py` — `_calculate_order_summary` (add `expired` count).
- `tests/test_kis_domestic_order_normalization.py` — status/normalizer unit tests.
- `tests/test_orders_history_summary_rob657.py` (new) — summary + `get_order_history_impl` integration.
- `app/mcp_server/README.md` — order-history status doc (only if it enumerates status values).

---

### Task 1: `_map_kis_status` death rule + signature

**Files:**
- Modify: `app/mcp_server/tooling/orders_modify_cancel.py:116-134` (`_map_kis_status`)
- Test: `tests/test_kis_domestic_order_normalization.py:11-32`

**Interfaces:**
- Produces: `_map_kis_status(ordered: int, filled: int, remaining: int, status_name: str | None) -> str`. Returns one of `"pending" | "partial" | "filled" | "cancelled" | "expired"`. Death rule: `ordered > 0 and filled == 0 and remaining <= 0` → `"expired"`, evaluated after the explicit `"주문취소"` → `"cancelled"` check and before every other branch.

- [ ] **Step 1: Update the failing test**

Replace the existing parametrized test (lines 11-32) so every case passes `ordered` and the death cases expect `expired`:

```python
@pytest.mark.unit
@pytest.mark.parametrize(
    ("ordered", "filled", "remaining", "status_name", "expected"),
    [
        # Live / filled / partial — unchanged behavior.
        (10, 10, 0, "체결", "filled"),
        (10, 0, 10, "접수", "pending"),
        (10, 5, 5, "체결", "partial"),
        (10, 10, 0, None, "filled"),
        (10, 10, 0, "", "filled"),
        (10, 5, 5, None, "partial"),
        (10, 0, 10, None, "pending"),
        # Explicit cancel evidence wins even with 0/0.
        (8, 0, 0, "주문취소", "cancelled"),
        # ROB-657: dead order (nothing filled, nothing left) → expired,
        # regardless of a stale/absent status name.
        (8, 0, 0, None, "expired"),
        (8, 0, 0, "", "expired"),
        (8, 0, 0, "접수", "expired"),
        (8, 0, 0, "미체결", "expired"),
        # Degenerate empty row → no order to expire.
        (0, 0, 0, None, "pending"),
    ],
)
def test_map_kis_status_handles_named_and_unnamed_statuses(
    ordered: int,
    filled: int,
    remaining: int,
    status_name: str | None,
    expected: str,
) -> None:
    assert _map_kis_status(ordered, filled, remaining, status_name) == expected
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kis_domestic_order_normalization.py::test_map_kis_status_handles_named_and_unnamed_statuses -v`
Expected: FAIL — `_map_kis_status()` takes 3 positional args (TypeError) / wrong results.

- [ ] **Step 3: Implement the death rule + signature**

Replace `_map_kis_status` (lines 116-134) with:

```python
def _map_kis_status(
    ordered: int, filled: int, remaining: int, status_name: str | None
) -> str:
    normalized_name = str(status_name or "").strip()

    # Explicit cancel evidence is authoritative at any point.
    if normalized_name == "주문취소":
        return "cancelled"
    # ROB-657: nothing filled and nothing left to modify/cancel
    # (정정취소가능수량 0) means the order is dead (EOD expiry / reject).
    # KIS ledger truth is "alive iff rmn_qty > 0", so this wins over a
    # stale '접수' status name that TTTC8036R may still carry.
    if ordered > 0 and filled == 0 and remaining <= 0:
        return "expired"
    if normalized_name in ("접수", "주문접수"):
        return "pending"
    if normalized_name == "체결":
        if filled > 0 and remaining > 0:
            return "partial"
        return "filled"
    if normalized_name == "미체결":
        return "pending"

    if filled > 0 and remaining <= 0:
        return "filled"
    if filled > 0 and remaining > 0:
        return "partial"
    return "pending"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kis_domestic_order_normalization.py::test_map_kis_status_handles_named_and_unnamed_statuses -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_modify_cancel.py tests/test_kis_domestic_order_normalization.py
git commit -m "fix(ROB-657): _map_kis_status maps dead KR order (0 filled/0 remaining) to expired

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: KR domestic normalizer — pass `ordered`, add `is_live`, expired end-to-end

**Files:**
- Modify: `app/mcp_server/tooling/orders_modify_cancel.py:179-221` (`_normalize_kis_domestic_order`)
- Test: `tests/test_kis_domestic_order_normalization.py`

**Interfaces:**
- Consumes: `_map_kis_status(ordered, filled, remaining, status_name)` from Task 1.
- Produces: `_normalize_kis_domestic_order(order)` return dict now includes `"is_live": bool` where `is_live == (status in ("pending", "partial"))`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_kis_domestic_order_normalization.py`:

```python
@pytest.mark.unit
def test_normalize_kis_domestic_order_dead_order_is_expired_not_live() -> None:
    # ROB-657 repro: 기아 000270, 8 ordered, 0 filled, 0 remaining.
    normalized = _normalize_kis_domestic_order(
        _build_domestic_order(
            pdno="000270",
            ord_qty="8",
            tot_ccld_qty="0",
            rmn_qty="0",
        )
    )

    assert normalized["status"] == "expired"
    assert normalized["is_live"] is False
    assert normalized["ordered_qty"] == 8
    assert normalized["filled_qty"] == 0
    assert normalized["remaining_qty"] == 0


@pytest.mark.unit
def test_normalize_kis_domestic_order_live_pending_is_live() -> None:
    normalized = _normalize_kis_domestic_order(
        _build_domestic_order(
            ord_qty="10",
            tot_ccld_qty="0",
            rmn_qty="10",
        )
    )

    assert normalized["status"] == "pending"
    assert normalized["is_live"] is True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_kis_domestic_order_normalization.py -k "dead_order_is_expired or live_pending_is_live" -v`
Expected: FAIL — `KeyError: 'is_live'` and/or `status == "pending"` for the dead order.

- [ ] **Step 3: Implement**

In `_normalize_kis_domestic_order`, change the `_map_kis_status` call (around line 179) to pass `ordered`:

```python
    status = _map_kis_status(
        ordered,
        filled,
        remaining,
        _get_kis_field(order, "prcs_stat_name", "PRCS_STAT_NAME"),
    )
```

Then add `is_live` to the returned dict (after the `"status": status,` line, ~line 212):

```python
        "status": status,
        "is_live": status in ("pending", "partial"),
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_kis_domestic_order_normalization.py -v`
Expected: PASS (new tests + all pre-existing normalizer tests).

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_modify_cancel.py tests/test_kis_domestic_order_normalization.py
git commit -m "fix(ROB-657): KR normalizer surfaces expired status + is_live flag

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Overseas normalizer — pass `ordered`, add `is_live`

**Files:**
- Modify: `app/mcp_server/tooling/orders_modify_cancel.py:352-393` (`_normalize_kis_overseas_order`)
- Test: `tests/test_kis_domestic_order_normalization.py`

**Interfaces:**
- Consumes: `_map_kis_status(ordered, filled, remaining, status_name)` from Task 1.
- Produces: `_normalize_kis_overseas_order(order)` return dict includes `"is_live": bool`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_kis_domestic_order_normalization.py`:

```python
from app.mcp_server.tooling.orders_modify_cancel import (  # noqa: E402
    _normalize_kis_overseas_order,
)


@pytest.mark.unit
def test_normalize_kis_overseas_order_reports_is_live() -> None:
    live = _normalize_kis_overseas_order(
        {
            "odno": "0007654321",
            "sll_buy_dvsn_cd": "02",
            "pdno": "AAPL",
            "ft_ord_qty": "10",
            "ft_ccld_qty": "0",
            "ft_ord_unpr3": "200.5",
            "ord_dt": "20260401",
            "ord_tmd": "223000",
        }
    )
    assert live["status"] == "pending"
    assert live["is_live"] is True
    assert live["remaining_qty"] == 10

    done = _normalize_kis_overseas_order(
        {
            "odno": "0007654322",
            "sll_buy_dvsn_cd": "02",
            "pdno": "AAPL",
            "ft_ord_qty": "10",
            "ft_ccld_qty": "10",
            "ft_ccld_unpr3": "201.0",
            "ord_dt": "20260401",
            "ord_tmd": "223500",
        }
    )
    assert done["status"] == "filled"
    assert done["is_live"] is False
```

(Prefer moving the `_normalize_kis_overseas_order` import to the top import block alongside `_map_kis_status` / `_normalize_kis_domestic_order` rather than an inline import; the inline form above is a fallback if you keep imports grouped per-test.)

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_kis_domestic_order_normalization.py::test_normalize_kis_overseas_order_reports_is_live -v`
Expected: FAIL — `KeyError: 'is_live'`.

- [ ] **Step 3: Implement**

In `_normalize_kis_overseas_order`, change the `_map_kis_status` call (around line 371) to pass `ordered`:

```python
    status = _map_kis_status(
        ordered,
        filled,
        remaining,
        _get_kis_field(order, "prcs_stat_name", "PRCS_STAT_NAME"),
    )
```

Add `is_live` to the returned dict (after `"status": status,`, ~line 381):

```python
        "status": status,
        "is_live": status in ("pending", "partial"),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_kis_domestic_order_normalization.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_modify_cancel.py tests/test_kis_domestic_order_normalization.py
git commit -m "fix(ROB-657): overseas normalizer passes ordered + emits is_live

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 4: Order summary `expired` count

**Files:**
- Modify: `app/mcp_server/tooling/orders_history.py:57-70` (`_calculate_order_summary`)
- Test: `tests/test_orders_history_summary_rob657.py` (new)

**Interfaces:**
- Produces: `_calculate_order_summary(orders)` return dict now includes `"expired": int` (count of `status == "expired"`).

- [ ] **Step 1: Write the failing test**

Create `tests/test_orders_history_summary_rob657.py`:

```python
from __future__ import annotations

import pytest

from app.mcp_server.tooling.orders_history import _calculate_order_summary


@pytest.mark.unit
def test_calculate_order_summary_counts_expired() -> None:
    orders = [
        {"status": "expired"},
        {"status": "pending"},
        {"status": "filled"},
        {"status": "expired"},
    ]

    summary = _calculate_order_summary(orders)

    assert summary["expired"] == 2
    assert summary["pending"] == 1
    assert summary["filled"] == 1
    assert summary["total_orders"] == 4
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orders_history_summary_rob657.py -v`
Expected: FAIL — `KeyError: 'expired'`.

- [ ] **Step 3: Implement**

In `_calculate_order_summary` add the count and include it in the return dict:

```python
def _calculate_order_summary(orders: list[dict[str, Any]]) -> dict[str, Any]:
    total_orders = len(orders)
    filled = sum(1 for o in orders if o.get("status") == "filled")
    pending = sum(1 for o in orders if o.get("status") == "pending")
    partial = sum(1 for o in orders if o.get("status") == "partial")
    cancelled = sum(1 for o in orders if o.get("status") == "cancelled")
    expired = sum(1 for o in orders if o.get("status") == "expired")

    return {
        "total_orders": total_orders,
        "filled": filled,
        "pending": pending,
        "partial": partial,
        "cancelled": cancelled,
        "expired": expired,
    }
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_orders_history_summary_rob657.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_history.py tests/test_orders_history_summary_rob657.py
git commit -m "fix(ROB-657): order-history summary reports expired count

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 5: `get_order_history_impl` integration — dead order drops out of pending, shows as expired under all

**Files:**
- Test: `tests/test_orders_history_summary_rob657.py` (extend)

**Interfaces:**
- Consumes: `get_order_history_impl(symbol=..., status=..., is_mock=False)` from `app/mcp_server/tooling/orders_history.py`; the KR fetch path calls `KISClient.inquire_korea_orders` (patched) whose rows flow through `_normalize_kis_domestic_order`.

- [ ] **Step 1: Write the failing test**

Extend `tests/test_orders_history_summary_rob657.py`. Patch the KR pending-orders broker call so exactly the dead 기아 row is returned; assert the tool no longer reports it as live:

```python
from unittest.mock import AsyncMock, patch

import app.mcp_server.tooling.orders_history as orders_history

_KIA_DEAD_ROW = {
    "ord_dt": "20260702",
    "ord_tmd": "100800",
    "odno": "0013894000",
    "sll_buy_dvsn_cd": "02",
    "pdno": "000270",
    "prdt_name": "기아",
    "ord_qty": "8",
    "ord_unpr": "129600",
    "tot_ccld_qty": "0",
    "rmn_qty": "0",
}


def _patch_kr(rows: list[dict]):
    # inquire_daily_order_domestic is only hit for status in (filled/cancelled);
    # return [] there so the pending path is what matters.
    client = AsyncMock()
    client.inquire_korea_orders = AsyncMock(return_value=rows)
    client.inquire_daily_order_domestic = AsyncMock(return_value=[])
    return patch.object(
        orders_history, "_create_kis_client", lambda *, is_mock: client
    )


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_order_history_all_marks_dead_order_expired() -> None:
    with _patch_kr([_KIA_DEAD_ROW]):
        resp = await orders_history.get_order_history_impl(
            symbol="000270", status="all", market="kr", is_mock=False
        )

    orders = resp["orders"]
    assert len(orders) == 1
    assert orders[0]["order_id"] == "0013894000"
    assert orders[0]["status"] == "expired"
    assert orders[0]["is_live"] is False
    assert resp["summary"]["expired"] == 1
    assert resp["summary"]["pending"] == 0


@pytest.mark.unit
@pytest.mark.asyncio
async def test_get_order_history_pending_excludes_dead_order() -> None:
    with _patch_kr([_KIA_DEAD_ROW]):
        resp = await orders_history.get_order_history_impl(
            symbol="000270", status="pending", market="kr", is_mock=False
        )

    assert resp["orders"] == []
    assert resp["summary"]["pending"] == 0
    assert resp["summary"]["expired"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_orders_history_summary_rob657.py -k get_order_history -v`
Expected: BEFORE Tasks 1-4 it would fail; after them it should already pass — run it to confirm the integration wiring. If it fails on `market` resolution or client patching, adjust the patch target to the actual client factory (`_create_kis_client`) — that is the seam `_fetch_kr_orders` uses.

Note: if `_resolve_market_type("000270", "kr")` needs a DB/universe lookup, prefer passing `market="kr"` (already done) so `market_types == ["equity_kr"]` without symbol resolution. If resolution still triggers network/DB, patch `orders_history._resolve_market_type` to return `("equity_kr", "000270")`.

- [ ] **Step 3: Implement**

No product code change expected — Tasks 1-4 already deliver the behavior. If Step 2 revealed a real gap (e.g. `is_live` not propagated through `_filter_and_sort_orders`, which does `dict(o)` copy and should preserve it), fix it minimally and note it here.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_orders_history_summary_rob657.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tests/test_orders_history_summary_rob657.py
git commit -m "test(ROB-657): get_order_history keeps dead order out of pending view

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 6: Docs + full verification

**Files:**
- Modify: `app/mcp_server/README.md` (only if it enumerates order-history status values / fields)
- No test file changes.

**Interfaces:** none (documentation + gate).

- [ ] **Step 1: Check the README for order-history status docs**

Run: `grep -n "get_order_history\|is_live\|\"pending\"\|status.*filled.*cancelled" app/mcp_server/README.md`

If the order-history tool documents its returned `status` values or per-order fields, add `expired` to the status list and document `is_live` (bool; true only for `pending`/`partial`). If it does not enumerate them, skip the edit — do not invent a new doc section.

- [ ] **Step 2: Format + lint**

Run: `make format && make lint`
Expected: no errors (ruff + ty clean).

- [ ] **Step 3: Run the focused + regression test set**

Run:
```bash
uv run pytest tests/test_kis_domestic_order_normalization.py tests/test_orders_history_summary_rob657.py tests/test_orders_history_kis_mock.py tests/test_mcp_kis_order_variants.py -v
```
Expected: PASS (no regressions in mock/variant suites).

- [ ] **Step 4: Commit**

```bash
git add -A
git commit -m "docs(ROB-657): document expired status + is_live in order-history tool

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

(If Step 1 produced no README change, fold this commit into whatever docs/verification note remains, or skip if the tree is clean.)

---

## Self-Review

**Spec coverage:**
- 변경 1 (status death rule) → Task 1 (+ wired in Tasks 2/3).
- 변경 2 (`is_live`) → Task 2 (KR), Task 3 (overseas).
- 변경 3 (summary `expired`) → Task 4.
- Filtering consequence (pending excludes / all includes) → Task 5.
- Docs → Task 6.
- Out-of-scope #3 (`expected_expiry`/NXT carry) → intentionally not planned; noted in spec.

**Placeholder scan:** No TBD/TODO. Task 5 Step 3 is conditional ("no change expected") with a concrete fallback seam named — acceptable because it is a verification-of-wiring task, not a hidden implementation.

**Type consistency:** `_map_kis_status(ordered, filled, remaining, status_name)` used identically in Tasks 1, 2, 3. `is_live` defined as `status in ("pending", "partial")` in Tasks 2 and 3. `summary["expired"]` produced in Task 4 and asserted in Tasks 4/5. Query `Literal` untouched throughout.
