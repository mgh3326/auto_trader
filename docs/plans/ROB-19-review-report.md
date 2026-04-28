# ROB-19 Review Report

**Verdict:** PASS_WITH_NOTES

AOE_STATUS: review
AOE_ISSUE: ROB-19
AOE_ROLE: reviewer-opus
AOE_NEXT: implementer addresses Notes (or files Phase-2 follow-up issue) before merge; reviewer signs off after env.example + tool-description fixes.

- **Branch / worktree:** `feature/ROB-19-kis-mock-account-routing`
  (`/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-19-kis-mock-account-routing`)
- **Commits reviewed:**
  - `6ece3c96 feat(rob-19): normalize kis mock account routing`
  - `3d4dd37b docs(rob-19): add implementation plan`
- **Test/lint evidence (already run by implementer):**
  - `uv run pytest tests/test_mcp_account_modes.py tests/test_paper_order_handler.py tests/test_mcp_portfolio_tools.py tests/test_mcp_order_tools.py tests/test_mcp_place_order.py tests/test_kis_order_ops.py -q` → **212 passed**
  - `uv run ruff check app tests` → **passed**
  - `uv run python -m py_compile $(rg --files app tests -g '*.py')` → **passed**

---

## Safety Review

The seven safety invariants from the plan were checked against the diff:

1. **`account_type="paper"` remains DB simulation only and never KIS mock.** ✅
   - `account_modes._ACCOUNT_TYPE_ALIASES` (`tooling/account_modes.py:23-29`) maps
     `paper → db_simulated` exclusively. `kis_mock` is reachable only via
     `account_mode="kis_mock"` (`_ACCOUNT_MODE_ALIASES`, line 12-21), never via
     `account_type`. Conflicting `account_mode="kis_mock"` + `account_type="paper"`
     raises `ValueError("conflicting account selectors")` — covered by
     `tests/test_mcp_account_modes.py::test_conflicting_account_selectors_fail`.
   - In `orders_registration.place_order` (line 179-198) the
     `routing.is_db_simulated` branch routes only to `_place_paper_order`; the
     KIS-mock branch (line 199-202) is reached only when `routing.is_kis_mock`
     is true. There is no code path where `account_type="paper"` reaches the
     KIS broker.

2. **`account_mode="kis_mock"` fails closed before broker calls if config is
   disabled/missing.** ✅
   - `core/config.validate_kis_mock_config(...)` (`app/core/config.py:465-477`)
     returns missing env-var **names only** (no values), checking
     `KIS_MOCK_ENABLED`, `KIS_MOCK_APP_KEY`, `KIS_MOCK_APP_SECRET`,
     `KIS_MOCK_ACCOUNT_NO`.
   - The validator is invoked **before** any KIS HTTP call on every kis_mock
     surface:
     - `place_order` — `orders_registration.py:199-202` (returns structured
       error dict containing only env names, no values).
     - `get_order_history` — `orders_registration.py:87-90`.
     - `get_holdings` — `portfolio_holdings.py:1153-1160` (raises `RuntimeError`).
     - `get_position` — `portfolio_holdings.py:1023-1030`.
     - `get_cash_balance` — `portfolio_holdings.py:1259-1266`.
     - `get_available_capital` — `portfolio_holdings.py:1298-1306`.
   - Tested by
     `tests/test_paper_order_handler.py::TestPlaceOrderRegistration::test_account_mode_kis_mock_fails_closed_before_broker_call`
     (asserts the live stub is **not** awaited and the missing env names appear
     in the error string).

3. **`kis_mock` uses only mock settings, `is_mock=True`, and a separate
   token/cache namespace.** ✅ (with one functional gap — see Findings #1).
   - `_KISSettingsView` (`app/services/brokers/kis/client.py:43-79`) overrides
     `kis_app_key`, `kis_app_secret`, `kis_account_no`, and
     `kis_access_token` with the mock variants when `is_mock=True`. It does
     **not** delegate live values when in mock mode for those four fields.
   - Token cache: `RedisTokenManager("kis_mock")` (`client.py:101`) produces
     keys `kis_mock:access_token` / `kis_mock:token:lock`, distinct from the
     live default `kis:access_token` / `kis:token:lock`
     (`redis_token_manager.py:16-19`).
   - `is_mock=True` is threaded through every KIS public method on the
     mock-credentialed client (cash, holdings, place orders, history) via
     `_call_kis(...)` helpers in `order_execution.py`, `orders_history.py`,
     `order_validation.py`, and `portfolio_cash.py`. Verified end-to-end by
     `tests/test_mcp_portfolio_tools.py::test_get_cash_balance_kis_mock_passes_is_mock`
     which asserts `all(is_mock for _, is_mock in calls)`.

4. **No secret values printed, logged, persisted, or documented.** ✅
   - `validate_kis_mock_config` reports env-var names only (line 470-476). No
     value access in any error path.
   - Tool error messages on kis_mock failure embed only the env-var names
     (`orders_registration.py:39-44`, `portfolio_holdings.py:1156-1160`).
   - Tested by `test_validate_kis_mock_config_reports_names_only`
     (`tests/test_mcp_account_modes.py:54-70`) including a positive assertion
     that the placeholder secret string is **not** present in
     `repr(missing)`.
   - No new logging statements expose `kis_mock_app_key/secret/account_no`.
     Confirmed by `grep -nE "kis_mock_app_secret|kis_mock_app_key|kis_mock_account_no" app/ tests/` — only definition/access sites in `config.py`,
     the settings view, the validator, and one test stub.
   - The path `/Users/mgh3326/services/auto_trader/shared/.env.kis-mock` is
     not referenced anywhere in the diff.

5. **Default/live behavior remains backward compatible and `dry_run=True`
   default preserved.** ✅
   - `KISClient(is_mock=False)` (the default) yields the same `_token_manager
     = redis_token_manager` and the same `_settings_view` returning live
     creds. The dispatch layer (`_create_kis_client`, `_call_kis`) routes
     live calls through the existing path with no `is_mock` keyword
     forwarded.
   - `place_order` signature still has `dry_run: bool = True`
     (`orders_registration.py:135`); no behavior or default changed.
   - Legacy `account_type="paper"` and `account_type="real"` continue to
     work, with `paper` carrying a deprecation warning and routing to the
     paper handler exactly as before. Verified by
     `test_paper_order_handler.py::TestPlaceOrderRegistration::test_account_type_paper_routes_to_paper_order`
     and `test_account_type_real_still_calls_live_impl`.

6. **Tests mock broker side effects and do not place orders.** ✅
   - All new tests use `AsyncMock` / `MagicMock` for KIS and paper services.
   - No `dry_run=False` test path was added that calls a real broker
     dispatcher; the `_place_order_impl` call in
     `test_account_mode_kis_mock_routes_to_order_impl_with_mock_enabled`
     is patched to a `live_stub`.
   - Confirmed by `grep -nE "dry_run\\s*=\\s*False" tests/test_paper_order_handler.py` — no new uses.

7. **README/docs accurately describe account routing.** ✅
   - `app/mcp_server/README.md` adds the “Account Routing” block
     (line 118-135) describing the three modes, deprecation policy, and
     fail-closed behavior. Public tool spec lines for `get_holdings`
     (line 48), `get_position` (line 50), `get_order_history` (line 87),
     and `place_order` (line 97) advertise the new `account_mode`
     parameter.

---

## Findings

| # | Severity | File / Line | Issue | Recommendation |
| - | -------- | ----------- | ----- | -------------- |
| 1 | **MEDIUM** | `app/services/brokers/kis/base.py:266`, `constants.py:4`, all `f"{constants.BASE}{...}"` callsites in `account.py` / `domestic_orders.py` / `overseas_orders.py` | KIS official mock **base URL is not switched**. Mock client still reads `kis_base_url` (or its hardcoded fallback `https://openapi.koreainvestment.com:9443`) and `constants.BASE` for trading URLs. The KIS official mock server is `https://openapivts.koreainvestment.com:29443`. Mock app keys cannot authenticate against the live auth endpoint, so once an operator sets `KIS_MOCK_ENABLED=true` plus the three mock credentials, the first read-only call (`inquire_integrated_margin`) will hit the **live** auth host with mock credentials and 401. **No live order can be placed** (the auth fails closed at the broker), so this is **not a safety violation**, but it is a functional gap that turns the “fail closed” promise into an opaque 401 instead of an explicit “mock not yet wired” error. | Either (a) add `kis_mock_base_url: str = "https://openapivts.koreainvestment.com:29443"` to `Settings`, override `kis_base_url` in `_KISSettingsView`, and route every `f"{constants.BASE}{...}"` in trading paths through a credentials/view-aware base URL; or (b) explicitly mark the kis_mock path as Phase-1 “preview only” with a guard in `KISClient.__init__(is_mock=True)` that raises a clear NotImplementedError on actual broker calls until base URL switching lands. Track as a Phase-2 follow-up issue. |
| 2 | LOW | `orders_registration.py:33-45` vs `portfolio_holdings.py:1156-1160` (and 3 other call sites in the same file) | **Inconsistent fail-closed error shape.** `place_order` and `get_order_history` return a structured `{"success": False, "error": "...", "account_mode": "kis_mock"}` dict on missing config; `get_holdings`, `get_position`, `get_cash_balance`, and `get_available_capital` raise a bare `RuntimeError`. Tests cover both behaviors (`test_get_cash_balance_kis_mock_fails_closed` uses `pytest.raises(RuntimeError)`) so the suite is green, but downstream MCP clients now have to handle two different shapes for the same operator condition. | Standardize on the structured-error dict (preferred — matches existing in-band MCP error contract) across all kis_mock-gated surfaces. Extract a single `_kis_mock_config_error()` helper into `account_modes.py` and call it from every gated tool. |
| 3 | LOW | `app/services/brokers/kis/client.py:43-79` | **Leaky `__getattr__` delegation in `_KISSettingsView`.** Only four fields are mock-aware (`kis_app_key`, `kis_app_secret`, `kis_account_no`, `kis_access_token`); every other settings access (notably `kis_base_url`, but also future per-account fields like rate limits) silently falls through to the live `settings`. This is the underlying cause of Finding #1. | Either replace the view with a frozen credentials dataclass that explicitly enumerates every field used by `BaseKISClient`, or document the override list at the top of `_KISSettingsView` so future contributors understand which fields are mock-isolated. |
| 4 | LOW | `app/mcp_server/tooling/order_execution.py:56-62`, `orders_history.py:32-38`, `portfolio_holdings.py:247-250` | **Unreachable `try/except TypeError` fallback** silently routes mock to live if `KISClient.__init__` ever loses its `is_mock` kwarg. `KISClient` already accepts `is_mock` keyword-only (`client.py:96`), so the `except TypeError: return KISClient()` branch is dead code today, but it would mask a future regression. | Remove the `TypeError` fallback. If the KIS facade ever drops `is_mock`, that is a programming bug and should fail loudly, not degrade silently to live credentials. |
| 5 | LOW | `app/mcp_server/tooling/orders_registration.py:51-58, 105-126`; `portfolio_holdings.py:1126-1136, 1175-1182, 1239-1246, 1276-1284` | **Tool description strings are stale.** They still say `"Set account_type='paper' to..."` and `"account_type='real' (default)"` and don't mention `account_mode`. The README block is updated, but the per-tool description is what the calling LLM sees in the schema first. | Append one sentence to each affected description: `"Use account_mode={'db_simulated','kis_mock','kis_live'} (preferred); account_type aliases are deprecated and emit warnings."` |
| 6 | LOW | `env.example` | **`KIS_MOCK_*` settings not added.** The new `kis_mock_enabled`, `kis_mock_app_key`, `kis_mock_app_secret`, `kis_mock_account_no` settings are defined in `app/core/config.py:174-177` but not advertised in `env.example`. Operators copying the example file won't see them. | Append a `KIS_MOCK_*` block (variable names only, no values, comment referencing operator-managed `.env.kis-mock` injection at runtime). Plan §1 already documented this; it just wasn't carried into the diff. |
| 7 | LOW | `tests/test_mcp_account_modes.py` | **No assertion that `RedisTokenManager("kis_mock")` produces a key namespace distinct from live.** `test_kis_token_namespace.py` from the original plan was not added. Token-cache isolation is provable by reading the constructor (`redis_token_manager.py:16-19`), but a 5-line `fakeredis`-backed test would prevent silent regressions in this load-bearing safety primitive. | Add `tests/test_kis_token_namespace.py` with two assertions: `RedisTokenManager()._token_key == "kis:access_token"` and `RedisTokenManager("kis_mock")._token_key == "kis_mock:access_token"`, plus an end-to-end fakeredis test that a token saved under `"kis_mock"` is not visible to the default-namespace manager. |
| 8 | INFO | n/a | **Phase 1 scope is intentionally read-only-friendly + dry-run-default.** No KIS-mock smoke harness was added. The plan’s production smoke section calls for an operator-driven run; nothing in the diff blocks that, but no helper exists. | Optional: defer to Phase 2 along with the base-URL fix. |

---

## Test Review

**Coverage of new behavior is solid where it lands; one isolation test is missing.**

What is well-covered:
- `tests/test_mcp_account_modes.py` (5 cases) — selector normalization, deprecation warnings, conflict detection, env-name-only validator. Includes a positive secret-leakage assertion.
- `tests/test_paper_order_handler.py` (`TestPlaceOrderRegistration` and
  `TestGetOrderHistoryRegistration` classes, ~6 new cases) — every routing
  branch:
  - `account_type="paper"` → paper handler, live impl never awaited.
  - `account_mode="db_simulated"` → paper handler.
  - `account_type="real"` → live impl, paper not called.
  - `account_mode="kis_mock"` (mock enabled) → live impl awaited with
    `is_mock=True`.
  - `account_mode="kis_mock"` (mock disabled) → fails closed, live impl
    never awaited.
- `tests/test_mcp_portfolio_tools.py` adds `test_get_cash_balance_kis_mock_passes_is_mock` (asserts `all(is_mock for _, is_mock in calls)` across `inquire_integrated_margin`, `inquire_korea_orders`, `inquire_overseas_margin`, `inquire_overseas_orders`) and `test_get_cash_balance_kis_mock_fails_closed`.
- All pre-existing live-path tests still pass (212 passed).

What is missing or could be strengthened:
- **Token-cache isolation test.** No test confirms that `kis:access_token` and `kis_mock:access_token` cannot collide. See Finding #7.
- **No test for `get_holdings(account_mode="kis_mock")`** at the MCP level — only `get_cash_balance` is exercised end-to-end. The other surfaces (`get_position`, `get_holdings`, `get_available_capital`) are gated by the same validator but a routing test that asserts the mock client receives `is_mock=True` for `fetch_my_stocks` / `fetch_my_us_stocks` would mirror the cash-balance coverage.
- **No test that mock and live clients use distinct `_token_manager` instances.** A 3-line assertion (`KISClient(is_mock=True)._token_manager is not KISClient()._token_manager`) would lock the invariant.
- **No assertion that `_KISSettingsView` fails to leak live creds.** A targeted unit test on `_KISSettingsView(is_mock=True)` confirming that `kis_app_key` / `kis_app_secret` / `kis_account_no` come exclusively from `kis_mock_*` (and not from live `kis_*`) would close the loop on Finding #3.

None of these gaps are blocking — the existing 212-pass suite covers all
explicit safety invariants the plan called out — but they would harden the
mock-isolation contract against future refactors.

---

## Final Recommendation

**PASS_WITH_NOTES — safe to merge after Notes #5 and #6 are addressed.**

The implementation correctly resolves the original ROB-19 ambiguity:
`account_type="paper"` cannot accidentally reach a KIS broker, `kis_mock`
fails closed at the MCP boundary when config is missing or disabled, mock and
live credentials never share a token cache namespace, and no secret values
are exposed in error messages or logs. The 212-pass suite plus ruff and
py_compile demonstrate that the live trading path is unchanged. The plan’s
hard safety constraints (no live orders, dry_run-default preserved, no
fallback to live creds, no leaked secrets, no watch/intent side effects in
tests) all hold.

**Required before merge (low effort, blocks operator adoption):**
1. Update tool description strings to advertise `account_mode` (Note #5).
2. Add `KIS_MOCK_*` block to `env.example` (Note #6).

**Strongly recommended follow-ups (Phase-2 issue, not blocking this PR):**
3. Switch the KIS base URL by mode (Note #1) — without this, `KIS_MOCK_ENABLED=true` produces opaque 401s instead of working mock calls. File a Phase-2 follow-up Linear issue: *"ROB-19 follow-up: route KIS mock requests to openapivts:29443 host."*
4. Standardize the kis_mock fail-closed error shape across all surfaces (Note #2).
5. Remove the `try/except TypeError` fallbacks that silently degrade mock → live (Note #4).

**Recommended hardening (cheap, defensive):**
6. Add `tests/test_kis_token_namespace.py` (Note #7).
7. Add per-field positive assertions for `_KISSettingsView` mock isolation (Test Review §missing).

After items 1–2 are committed, this PR is ready for merge. Items 3–5 should
be tracked as a follow-up Phase-2 issue that lands before any operator turns
on `KIS_MOCK_ENABLED=true` in production.
