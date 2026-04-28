# ROB-28 Review Report — Harden KIS mock account_mode routing

**Reviewer:** Claude Opus (planner/reviewer for this AoE slice)
**Branch:** `feature/ROB-28-kis-mock-routing`
**Base:** `origin/main`
**Plan:** `docs/plans/ROB-28-kis-mock-routing-plan.md`
**Implementer:** OpenCode (Kimi K2.5) + Hermes (formatting fix)
**CI signal (per Hermes):**
- `uv run ruff format --check app/ tests/` → pass
- `uv run ruff check app/ tests/` → pass
- `uv run pytest tests -q -k 'account_mode or kis_mock or order_history or modify_order or cancel_order or cash_balance'` → 111 passed
- `git status` → clean

---

## 1. Verdict

**APPROVED for PR.** No must-fix issues. All hard safety invariants and
acceptance criteria are met. A small number of non-blocking observations are
listed in §5 for the PR description and follow-up triage.

## 2. Diff scope

13 commits, 15 files, +1869 / −87 (1130 of those lines are the plan file
itself). Production code changes are tightly scoped:

| File | Δ | Notes |
|---|---|---|
| `app/services/brokers/kis/account.py` | +6 | fail-closed on `inquire_integrated_margin(is_mock=True)` |
| `app/services/brokers/kis/overseas_orders.py` | +6 | fail-closed on `inquire_overseas_orders(is_mock=True)` |
| `app/mcp_server/tooling/portfolio_cash.py` | +75 / −54 | mock domestic via `inquire_domestic_cash_balance`; mock overseas surfaced as `mock_unsupported` error |
| `app/mcp_server/tooling/orders_modify_cancel.py` | +56 / −9 | `is_mock` plumbing through `cancel_order_impl` / `modify_order_impl` and `_cancel_kis_*` / `_modify_kis_*`; US cancel/modify fail-closed under mock |
| `app/mcp_server/tooling/orders_history.py` | +12 / −2 | US pending mock surfaces `mock` error instead of silent empty; per-exchange non-mock failures keep prior best-effort behavior with explicit `logger.warning` |
| `app/mcp_server/tooling/orders_registration.py` | +71 / −10 | `cancel_order` / `modify_order` accept `account_mode` + `account_type`; gate `kis_mock` on `validate_kis_mock_config`; reject `db_simulated` |
| `app/mcp_server/README.md` | +44 / −2 | `cancel_order` / `modify_order` signatures updated; new "KIS mock unsupported endpoints" + "Operator runtime config" subsections |

Tests: +405 lines across 5 files, with one extension to
`tests/test_mcp_portfolio_tools.py` (mock harness gains
`inquire_domestic_cash_balance`) and one to `tests/test_mcp_order_tools.py`
(existing fakes upgraded to accept `is_mock` so they don't blow up on the
new keyword).

## 3. Hard safety invariants

| Invariant | Verified by | Status |
|---|---|---|
| `account_mode="kis_mock"` never falls back to live KIS credentials, base URL, or token namespace | `KISClient(is_mock=True)` → `_KISSettingsView(is_mock=True)` reads only `kis_mock_*` settings (no live fallback); `RedisTokenManager("kis_mock")` namespace preserved (no change in `client.py`); new `_create_kis_client(is_mock=is_mock)` helpers in `orders_modify_cancel.py` always pass `is_mock=True` when routing requires it | ✅ pass |
| Missing/disabled `KIS_MOCK_*` config fail-closes before any KIS broker call | `cancel_order` / `modify_order` registered tools call `_kis_mock_config_error()` (which delegates to `validate_kis_mock_config()`) before `cancel_order_impl` / `modify_order_impl`; tested by `test_cancel_order_kis_mock_fails_closed_when_config_missing` and `test_modify_order_kis_mock_fails_closed_when_config_missing` | ✅ pass |
| No secret values in errors / logs / commits — only env-variable names | Error string format `"KIS mock account is disabled or missing required configuration: KIS_MOCK_ENABLED, KIS_MOCK_APP_KEY"` confirmed; `test_validate_kis_mock_config_reports_names_only` already asserted secret values absent from `repr(missing)`; new mock-unsupported errors mention only TR IDs (`TTTS3018R`, `TTTC0869R`) | ✅ pass |
| No live orders or `dry_run=False` execution introduced | `place_order` default `dry_run=True` unchanged; tests use only `AsyncMock` / `MagicMock` substitutes; new `test_modify_order_kis_mock_dry_run_does_not_instantiate_kis` actively asserts the dry-run preview path never instantiates `KISClient` | ✅ pass |
| ROB-22 normalized order dict shape preserved | Diff vs main shows zero changes to `_normalize_kis_domestic_order` / `_normalize_kis_overseas_order` / `_normalize_upbit_order`; only siblings (`_create_kis_client`, `_cancel_kis_*`, `_modify_kis_*`, `cancel_order_impl`, `modify_order_impl`) edited | ✅ pass |
| No production-secret env files read | Implementation does not read `services/auto_trader/shared/.env.kis-mock`; the path is only mentioned in the README as operator guidance, not loaded from code | ✅ pass |

## 4. Acceptance criteria mapping

| Criterion | Coverage |
|---|---|
| `kis_mock` fail-closes on missing config; never silently uses live | `cancel_order`/`modify_order` gated in `orders_registration.py` (Task 6); `place_order`/`get_holdings`/`get_position`/`get_cash_balance`/`get_available_capital`/`get_order_history` were already gated in ROB-19. ✅ |
| Mock and live token cache namespaces separated | Preserved from ROB-19 (`RedisTokenManager("kis_mock")` vs `RedisTokenManager("kis")`); no regression in this diff. ✅ |
| Read-only mock paths either succeed or return clear unsupported errors | Tasks 1 / 2 / 3 / 7. `inquire_integrated_margin` and `inquire_overseas_orders` raise explicit `RuntimeError` containing `"mock"`. Mock domestic cash uses `inquire_domestic_cash_balance` (real `VTTC8434R`). Mock overseas margin and US pending history surface as structured `errors[]`. ✅ |
| Pending/history mock paths no longer produce `EGW02006` from live TR mapping | Broker layer fails closed before any live TR is sent (Task 2). KR pending under mock still raises naturally if KIS rejects `TTTC8036R`, but `_fetch_kr_orders`'s outer loop already records the error in `errors[]` rather than logging EGW02006 from a successful HTTP roundtrip. ✅ |
| `cancel_order` / `modify_order` accept `account_mode` and route mock through | `orders_registration.py` adds `account_mode` + deprecated `account_type` parameters; `is_mock=routing.is_kis_mock` flows into `cancel_order_impl(..., is_mock=...)` / `modify_order_impl(..., is_mock=...)` and downstream into `_cancel_kis_domestic`, `_cancel_kis_overseas`, `_modify_kis_domestic`, `_modify_kis_overseas`. Verified by `test_cancel_order_kis_mock_passes_is_mock_to_impl` / `test_modify_order_kis_mock_passes_is_mock_to_impl`. ✅ |
| Tests cover account-mode routing and fail-closed safety for place / history / cancel / modify / read-only | place_order: pre-existing ROB-19 tests; cancel/modify: 6 new tests in `tests/test_mcp_account_modes.py` + 4 in `tests/test_kis_mock_routing.py`; history: `test_get_order_history_pending_us_mock_surfaces_unsupported`; cash: 2 new tests in `tests/test_portfolio_cash_kis_mock.py`; broker fail-closed: `tests/test_kis_integrated_margin_mock.py` and `tests/test_kis_overseas_pending_mock.py`. ✅ |
| PR/CI green | Confirmed by Hermes (111 passed, ruff format + ruff check clean). ✅ |
| Merge gated behind ROB-22 / ROB-8 / ROB-10 review | Plan §5 documents the overlap analysis; ROB-22 shape preserved; ROB-8 / ROB-10 not in worktree, flagged for human reviewer per plan. ✅ (reviewer-side gate; PR description should restate it) |

## 5. Observations (non-blocking)

These are notes for the PR description / follow-up triage. None block merge.

### 5.1 US cancel/modify under mock is more conservative than strictly necessary

`_cancel_kis_overseas(is_mock=True)` and `_modify_kis_overseas(is_mock=True)`
short-circuit at the top with a `mock_unsupported` response. KIS does
publish mock TRs for the underlying `cancel_overseas_order`
(`VTTT1004U`) and the daily order history (`VTTS3035R`) used by
`_find_us_order_in_recent_history`, so an alternative implementation could
look up the order via daily history (`is_mock=True`) and then cancel via
`VTTT1004U`. The chosen design avoids that complexity for the first mock
pilot and is safer; the docs string ("overseas pending-orders inquiry
(TTTS3018R) is not available in mock mode") accurately states the
limitation but is slightly oblique because the *cancel* TR itself is
fine — only the open-order lookup that precedes it is unsupported.

**Recommendation:** Track as a future enhancement if mock day-trading
needs US cancel/modify rehearsal. For now, the explicit fail-closed plus
`mock_unsupported: True` field is the right safety posture.

### 5.2 Mock overseas cash + strict mode silently degrades instead of raising

In `portfolio_cash.get_cash_balance_impl`, when `account="kis_overseas"`
(`strict_mode=True`) and `is_mock=True`, the new code appends to
`errors[]` and returns `accounts: []`. The live equivalent re-raises with
`RuntimeError(...)` when `strict_mode` is set and the call fails. This is
a minor behavioral asymmetry: a caller that explicitly asks for the
`kis_overseas` account in mock mode receives a "soft" empty response
instead of the strict-mode raise.

**Recommendation:** If callers rely on strict-mode raises to detect
configuration errors, consider raising under `strict_mode and is_mock`
in a follow-up. The plan accepted "structured error" as the contract, so
the current behavior is plan-consistent.

### 5.3 Mock-unsupported flag shape is mildly inconsistent

`orders_modify_cancel.py` adds `"mock_unsupported": True` as a
top-level key. `portfolio_cash.py` and `orders_history.py` use the
substring `"mock"` inside the `error` field instead. Both communicate
the same intent and tests check the lowercase substring. This is a UX
nit, not a correctness issue.

**Recommendation:** Pick one convention before this becomes a public
contract that gets baked into a UI badge. Suggestion: keep the boolean
flag and also include `"mock"` in the error string (the modify/cancel
overseas branch already does both).

### 5.4 No direct test for `_cancel_kis_overseas(is_mock=True)` / `_modify_kis_overseas(is_mock=True)`

KR cancel/modify mock paths are tested end-to-end. US mock cancel/modify
short-circuit branches are exercised only indirectly via the type-system
(both functions return `mock_unsupported: True` early). A direct test
that calls `cancel_order_impl(market="us", is_mock=True)` and asserts the
response shape would close the gap.

**Recommendation:** Add as a small follow-up test, not a blocker.

### 5.5 Plan-vs-implementation deviation: KR pending under mock is best-effort, not structured-error

The plan asked for a comment in `_fetch_kr_orders` noting that EGW02006
from KR pending under mock surfaces as a structured error rather than
silent empty. The implementer relied on the existing outer `for m_type
in market_types: try / except` loop in `get_order_history_impl` (which
already records `{"market": m_type, "error": str(e)}`) and did not add
a comment. The behavior is correct — a real EGW02006 will appear in
`errors[]` — but the inline doc-cue is missing. Cosmetic.

### 5.6 `_get_kis_domestic_pending_buy_amount` under mock relies on `try/except` to swallow EGW02006

In `portfolio_cash.get_cash_balance_impl`, the mock domestic branch
still calls `_get_kis_domestic_pending_buy_amount(kis, is_mock=is_mock)`
to deduct pending buys, which calls `kis.inquire_korea_orders(is_mock=True)`.
On a mock account where KIS rejects `TTTC8036R` with EGW02006, the
existing `except Exception as exc: logger.warning(...)` falls back to
raw orderable, which is exactly what the test
`test_cash_balance_mock_pending_buy_tolerates_egw02006` confirms. This
is correct, but means mock orderable can be slightly optimistic
(pending KR mock buys are not deducted). For a rehearsal pilot this is
acceptable; document in the operator runbook when the pilot starts.

## 6. Files inspected

- `app/services/brokers/kis/account.py` — diff confirmed (+6 lines, fail-closed branch)
- `app/services/brokers/kis/overseas_orders.py` — diff confirmed (+6 lines, fail-closed branch)
- `app/services/brokers/kis/client.py` — unchanged; verified `_KISSettingsView` keeps mock isolation and `RedisTokenManager("kis_mock")` namespace
- `app/services/redis_token_manager.py` — unchanged
- `app/core/config.py` — unchanged; `validate_kis_mock_config` reused
- `app/mcp_server/tooling/account_modes.py` — unchanged; reused
- `app/mcp_server/tooling/orders_modify_cancel.py` — full diff reviewed
- `app/mcp_server/tooling/orders_registration.py` — full diff reviewed
- `app/mcp_server/tooling/orders_history.py` — full diff reviewed (incl. confirming `ex` loop variable in the warning log)
- `app/mcp_server/tooling/portfolio_cash.py` — full diff reviewed
- `app/mcp_server/README.md` — full diff reviewed
- All 5 test files (3 new + 2 extended) — full diff reviewed; harness `tests/_mcp_tooling_support.DummyMCP` is the established pattern

## 7. PR description guidance

Recommended bullets for the PR:

```
## Summary
- Fail closed on KIS endpoints that are live-only on mock investment:
  inquire_integrated_margin (TTTC0869R) and inquire_overseas_orders (TTTS3018R).
- Mock domestic cash now routes via inquire_domestic_cash_balance (VTTC8434R)
  instead of integrated margin (which returns OPSQ0002 on mock).
- cancel_order and modify_order MCP tools accept account_mode; kis_mock routes
  through KISClient(is_mock=True) and fails closed when KIS_MOCK_* config is
  missing.
- US pending order history under kis_mock surfaces mock-unsupported endpoints
  as structured errors instead of silent empty results.
- Operator-facing README documents kis_mock unsupported endpoints and the
  recommended separate-env-file launchd pattern.

## Test plan
- uv run pytest tests -q -k 'account_mode or kis_mock or order_history or modify_order or cancel_order or cash_balance' → 111 passed
- uv run ruff format --check app/ tests/ → clean
- uv run ruff check app/ tests/ → clean
- No secret values in diff (verified env-variable name string literals only)

## Overlap / merge gate
- ROB-22 (pending reconciliation): normalized KIS order dict shape preserved
  (no changes to _normalize_kis_domestic_order / _normalize_kis_overseas_order).
- ROB-8 / ROB-10: not present locally; reviewer to confirm before merge.
- Hard safety: no live orders, no dry_run=False execution, no real-account
  canary, no automated strategy, no Kiwoom integration.
```

## 8. Reviewer checklist (final)

- [x] No live orders introduced
- [x] No dry_run=False execution path executed in tests
- [x] No KIS secret values in diff (only env-variable name string literals)
- [x] Mock token cache namespace remains separate from live
- [x] Mock surfaces fail closed when `KIS_MOCK_*` config is missing
- [x] Mock surfaces never read live `KIS_*` settings
- [x] ROB-22 normalized order dict shape preserved
- [x] CI green per Hermes (ruff + focused pytest)
- [x] Plan adherence: all 9 tasks completed; deviations in §5 are non-blocking
- [x] No reads of `/Users/mgh3326/services/auto_trader/shared/.env.kis-mock`

---

AOE_STATUS: review_passed
AOE_ISSUE: ROB-28
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-28-review-report.md
AOE_NEXT: create_pr
