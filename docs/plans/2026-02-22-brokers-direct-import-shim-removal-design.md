# Brokers Direct Import Shim Removal Design

## Summary

- Goal: remove provider shim modules and unify provider client imports to:
  - `app.services.brokers.kis.client`
  - `app.services.brokers.upbit.client`
  - `app.services.brokers.yahoo.client`
- Providers in scope: `kis`, `upbit`, `yahoo`
- Policy: breaking change is allowed, no backward compatibility shims retained.
- Behavior: no runtime logic/signature change; import paths only.

## Problem Statement

The codebase currently allows multiple import entrypoints for the same provider clients:

- `app.services.kis|upbit|yahoo` (shim)
- `app.integrations.kis|upbit|yahoo` (shim)
- `from app.services import kis|upbit|yahoo` (package re-export)

This increases maintenance cost and causes patch-target ambiguity in tests.

## Current-State Inventory (Exhaustive Scan)

Scan scope: `app/`, `tests/`, `blog/`, `scripts/` (`*.py`)

| Pattern | Hits | Files |
|---|---:|---:|
| `app.integrations.kis` | 103 | 24 |
| `app.integrations.upbit` | 6 | 5 |
| `app.integrations.yahoo` | 16 | 2 |
| `app.services.kis` | 10 | 7 |
| `app.services.upbit` | 17 | 8 |
| `app.services.yahoo` | 8 | 5 |
| `from app.services import kis\|upbit\|yahoo` | 4 | 4 |

High-concentration areas:

- `tests/test_services.py` (heavy `app.integrations.kis` + patch targets)
- MCP tooling: `app/mcp_server/tooling/order_execution.py`, `app/mcp_server/tooling/portfolio_cash.py`, `app/mcp_server/tooling/portfolio_holdings.py`
- Service layer: `app/services/account/service.py`, `app/services/market_data/service.py`, `app/services/orders/service.py`

## Public Interface Changes

### Removed import paths

- `app.services.kis`
- `app.services.upbit`
- `app.services.yahoo`
- `app.integrations.kis`
- `app.integrations.upbit`
- `app.integrations.yahoo`
- `from app.services import kis|upbit|yahoo`

### Standard import paths

- `app.services.brokers.kis.client`
- `app.services.brokers.upbit.client`
- `app.services.brokers.yahoo.client`

## File-Level Design

### 1) Delete shim modules

- `app/services/kis.py`
- `app/services/upbit.py`
- `app/services/yahoo.py`
- `app/integrations/kis/__init__.py`
- `app/integrations/upbit/__init__.py`
- `app/integrations/yahoo/__init__.py`

### 2) Remove package re-exports

- Edit `app/services/__init__.py`
- Remove `kis`, `upbit`, `yahoo` re-export imports.

### 3) Bulk import + patch-target migration

Mechanical mapping rules:

- `app.integrations.kis` -> `app.services.brokers.kis.client`
- `app.integrations.upbit` -> `app.services.brokers.upbit.client`
- `app.integrations.yahoo` -> `app.services.brokers.yahoo.client`
- `app.services.kis` -> `app.services.brokers.kis.client`
- `app.services.upbit` -> `app.services.brokers.upbit.client`
- `app.services.yahoo` -> `app.services.brokers.yahoo.client`

Apply to:

- Python import statements
- `patch("...")` target strings
- `monkeypatch.setattr("...")` target strings
- any module-path string constants used as patch targets

### 4) Import-contract guardrail strengthening

- Update `tests/test_import_contracts.py`:
  - Ban all 6 shim module paths.
  - Ban `from app.services import kis|upbit|yahoo` pattern.
  - Expand scan coverage to `app/`, `tests/`, `blog/`, `scripts/` with explicit allowlist exceptions only if truly needed.

### 5) Runtime doc update only

- Update `app/mcp_server/README.md` old module-path references to direct client paths.
- Do not modify historical plan docs under `docs/plans/*`.

## Test/Patch Migration Safety Rules

Critical rule for `unittest.mock.patch` and `monkeypatch`:

- Patch where the object is looked up by code-under-test, not where it originally came from.
- During this migration, the lookup namespace shifts to `app.services.brokers.<provider>.client` for all provider-client symbols.

Practical implication:

- Any stale patch target on removed shim paths can silently stop intercepting calls and produce misleading tests.
- All migrated patches should keep assertion coverage that proves the mock was actually used.

## Risks and Mitigations

1. Risk: stale string patch targets after import rewrite
- Mitigation: dedicated grep/AST checks for `patch(` and `monkeypatch.setattr(` strings before running tests.

2. Risk: partial migration in large test files (especially `tests/test_services.py`)
- Mitigation: perform deterministic codemod replacement, then targeted review of all remaining old-path hits.

3. Risk: accidental edits to unrelated `app.services.*` modules
- Mitigation: restrict replacements to exact path tokens above, not broad `app.services.` rewrites.

4. Risk: docs drift
- Mitigation: update only `app/mcp_server/README.md` as required by scope.

## Verification Plan

### Static regression checks

```bash
rg -n --hidden --glob '*.py' 'app\.(integrations|services)\.(kis|upbit|yahoo)|from\s+app\.services\s+import\s+.*\b(kis|upbit|yahoo)\b' app tests blog scripts
```

Expected: 0 hits.

### Required tests

```bash
uv run pytest --no-cov tests/test_import_contracts.py -q
uv run pytest --no-cov tests/test_services.py -q
uv run pytest --no-cov tests/test_integration.py -q
uv run pytest --no-cov tests/test_mcp_server_tools.py -q
uv run pytest --no-cov tests/test_settings.py -q
```

### Quality checks

```bash
uv run ruff check app tests
uv run ty check app
```

## Done Criteria

- 6 shim files deleted
- `app/services/__init__.py` re-export cleanup complete
- all shim imports/patch strings migrated to broker direct paths
- `tests/test_import_contracts.py` blocks regressions for all banned paths and package import pattern
- `app/mcp_server/README.md` references updated
- static checks and required tests pass

## References

- Python docs: "Where to patch" (`unittest.mock`)  
  https://docs.python.org/3/library/unittest.mock.html#where-to-patch
- Python docs: `unittest.mock` examples (`patch`)  
  https://docs.python.org/3/library/unittest.mock-examples.html
- pytest-mock usage patterns  
  https://github.com/pytest-dev/pytest-mock/blob/main/README.rst

### OSS enforcement precedents (import contract hardening)

- DataHub denylist import checker script  
  https://github.com/datahub-project/datahub/blob/8a223adea045e7196754258856cfd91c02418544/metadata-ingestion/src/datahub/testing/check_imports.py
- Apache Airflow AST-based allowed-import CI checker  
  https://github.com/apache/airflow/blob/e90d953e4419ac8713f5b66432351897e6b48834/scripts/ci/prek/check_cli_definition_imports.py
- Open edX import-linter boundary contract example  
  https://github.com/openedx/openedx-platform/blob/e5ebde83f25bfc51eb4fd62a80846a28cd316ba7/openedx/testing/importlinter/isolated_apps_contract.py
