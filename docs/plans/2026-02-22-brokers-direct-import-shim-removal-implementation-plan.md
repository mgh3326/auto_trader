# Brokers Direct Import Shim Removal Implementation Plan

> **For Claude:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task.

**Goal:** Remove KIS/Upbit/Yahoo shim modules and migrate all code/tests/patch-targets to direct provider client imports under `app.services.brokers.<provider>.client` with zero legacy references in `app/`, `tests/`, `blog/`, `scripts/`.

**Architecture:** Apply a deterministic path migration in small batches (contracts -> app imports -> tests/patch strings -> blog/scripts -> shim deletion -> docs), keeping behavior unchanged. Enforce the migration with strengthened AST-based import contract tests plus global `rg` regression checks for banned paths.

**Tech Stack:** Python 3.13+, pytest, unittest.mock, monkeypatch, Ruff, ty, ripgrep.

---

### Task 1: Strengthen import contract guardrails first

**Files:**
- Modify: `tests/test_import_contracts.py`
- Reference: `app/services/__init__.py`, `app/services/kis.py`, `app/services/upbit.py`, `app/services/yahoo.py`, `app/integrations/kis/__init__.py`, `app/integrations/upbit/__init__.py`, `app/integrations/yahoo/__init__.py`
- Test: `tests/test_import_contracts.py`

**Step 1: Add failing tests for banned shim imports and package import pattern**

Add explicit banned module set and banned `from app.services import kis|upbit|yahoo` check across `app/`, `tests/`, `blog/`, `scripts/`.

```python
BANNED_MODULES = {
    "app.services.kis",
    "app.services.upbit",
    "app.services.yahoo",
    "app.integrations.kis",
    "app.integrations.upbit",
    "app.integrations.yahoo",
}

BANNED_SERVICE_IMPORT_NAMES = {"kis", "upbit", "yahoo"}
SCAN_DIRS = (ROOT / "app", ROOT / "tests", ROOT / "blog", ROOT / "scripts")
```

**Step 2: Run contract test and confirm failure before migration**

Run: `uv run pytest --no-cov tests/test_import_contracts.py -q`
Expected: FAIL with current legacy import usages.

**Step 3: Implement AST checks in the same test file**

Add helpers to detect:
- imports/re-exports to banned module names
- `from app.services import kis|upbit|yahoo`

**Step 4: Re-run contract test (still expected to fail until migration tasks)**

Run: `uv run pytest --no-cov tests/test_import_contracts.py -q`
Expected: FAIL (guardrail is active).

**Step 5: Commit guardrail-only change**

```bash
git add tests/test_import_contracts.py
git commit -m "test: enforce banned broker shim import contracts"
```

---

### Task 2: Migrate production/runtime imports to direct broker clients

**Files:**
- Modify: `app/jobs/daily_scan.py`
- Modify: `app/jobs/kis_trading.py`
- Modify: `app/mcp_server/tooling/analysis_tool_handlers.py`
- Modify: `app/mcp_server/tooling/market_data_indicators.py`
- Modify: `app/mcp_server/tooling/market_data_quotes.py`
- Modify: `app/mcp_server/tooling/order_execution.py`
- Modify: `app/mcp_server/tooling/orders_history.py`
- Modify: `app/mcp_server/tooling/orders_modify_cancel.py`
- Modify: `app/mcp_server/tooling/portfolio_cash.py`
- Modify: `app/mcp_server/tooling/portfolio_holdings.py`
- Modify: `app/routers/kis_domestic_trading.py`
- Modify: `app/routers/kis_overseas_trading.py`
- Modify: `app/routers/portfolio.py`
- Modify: `app/routers/symbol_settings.py`
- Modify: `app/routers/trading.py`
- Modify: `app/services/account/service.py`
- Modify: `app/services/kis_holdings_service.py`
- Modify: `app/services/kis_trading_service.py`
- Modify: `app/services/market_data/service.py`
- Modify: `app/services/merged_portfolio_service.py`
- Modify: `app/services/orders/service.py`
- Modify: `app/services/portfolio_overview_service.py`

**Step 1: Apply deterministic import replacement rules**

Use only exact path replacements:

```text
app.integrations.kis    -> app.services.brokers.kis.client
app.integrations.upbit  -> app.services.brokers.upbit.client
app.integrations.yahoo  -> app.services.brokers.yahoo.client
app.services.kis        -> app.services.brokers.kis.client
app.services.upbit      -> app.services.brokers.upbit.client
app.services.yahoo      -> app.services.brokers.yahoo.client
```

For module alias imports, use direct module path aliasing:

```python
import app.services.brokers.upbit.client as upbit_service
```

**Step 2: Run static check over `app/` only**

Run:

```bash
rg -n --hidden --glob '*.py' 'app\.(integrations|services)\.(kis|upbit|yahoo)|from\s+app\.services\s+import\s+.*\b(kis|upbit|yahoo)\b' app
```

Expected: 0 hits in `app/`.

**Step 3: Run targeted runtime regression tests**

Run:

```bash
uv run pytest --no-cov tests/test_mcp_server_tools.py -q
uv run pytest --no-cov tests/test_integration.py -q
```

Expected: PASS (or only pre-existing unrelated failures).

**Step 4: Commit runtime import migration**

```bash
git add app/jobs app/mcp_server/tooling app/routers app/services
git commit -m "refactor: migrate runtime broker imports to direct client modules"
```

---

### Task 3: Migrate tests and patch/monkeypatch target strings

**Files:**
- Modify: `tests/conftest.py`
- Modify: `tests/test_integration.py`
- Modify: `tests/test_kis_rankings.py`
- Modify: `tests/test_kis_toss_notification.py`
- Modify: `tests/test_kis_trading_service.py`
- Modify: `tests/test_mcp_server_tools.py`
- Modify: `tests/test_routers.py`
- Modify: `tests/test_services.py`
- Modify: `tests/test_settings.py`
- Modify: `tests/test_tasks.py`
- Modify: `tests/test_upbit_trading.py`

**Step 1: Replace direct imports in tests**

Convert all `from app.integrations.* import ...` and `from app.services.kis import ...` to `from app.services.brokers.<provider>.client import ...`.

**Step 2: Replace all patch/monkeypatch target strings**

Examples:

```python
@patch("app.integrations.kis.httpx.AsyncClient")
# ->
@patch("app.services.brokers.kis.client.httpx.AsyncClient")

monkeypatch.setattr("app.services.upbit.fetch_price", fake_fetch_price)
# ->
monkeypatch.setattr("app.services.brokers.upbit.client.fetch_price", fake_fetch_price)
```

Do not rewrite similarly named non-shim modules/targets:

- `app.services.upbit_symbol_universe_service.*` (valid, keep)
- `app.services.kis_websocket.*` (valid, keep)
- `app.jobs.kis_trading.*` patch targets (patching lookup module, keep)

**Step 3: Run static check over `tests/`**

Run:

```bash
rg -n --hidden --glob '*.py' 'app\.(integrations|services)\.(kis|upbit|yahoo)|from\s+app\.services\s+import\s+.*\b(kis|upbit|yahoo)\b' tests
```

Expected: 0 hits in `tests/`.

**Step 4: Run required test set**

Run:

```bash
uv run pytest --no-cov tests/test_import_contracts.py -q
uv run pytest --no-cov tests/test_services.py -q
uv run pytest --no-cov tests/test_integration.py -q
uv run pytest --no-cov tests/test_mcp_server_tools.py -q
uv run pytest --no-cov tests/test_settings.py -q
```

Expected: PASS (or only pre-existing unrelated failures).

**Step 5: Commit test migration**

```bash
git add tests
git commit -m "test: migrate provider import and patch targets to broker clients"
```

---

### Task 4: Migrate `blog/` and `scripts/` references included in regression scope

**Files:**
- Modify: `blog/test_kis_blog_simple.py`
- Modify: `blog/blog_upbit_web_trading.md`
- Optional Modify (only if matched): `scripts/**/*.py`

**Step 1: Update code and code-block references for old paths**

At minimum:
- `from app.services import kis` -> `import app.services.brokers.kis.client as kis`
- `patch('app.services.upbit...')` snippets -> `patch('app.services.brokers.upbit.client...')`

**Step 2: Run scope check for `blog/` and `scripts/`**

Run:

```bash
rg -n --hidden 'app\.(integrations|services)\.(kis|upbit|yahoo)|from\s+app\.services\s+import\s+.*\b(kis|upbit|yahoo)\b' blog scripts
```

Expected: 0 hits.

**Step 3: Commit docs/script scope migration**

```bash
git add blog scripts
git commit -m "docs: update broker import examples to direct client paths"
```

---

### Task 5: Remove shim files and package re-exports

**Files:**
- Delete: `app/services/kis.py`
- Delete: `app/services/upbit.py`
- Delete: `app/services/yahoo.py`
- Delete: `app/integrations/kis/__init__.py`
- Delete: `app/integrations/upbit/__init__.py`
- Delete: `app/integrations/yahoo/__init__.py`
- Modify: `app/services/__init__.py`

**Step 1: Delete all 6 shim files**

Remove files exactly as listed.

**Step 2: Clean package-level re-exports**

In `app/services/__init__.py`, remove re-exports:

```python
from . import kis as kis
from . import upbit as upbit
from . import yahoo as yahoo
```

Keep unrelated exports intact.

**Step 3: Run import regression check over full scope**

Run:

```bash
rg -n --hidden --glob '*.py' 'app\.(integrations|services)\.(kis|upbit|yahoo)|from\s+app\.services\s+import\s+.*\b(kis|upbit|yahoo)\b' app tests blog scripts
```

Expected: 0 hits.

**Step 4: Commit shim removal**

```bash
git add app/services/__init__.py app/integrations app/services
git commit -m "refactor: remove provider shim modules and service re-exports"
```

---

### Task 6: Update MCP runtime docs path references

**Files:**
- Modify: `app/mcp_server/README.md`

**Step 1: Replace old provider path references in MCP docs**

Examples:

```text
app.services.yahoo
app.services.yahoo.fetch_ohlcv
```

to:

```text
app.services.brokers.yahoo.client
app.services.brokers.yahoo.client.fetch_ohlcv
```

**Step 2: Validate no old provider paths remain in MCP README**

Run:

```bash
rg -n 'app\.(integrations|services)\.(kis|upbit|yahoo)|from\s+app\.services\s+import\s+.*\b(kis|upbit|yahoo)\b' app/mcp_server/README.md
```

Expected: 0 hits.

**Step 3: Commit docs update**

```bash
git add app/mcp_server/README.md
git commit -m "docs: align MCP README with direct broker client import paths"
```

---

### Task 7: Final verification and release gate

**Files:**
- Verify only (no new edits unless failures found)

**Step 1: Run full banned-path scan in requested scope**

```bash
rg -n --hidden 'app\.(integrations|services)\.(kis|upbit|yahoo)|from\s+app\.services\s+import\s+.*\b(kis|upbit|yahoo)\b' app tests blog scripts
```

Expected: no output.

**Step 2: Run required regression tests**

```bash
uv run pytest --no-cov tests/test_import_contracts.py -q
uv run pytest --no-cov tests/test_services.py -q
uv run pytest --no-cov tests/test_integration.py -q
uv run pytest --no-cov tests/test_mcp_server_tools.py -q
uv run pytest --no-cov tests/test_settings.py -q
```

Expected: PASS.

**Step 3: Run quality gates**

```bash
uv run ruff check app tests
uv run ty check app
```

Expected: PASS.

**Step 4: Record completion evidence**

Capture command outputs (or failure diffs) in PR/body notes.

**Step 5: Final commit for any verification-driven fixes**

```bash
git add -A
git commit -m "chore: finalize broker direct-import shim removal verification fixes"
```
