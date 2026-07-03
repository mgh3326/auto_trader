# ROB-661 — `trade_retrospective_pending` Noise Filter Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `trade_retrospective_pending` default to actionable retros
(`filled + rejected + anomaly`) and hide cancel-family noise (DAY expiry +
strategic cancels) behind an opt-in `include_cancelled` param, with a transparent
`excluded_by_filter` count.

**Architecture:** Split each live ledger's terminal-status frozenset into a
DEFAULT group (always scanned into `pending`) and a CANCEL-family group
(`cancelled` + Toss `cancel_rejected`/`replace_rejected`). The scan still fetches
the full terminal set (so we can count what we hide), then post-filters
cancel-family entries out of `pending` unless `include_cancelled=True`. The MCP
wrapper passes the flag through and documents the default.

**Tech Stack:** Python 3.13, SQLAlchemy async, pytest / pytest-asyncio, FastMCP.

## Global Constraints

- Migration count: **0** — read-only tool behavior change only, no schema.
- Backcompat: existing response keys (`kst_date_from`, `kst_date_to`,
  `account_mode`, `terminal_scanned`, `total_pending`, `returned`, `pending`)
  MUST remain; only additive keys are new.
- `expired` is NOT a distinct ledger status — KIS collapses `expired → cancelled`
  at booking (`app/mcp_server/tooling/kis_live_ledger.py:49`). Filtering
  `cancelled` therefore covers both DAY expiry and strategic cancels.
- Run tests with: `uv run --all-groups pytest` (bare `uv run` lacks
  pytest_asyncio). Integration tests need the persistent test DB at
  `localhost:5432`; conftest forces `DATABASE_URL`.
- Anomaly stays in the DEFAULT set (real fills leaked to `anomaly` before the
  ROB-631 fix; booking/reconcile anomalies deserve a retro).
- Scope: proposal 1 (filter) only. Proposals 2 (daily net-summary bundling) and
  3 (broker-evidence fill detection) are explicitly deferred.

---

### Task 1: Service — split terminal sets, add `include_cancelled`, excluded count

**Files:**
- Modify: `app/services/trade_journal/trade_retrospective_service.py:611-628`
  (terminal-status constants) and `:689-819` (`build_retrospective_pending`)
- Test: `tests/test_trade_retrospective_pending.py`

**Interfaces:**
- Consumes: existing `_pending_entry`, `_is_covered`, `_covered_keys`,
  `_kst_day_start`, `_kst_day_end`, ledger models `KISLiveOrderLedger`,
  `LiveOrderLedger`, `TossLiveOrderLedger`.
- Produces: `build_retrospective_pending(db, *, kst_date_from, kst_date_to,
  account_mode=None, limit=100, include_cancelled=False) -> dict`. Return dict
  gains keys `include_cancelled: bool` and `excluded_by_filter: {"cancelled":
  int}`. Cancel-family statuses = `{"cancelled", "cancel_rejected",
  "replace_rejected"}`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_trade_retrospective_pending.py` (helpers `_kis_row`,
`_generic_row`, `_toss_row` already exist in that file):

```python
@pytest.mark.asyncio
async def test_cancelled_excluded_by_default(db_session: AsyncSession):
    db_session.add_all(
        [
            _kis_row(order_no="K-FILL", status="filled"),
            _kis_row(order_no="K-CANCEL", status="cancelled"),
        ]
    )
    await db_session.commit()

    result = await svc.build_retrospective_pending(
        db_session, kst_date_from="2000-01-01", kst_date_to="2100-01-01"
    )
    refs = {p["suggested_correlation_id"] for p in result["pending"]}
    assert refs == {"kis_live:K-FILL"}
    assert result["total_pending"] == 1
    assert result["include_cancelled"] is False
    assert result["excluded_by_filter"] == {"cancelled": 1}


@pytest.mark.asyncio
async def test_include_cancelled_restores_cancel_rows(db_session: AsyncSession):
    db_session.add_all(
        [
            _kis_row(order_no="K-FILL", status="filled"),
            _kis_row(order_no="K-CANCEL", status="cancelled"),
        ]
    )
    await db_session.commit()

    result = await svc.build_retrospective_pending(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        include_cancelled=True,
    )
    refs = {p["suggested_correlation_id"] for p in result["pending"]}
    assert refs == {"kis_live:K-FILL", "kis_live:K-CANCEL"}
    assert result["total_pending"] == 2
    assert result["include_cancelled"] is True
    assert result["excluded_by_filter"] == {"cancelled": 0}


@pytest.mark.asyncio
async def test_anomaly_and_rejected_kept_by_default(db_session: AsyncSession):
    db_session.add_all(
        [
            _kis_row(order_no="K-ANOM", status="anomaly"),
            _kis_row(order_no="K-REJ", status="rejected"),
        ]
    )
    await db_session.commit()

    result = await svc.build_retrospective_pending(
        db_session, kst_date_from="2000-01-01", kst_date_to="2100-01-01"
    )
    refs = {p["suggested_correlation_id"] for p in result["pending"]}
    assert refs == {"kis_live:K-ANOM", "kis_live:K-REJ"}
    assert result["excluded_by_filter"] == {"cancelled": 0}


@pytest.mark.asyncio
async def test_toss_cancel_family_excluded_by_default(db_session: AsyncSession):
    db_session.add_all(
        [
            _toss_row(client_order_id="T-CR", status="cancel_rejected"),
            _toss_row(client_order_id="T-RR", status="replace_rejected"),
            _toss_row(client_order_id="T-FILL", broker_order_id="TB-FILL", status="filled"),
        ]
    )
    await db_session.commit()

    default = await svc.build_retrospective_pending(
        db_session, kst_date_from="2000-01-01", kst_date_to="2100-01-01"
    )
    assert {p["ledger"] for p in default["pending"]} == {"toss_live"}
    assert default["total_pending"] == 1  # only the filled row
    assert default["excluded_by_filter"] == {"cancelled": 2}

    opted_in = await svc.build_retrospective_pending(
        db_session,
        kst_date_from="2000-01-01",
        kst_date_to="2100-01-01",
        include_cancelled=True,
    )
    assert opted_in["total_pending"] == 3
    assert opted_in["excluded_by_filter"] == {"cancelled": 0}
```

- [ ] **Step 2: Run the tests to verify they fail**

Run: `uv run --all-groups pytest tests/test_trade_retrospective_pending.py -k "cancelled or anomaly_and_rejected or toss_cancel_family" -v`
Expected: FAIL — `KeyError: 'include_cancelled'` / `KeyError: 'excluded_by_filter'`
(cancel rows currently appear in `pending`), and
`build_retrospective_pending() got an unexpected keyword argument 'include_cancelled'`.

- [ ] **Step 3: Split the terminal-status constants**

Replace `app/services/trade_journal/trade_retrospective_service.py:611-626` (the
three `_*_TERMINAL` frozenset definitions and their comment) with:

```python
# ROB-647/ROB-661 — terminal (lifecycle-complete) statuses per live ledger,
# split into a DEFAULT group (always due for a retrospective: filled / rejected /
# anomaly) and a CANCEL-family group (DAY expiry collapses to `cancelled`, plus
# Toss cancel/replace rejections). Cancel-family is noise by default (grid
# re-placement churn) and only surfaces when include_cancelled=True. Non-terminal
# states (accepted / pending / partial / replaced) stay omitted — they may still
# change.
_KIS_LIVE_DEFAULT_TERMINAL = frozenset({"filled", "rejected", "anomaly"})
_KIS_LIVE_CANCEL_TERMINAL = frozenset({"cancelled"})
_GENERIC_LIVE_DEFAULT_TERMINAL = frozenset({"filled", "rejected", "anomaly"})
_GENERIC_LIVE_CANCEL_TERMINAL = frozenset({"cancelled"})
_TOSS_DEFAULT_TERMINAL = frozenset({"filled", "rejected", "anomaly"})
_TOSS_CANCEL_TERMINAL = frozenset(
    {"cancelled", "cancel_rejected", "replace_rejected"}
)

_KIS_LIVE_TERMINAL = _KIS_LIVE_DEFAULT_TERMINAL | _KIS_LIVE_CANCEL_TERMINAL
_GENERIC_LIVE_TERMINAL = _GENERIC_LIVE_DEFAULT_TERMINAL | _GENERIC_LIVE_CANCEL_TERMINAL
_TOSS_TERMINAL = _TOSS_DEFAULT_TERMINAL | _TOSS_CANCEL_TERMINAL

# Statuses hidden from `pending` unless include_cancelled=True. Disjoint from the
# DEFAULT statuses (note: `rejected` is DEFAULT; `cancel_rejected` /
# `replace_rejected` are cancel-family).
_CANCEL_FAMILY_STATUSES = (
    _KIS_LIVE_CANCEL_TERMINAL | _GENERIC_LIVE_CANCEL_TERMINAL | _TOSS_CANCEL_TERMINAL
)
```

The `_PENDING_LEDGER_FETCH_CAP = 1000` line immediately below stays unchanged.

- [ ] **Step 4: Add the `include_cancelled` param and post-filter**

In `build_retrospective_pending`, change the signature (currently at
`:689-696`) to add the new keyword-only param before `limit` is fine; place it
last to stay additive:

```python
async def build_retrospective_pending(
    db: AsyncSession,
    *,
    kst_date_from: str,
    kst_date_to: str,
    account_mode: str | None = None,
    limit: int = 100,
    include_cancelled: bool = False,
) -> dict[str, Any]:
```

The three ledger scan loops stay unchanged (they already use the union
`_KIS_LIVE_TERMINAL` / `_GENERIC_LIVE_TERMINAL` / `_TOSS_TERMINAL`). Replace the
tail of the function (currently `:808-819`, from `pending.sort(...)` to the
`return`) with:

```python
    excluded_cancelled = 0
    if not include_cancelled:
        kept: list[dict[str, Any]] = []
        for entry in pending:
            if entry["status"] in _CANCEL_FAMILY_STATUSES:
                excluded_cancelled += 1
            else:
                kept.append(entry)
        pending = kept

    pending.sort(key=lambda e: e["trade_date_kst"] or "", reverse=True)
    total_pending = len(pending)
    limited = pending[: max(0, limit)]
    return {
        "kst_date_from": kst_date_from,
        "kst_date_to": kst_date_to,
        "account_mode": account_mode,
        "include_cancelled": include_cancelled,
        "terminal_scanned": scanned,
        "total_pending": total_pending,
        "returned": len(limited),
        "excluded_by_filter": {"cancelled": excluded_cancelled},
        "pending": limited,
    }
```

Also update the function docstring's first line to note the default filter, e.g.
after the existing summary add: `Cancel-family rows (cancelled / cancel_rejected
/ replace_rejected) are hidden unless include_cancelled=True; the hidden count is
reported in excluded_by_filter.`

- [ ] **Step 5: Run the new tests to verify they pass**

Run: `uv run --all-groups pytest tests/test_trade_retrospective_pending.py -k "cancelled or anomaly_and_rejected or toss_cancel_family" -v`
Expected: PASS (4 tests).

- [ ] **Step 6: Run the full pending suite for regressions**

Run: `uv run --all-groups pytest tests/test_trade_retrospective_pending.py -v`
Expected: PASS — all pre-existing tests still green (they only assert
`filled` rows and `total_pending`, which are unaffected by the default filter).

- [ ] **Step 7: Commit**

```bash
git add app/services/trade_journal/trade_retrospective_service.py tests/test_trade_retrospective_pending.py
git commit -m "feat(ROB-661): default retrospective_pending to filled/rejected/anomaly, opt-in cancelled

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 2: MCP wrapper — pass `include_cancelled` through + document default

**Files:**
- Modify: `app/mcp_server/tooling/trade_retrospective_tools.py:197-216`
  (`trade_retrospective_pending` wrapper)
- Modify: `app/mcp_server/tooling/trade_retrospective_registration.py:60-71`
  (tool description)
- Test: `tests/test_trade_retrospective_tools.py`

**Interfaces:**
- Consumes: `build_retrospective_pending(..., include_cancelled=...)` from Task 1.
- Produces: MCP tool `trade_retrospective_pending(kst_date_from=None,
  kst_date_to=None, account_mode=None, limit=100, include_cancelled=False)`
  returning `{"success": True, ..., "include_cancelled": ..., "excluded_by_filter":
  {...}}`.

- [ ] **Step 1: Write the failing test**

Append to `tests/test_trade_retrospective_tools.py` (imports `now_kst`,
`KISLiveOrderLedger`, and `trade_retrospective_pending` already present — see the
existing `test_pending_tool_envelope`):

```python
@pytest.mark.asyncio
async def test_pending_tool_include_cancelled_passthrough(db_session: AsyncSession):
    db_session.add_all(
        [
            KISLiveOrderLedger(
                trade_date=now_kst(),
                symbol="005930",
                instrument_type="equity_kr",
                side="buy",
                order_type="limit",
                account_mode="kis_live",
                broker="kis",
                status="cancelled",
                lifecycle_state="cancelled",
                order_no="K-TOOL-CANCEL",
            )
        ]
    )
    await db_session.commit()

    default = await trade_retrospective_pending()
    assert default["success"] is True
    assert default["include_cancelled"] is False
    default_refs = {p["suggested_correlation_id"] for p in default["pending"]}
    assert "kis_live:K-TOOL-CANCEL" not in default_refs
    assert default["excluded_by_filter"]["cancelled"] >= 1

    opted = await trade_retrospective_pending(include_cancelled=True)
    assert opted["include_cancelled"] is True
    opted_refs = {p["suggested_correlation_id"] for p in opted["pending"]}
    assert "kis_live:K-TOOL-CANCEL" in opted_refs
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run --all-groups pytest tests/test_trade_retrospective_tools.py::test_pending_tool_include_cancelled_passthrough -v`
Expected: FAIL — `trade_retrospective_pending() got an unexpected keyword
argument 'include_cancelled'`.

- [ ] **Step 3: Add the param to the MCP wrapper**

In `app/mcp_server/tooling/trade_retrospective_tools.py`, change the wrapper
signature and the service call (currently `:197-215`):

```python
async def trade_retrospective_pending(
    kst_date_from: str | None = None,
    kst_date_to: str | None = None,
    account_mode: str | None = None,
    limit: int = 100,
    include_cancelled: bool = False,
) -> dict[str, Any]:
    today = now_kst().date().isoformat()
    date_to = kst_date_to or today
    # Default lookback window: 14 KST days ending today.
    date_from = kst_date_from or (now_kst().date() - timedelta(days=14)).isoformat()
    try:
        async with _session_factory()() as db:
            result = await build_retrospective_pending(
                db,
                kst_date_from=date_from,
                kst_date_to=date_to,
                account_mode=account_mode,
                limit=limit,
                include_cancelled=include_cancelled,
            )
        return {"success": True, **result}
    except Exception as exc:  # noqa: BLE001
        logger.exception("trade_retrospective_pending failed")
        return {"success": False, "error": f"trade_retrospective_pending failed: {exc}"}
```

- [ ] **Step 4: Update the tool description**

In `app/mcp_server/tooling/trade_retrospective_registration.py:62-70`, replace
the description string with:

```python
        description=(
            "List lifecycle-terminal live orders across the 3 live ledgers "
            "(kis_live KR, generic live US/crypto, toss_live) that still lack a "
            "trade retrospective, over a KST trade_date window (default: last 14 "
            "days). Defaults to actionable terminals only: filled / rejected / "
            "anomaly. Cancel-family rows (cancelled — which includes DAY expiry "
            "and strategic cancels — plus toss cancel_rejected/replace_rejected) "
            "are hidden by default and their count is reported in "
            "excluded_by_filter; pass include_cancelled=true to surface them. "
            "Each row carries a suggested_correlation_id to pass to "
            "save_trade_retrospective so it is marked covered next scan. Optional "
            "account_mode filter in {kis_live, upbit_live, toss_live}. Read-only "
            "due-list — no broker/order mutation. (ROB-647, ROB-661)"
        ),
```

- [ ] **Step 5: Run the test to verify it passes**

Run: `uv run --all-groups pytest tests/test_trade_retrospective_tools.py::test_pending_tool_include_cancelled_passthrough -v`
Expected: PASS.

- [ ] **Step 6: Run the full tools suite for regressions**

Run: `uv run --all-groups pytest tests/test_trade_retrospective_tools.py -v`
Expected: PASS — including `test_pending_tool_envelope` (unchanged filled row)
and `test_register_wires_three_tools` (description text change is not asserted).

- [ ] **Step 7: Commit**

```bash
git add app/mcp_server/tooling/trade_retrospective_tools.py app/mcp_server/tooling/trade_retrospective_registration.py tests/test_trade_retrospective_tools.py
git commit -m "feat(ROB-661): expose include_cancelled on trade_retrospective_pending MCP tool

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>"
```

---

### Task 3: Lint, typecheck, and final regression gate

**Files:** none (verification only).

- [ ] **Step 1: Format + lint**

Run: `make format && make lint`
Expected: ruff clean, ty clean. (The `make lint` includes `app/` and `tests/`.)

- [ ] **Step 2: Run both retrospective suites together**

Run: `uv run --all-groups pytest tests/test_trade_retrospective_pending.py tests/test_trade_retrospective_tools.py -v`
Expected: all PASS.

- [ ] **Step 3: Confirm single alembic head (no migration added)**

Run: `uv run alembic heads`
Expected: exactly one head, unchanged from `main` (migration count 0 for this
change).

- [ ] **Step 4: Commit any formatting-only changes**

```bash
git add -A
git commit -m "chore(ROB-661): ruff format" || echo "nothing to format"
```

---

## Self-Review Notes

- **Spec coverage:** DEFAULT/CANCEL split (Task 1 Step 3) ✓; `include_cancelled`
  param service + MCP (Task 1 Step 4, Task 2 Step 3) ✓; `excluded_by_filter`
  transparency (Task 1 Step 4) ✓; anomaly kept default (Task 1 test
  `test_anomaly_and_rejected_kept_by_default`) ✓; Toss cancel-family
  classification (Task 1 `test_toss_cancel_family_excluded_by_default`) ✓; MCP
  description (Task 2 Step 4) ✓; migration 0 (Task 3 Step 3) ✓.
- **Deferred (spec §범위 밖):** proposal 2 (daily net-summary bundling), proposal
  3 (broker-evidence fill detection) — not in any task, by design.
- **Type consistency:** `include_cancelled: bool` and `excluded_by_filter:
  {"cancelled": int}` used identically across service, wrapper, and tests.
  Cancel-family status set spelled `_CANCEL_FAMILY_STATUSES` everywhere it is
  referenced.
