# ROB-28 Harden KIS mock account_mode routing for order lifecycle — Implementation Plan

> **For agentic workers (OpenCode / Kimi K2.5):** Follow this plan task-by-task.
> Steps use checkbox (`- [ ]`) syntax. After each task, commit, run the focused
> tests for that task, then proceed. Do **not** read or write any file under
> `/Users/mgh3326/services/auto_trader/shared/` — those are production secrets.

AOE_STATUS: plan_ready
AOE_ISSUE: ROB-28
AOE_ROLE: planner-opus
AOE_NEXT: start_implementer_same_session (OpenCode, Kimi K2.5 default)

---

## 1. Goal

Harden the `account_mode="kis_mock"` runtime + order lifecycle paths after
ROB-19 so a day-trading rehearsal can safely use the official KIS mock
investment account before any live-account canary, while preserving the
ROB-19 invariant that `kis_mock` never falls back to live KIS credentials,
token cache, or live TR IDs.

This PR is **read-mostly** plus a small surface change on
`cancel_order` / `modify_order`. It does **not** introduce dry_run=False
execution, watch registration, real broker side effects, real-account canary,
or strategy automation.

## 2. Hard safety invariants (non-negotiable)

- No live orders. No `dry_run=False` execution path executed in tests, smoke,
  or review.
- No KIS secret values, tokens, account numbers, or KIS mock credential
  values may appear in source, logs, errors, commits, or test fixtures.
  Errors that report mock-config gaps may name **environment variable names
  only** (e.g. `KIS_MOCK_APP_KEY`), never values.
- `account_mode="kis_mock"` must fail closed when
  `validate_kis_mock_config()` returns missing names. It must never silently
  fall back to `kis_live` credentials, live `KIS_*` env vars, the live KIS
  base URL, or the live token namespace.
- The KIS Redis token cache namespace for mock (`RedisTokenManager("kis_mock")`)
  must remain isolated from the live namespace (`RedisTokenManager("kis")`).
  Do not introduce code paths that share a token across modes.
- AoE agents must not read `/Users/mgh3326/services/auto_trader/shared/.env.kis-mock`
  or any production env file. Documentation may *reference* the file path,
  but the implementer must not read it.
- `account_type="paper"` continues to mean DB simulation only. It is never
  a synonym for KIS mock.
- The normalized order dict shape returned by
  `app/mcp_server/tooling/orders_modify_cancel.py:_normalize_kis_domestic_order`
  and `_normalize_kis_overseas_order` must remain stable — ROB-22's
  pending reconciliation service consumes that shape.

## 3. Background reconnaissance (read once; do not re-investigate)

Source: ROB-19 implementation (commit `70d901ae`) and post-deploy mock smoke.

- `app/mcp_server/tooling/account_modes.py` already supplies
  `normalize_account_mode(...)`, `AccountRouting`, and
  `apply_account_routing_metadata(...)`. **Do not redesign it.** Reuse.
- `app/core/config.py:467` already exports
  `validate_kis_mock_config(settings_obj=settings) -> list[str]` returning
  variable names of missing fields. Reuse it everywhere we gate `kis_mock`.
- `app/services/brokers/kis/client.py` already routes `is_mock=True` to
  `_KISSettingsView` (mock-only credentials, mock base URL, mock token
  namespace via `RedisTokenManager("kis_mock")`). The fail-closed
  ROB-19 safeguard is preserved by **not** instantiating
  `KISClient()` (live default) on a mock branch.

Confirmed mock-routing gaps that this plan must close:

1. `app/mcp_server/tooling/orders_modify_cancel.py`
   - `cancel_order_impl(...)` and `modify_order_impl(...)` do **not** accept
     `account_mode` and unconditionally instantiate `KISClient()` (live).
     Lines: `_cancel_kis_domestic` `KISClient()` at ~447, ~471;
     `_cancel_kis_overseas` at ~535; `_modify_kis_domestic` at ~833;
     `_modify_kis_overseas` at ~945.
   - These are routed from `app/mcp_server/tooling/orders_registration.py`
     `cancel_order` / `modify_order`, which also do not accept `account_mode`.

2. `app/services/brokers/kis/overseas_orders.py`
   - `inquire_overseas_orders(...)` (~line 256) hard-codes
     `tr_id = constants.OVERSEAS_ORDER_INQUIRY_TR` (`TTTS3018R`, live).
     KIS does **not** publish a mock equivalent for this endpoint, so
     calling it under mock returns `EGW02006 모의투자 TR 이 아닙니다`.
   - This is the mock pending-history smoke EGW02006 source.

3. `app/services/brokers/kis/domestic_orders.py`
   - `inquire_korea_orders(...)` (~line 87) hard-codes
     `tr_id = constants.DOMESTIC_ORDER_INQUIRY_TR` (`TTTC8036R`).
     The constant comment says "실전/모의 공통", but mock smoke logs show
     EGW02006 from this path on at least some mock accounts. Treat KR
     pending under mock as best-effort: if KIS returns EGW02006, the
     surface must turn that into an explicit, mock-aware error in
     `errors[]` rather than appearing as "empty pending".
   - `cancel_korea_order` (~line 371) and `modify_korea_order` (~line 679)
     already accept `is_mock` and have mock TR IDs (`VTTC0013U`).

4. `app/mcp_server/tooling/portfolio_cash.py`
   - `get_cash_balance_impl(...)` calls `kis.inquire_integrated_margin(...)`
     for KIS domestic cash. The mock TR `VTTC0869R` does not exist in KIS
     mock; the endpoint returns `OPSQ0002 없는 서비스 코드 입니다`.
   - For mock mode we must route domestic cash through
     `inquire_domestic_cash_balance(is_mock=True)` (real mock TR
     `VTTC8434R`, already implemented) instead of integrated margin.
   - For mock mode US orderable, `inquire_overseas_margin(is_mock=True)`
     (`VTTS2101R`) may also be unsupported on the mock account. Treat
     mock USD orderable as **explicit unsupported**: surface a clear
     `mock_unsupported` error in the per-account errors list and skip
     the USD row, rather than reporting `0.0` USD as success.

5. `app/mcp_server/tooling/orders_history.py`
   - `_fetch_us_orders(...)` already passes `is_mock` into
     `kis.inquire_overseas_orders(...)` (which hard-codes the live TR ID
     above). The fix is in the broker layer, not here, but the orders
     history surface must classify the resulting RuntimeError as a
     mock-unsupported error (not "empty pending") in `errors[]`.

6. `app/services/brokers/kis/account.py`
   - `inquire_integrated_margin` (~line 590) is **live-only on the KIS
     mock account in practice**. Add a fail-closed branch: when
     `is_mock=True`, raise an explicit
     `RuntimeError("KIS integrated margin is not supported in mock mode; "
     "use inquire_domestic_cash_balance(is_mock=True)")` rather than
     calling KIS with a non-existent TR.

7. Production runtime / launchd
   - `scripts/deploy-native.sh:39` loads only
     `$AUTO_TRADER_BASE/shared/.env.prod.native`. Optional
     `KIS_MOCK_*` env loading is **not** wired and may not be safe to
     auto-load. We will *document* the recommended operator pattern
     (sourcing `shared/.env.kis-mock` in the launchd plist `EnvFile` or
     prefixing `set -a; source ...; set +a` in the wrapper) and ensure
     the **runtime fail-closes** if the file is absent. This PR will not
     read or commit secrets, and will not auto-load `.env.kis-mock` from
     code.

## 4. File map

| Path | Status | Responsibility |
|------|--------|----------------|
| `app/mcp_server/tooling/orders_modify_cancel.py` | modify | Add `is_mock` param; route `_cancel_kis_*` / `_modify_kis_*` through `KISClient(is_mock=...)` and pass `is_mock=` to KIS broker calls |
| `app/mcp_server/tooling/orders_registration.py` | modify | `cancel_order` / `modify_order` accept `account_mode` (+ deprecated `account_type` alias); fail-closed when `kis_mock` config missing; pass `is_mock` through |
| `app/services/brokers/kis/overseas_orders.py` | modify | `inquire_overseas_orders(is_mock)` raises an explicit "mock unsupported (TTTS3018R is live-only)" `RuntimeError` when `is_mock=True`; live path unchanged |
| `app/services/brokers/kis/account.py` | modify | `inquire_integrated_margin(is_mock=True)` raises explicit "mock unsupported" `RuntimeError`; live path unchanged |
| `app/mcp_server/tooling/portfolio_cash.py` | modify | When `is_mock=True`: domestic cash via `inquire_domestic_cash_balance(is_mock=True)` instead of `inquire_integrated_margin`; overseas cash treated as `mock_unsupported` and surfaced in per-account errors; pending-buy deductions tolerate KR `EGW02006` and US `mock_unsupported` |
| `app/mcp_server/tooling/orders_history.py` | modify | Tolerate KR `EGW02006` and US `mock_unsupported` from broker calls under mock — surface in `errors[]` with `account_mode="kis_mock"` and `mock_unsupported=true`, not empty success |
| `tests/test_kis_mock_routing.py` | extend | Cancel/modify factory mock-only safety; add `account_mode="kis_mock"` end-to-end mocked tests for cancel/modify; mock pending US explicit unsupported error; mock cash uses cash-balance TR not integrated-margin TR |
| `tests/test_mcp_account_modes.py` | extend | `cancel_order`/`modify_order` `account_mode` aliasing & fail-closed cases |
| `tests/test_kis_constants.py` | extend | Assert `OVERSEAS_ORDER_INQUIRY_TR` and `INTEGRATED_MARGIN_TR_MOCK` mock unsupported invariants are documented in code (string-presence test + comment) |
| `app/mcp_server/README.md` | modify | Update `cancel_order`/`modify_order` signatures to show `account_mode`; add a "Mock unsupported endpoints" subsection naming integrated-margin, US pending-orders inquiry, and KR pending-orders inquiry behavior |
| `docs/plans/ROB-28-kis-mock-routing-plan.md` | create (this file) | Implementation plan |

No changes to:
- `app/services/brokers/kis/client.py` (already mock-isolated)
- `app/services/redis_token_manager.py` (already namespaced)
- `app/core/config.py` (already has `KIS_MOCK_*` settings + validator)
- `app/services/pending_reconciliation_service.py` (ROB-22; consumes
  normalized order dicts — shape preserved)
- `scripts/deploy-native.sh` (production env loading is operator-managed
  outside the repo; we only document the recommended pattern)
- Any file under `/Users/mgh3326/services/auto_trader/shared/`

## 5. Overlap / conflict check vs active ROB work

| Ticket | Branch / commit | Overlap? | Mitigation |
|---|---|---|---|
| ROB-22 — pending reconciliation service | `feature/ROB-22-pending-reconciliation-service` (local), commit `23ac923c feat(research): add pending reconciliation service` | **Low overlap.** Pure read-only consumer of `_normalize_kis_domestic_order` shape and `get_order_history_impl(status="pending", market="kr")`. ROB-28 modifies `orders_modify_cancel.py` *signatures* (cancel/modify) and `orders_history.py` *error tolerance*, not the normalized dict shape. | **Do not change** the keys returned by `_normalize_kis_domestic_order` / `_normalize_kis_overseas_order`. Add error/`mock_unsupported` info as a sibling field on the surface response, not on each order dict. |
| ROB-9 — TradingAgents advisory ingest | merged (PRs #601/#604/#605) | **None.** Advisory-only, never touches KIS broker. | n/a |
| ROB-8 / ROB-10 | No local plan or branch found in this worktree (`docs/plans/ROB-{8,10}-*.md` absent; no matching `feature/ROB-{8,10}-*` branch). | **Unknown.** Likely in-progress on Linear only. | Reviewer must confirm before merge. ROB-28 does not change DB schema, models, or routers, which lowers conflict surface. |

If the implementer finds during work that any change here would also touch a
file under active edit on ROB-22/ROB-8/ROB-10, **stop and report** rather
than reconcile silently.

## 6. Tasks

Each task is a self-contained slice with focused tests. Each task ends with a
commit. The implementer should run only the focused tests for that task plus
ruff format/check on changed files, not the whole suite, until Task 9.

---

### Task 1 — Broker-layer: integrated margin fail-closed under mock

**Files:**
- Modify: `app/services/brokers/kis/account.py` (`inquire_integrated_margin`, ~line 590)
- Test: `tests/test_kis_account_fetch_stocks.py` (add a new test) — or new
  `tests/test_kis_integrated_margin_mock.py` if the existing file is too
  narrowly scoped.

- [ ] **Step 1: Write failing test**

```python
# tests/test_kis_integrated_margin_mock.py
import pytest
from unittest.mock import AsyncMock

from app.services.brokers.kis.client import KISClient


@pytest.mark.asyncio
async def test_integrated_margin_mock_fails_closed(monkeypatch):
    client = KISClient(is_mock=True)

    # Patch token + transport so we never hit the network even if the
    # fail-closed branch regresses.
    monkeypatch.setattr(client, "_ensure_token", AsyncMock(return_value=None))
    monkeypatch.setattr(
        client,
        "_request_with_rate_limit",
        AsyncMock(side_effect=AssertionError("must not call KIS in mock")),
    )

    with pytest.raises(RuntimeError, match="mock"):
        await client.inquire_integrated_margin(is_mock=True)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_kis_integrated_margin_mock.py -q
```
Expected: FAIL (currently calls KIS with `VTTC0869R`).

- [ ] **Step 3: Implement fail-closed branch**

In `app/services/brokers/kis/account.py` `inquire_integrated_margin(...)`,
at the top of the function (after the docstring, before
`await self._parent._ensure_token()`), add:

```python
if is_mock:
    raise RuntimeError(
        "KIS integrated margin is not supported in mock mode; "
        "use inquire_domestic_cash_balance(is_mock=True) instead."
    )
```

- [ ] **Step 4: Run focused tests**

```bash
uv run pytest tests/test_kis_integrated_margin_mock.py -q
uv run ruff format --check app/services/brokers/kis/account.py
uv run ruff check app/services/brokers/kis/account.py
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/kis/account.py tests/test_kis_integrated_margin_mock.py
git commit -m "feat(rob-28): fail closed on inquire_integrated_margin mock"
```

---

### Task 2 — Broker-layer: overseas pending inquiry fail-closed under mock

**Files:**
- Modify: `app/services/brokers/kis/overseas_orders.py` (`inquire_overseas_orders`, ~line 256)
- Test: new `tests/test_kis_overseas_pending_mock.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_kis_overseas_pending_mock.py
import pytest
from unittest.mock import AsyncMock

from app.services.brokers.kis.client import KISClient


@pytest.mark.asyncio
async def test_inquire_overseas_orders_mock_fails_closed(monkeypatch):
    client = KISClient(is_mock=True)

    monkeypatch.setattr(client, "_ensure_token", AsyncMock(return_value=None))
    monkeypatch.setattr(
        client,
        "_request_with_rate_limit",
        AsyncMock(side_effect=AssertionError("must not call KIS in mock")),
    )

    with pytest.raises(RuntimeError, match="mock"):
        await client.inquire_overseas_orders("NASD", is_mock=True)
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_kis_overseas_pending_mock.py -q
```
Expected: FAIL (currently sends `TTTS3018R` to mock host → EGW02006).

- [ ] **Step 3: Implement fail-closed branch**

In `app/services/brokers/kis/overseas_orders.py`
`inquire_overseas_orders(...)`, at the top (after docstring, before any
network call), add:

```python
if is_mock:
    raise RuntimeError(
        "KIS overseas pending-orders inquiry (TTTS3018R) is not "
        "available in mock mode."
    )
```

- [ ] **Step 4: Run focused tests + lint**

```bash
uv run pytest tests/test_kis_overseas_pending_mock.py tests/test_kis_overseas_orders_retry.py -q
uv run ruff format --check app/services/brokers/kis/overseas_orders.py
uv run ruff check app/services/brokers/kis/overseas_orders.py
```
Expected: PASS. (Existing `test_kis_overseas_orders_retry.py` should still
pass because it does not test mock mode.)

- [ ] **Step 5: Commit**

```bash
git add app/services/brokers/kis/overseas_orders.py tests/test_kis_overseas_pending_mock.py
git commit -m "feat(rob-28): fail closed on inquire_overseas_orders mock"
```

---

### Task 3 — Cash balance: route mock domestic to cash-balance TR, mark overseas mock unsupported

**Files:**
- Modify: `app/mcp_server/tooling/portfolio_cash.py`
- Test: extend `tests/test_kis_mock_routing.py` (or new
  `tests/test_portfolio_cash_kis_mock.py`)

- [ ] **Step 1: Write failing tests**

```python
# tests/test_portfolio_cash_kis_mock.py
import pytest
from unittest.mock import AsyncMock, MagicMock

from app.mcp_server.tooling import portfolio_cash


@pytest.mark.asyncio
async def test_cash_balance_mock_uses_domestic_cash_not_integrated_margin(
    monkeypatch,
):
    fake_kis = MagicMock()
    fake_kis.inquire_integrated_margin = AsyncMock(
        side_effect=AssertionError(
            "must not call integrated margin in mock"
        ),
    )
    fake_kis.inquire_domestic_cash_balance = AsyncMock(
        return_value={
            "dnca_tot_amt": 1000.0,
            "stck_cash_ord_psbl_amt": 900.0,
            "raw": {},
        },
    )
    fake_kis.inquire_overseas_margin = AsyncMock(
        side_effect=RuntimeError("mock unsupported"),
    )
    fake_kis.inquire_korea_orders = AsyncMock(return_value=[])

    monkeypatch.setattr(
        portfolio_cash, "_create_kis_client", lambda *, is_mock: fake_kis
    )
    monkeypatch.setattr(
        portfolio_cash.upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 0.0, "orderable": 0.0}),
    )

    result = await portfolio_cash.get_cash_balance_impl(is_mock=True)

    accounts = {a["account"]: a for a in result["accounts"]}
    assert "kis_domestic" in accounts
    assert accounts["kis_domestic"]["balance"] == 1000.0
    assert accounts["kis_domestic"]["orderable"] == 900.0
    # Overseas should be reported as a mock_unsupported error, not silent zero.
    assert any(
        e.get("market") == "us" and "mock" in (e.get("error") or "").lower()
        for e in result["errors"]
    )


@pytest.mark.asyncio
async def test_cash_balance_mock_pending_buy_tolerates_egw02006(monkeypatch):
    fake_kis = MagicMock()
    fake_kis.inquire_domestic_cash_balance = AsyncMock(
        return_value={
            "dnca_tot_amt": 1000.0,
            "stck_cash_ord_psbl_amt": 1000.0,
            "raw": {},
        },
    )
    fake_kis.inquire_overseas_margin = AsyncMock(
        side_effect=RuntimeError("mock unsupported"),
    )
    fake_kis.inquire_korea_orders = AsyncMock(
        side_effect=RuntimeError("EGW02006 모의투자 TR 이 아닙니다"),
    )

    monkeypatch.setattr(
        portfolio_cash, "_create_kis_client", lambda *, is_mock: fake_kis
    )
    monkeypatch.setattr(
        portfolio_cash.upbit_service,
        "fetch_krw_cash_summary",
        AsyncMock(return_value={"balance": 0.0, "orderable": 0.0}),
    )

    result = await portfolio_cash.get_cash_balance_impl(is_mock=True)

    # Pending deduction failed → orderable falls back to raw orderable
    # (not zero, not crash).
    accounts = {a["account"]: a for a in result["accounts"]}
    assert accounts["kis_domestic"]["orderable"] == 1000.0
```

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_portfolio_cash_kis_mock.py -q
```
Expected: FAIL (mock currently calls integrated margin).

- [ ] **Step 3: Implement mock-aware cash routing**

In `app/mcp_server/tooling/portfolio_cash.py` `get_cash_balance_impl(...)`,
inside the `kis` / `kis_domestic` branch, replace the
`inquire_integrated_margin(...)` call with a mock-aware fork:

```python
if is_mock:
    cash_summary = await _call_kis(
        kis.inquire_domestic_cash_balance,
        is_mock=is_mock,
    )
    dncl_amt = float(cash_summary.get("dnca_tot_amt", 0) or 0)
    raw_orderable = float(cash_summary.get("stck_cash_ord_psbl_amt", 0) or 0)
else:
    margin_data = await _call_kis(
        kis.inquire_integrated_margin,
        is_mock=is_mock,
    )
    domestic_cash = extract_domestic_cash_summary_from_integrated_margin(
        margin_data
    )
    dncl_amt = float(domestic_cash.get("balance", 0) or 0)
    raw_orderable = float(domestic_cash.get("orderable", 0) or 0)
```

For the overseas branch: keep the live call; in `except Exception as exc:`,
when `is_mock` is True, append an explicit error
`{"source": "kis", "market": "us", "error": "mock_unsupported: " + str(exc)}`
and skip the USD account entry instead of returning a fake zero.

For the pending-buy deduction (`_get_kis_domestic_pending_buy_amount` /
`_get_kis_overseas_pending_buy_amount_usd`) — the existing
`logger.warning` + raw-orderable fallback already tolerates exceptions.
Verify the fallback is preserved (the warning logged should not include
secret values).

- [ ] **Step 4: Run focused tests + lint**

```bash
uv run pytest tests/test_portfolio_cash_kis_mock.py tests/test_kis_mock_routing.py -q
uv run ruff format --check app/mcp_server/tooling/portfolio_cash.py
uv run ruff check app/mcp_server/tooling/portfolio_cash.py
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/portfolio_cash.py tests/test_portfolio_cash_kis_mock.py
git commit -m "feat(rob-28): mock cash via inquire_domestic_cash_balance"
```

---

### Task 4 — `cancel_order_impl`: accept `is_mock` and route mock through mock client

**Files:**
- Modify: `app/mcp_server/tooling/orders_modify_cancel.py`
  (`cancel_order_impl`, `_cancel_kis_domestic`, `_cancel_kis_overseas`,
  `_find_us_open_order_by_id`, `_find_us_order_in_recent_history`)
- Test: extend `tests/test_kis_mock_routing.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_kis_mock_routing.py (append)
import pytest
from unittest.mock import AsyncMock, MagicMock


@pytest.mark.asyncio
async def test_cancel_order_kis_mock_uses_mock_client(monkeypatch):
    from app.mcp_server.tooling import orders_modify_cancel

    instances: list[bool] = []

    class TrackedKISClient:
        def __init__(self, *, is_mock: bool = False) -> None:
            instances.append(is_mock)
            self.is_mock = is_mock
            self.inquire_korea_orders = AsyncMock(
                return_value=[
                    {
                        "odno": "0001",
                        "pdno": "005930",
                        "sll_buy_dvsn_cd": "02",
                        "ord_unpr": "70000",
                        "ord_qty": "1",
                    }
                ]
            )
            self.cancel_korea_order = AsyncMock(
                return_value={"ord_tmd": "100000"}
            )

    monkeypatch.setattr(orders_modify_cancel, "KISClient", TrackedKISClient)

    result = await orders_modify_cancel.cancel_order_impl(
        order_id="0001", symbol="005930", market="kr", is_mock=True
    )

    assert result["success"] is True
    assert all(flag is True for flag in instances), instances
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_kis_mock_routing.py::test_cancel_order_kis_mock_uses_mock_client -q
```
Expected: FAIL (`cancel_order_impl` does not accept `is_mock`).

- [ ] **Step 3: Add `is_mock` to cancel surfaces**

In `orders_modify_cancel.py`:

1. Add a module-level helper consistent with the other tools:
   ```python
   def _create_kis_client(*, is_mock: bool) -> KISClient:
       if is_mock:
           return KISClient(is_mock=True)
       return KISClient()
   ```
2. Change `cancel_order_impl(order_id, symbol=None, market=None)` to
   `cancel_order_impl(order_id, symbol=None, market=None, *, is_mock: bool = False)`.
3. Change `_cancel_kis_domestic(order_id, symbol)` to
   `_cancel_kis_domestic(order_id, symbol, *, is_mock: bool = False)`. Replace
   each `KISClient()` with `_create_kis_client(is_mock=is_mock)`. Pass
   `is_mock=is_mock` to `inquire_korea_orders` and `cancel_korea_order`.
4. Change `_cancel_kis_overseas(order_id, symbol)` to
   `_cancel_kis_overseas(order_id, symbol, *, is_mock: bool = False)`. Use
   `_create_kis_client(is_mock=is_mock)` and pass `is_mock=is_mock` to
   `inquire_overseas_orders` (will fail closed under mock — Task 2),
   `inquire_daily_order_overseas`, and `cancel_overseas_order`.
5. Pass `is_mock=is_mock` from `cancel_order_impl` into the dispatcher.
6. `_find_us_open_order_by_id` and `_find_us_order_in_recent_history`
   accept the `kis` instance already; no signature change needed beyond
   the parent dispatch.
7. For `kis_mock`, when `_cancel_kis_overseas` catches the
   "mock unsupported" `RuntimeError` from `inquire_overseas_orders`,
   return:
   ```python
   {
       "success": False,
       "order_id": order_id,
       "error": "kis_mock: overseas pending-orders inquiry (TTTS3018R) is "
                "not available in mock mode",
       "market": _normalize_market_type_to_external("equity_us"),
       "mock_unsupported": True,
   }
   ```

- [ ] **Step 4: Run focused tests + lint**

```bash
uv run pytest tests/test_kis_mock_routing.py -q
uv run ruff format --check app/mcp_server/tooling/orders_modify_cancel.py
uv run ruff check app/mcp_server/tooling/orders_modify_cancel.py
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_modify_cancel.py tests/test_kis_mock_routing.py
git commit -m "feat(rob-28): cancel_order accepts is_mock for kis_mock routing"
```

---

### Task 5 — `modify_order_impl`: accept `is_mock` and route mock through mock client

**Files:**
- Modify: `app/mcp_server/tooling/orders_modify_cancel.py`
  (`modify_order_impl`, `_modify_kis_domestic`, `_modify_kis_overseas`)
- Test: extend `tests/test_kis_mock_routing.py`

- [ ] **Step 1: Write failing test**

```python
# tests/test_kis_mock_routing.py (append)
@pytest.mark.asyncio
async def test_modify_order_kis_mock_uses_mock_client(monkeypatch):
    from app.mcp_server.tooling import orders_modify_cancel

    instances: list[bool] = []

    class TrackedKISClient:
        def __init__(self, *, is_mock: bool = False) -> None:
            instances.append(is_mock)
            self.inquire_korea_orders = AsyncMock(
                return_value=[
                    {
                        "odno": "0001",
                        "pdno": "005930",
                        "sll_buy_dvsn_cd": "02",
                        "ord_unpr": "70000",
                        "ord_qty": "1",
                    }
                ]
            )
            self.modify_korea_order = AsyncMock(return_value={"odno": "0002"})

    monkeypatch.setattr(orders_modify_cancel, "KISClient", TrackedKISClient)

    result = await orders_modify_cancel.modify_order_impl(
        order_id="0001",
        symbol="005930",
        market="kr",
        new_price=70100.0,
        dry_run=False,
        is_mock=True,
    )

    assert result["success"] is True
    assert result["new_order_id"] == "0002"
    assert all(flag is True for flag in instances), instances


def test_modify_order_kis_mock_dry_run_does_not_instantiate_kis(monkeypatch):
    """Dry-run preview must not require any KIS client instantiation."""
    from app.mcp_server.tooling import orders_modify_cancel

    class BrokenKISClient:
        def __init__(self, *, is_mock: bool = False) -> None:
            raise AssertionError("must not instantiate KIS in dry-run preview")

    monkeypatch.setattr(orders_modify_cancel, "KISClient", BrokenKISClient)
    # Will raise inside the test if the dry-run path tries to talk to KIS.
```

- [ ] **Step 2: Run test to verify it fails**

```bash
uv run pytest tests/test_kis_mock_routing.py::test_modify_order_kis_mock_uses_mock_client -q
```
Expected: FAIL.

- [ ] **Step 3: Add `is_mock` to modify surfaces**

Same pattern as Task 4:
- `modify_order_impl(..., *, is_mock: bool = False)`
- `_modify_kis_domestic(..., *, is_mock: bool = False)` — replace
  `KISClient()` with `_create_kis_client(is_mock=is_mock)`. Pass
  `is_mock=is_mock` to `inquire_korea_orders` and `modify_korea_order`.
- `_modify_kis_overseas(..., *, is_mock: bool = False)` — same. The first
  call (`inquire_overseas_orders`) fails closed under mock (Task 2);
  catch and return:
  ```python
  {
      "success": False,
      "status": "failed",
      "order_id": order_id,
      "symbol": normalized_symbol,
      "market": _normalize_market_type_to_external("equity_us"),
      "error": "kis_mock: overseas pending-orders inquiry (TTTS3018R) is "
               "not available in mock mode",
      "mock_unsupported": True,
      "dry_run": dry_run,
  }
  ```
- The dry-run preview branch (`_build_modify_dry_run_response`) must
  remain unchanged — it must not instantiate any KIS client regardless
  of `is_mock`.

- [ ] **Step 4: Run focused tests + lint**

```bash
uv run pytest tests/test_kis_mock_routing.py -q
uv run ruff format --check app/mcp_server/tooling/orders_modify_cancel.py
uv run ruff check app/mcp_server/tooling/orders_modify_cancel.py
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_modify_cancel.py tests/test_kis_mock_routing.py
git commit -m "feat(rob-28): modify_order accepts is_mock for kis_mock routing"
```

---

### Task 6 — `cancel_order` / `modify_order` MCP surface: accept `account_mode`

**Files:**
- Modify: `app/mcp_server/tooling/orders_registration.py`
  (`cancel_order`, `modify_order` registered tools)
- Test: extend `tests/test_mcp_account_modes.py`

- [ ] **Step 1: Write failing tests**

```python
# tests/test_mcp_account_modes.py (append)
import pytest
from unittest.mock import AsyncMock


@pytest.mark.asyncio
async def test_cancel_order_kis_mock_fails_closed_when_config_missing(
    monkeypatch,
):
    from app.mcp_server.tooling import orders_registration

    monkeypatch.setattr(
        orders_registration,
        "validate_kis_mock_config",
        lambda: ["KIS_MOCK_ENABLED", "KIS_MOCK_APP_KEY"],
    )

    captured: list = []

    async def fake_cancel_impl(**kwargs):
        captured.append(kwargs)
        return {"success": True, "order_id": kwargs["order_id"]}

    monkeypatch.setattr(
        orders_registration, "cancel_order_impl", fake_cancel_impl
    )

    # Build a minimal harness that calls the wrapped function directly.
    # Use the FastMCP test pattern already used by existing tests.
    ...  # see existing tests for harness
    # Assert: returns {"success": False, "error": "...KIS_MOCK_ENABLED, KIS_MOCK_APP_KEY"}
    # Assert: captured == [] (impl never invoked)


@pytest.mark.asyncio
async def test_modify_order_account_type_paper_is_rejected_for_kis():
    """account_type='paper' on modify_order must not route to KIS at all."""
    # Either error out as "modify_order not supported for db_simulated"
    # OR delegate to a paper handler. Choose whichever matches existing
    # paper modify policy. Implementer: confirm by reading
    # paper_order_handler.py for whether modify is supported.
```

If the registered FastMCP closures aren't directly importable, use the
existing test pattern from `tests/test_mcp_order_tools.py` — register
the tools onto a `FastMCP` instance and call `await mcp.tools["cancel_order"]
.fn(...)`.

- [ ] **Step 2: Run tests to verify they fail**

```bash
uv run pytest tests/test_mcp_account_modes.py -q
```
Expected: FAIL.

- [ ] **Step 3: Wire `account_mode` into the registered tools**

In `orders_registration.py`:

```python
@mcp.tool(
    name="cancel_order",
    description=(
        "Cancel a pending order. Supports Upbit (crypto) and KIS "
        "(KR/US equities). For KIS US orders, resolves exchange/order "
        "details from symbol lookup and order history when possible. "
        "Use account_mode={'kis_live','kis_mock'} to choose KIS routing; "
        "account_type aliases are deprecated and emit warnings. "
        "account_mode='kis_mock' fails closed if KIS_MOCK_ENABLED, "
        "KIS_MOCK_APP_KEY, KIS_MOCK_APP_SECRET, or KIS_MOCK_ACCOUNT_NO "
        "are missing."
    ),
)
async def cancel_order(
    order_id: str,
    symbol: str | None = None,
    market: str | None = None,
    account_mode: str | None = None,
    account_type: str | None = None,
):
    routing = normalize_account_mode(
        account_mode=account_mode,
        account_type=account_type,
    )
    if routing.is_db_simulated:
        # cancel is not implemented against the paper engine in this PR;
        # follow the same pattern used by modify_order for db_simulated.
        return apply_account_routing_metadata(
            {
                "success": False,
                "error": "cancel_order is not supported for db_simulated",
                "order_id": order_id,
            },
            routing,
        )
    if routing.is_kis_mock:
        config_error = _kis_mock_config_error()
        if config_error:
            return apply_account_routing_metadata(config_error, routing)
    return apply_account_routing_metadata(
        await cancel_order_impl(
            order_id=order_id,
            symbol=symbol,
            market=market,
            is_mock=routing.is_kis_mock,
        ),
        routing,
    )
```

Apply the equivalent change to `modify_order`. Confirm the existing
`paper_order_handler` path for modify; if there is no paper modify
handler, return the same fail-explicit shape used in the snippet above.

- [ ] **Step 4: Run focused tests + lint**

```bash
uv run pytest tests/test_mcp_account_modes.py tests/test_kis_mock_routing.py -q
uv run ruff format --check app/mcp_server/tooling/orders_registration.py
uv run ruff check app/mcp_server/tooling/orders_registration.py
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_registration.py tests/test_mcp_account_modes.py
git commit -m "feat(rob-28): cancel/modify_order accept account_mode"
```

---

### Task 7 — Order history: classify mock unsupported as explicit errors

**Files:**
- Modify: `app/mcp_server/tooling/orders_history.py` (`_fetch_us_orders`,
  `_fetch_kr_orders`, `_build_history_response`)
- Test: extend `tests/test_kis_mock_routing.py`

- [ ] **Step 1: Write failing test**

```python
@pytest.mark.asyncio
async def test_get_order_history_pending_us_mock_surfaces_unsupported(
    monkeypatch,
):
    """Mock pending US history must NOT silently return empty."""
    from app.mcp_server.tooling import orders_history

    class FakeKIS:
        def __init__(self, *, is_mock: bool = False) -> None:
            pass

        async def inquire_overseas_orders(self, exchange, *, is_mock=False):
            raise RuntimeError(
                "KIS overseas pending-orders inquiry (TTTS3018R) is not "
                "available in mock mode."
            )

    monkeypatch.setattr(orders_history, "KISClient", FakeKIS)
    result = await orders_history.get_order_history_impl(
        status="pending", market="us", is_mock=True
    )

    assert result["orders"] == []
    assert any(
        e.get("market") == "equity_us"
        and "mock" in (e.get("error") or "").lower()
        for e in result["errors"]
    )
```

- [ ] **Step 2: Run test to verify it fails**

Currently `_fetch_us_orders` swallows `inquire_overseas_orders` exceptions
silently inside its `try/except Exception: pass` (line ~233), which is
why mock smoke saw empty results despite the EGW02006 log. Verify it
fails:

```bash
uv run pytest tests/test_kis_mock_routing.py::test_get_order_history_pending_us_mock_surfaces_unsupported -q
```
Expected: FAIL (errors list is empty).

- [ ] **Step 3: Surface mock-unsupported errors**

In `_fetch_us_orders`, replace the silent `except Exception: pass` for
the per-exchange `inquire_overseas_orders` call with:

```python
except Exception as exc:
    if is_mock and "mock" in str(exc).lower():
        # Surface mock-unsupported once per market, not per exchange.
        raise RuntimeError(
            "kis_mock: overseas pending-orders inquiry is not "
            "available in mock mode"
        ) from exc
    logger.warning(
        "US pending-orders inquiry failed for exchange=%s: %s",
        ex,
        exc,
    )
```

The outer `for m_type in market_types:` loop in `get_order_history_impl`
already catches and records to `errors[]` with
`{"market": m_type, "error": str(e)}`, so re-raising from inside the
fetcher yields the desired structured error.

For `_fetch_kr_orders`: leave the existing behavior (it raises naturally
via `_call_kis(kis.inquire_korea_orders, is_mock=is_mock)`), and the
outer loop already records `{"market": "equity_kr", "error": "..."}`.
Add a one-line comment that EGW02006 from KR pending under mock is
expected on some KIS mock accounts and surfaces as a structured error,
not as silent empty.

- [ ] **Step 4: Run focused tests + lint**

```bash
uv run pytest tests/test_kis_mock_routing.py tests/test_mcp_order_tools.py -q
uv run ruff format --check app/mcp_server/tooling/orders_history.py
uv run ruff check app/mcp_server/tooling/orders_history.py
```
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add app/mcp_server/tooling/orders_history.py tests/test_kis_mock_routing.py
git commit -m "feat(rob-28): surface mock-unsupported pending history errors"
```

---

### Task 8 — Operator docs

**Files:**
- Modify: `app/mcp_server/README.md` (Account Routing + KR/US order routing
  + new "Mock-unsupported endpoints" subsection)

- [ ] **Step 1: Update `cancel_order` / `modify_order` signatures in README**

Replace:
```
- `modify_order(order_id, symbol, market=None, new_price=None, new_quantity=None, dry_run=True)`
- `cancel_order(order_id, symbol=None, market=None)`
```

with:
```
- `modify_order(order_id, symbol, market=None, new_price=None, new_quantity=None, dry_run=True, account_mode=None)`
- `cancel_order(order_id, symbol=None, market=None, account_mode=None)`
```

- [ ] **Step 2: Add the "Mock-unsupported endpoints" subsection**

Under "Account Routing", append:

```markdown
#### KIS mock unsupported endpoints

`account_mode="kis_mock"` returns explicit "mock unsupported" errors instead
of silently degrading for the following KIS endpoints, which are live-only on
the official KIS mock account:

- `inquire_integrated_margin` (`TTTC0869R`) — returns `OPSQ0002 없는 서비스 코드 입니다`
  on mock. Mock cash routes via `inquire_domestic_cash_balance` (`VTTC8434R`)
  instead.
- `inquire_overseas_orders` (`TTTS3018R`) — KIS does not publish a mock TR.
  Pending US history under `account_mode="kis_mock"` returns
  `errors: [{market: "equity_us", error: "kis_mock: overseas pending-orders
  inquiry ..."}]` and an empty orders list.
- `inquire_korea_orders` (`TTTC8036R`) — documented as "실전/모의 공통" but
  some mock accounts return `EGW02006 모의투자 TR 이 아닙니다`. KR pending
  under `account_mode="kis_mock"` surfaces these as structured errors, not
  silent empty results.
- KIS overseas margin (`TTTS2101R` / `VTTS2101R`) — treated as
  mock-unsupported; the USD account row is omitted under
  `account_mode="kis_mock"` and the failure is reported in `errors[]`.

#### Operator runtime config

`account_mode="kis_mock"` reads only `KIS_MOCK_*` settings. To enable the
mock account in production, the operator should source a separate env file
(for example `~/services/auto_trader/shared/.env.kis-mock`) into the launchd
plist environment for the MCP / API processes — **never** merge mock
secrets into the live `.env.prod.native` file. When any of
`KIS_MOCK_ENABLED=true`, `KIS_MOCK_APP_KEY`, `KIS_MOCK_APP_SECRET`, or
`KIS_MOCK_ACCOUNT_NO` are missing, every mock surface returns:

```
{
  "success": false,
  "error": "KIS mock account is disabled or missing required configuration: KIS_MOCK_ENABLED, ...",
  "source": "kis",
  "account_mode": "kis_mock"
}
```

The error names variables only — never values.
```

- [ ] **Step 3: Commit**

```bash
git add app/mcp_server/README.md
git commit -m "docs(rob-28): document kis_mock unsupported endpoints"
```

---

### Task 9 — Full validation pass

- [ ] **Step 1: Lint + format**

```bash
uv sync --group test --group dev
uv run ruff format --check app/ tests/
uv run ruff check app/ tests/
```
Expected: PASS.

- [ ] **Step 2: Focused test slice from the issue**

```bash
uv run pytest tests -q -k 'account_mode or kis_mock or order_history or modify_order or cancel_order or cash_balance'
```
Expected: PASS.

- [ ] **Step 3: Type check (advisory; optional if `ty` not in dev group)**

```bash
make typecheck || true
```

- [ ] **Step 4: Confirm no secrets in diff**

```bash
git diff origin/main..HEAD | grep -iE 'KIS_MOCK_APP_KEY|KIS_MOCK_APP_SECRET|KIS_MOCK_ACCOUNT_NO' \
  | grep -v 'KIS_MOCK_APP_KEY"\|KIS_MOCK_APP_SECRET"\|KIS_MOCK_ACCOUNT_NO"' || true
```
Expected: only variable-name string literals, never values.

- [ ] **Step 5: Push and open PR**

```bash
git push -u origin HEAD
gh pr create --title "ROB-28 Harden KIS mock account_mode routing" \
  --body "$(cat <<'EOF'
## Summary
- Fail-closed on KIS endpoints that are live-only on mock (integrated margin, overseas pending-orders inquiry).
- Mock cash routes via inquire_domestic_cash_balance (VTTC8434R) instead of integrated margin.
- cancel_order and modify_order accept account_mode; kis_mock routes through KISClient(is_mock=True) and fails closed when KIS_MOCK_* config is missing.
- Order history under kis_mock surfaces mock-unsupported endpoints as structured errors instead of silent empty results.

## Test plan
- [ ] uv run pytest tests -q -k 'account_mode or kis_mock or order_history or modify_order or cancel_order or cash_balance'
- [ ] uv run ruff format --check app/ tests/
- [ ] uv run ruff check app/ tests/
- [ ] Verify no secret values in diff (only env-variable name literals)
- [ ] Reviewer confirms no overlap with active ROB-22 / ROB-8 / ROB-10 work

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## 7. Acceptance criteria mapping

| Criterion (from issue) | Tasks |
|---|---|
| `account_mode="kis_mock"` fail-closes when mock config missing/disabled; never silently uses `kis_live` credentials | Task 6 (already enforced for place_order/get_order_history/get_holdings/get_position by ROB-19; Task 6 extends to cancel_order/modify_order) |
| Mock and live KIS token cache namespaces remain separated | Verified by ROB-19 (`RedisTokenManager("kis_mock")` vs `RedisTokenManager("kis")`); no change in this PR |
| Read-only mock paths either succeed or return clear unsupported-mock errors | Tasks 1, 2, 3, 7 |
| Pending/history mock paths no longer log EGW02006 from live TR mapping | Tasks 2 + 7 (broker fail-closed before TR is sent; surface returns structured error) |
| `cancel_order` and `modify_order` accept `account_mode` and route mock through | Tasks 4, 5, 6 |
| Tests cover account-mode routing and fail-closed safety for place / history / cancel / modify / read-only | Tasks 1-7 |
| PR/CI green | Task 9 |
| Merge gated behind ROB-22 / ROB-8 / ROB-10 overlap review | §5 above; reviewer confirms in PR |

## 8. Out of scope

- Auto-loading `~/services/auto_trader/shared/.env.kis-mock` from Python.
  Documented as operator-managed launchd plist env wiring instead.
- Live-account canary, day-trading pilot execution, strategy automation.
- Adding paper modify/cancel handlers (DB simulation parity for cancel/modify).
  Returns explicit `db_simulated` not-supported responses for now.
- Removing `account_type` legacy aliases.
- Kiwoom integration.

## 9. Notes for the implementer (OpenCode / Kimi K2.5)

- Read this whole plan once before touching code.
- One commit per task; do not squash.
- Keep diffs minimal — do not refactor unrelated code.
- Do not change `_normalize_kis_domestic_order` / `_normalize_kis_overseas_order`
  return shapes — ROB-22 depends on them.
- If a step says "explicit error", the error string must include the literal
  substring `"mock"` so callers can detect mock-unsupported branches in tests
  and logs without parsing TR IDs.
- If you discover a sub-issue that requires editing a file under active edit
  on ROB-22, ROB-8, or ROB-10, **stop and report** to the reviewer rather
  than reconcile silently.
- Never read or write any file under `/Users/mgh3326/services/auto_trader/shared/`.
- After Task 9, hand off to the Opus reviewer (same session); do not merge.
