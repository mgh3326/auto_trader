# ROB-19 Normalize simulated vs KIS mock account routing — Implementation Plan

AOE_STATUS: plan-ready
AOE_ISSUE: ROB-19
AOE_ROLE: planner-opus/manual-handoff
AOE_NEXT: codex --yolo implementer should execute the scoped plan, run focused tests, commit, then request Opus review.

## Goal

Normalize auto_trader MCP account routing so three modes are explicit and cannot be confused:

- `db_simulated`: DB-backed virtual/paper trading engine. Existing `account_type="paper"` remains a deprecated alias.
- `kis_mock`: official KIS mock/sandbox account. Uses only `KIS_MOCK_*` credentials and `is_mock=True` broker paths.
- `kis_live`: real KIS live account. Existing default real behavior remains, but live execution still requires `dry_run=False` and should not be exercised in this PR.

## Safety invariants

- Never place a live order during tests, smoke, or review.
- Preserve `dry_run=True` default for MCP `place_order`.
- `kis_mock` must fail closed if disabled/incomplete and must never fall back to live credentials.
- Do not read, print, persist, or commit secret values from `/Users/mgh3326/services/auto_trader/shared/.env.kis-mock`.
- Token/cache namespaces for mock must be distinct from live where modified.
- `account_type="paper"` continues to mean DB simulation only, never KIS mock.
- No watch registration/order intent/broker side effects in test or smoke unless using explicit mocks/fakes.

## Suggested implementation scope

Implement a safe first slice that exposes unambiguous routing at MCP boundary and broker config layer without requiring production to load mock secrets yet.

### 1. Central account-mode helper

Add `app/mcp_server/tooling/account_modes.py` or equivalent with:

- enum/string constants: `db_simulated`, `kis_mock`, `kis_live`
- `normalize_account_mode(account_mode: str | None = None, account_type: str | None = None) -> AccountRouting`
- deprecated aliases:
  - `account_type="paper"`, `account_mode="paper"`, `account_mode="simulated"` => `db_simulated` + warning
  - `account_type="real"`, absent selector => `kis_live`
  - `account_mode="kis_mock"`, maybe `account_type="kis_mock"` => `kis_mock`
- conflict handling: incompatible `account_mode`/`account_type` should return/raise a clear validation error.
- return fields: `account_mode`, `is_db_simulated`, `is_kis_mock`, `is_kis_live`, `deprecated_alias_used`, `warnings`.

### 2. Settings/config

Add optional settings in `app/core/config.py` or existing settings module:

- `KIS_MOCK_ENABLED: bool = False`
- `KIS_MOCK_APP_KEY: str | None = None`
- `KIS_MOCK_APP_SECRET: str | None = None`
- `KIS_MOCK_ACCOUNT_NO: str | None = None`

Add a helper such as `validate_kis_mock_config()` returning missing names only, never values. `kis_mock` paths must fail closed when not enabled or missing values.

### 3. KIS token/account separation

Inspect existing `app/services/brokers/kis/*`, `app/services/kis_trading_service.py`, `app/services/orders/service.py`, and token manager. Where current KIS clients already accept `is_mock=True`, ensure MCP official mock routing passes `is_mock=True` and mock config only. If token key names are centralized, include account mode/mock in token key namespace to avoid live/mock token collision.

### 4. MCP surfaces to update

Prioritize these files:

- `app/mcp_server/tooling/order_execution.py`
- `app/mcp_server/tooling/orders_history.py`
- `app/mcp_server/tooling/orders_modify_cancel.py` if small/safe
- `app/mcp_server/tooling/portfolio_cash.py`
- `app/mcp_server/tooling/portfolio_holdings.py`
- `app/mcp_server/tooling/portfolio_registration.py` / `orders_registration.py` for schema docs if parameters are registered there
- `app/mcp_server/README.md`

Behavior:

- Add optional `account_mode` parameter while retaining `account_type` for backward compatibility.
- DB simulation path remains routed to `paper_*` handlers/services.
- `kis_mock` routes to KIS real broker code with `is_mock=True`, but fail closed if config disabled/incomplete.
- `kis_live` routes to existing live KIS path.
- Responses include `account_mode` and, for deprecated alias, `warnings`.
- Error messages should be explicit: e.g. `KIS mock account is disabled or missing required configuration: KIS_MOCK_ENABLED, KIS_MOCK_APP_KEY, ...` without values.

### 5. Tests

Add/extend focused tests, no real provider calls:

- New unit tests for account-mode normalization:
  - absent/default => `kis_live`
  - `account_type="paper"` => `db_simulated` with deprecation warning
  - `account_mode="db_simulated"` => DB simulation
  - `account_mode="kis_mock"` => official mock
  - conflicting selectors fail
- MCP order tests:
  - `account_type="paper"` still uses DB paper handler and not KIS.
  - `account_mode="kis_mock"` passes `is_mock=True` to patched/fake KIS order service and includes `account_mode` in response.
  - `account_mode="kis_mock"` with missing/disabled config fails before broker call.
  - default/live dry-run preview remains dry-run; no live side effect.
- Portfolio/cash/holdings tests:
  - DB simulation alias remains DB simulation.
  - KIS mock routes with `is_mock=True` under fakes and fails closed without config.
- Token/cache test if central token key touched.

Likely test files:

- `tests/test_mcp_order_tools.py`
- `tests/test_mcp_portfolio_tools.py` or related existing MCP portfolio tests
- `tests/test_kis_constants.py` / new `tests/test_kis_account_modes.py`

Commands:

```bash
uv sync --group test --group dev
uv run pytest tests/test_mcp_order_tools.py tests/test_mcp_portfolio_tools.py tests/test_kis_constants.py -q
uv run ruff check app tests
uv run python -m py_compile $(git ls-files 'app/**/*.py' 'tests/**/*.py')
```

Adjust test file list to actual filenames present in repository.

## Implementation cautions

- Do not silently rename public `account_type`; keep compatibility but mark it in response warnings/docs.
- Do not make `account_type="paper"` mean KIS mock.
- Do not load `.env.kis-mock` automatically in code unless project already has a safe operator-approved env loading convention. Prefer documenting that production must inject `KIS_MOCK_*` into the MCP runtime environment later.
- If official mock side-effect execution is technically enabled, tests and smoke must remain mocked/dry-run/fail-closed only.

## PR / review / smoke plan

1. Codex implements and commits.
2. Opus review checks:
   - account selector ambiguity eliminated
   - fail-closed KIS mock config
   - no live fallback
   - no secret leakage
   - tests mock all broker side effects
3. PR CI required checks pass.
4. Merge/deploy.
5. Production smoke, no secrets and no broker side effects:
   - verify `account_type="paper"`/`account_mode="db_simulated"` still returns DB simulation path marker or dry-run preview under simulation.
   - verify `account_mode="kis_mock"` fails closed if production runtime lacks `KIS_MOCK_ENABLED=true`/required vars, with only variable names in error.
   - verify default `place_order` remains `dry_run=True`; do not run `dry_run=False`.

