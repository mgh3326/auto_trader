# ROB-19 Final Review Report

**Verdict:** PASS

AOE_STATUS: final-review
AOE_ISSUE: ROB-19
AOE_ROLE: reviewer-opus
AOE_NEXT: signoff complete; PR is ready to merge to `main`. Phase-2 follow-up
issue (KIS-mock write-side execution + standardized fail-closed shape) should
be filed before any operator flips `KIS_MOCK_ENABLED=true` in production.

- **Branch / worktree:** `feature/ROB-19-kis-mock-account-routing`
  (`/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-19-kis-mock-account-routing`)
- **Commits since prior review:**
  - `f9f1d883 fix(rob-19): harden kis mock routing safeguards`
  - `8049e53b docs(rob-19): add opus review report`
- **Test/lint evidence (re-run by orchestrator after fix commit):**
  - `uv run pytest tests/test_kis_mock_routing.py tests/test_mcp_account_modes.py tests/test_paper_order_handler.py tests/test_mcp_portfolio_tools.py tests/test_mcp_order_tools.py tests/test_mcp_place_order.py tests/test_kis_order_ops.py -q` → **219 passed** (was 212; +7 from new
    `tests/test_kis_mock_routing.py`)
  - `uv run ruff check app tests` → **passed**
  - `uv run python -m py_compile $(rg --files app tests -g '*.py')` → **passed**

---

## Fix Verification

The three required fixes from the prior review (PASS_WITH_NOTES) have all
landed and are covered by the new test suite.

### Required fix #1 — KIS mock base URL switching ✅

**Prior issue:** `_KISSettingsView` only overrode `kis_app_key`,
`kis_app_secret`, `kis_account_no`, `kis_access_token`. `kis_base_url` fell
through `__getattr__` to live `settings`, and trading paths used a hardcoded
`f"{constants.BASE}{...}"` (live URL `:9443`). Mock requests would have hit
the live host and 401'd — not a safety violation, but a functional gap.

**Now:**

- `Settings` exposes both `kis_base_url: str = "https://openapi.koreainvestment.com:9443"` and `kis_mock_base_url: str = "https://openapivts.koreainvestment.com:29443"`
  as first-class fields (`app/core/config.py:169, 178`).
- `_KISSettingsView.kis_base_url` (`app/services/brokers/kis/client.py:71-74`)
  now picks the mock host when `is_mock=True` and the live host otherwise —
  no more `__getattr__` fallback for this field.
- `BaseKISClient._kis_url(path)` (`app/services/brokers/kis/base.py:138-142`)
  resolves URLs from `self._settings.kis_base_url`, with the same
  hardcoded-fallback only when settings is missing the field entirely (defense
  in depth for legacy `Settings` shims).
- All `f"{constants.BASE}{...}"` references in the trading paths have been
  rewritten to `self._parent._kis_url(...)` /  `self._kis_url(...)`. Confirmed
  by `rg -n 'constants\.BASE\b' app/services/brokers/kis/` returning **zero
  matches** post-fix. The 26+ rewrites span:
  - `account.py` (4 sites: integrated margin, balance, overseas margin, generic)
  - `domestic_orders.py` (5 sites)
  - `overseas_orders.py` (5 sites)
  - `domestic_market_data.py` (12 sites)
  - `overseas_market_data.py` (2 sites)
  - `base.py` `_fetch_token` (1 site, for `/oauth2/token`)
- `KISClientProtocol._kis_url(self, path: str) -> str` is added to the typing
  protocol (`app/services/brokers/kis/protocols.py:65-67`) so sub-clients can
  call `self._parent._kis_url(...)` under static analysis.

**Test coverage:**
- `tests/test_kis_mock_routing.py::test_kis_mock_settings_view_uses_mock_base_url`
  asserts `KISClient(is_mock=True)._settings.kis_base_url` and `_kis_url(...)`
  resolve to the mock host even when the live `kis_base_url` is patched to a
  distinct value — closing the loop on Finding #3 from the prior review (the
  leaky `__getattr__`).
- `tests/test_kis_mock_routing.py::test_kis_mock_fetch_token_posts_to_mock_base_url`
  is an end-to-end assertion that `_fetch_token` POSTs to
  `https://mock.example.invalid/oauth2/token` — proves the mock auth call
  no longer leaks to the live host.

### Required fix #2 — env/docs/tool descriptions ✅

**Prior issue:** `env.example` did not list `KIS_MOCK_*` settings, and tool
description strings on `place_order`, `get_order_history`, `get_holdings`,
`get_position` did not mention `account_mode`.

**Now:**
- `env.example:16-26` adds `KIS_BASE_URL`, `KIS_MOCK_ENABLED`, `KIS_MOCK_APP_KEY`,
  `KIS_MOCK_APP_SECRET`, `KIS_MOCK_ACCOUNT_NO`, `KIS_MOCK_BASE_URL`,
  `KIS_MOCK_ACCESS_TOKEN`, with the explicit comment *"Keep credential values
  in operator-managed runtime env files; do not commit them."* — matches the
  ROB-19 plan §1 directive and never embeds a real value.
- All four affected tool descriptions append the canonical sentence
  *"Use account_mode={'db_simulated','kis_mock','kis_live'} (preferred);
  account_type aliases are deprecated and emit warnings."* — verified at:
  - `orders_registration.py:58-59` (`get_order_history`)
  - `orders_registration.py:122-123` (`place_order`)
  - `portfolio_holdings.py:1133-1134` (`get_holdings`)
  - `portfolio_holdings.py:1179-1180` (`get_position`)
- README block at `app/mcp_server/README.md:118-136` was updated to mention
  `KIS_MOCK_BASE_URL` and the official mock host default.

### Required fix #3 — TypeError fallbacks ✅

**Prior issue:** Five callsites had `try: KISClient(is_mock=True) except
TypeError: return KISClient()`, which would silently route mock requests to
live credentials if the `is_mock` keyword were ever dropped from
`KISClient.__init__`.

**Now:**
- All five `try/except TypeError` fallbacks are removed. Confirmed by `grep`
  across `order_execution.py:56-59`, `orders_history.py:32-35`,
  `order_validation.py:38-41`, `portfolio_cash.py:25-28`, and
  `portfolio_holdings.py:247` — each now reads simply
  `return KISClient(is_mock=True)` (or for `_collect_kis_positions`,
  `kis = KISClient(is_mock=True) if is_mock else KISClient()`).

**Test coverage:** `tests/test_kis_mock_routing.py` adds **five regression
tests** (`test_*_does_not_fallback_to_live`) — one per former fallback site
— each substitutes a `BrokenKISClient` whose `__init__(is_mock=True)` raises
`TypeError`. The tests assert the factory propagates the `TypeError` rather
than silently degrading to a no-arg `KISClient()`. This is a high-quality
regression net: any future refactor that re-introduces a silent fallback
will fail one of these tests.

---

## Remaining Notes

These are **not blockers** — they were rated LOW in the prior review and
were not part of the required-fix list. They remain valid but can be
deferred to the Phase-2 follow-up issue.

| # | Severity | Status | Location | Note |
| - | -------- | ------ | -------- | ---- |
| 1 | LOW | Deferred | `orders_registration.py:33-45` (dict shape) vs `portfolio_holdings.py:1153-1159` (`RuntimeError`) | **Inconsistent fail-closed shape** persists: order tools return `{"success": False, "error": "...", "account_mode": "kis_mock"}`; portfolio tools raise `RuntimeError`. Tests cover both shapes. Downstream MCP clients still need to handle two flavors of the same condition. Recommend extracting a single `_kis_mock_config_error()` helper into `account_modes.py` and standardizing on the structured-error dict in Phase 2. |
| 2 | LOW | Deferred | `client.py:43-87` | `_KISSettingsView.__getattr__` still falls through to live `settings` for any field not explicitly listed. Today the four credential fields plus `kis_base_url` are mock-isolated, which covers every settings access made by `BaseKISClient` per the current grep. Future settings (e.g., per-account rate limits) would need to be added to the explicit override list. The class docstring at `client.py:44` (*"Expose live or KIS mock credentials without cross-account fallback."*) signals intent, but does not enumerate the override list. Recommend converting to a frozen dataclass or annotating the explicit field list in Phase 2. |
| 3 | LOW | Deferred | n/a | No fakeredis-backed test asserts that a token saved under the `kis_mock` namespace is invisible to the default-namespace `RedisTokenManager`. The constructor-level invariant is straightforward (`redis_token_manager.py:16-19`) but a 5-line round-trip test would harden the cache-isolation contract. The new `tests/test_kis_mock_routing.py` covers everything else; this is the only gap. |
| 4 | INFO | n/a | n/a | KIS-mock **non-dry-run execution** still flows through `_place_order_impl` with `is_mock=True` once `KIS_MOCK_ENABLED=true`. The base-URL switch and credential isolation now make this a real path (rather than a 401 against live), but the journal/order-history persistence layer was not extended to source-tag mock fills. Operator-driven smoke before any production enablement should confirm KR/US mock fills don't collide with live `order_history` rows. Track in Phase-2 follow-up. |

---

## Final Recommendation

**PASS — merge to `main`.**

All three previously identified required fixes are present, structurally
correct, and protected by new regression tests. The 219-pass test suite
covers:

- Account-mode selector normalization (`test_mcp_account_modes.py`).
- Routing isolation: `account_type="paper"` → DB simulation only;
  `account_mode="kis_mock"` → live impl with `is_mock=True`;
  `account_mode="kis_live"` → unchanged live path
  (`test_paper_order_handler.py`).
- Fail-closed-before-broker contract on missing `KIS_MOCK_*` env
  (`test_paper_order_handler.py`, `test_mcp_portfolio_tools.py`).
- Mock base URL is honored at the credentials view, the URL builder, AND the
  `/oauth2/token` POST (`test_kis_mock_routing.py`).
- No silent fallback to live when the mock-client factory fails
  (`test_kis_mock_routing.py` × 5 sites).

Hard safety invariants from the original plan all hold:
- ✅ No live order placed in tests, smoke, or review.
- ✅ `dry_run=True` default preserved on `place_order`.
- ✅ `kis_mock` fails closed with names-only error when config is incomplete;
  no live fallback path exists.
- ✅ No secret values printed/logged/persisted; `validate_kis_mock_config`
  reports env-var **names only**.
- ✅ Token cache namespaces (`kis:access_token` vs `kis_mock:access_token`)
  cannot collide.
- ✅ Mock requests now provably go to `openapivts.koreainvestment.com:29443`,
  not the live host.

**Required for Phase 2 (file as separate Linear issue before operator
turns on `KIS_MOCK_ENABLED=true` in production):**
1. Standardize the kis_mock fail-closed error shape (Remaining Note #1).
2. Tighten `_KISSettingsView` against future leaky `__getattr__` regressions
   (Remaining Note #2).
3. Add a fakeredis round-trip test for token-namespace isolation
   (Remaining Note #3).
4. Source-tag KIS-mock order-history rows so they cannot interleave with
   live `order_history` records (Remaining Note #4).
5. Operator-driven read-only smoke harness against the actual KIS official
   mock server (separate from CI) — recommended in the original plan, not
   yet present.

This PR is ready to merge as-is. Implementer is signed off for Phase 1.
