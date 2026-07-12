# Task 1 report: DTO contract, create-new persistence, and bundle policy

## Scope implemented

- Added the locked analysis bundle DTO contract in `app/schemas/analysis_snapshot_bundle.py`, including the six stable section names, payload/error validation, complete-section validation, and create/get response models.
- Extended `EnsureMode` with `create_new`.
- Registered `analysis_snapshot_bundle_v1` with a 180-second soft TTL, 300-second hard TTL, and one required `llm_input_frozen` kind using a 60-second collector timeout.
- Made `SnapshotBundleEnsureService.ensure()` bypass only the latest-bundle lookup for `create_new`; the existing `reuse_only` and `ensure_fresh` logic remains unchanged after that gate.
- Added schema contract tests and a persistence test proving a fresh prior bundle is ignored, a new bundle UUID is returned, and the collector runs exactly once.

## TDD evidence

### RED

Command:

```text
uv run pytest tests/services/analysis_snapshot_bundle/test_schemas.py tests/services/investment_snapshots/test_bundle_ensure_service.py -q
```

Observed before production changes:

```text
ERROR tests/services/analysis_snapshot_bundle/test_schemas.py
ModuleNotFoundError: No module named 'app.schemas.analysis_snapshot_bundle'
1 error in 2.46s
```

This was the expected contract-level failure: the new schema module did not exist.

### GREEN

The same command after the minimal implementation produced:

```text
.................                                                        [100%]
17 passed, 2 warnings in 9.69s
```

The two warnings are pre-existing Pydantic v2 deprecation warnings from `app/auth/schemas.py` and are outside Task 1.

## Static verification

Commands:

```text
git diff --check
uv run ruff check app/schemas/analysis_snapshot_bundle.py app/schemas/investment_snapshots_mcp.py app/services/investment_snapshots/policy.py app/services/action_report/common/snapshot_bundle.py tests/services/analysis_snapshot_bundle/test_schemas.py tests/services/investment_snapshots/test_bundle_ensure_service.py
uv run ruff format --check app/schemas/analysis_snapshot_bundle.py app/schemas/investment_snapshots_mcp.py app/services/investment_snapshots/policy.py app/services/action_report/common/snapshot_bundle.py tests/services/analysis_snapshot_bundle/test_schemas.py tests/services/investment_snapshots/test_bundle_ensure_service.py
```

Result: diff check clean, Ruff checks passed, and all six files were formatted.

## Self-review

- Compared DTO field names, types, literal values, defaults, validators, and error strings against the task brief; no deviations found.
- Confirmed the new policy has exactly one kind and that it is required.
- Confirmed the only service behavior change is conditional suppression of `find_latest_bundle()` for `create_new`.
- Confirmed the persistence test uses the exact requested `EnsureBundleRequest` values and assertions.
- No production behavior outside the requested mode/policy/schema surface was intentionally changed.

## Concerns

- None blocking. The targeted suite emits two unrelated pre-existing Pydantic deprecation warnings.
