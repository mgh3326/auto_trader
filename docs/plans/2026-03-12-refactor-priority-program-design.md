# Refactor Priority Program Design

Date: 2026-03-12
Status: validated from repo exploration, background agents, and existing plans

## 1. Background

- The original ranking was checked against runtime code, tests, recent git churn, existing design docs, and external refactor patterns.
- Priority is based on duplication drift, runtime impact, contract blast radius, and the availability of safe characterization seams.
- The goal is to reduce maintenance cost without changing public MCP payloads or KIS automation result shapes.

## 2. Confirmed guardrails

- Preserve MCP response fields and warning/diagnostic shapes currently asserted around `app/mcp_server/tooling/analysis_screen_core.py` and `app/mcp_server/tooling/analysis_recommend.py`.
- Preserve KIS automation payloads: top-level `status`, per-stock `results`, and per-step `result` dict shapes returned from `app/jobs/kis_trading.py`.
- Keep public handler names and signatures stable; internal extraction is allowed behind existing facades.
- Add typed internal helpers and request/context objects only when they preserve backward-compatible dict payloads.

## 3. Approach options

### Option A: largest-file-first

- Start with `app/mcp_server/tooling/analysis_screen_core.py` because it is the largest hotspot.
- Pros: maximum raw LOC reduction early.
- Cons: highest public-contract density, widest MCP test blast radius, and active ongoing churn in the same area.

### Option B: public-contract-first

- Start with MCP because its contract is the most visible and already documented.
- Pros: aligns with existing MCP facade work and keeps the most user-facing area under active control.
- Cons: spends the first wave on the riskiest contract surface instead of the clearest duplication payoff.

### Option C: duplication-drift-first (selected)

- Start where duplicated runtime logic has already drifted behaviorally and where tests are local enough to characterize safely.
- Pros: highest immediate ROI, lowest public API risk, and strong local regression seams.
- Cons: does not reduce the largest file in the repo first.

## 4. Selected design

### 4.1 Wave 1: KIS automation cluster

Treat `app/jobs/kis_trading.py` and `tests/test_kis_tasks.py` as a single refactor cluster, not two separate priorities.

Why this wave goes first:

- Domestic and overseas automation loops are near-duplicates with behavior drift around holdings refresh and sell cancellation.
- `tests/test_kis_tasks.py` repeats `DummyKIS`, `MockManualService`, and notifier scaffolding across many scenarios, so production and test cleanup should move together.
- `app/services/kis_trading_contracts.py` already provides an internal result seam via `OrderStepResult` and `to_payload()`, which lowers extraction risk.

Target structure:

- Keep public task entrypoints in `app/jobs/kis_trading.py`.
- Extract a shared automation runner module for common step orchestration.
- Move market-specific behavior into domestic and overseas adapters or step hooks.
- Extract reusable test scenario factories into `tests/_kis_tasks_support.py`.

### 4.2 Wave 2: MCP recommendation pipeline

Refactor `app/mcp_server/tooling/analysis_recommend.py` before `analysis_screen_core.py`.

Why it comes before screening core:

- It has real duplication in budget allocation and a mutable request dict threaded through multiple phases.
- Its contract surface is smaller than screening core, even though warnings, diagnostics, and fallback flags are still asserted.
- The file already contains meaningful phase seams (`_prepare_recommend_request`, `_collect_*`, `_score_and_allocate`, `_build_recommend_response`) that can be tightened.

Target structure:

- Freeze request preparation into an immutable context object.
- Merge duplicated allocation logic behind one allocator strategy.
- Keep `recommend_stocks_impl(...)` as the stable orchestrator entrypoint.

### 4.3 Wave 3: MCP screening core

Continue the existing facade-first MCP plan instead of reopening the whole surface.

Why it is third, not first:

- `analysis_screen_core.py` is large, but it has a dense and already-stabilized contract surface.
- Existing docs in `docs/plans/2026-03-10-mcp-analysis-path-refactor-design.md` and `docs/plans/2026-03-10-mcp-analysis-path-refactor-implementation-plan.md` already define a safe decomposition order.
- The right move is to continue that plan, not replace it.

Target structure:

- Keep `screen_stocks_unified(...)` stable.
- Extract pure request normalization, filter validation, source routing, and response-building helpers behind the current facade.
- Defer test-file splitting until ownership and patch surfaces are stable.

### 4.4 Wave 4: analyzer orchestration

Refactor `app/analysis/analyzer.py` only after adding characterization around retry, validation, and persistence behavior.

Target structure:

- Analyzer becomes an orchestrator only.
- Split prompt assembly, model execution/retry, response validation, and persistence into dedicated collaborators.

### 4.5 Wave 5: prompt composition

Refactor `app/analysis/prompt.py` last, after the surrounding analyzer boundaries exist.

Why it is last:

- It is clearly mixed, but it is less runtime-critical than KIS and less contract-central than MCP.
- Current direct tests are thin, so snapshot coverage should be added before structural cleanup.

## 5. Confirmed ranking

1. `app/jobs/kis_trading.py` + `tests/test_kis_tasks.py` (single first-wave cluster)
2. `app/mcp_server/tooling/analysis_recommend.py`
3. `app/mcp_server/tooling/analysis_screen_core.py`
4. `app/analysis/analyzer.py`
5. `app/analysis/prompt.py`

## 6. Architecture notes per wave

### KIS automation

- Use a shared runner plus market adapters/step hooks instead of two full market loops.
- Preserve dict payloads at the boundary; use `OrderStepResult` internally.
- Move fixture duplication into test support helpers before or alongside runner extraction.

### MCP recommendation

- Replace the mutable request dict with a typed or frozen request context.
- Keep collectors, fallback, scoring, and response building as explicit phases.
- Preserve `warnings`, `diagnostics`, `fallback_applied`, and recommendation entry keys.

### MCP screening

- Continue facade-first extraction.
- Preserve `filters_applied`, `warnings`, `meta.rsi_enrichment`, and current warning text.
- Prefer pure helper extraction over broader behavioral rewrites.

### Analyzer and prompt

- Add characterization before extraction.
- Use a prompt composer + model runner + response validator + repository split.
- Keep existing persistence targets and payload semantics stable during the first structural pass.

## 7. Test strategy

### KIS

- Freeze step order, step result shapes, pending-order cancellation behavior, holdings refresh behavior, manual-holdings branching, and notifier calls.
- Extract shared fixtures only after those regressions exist.

### MCP

- Keep current contract suites green while extracting helpers.
- Add seam tests that prove stable public entrypoints still flow through the new internal boundaries.

### Analyzer and prompt

- Add prompt snapshot tests for representative KR/US payloads.
- Add analyzer tests that isolate retry, validation, and persistence calls.

## 8. Non-goals

- No public contract rename or field reshaping in this program.
- No business-rule redesign for KIS, tvscreener, Upbit, or Yahoo logic.
- No test-file split ahead of seam stabilization in MCP.

## 9. Risks to watch

- KIS has lower public API risk than MCP, but higher business-risk sensitivity; preserve behavior before improving elegance.
- MCP patch surfaces act like contracts in tests; handler imports and facade ownership matter as much as runtime payloads.
- Analyzer/prompt cleanup is not a quick win unless characterization lands first.
