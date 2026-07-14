# ROB-877 Toss Absence-Proof Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let explicit operator voids prove legacy Toss orders absent using an accepted-only ledger zero-row check plus a complete, time-bounded symbol/side/quantity/price scan.

**Architecture:** Keep `OrderProposalsService.void_proposal` unchanged and strengthen only the Toss branch of `fetch_operator_void_evidence`. The MCP adapter passes the group's `valid_until`; the gateway scans the union KST date range once, evaluates each rung's inclusive instant window with normalized `Decimal` tuple matching, and reports all ambiguity as `unknown`.

**Tech Stack:** Python 3.13, async SQLAlchemy service boundary, `Decimal`, pytest/pytest-asyncio, Ruff, ty.

## Global Constraints

- `service.py` must remain unchanged.
- Production broker access is prohibited; all Toss responses are mocked.
- A rung window is `created_at - 24h` through `max(valid_until, updated_at) + 24h`, inclusive.
- Toss `from`/`to` dates are derived in KST after timezone conversion.
- Quantity and price comparisons use finite `Decimal` values, never string equality.
- Request failure, timeout, malformed potential match, repeated/missing cursor, or CLOSED page-cap exhaustion returns `unknown`.
- KIS, Upbit, and Toss 4xx-to-rejected behavior must not change.

---

### Task 1: Lock the composite Toss evidence contract with failing tests

**Files:**
- Modify: `tests/services/order_proposals/test_broker_gateway.py:332`

**Interfaces:**
- Consumes: current `fetch_operator_void_evidence(...)` Toss branch.
- Produces: regression tests for the new optional `valid_until: datetime | None` input and exact `OperatorVoidEvidence` outcomes.

- [ ] **Step 1: Add realistic Toss order and rung builders**

```python
def _toss_order(**overrides):
    values = {
        "order_id": "broker-1",
        "client_order_id": None,
        "status": "FILLED",
        "symbol": "005930",
        "side": "BUY",
        "quantity": Decimal("1.00000000"),
        "price": Decimal("100.00"),
        "ordered_at": NOW.isoformat(),
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _toss_rung(**overrides):
    values = {
        "rung_index": 0,
        "idempotency_key": "tosprop-legacy-1",
        "broker_order_id": None,
        "side": "buy",
        "quantity": Decimal("1"),
        "limit_price": Decimal("100.0000"),
        "created_at": NOW,
        "updated_at": NOW + timedelta(hours=2),
    }
    values.update(overrides)
    return SimpleNamespace(**values)
```

- [ ] **Step 2: Replace the client-ID-required expectation with composite absence**

Use an unrelated order with `client_order_id=None` and a complete OPEN+CLOSED scan. Assert:

```python
assert evidence[0].outcome == "absent"
assert "combination_matches=0" in evidence[0].lookup_scope
```

- [ ] **Step 3: Add Decimal-normalized tuple match and state exposure tests**

Return `_toss_order(quantity=Decimal("1.00000000"), price=Decimal("100.00"))` for a rung holding `Decimal("1")` and `Decimal("100.0000")`. Assert `found`, `broker_order_id == "broker-1"`, and the returned broker state.

- [ ] **Step 4: Add inclusive-window boundary tests**

Parameterize orders at exactly `created_at - timedelta(hours=24)` and exactly `max(valid_until, updated_at) + timedelta(hours=24)` and assert `found`. Parameterize one microsecond outside either edge and assert `absent`.

- [ ] **Step 5: Add KST request-date and attempt-anchor test**

Use UTC timestamps whose KST dates differ, pass a `valid_until` later than `updated_at`, capture both list calls, and assert OPEN and CLOSED both receive:

```python
{
    "from_date": expected_start.astimezone(KST).date().isoformat(),
    "to_date": expected_end.astimezone(KST).date().isoformat(),
}
```

- [ ] **Step 6: Add CLOSED page-cap exhaustion test**

Mock CLOSED pages with unique cursors and `has_next=True` through `_TOSS_CLOSED_PAGE_CAP`. Assert `unknown`, reason `CLOSED order scan page cap reached`, and no absence result.

- [ ] **Step 7: Run the focused tests and verify RED**

Run:

```bash
uv sync --group dev
uv run pytest tests/services/order_proposals/test_broker_gateway.py -q
```

Expected: the new composite-absence, tuple-match, date-window, or page-cap assertions fail against the current client-ID guard/window behavior. Existing import errors are setup failures and must be resolved before accepting RED.

---

### Task 2: Implement the time-bounded Decimal composite matcher

**Files:**
- Modify: `app/services/order_proposals/broker_gateway.py:1-393`
- Modify: `app/mcp_server/tooling/order_proposal_tools.py:123-130`
- Test: `tests/services/order_proposals/test_broker_gateway.py`

**Interfaces:**
- Consumes: `group.valid_until`, rung `created_at`, `updated_at`, `side`, `quantity`, and `limit_price`; Toss `TossOrder` fields.
- Produces: `fetch_operator_void_evidence(..., valid_until: datetime | None = None) -> dict[int, OperatorVoidEvidence]`.

- [ ] **Step 1: Add normalization and window helpers**

Add `timedelta` and `Decimal, InvalidOperation` imports. Implement focused private helpers with these contracts:

```python
_TOSS_VOID_WINDOW_PAD = timedelta(hours=24)


def _finite_decimal(value: Any) -> Decimal | None:
    if value is None:
        return None
    try:
        parsed = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise ValueError("invalid decimal order evidence") from None
    if not parsed.is_finite():
        raise ValueError("non-finite decimal order evidence")
    return parsed


def _toss_rung_window(
    rung: Any, *, valid_until: datetime | None
) -> tuple[datetime, datetime]:
    start = rung.created_at - _TOSS_VOID_WINDOW_PAD
    attempt_end = max(
        value for value in (valid_until, rung.updated_at) if value is not None
    )
    return start, attempt_end + _TOSS_VOID_WINDOW_PAD
```

All temporal values must be timezone-aware; invalid values become `unknown` in the Toss branch.

- [ ] **Step 2: Expand the function boundary without changing other brokers**

Add the optional keyword argument:

```python
valid_until: datetime | None = None,
```

Do not use it in KIS or Upbit branches.

- [ ] **Step 3: Build the shared KST scan envelope**

Compute every rung window, take the minimum start and maximum end, convert each to KST, and pass the resulting inclusive dates as `from_date` and `to_date` to both OPEN and CLOSED `list_orders` calls.

- [ ] **Step 4: Preserve complete pagination semantics**

Keep missing/repeated cursor rejection and the 20-page cap. If a page at the cap still says `has_next`, set `incomplete_reason = "CLOSED order scan page cap reached"`; never emit `absent` afterward.

- [ ] **Step 5: Evaluate strong identifiers, then the composite tuple**

For each rung:

1. Return `found` for an exact broker order ID or non-empty client order ID.
2. Otherwise compare normalized symbol, side, finite Decimal quantity, and finite Decimal/`None` price.
3. Parse an otherwise matching order's `ordered_at` as an aware ISO-8601 datetime.
4. Count it only when `window_start <= ordered_at <= window_end`.
5. Return `found` with state/ID for any in-window match.
6. Return `unknown` for malformed data on a potential match.
7. Return `absent` only after a complete scan with zero matches.

The per-rung scope must include the KST scan dates, rung instant window, CLOSED page count, completeness, and `combination_matches=0` or `1`.

- [ ] **Step 6: Forward group validity from the MCP adapter**

Change `_fetch_void_evidence` to call:

```python
return await fetch_operator_void_evidence(
    account_mode=group.account_mode,
    market=group.market,
    symbol=group.symbol,
    rungs=rungs,
    now=now,
    valid_until=group.valid_until,
)
```

Do not edit `app/services/order_proposals/service.py`.

- [ ] **Step 7: Run focused tests and verify GREEN**

Run:

```bash
uv run pytest tests/services/order_proposals/test_broker_gateway.py -q
```

Expected: all tests pass with zero failures.

---

### Task 3: Fix audit/documentation contracts and run regressions

**Files:**
- Modify: `tests/services/order_proposals/test_service.py:867-910` only if an assertion needs the richer injected scope; do not modify production `service.py`.
- Modify: `tests/test_mcp_order_proposal_tools.py:600-635`
- Modify: `app/mcp_server/README.md:681-696`

**Interfaces:**
- Consumes: existing service evidence-summary reuse and `_fetch_void_evidence` MCP adapter.
- Produces: persisted `void_reason` proof text and documented operator behavior.

- [ ] **Step 1: Assert successful audit evidence is persisted**

Use the existing service-level zero-ledger test with an injected absent scope containing:

```text
scan_kst=2026-07-11..2026-07-15 combination_matches=0
```

Assert the group and rung `void_reason` contain the operator reason, scan dates, `combination_matches=0`, and `toss_live_order_ledger rows=0`.

- [ ] **Step 2: Assert the MCP adapter forwards valid_until**

Patch `fetch_operator_void_evidence`, call `_fetch_void_evidence` with a group carrying `valid_until`, and assert the captured keyword equals that datetime. Keep the public tool response unchanged.

- [ ] **Step 3: Update the MCP operator documentation**

Replace the obsolete “missing clientOrderId is inconclusive” Toss paragraph with the composite rule, inclusive attempt window, normalized Decimal match, accepted-ledger requirement, and fail-closed incomplete-scan behavior. Note that KIS/Upbit behavior is unchanged.

- [ ] **Step 4: Run the required domain suite**

Run:

```bash
uv run pytest tests/services/order_proposals/ -q
```

Expected: zero failures, including KIS/Upbit operator-void and Toss 4xx-to-rejected regressions.

- [ ] **Step 5: Run MCP contract tests**

Run:

```bash
uv run pytest tests/test_mcp_order_proposal_tools.py -q
```

Expected: zero failures and unchanged public response shape.

- [ ] **Step 6: Run lint**

Run:

```bash
make lint
```

Expected: Ruff formatting/check and ty checks exit 0.

- [ ] **Step 7: Verify service.py is untouched**

Run:

```bash
git diff origin/main -- app/services/order_proposals/service.py
```

Expected: no output.

- [ ] **Step 8: Commit the implementation**

```bash
git add \
  app/services/order_proposals/broker_gateway.py \
  app/mcp_server/tooling/order_proposal_tools.py \
  app/mcp_server/README.md \
  tests/services/order_proposals/test_broker_gateway.py \
  tests/services/order_proposals/test_service.py \
  tests/test_mcp_order_proposal_tools.py \
  docs/superpowers/plans/2026-07-14-rob-877-toss-absence-proof.md
git commit -m "fix(ROB-877): recover Toss absence proof"
```

The final PR targets `main` and records the OpenAPI CLOSED-pagination contradiction plus the post-deploy 13-row operator verification requirement.
