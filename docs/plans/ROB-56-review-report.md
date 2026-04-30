# ROB-56 Review Report — KIS Official Mock Hard-Separation

**Reviewer:** Claude Opus 4.7
**Branch / worktree:** `feature/ROB-56-kis-mock-hard-separation`
(`/Users/mgh3326/work/auto_trader-worktrees/feature-ROB-56-kis-mock-hard-separation`)
**Plan:** `docs/plans/ROB-56-kis-mock-hard-separation-plan.md`
**Implementer:** Claude Sonnet
**Status:** review_passed

---

## 1. Scope of review

Review-only pass over the diff produced by Sonnet against the
ROB-56 plan. No production code modified. No broker mutation calls.
No live autonomous trading enabled.

Files inspected (changed / added):

- `app/mcp_server/main.py` (modified)
- `app/mcp_server/profiles.py` (new)
- `app/mcp_server/tooling/registry.py` (modified)
- `app/mcp_server/tooling/orders_kis_variants.py` (new)
- `app/services/brokers/capabilities.py` (new)
- `app/mcp_server/README.md` (modified — appended profile docs)
- `env.example` (modified)
- `CHANGELOG.md` (modified)
- `tests/test_mcp_profiles.py` (new)
- `tests/test_mcp_kis_order_variants.py` (new)
- `tests/test_kis_settings_view_isolation.py` (new)
- `tests/test_broker_capabilities.py` (new)

Reference files (read-only): `app/services/brokers/kis/client.py`,
`app/mcp_server/tooling/account_modes.py`,
`app/mcp_server/tooling/orders_registration.py`.

---

## 2. Required-scope findings

### 2.1 Live/mock credential and env separation (G1, plan §2.1, §4.6)
**Pass.** `_KISSettingsView` (`client.py:43-88`) is unchanged on this
branch and continues to expose `kis_app_key`, `kis_app_secret`,
`kis_account_no`, `kis_base_url`, `kis_access_token` via explicit
`@property` accessors that branch on `self._is_mock`. Since properties
take precedence over `__getattr__`, the four hot fields cannot fall
through to the live settings object even if a future regression weakens
`__getattr__`. The new
`tests/test_kis_settings_view_isolation.py` pins this for both
directions (mock→mock-only, live→live-only) across all five fields,
which is the ROB-19 phase-2 carry the plan called out.

### 2.2 Mock-only service / order boundary fail-closed (G1, plan §4.5)
**Pass.** `orders_kis_variants._mock_config_error` in
`orders_kis_variants.py:53-65` reproduces the canonical envelope
(`success=False`, `source="kis"`, `account_mode="kis_mock"`,
error message lists missing var **names only**) used by the legacy
path in `orders_registration.py:33-45`. Each `kis_mock_*` wrapper
calls it **before** delegating to the underlying impl. There is no
fallback to live credentials anywhere in the mock variants —
`is_mock=True` is a literal in the delegated call, not derived from
input.

### 2.3 MCP tool surface split into `kis_live_*` / `kis_mock_*` (G2, plan §4.3, §5.2)
**Pass.** All eight tool names from the plan are present and exact:
- `kis_live_place_order`, `kis_live_cancel_order`,
  `kis_live_modify_order`, `kis_live_get_order_history`
- `kis_mock_place_order`, `kis_mock_cancel_order`,
  `kis_mock_modify_order`, `kis_mock_get_order_history`

Each variant:
- Hard-pins `is_mock` (literal `False` for live, `True` for mock —
  `orders_kis_variants.py:172, 204, 244, 284, 372, 409, 455, 502`).
- Validates any supplied `account_mode` / `account_type` against the
  pinned mode via `_check_mode_arg` and rejects mismatches with a
  structured error (`source="mcp"`, `account_mode=pinned`).
- Delegates to the same `_place_order_impl` / `cancel_order_impl` /
  `modify_order_impl` / `orders_history.get_order_history_impl` used
  by the legacy `place_order` etc., so ROB-37's mock-ledger isolation
  and ROB-31's `mock_unsupported` tagging are preserved.
- Wraps the result in `apply_account_routing_metadata` for envelope
  parity with the legacy tools.

### 2.4 `hermes-paper-kis` profile excludes live tools (G1, plan §4.4)
**Pass.** `registry.py:101-110` gates the side-effect order tool
registration on `profile`:
- `DEFAULT`: `register_order_tools` + `register_kis_live_order_tools`
  + `register_kis_mock_order_tools` (additive — preserves backward
  compat, G3).
- `HERMES_PAPER_KIS`: only `register_kis_mock_order_tools`. The
  legacy ambiguous tools and `register_kis_live_order_tools` are
  **physically not registered**.

`tests/test_mcp_profiles.py::TestHermesPaperKisProfile` enforces this:
- `test_does_not_register_legacy_order_tools` asserts none of
  `{place_order, cancel_order, modify_order, get_order_history}` is
  in `mcp.tools` under `HERMES_PAPER_KIS`.
- `test_does_not_register_live_order_tools` asserts none of the four
  `kis_live_*` names is registered.
- `test_registers_kis_mock_order_tools` asserts the four mock-pinned
  variants are registered.

These tests would fail if any live or legacy ambiguous order tool
ever leaked into the paper profile. Verified by running the suite
locally — all assertions pass.

`MCP_PROFILE` env wiring is hooked exactly once, in `main.py:52` via
`resolve_mcp_profile(_env("MCP_PROFILE"))`, and the resolved profile
is passed into `register_all_tools`. `resolve_mcp_profile`
(`profiles.py:17-31`) handles `None`, empty, whitespace-only,
`"default"`, `"hermes-paper-kis"`, and raises `ValueError` for
unknown values — pinned by `TestResolveMcpProfile`.

### 2.5 Shared read-only/research tools remain side-effect-free (plan §5.1)
**Pass.** The unconditional registration block in `registry.py:81-98`
matches the plan's §5.1 table: all market data, fundamentals,
analysis, news, market report/brief, watch alerts, trade profile,
user settings, portfolio (read-only with mock-safe `account_mode`),
trade journal, paperclip comment, execution comment, and paper
account/analytics/journal helpers. Each of these helpers is either
read-only against external APIs / DB or strictly bound to
`db_simulated` (paper_*) — none can issue a KIS broker mutation.
`update_manual_holdings` writes to user-owned `manual_holdings`, not
through any broker, so retaining it in the paper profile is correct.

### 2.6 Broker capability model (plan §6, §2.6)
**Pass.** `app/services/brokers/capabilities.py` introduces
`Market`, `Broker`, `BrokerCapability`, and `BROKER_CAPABILITIES`
with the exact contents the plan specifies:
- KIS → `{KR_EQUITY, US_EQUITY}`, `supports_paper=True`,
  `supports_live=True`.
- Kiwoom → `{KR_EQUITY, US_EQUITY}`, `supports_paper=False`,
  `supports_live=False` (metadata only — no client integrated).
- Upbit → `{CRYPTO}`, `supports_paper=False`, `supports_live=True`.

`tests/test_broker_capabilities.py` pins each broker's market set,
paper/live flags, and the registry's exact key set. The dataclass is
frozen and uses a `frozenset` for markets, so the registry cannot be
silently mutated. No production code consumes `BROKER_CAPABILITIES`
yet, which matches the plan's metadata-only scope.

### 2.7 Test meaningfulness against live-tool leakage
**Pass.** The new tests are not surface-only — they directly assert
the absence of live and legacy ambiguous order tool names from the
paper profile (`test_does_not_register_legacy_order_tools`,
`test_does_not_register_live_order_tools`), and they assert
`is_mock=True` / `is_mock=False` is propagated from each typed wrapper
to the underlying impl by patching the impl and capturing kwargs.
A regression that registered a `kis_live_*` tool (or the legacy
ambiguous tools) into `HERMES_PAPER_KIS` would fail loudly here.

### 2.8 No live autonomous trading or broker mutation enabled
**Pass.** `dry_run` defaults to `True` on every wrapper that takes it
(`kis_live_place_order`, `kis_live_modify_order`,
`kis_mock_place_order`, `kis_mock_modify_order`). No code path
removes the safety; no scheduler, cron, or autonomous loop is
introduced. The diff does not modify `_place_order_impl`,
`cancel_order_impl`, `modify_order_impl`, or any broker client.

### 2.9 Backward compatibility for default profile
**Pass.** Under `MCP_PROFILE` unset (or `"default"`), the legacy
ambiguous tools (`place_order`, `cancel_order`, `modify_order`,
`get_order_history`) are still registered via `register_order_tools`
and continue to honor `account_mode` switching exactly as before.
The new typed `kis_live_*` / `kis_mock_*` tools are additive. ROB-19,
ROB-28, ROB-31, ROB-37 callers using
`account_mode="kis_mock"` / `"kis_live"` see no behavior change. The
regression suite (`test_mcp_account_modes.py`,
`test_kis_mock_routing.py`, `test_kis_mock_order_ledger.py`,
`test_kis_constants.py`, `test_orders_history_kis_mock.py`,
`test_portfolio_cash_kis_mock.py`,
`test_kis_integrated_margin_mock.py`,
`test_paper_order_handler.py`, `test_mcp_order_tools.py`,
`test_mcp_place_order.py`) passes locally — 163 tests, no failures.

---

## 3. Test results

Focused suite (plan §8.3, the new tests):

```
uv run pytest \
  tests/test_mcp_profiles.py \
  tests/test_mcp_kis_order_variants.py \
  tests/test_kis_settings_view_isolation.py \
  tests/test_broker_capabilities.py -q
→ 58 passed, 2 warnings in 2.43s
```

Regression suite (existing tests, no edits expected):

```
uv run pytest \
  tests/test_mcp_account_modes.py \
  tests/test_kis_mock_routing.py \
  tests/test_kis_mock_order_ledger.py \
  tests/test_kis_constants.py \
  tests/test_orders_history_kis_mock.py \
  tests/test_portfolio_cash_kis_mock.py \
  tests/test_kis_integrated_margin_mock.py -q
→ 47 passed, 2 warnings in 1.96s

uv run pytest \
  tests/test_paper_order_handler.py \
  tests/test_mcp_order_tools.py \
  tests/test_mcp_place_order.py -q
→ 116 passed, 3 warnings in 5.62s
```

Lint:

```
uv run ruff check app/mcp_server/profiles.py \
  app/mcp_server/tooling/orders_kis_variants.py \
  app/mcp_server/tooling/registry.py \
  app/services/brokers/capabilities.py \
  tests/test_mcp_profiles.py \
  tests/test_mcp_kis_order_variants.py \
  tests/test_kis_settings_view_isolation.py \
  tests/test_broker_capabilities.py
→ All checks passed!
```

Total: 221 tests pass across new + regression coverage. No live
broker calls were made.

---

## 4. Documentation & changelog

- `app/mcp_server/README.md` adds an "MCP Profiles (ROB-56)" section
  (line 1009+) covering the env var, the profile→tool-surface table,
  the typed `kis_live_*` / `kis_mock_*` names, the operator
  validation step, and the fail-closed envelope shape.
- `env.example` adds `MCP_PROFILE=default` plus a comment near the
  KIS_MOCK block instructing operators to set
  `MCP_PROFILE=hermes-paper-kis` on paper-only deployments.
- `CHANGELOG.md` has a complete "Unreleased / Added (ROB-56)" entry
  enumerating the env var, profile, typed tools, capability registry,
  and `_KISSettingsView` regression tests.

---

## 5. Minor observations (non-blocking, no fix required)

These are advisory only — the plan's acceptance criteria are met and
none of these block merge.

- **O1.** `_check_mode_arg` only accepts the canonical `account_mode`
  value (`"kis_mock"` / `"kis_live"`); aliases like `"mock"` or
  `"live"` are rejected as mismatches. This matches the plan's §5.3
  contract ("accepted only if equal to the tool's pinned mode") and
  is the safer choice — but if a typed caller is migrated from a
  client that previously passed alias values it would need to use the
  canonical form. No action required; mention in operator notes if
  the deprecated alias path was widely used by automation.
- **O2.** `tests/test_mcp_kis_order_variants.py` covers
  `account_mode` rejection but not `account_type` rejection.
  `_check_mode_arg` covers both, so functionally this is fine; a
  future test could pin the `account_type='kis_live'` rejection on a
  `kis_mock_*` tool for symmetry.
- **O3.** The plan §9 mentions verifying `dry_run=True` defaults in
  the contract tests. The new variants preserve `dry_run: bool =
  True` as a parameter default; the tests rely on this implicitly
  (callers omit it) but do not assert the resulting `is_mock+dry_run`
  pair on the captured kwargs. Existing `test_mcp_place_order.py`
  covers the default for the legacy tool, so the live wrapper's
  delegation chain is exercised — but a direct assertion on the
  typed wrappers' default would close the loop.
- **O4.** Out-of-scope items F1 (Upbit/crypto in `hermes-paper-kis`)
  and F5 (deprecation of legacy ambiguous tools) remain follow-ups.
  Confirmed in scope only as documented; no action this PR.

---

## 6. Verdict

The diff implements ROB-56 exactly as planned. Live KIS credentials
and live-order MCP surfaces cannot be reached from the
`hermes-paper-kis` profile; mock variants fail closed without
fallback; the capability registry pins KIS+Kiwoom KR/US support; the
default profile is unchanged; tests are meaningful and would catch
the regression the issue is designed to prevent. All focused and
regression tests pass; lint is clean.

---

AOE_STATUS: review_passed
AOE_ISSUE: ROB-56
AOE_ROLE: reviewer
AOE_REPORT_PATH: docs/plans/ROB-56-review-report.md
AOE_NEXT: create_pr
