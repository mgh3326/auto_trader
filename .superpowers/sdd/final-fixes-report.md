# ROB-838 final whole-branch review fixes

Date: 2026-07-12

## Scope and invariants

This pass fixes the four Important findings from the final whole-branch review. The capture path remains read-only with respect to trading: it performs evidence reads and append-only snapshot/bundle persistence only. The persisted read path was not changed, so zero-recompute reads remain projection and integrity verification over stored JSON.

## Findings to fixes

### 1. Production crypto breadth diagnostic was swallowed

Root cause: `_build_altseason_fn()` caught every exception and returned `None`. `MarketEventsSnapshotCollector` therefore could not distinguish a valid absent payload from a provider failure and emitted a fresh market snapshot without `errors_json.altseason`.

Fix: the production adapter now passes the provider call through. `MarketEventsSnapshotCollector` remains the fail-open boundary: it catches the exception, preserves the exact normalized original diagnostic, retains the legacy market/events payload, omits only `altseason`, and marks freshness partial.

Regression: `test_production_registry_preserves_altseason_error_diagnostic` exercises the actual production registry wiring and asserts `RuntimeError: upbit down` survives in the market result.

### 2. Shared AsyncSession operations overlapped

Root cause: `AnalysisInputFrozenCollector.collect()` used one `asyncio.gather()` across four production collectors, full analysis, and decision history; decision-history symbols used another gather. These surfaces can share the request AsyncSession, which does not support concurrent operations.

Fix: the six section reads now execute serially in stable contract order, and per-symbol decision-history reads are serial. Section-local exception isolation and partial/unavailable behavior remain intact.

Regression: `test_frozen_collector_never_overlaps_shared_session_reads` instruments every injected read with an active-operation counter and proves the maximum concurrency is one.

### 3. Section completion timing and provenance were inaccurate

Root cause: every section received the orchestration-start timestamp. Analysis and decision history used that start time as fallback `as_of`, while collector section sources omitted their upstream `source_timestamps_json` and did not explain fallback timestamps.

Fix:

- Every section calls the injected clock after its own read completes and stores that value as `collected_at`.
- Analysis and decision-history fallback `as_of` equals their own completion time.
- Collector-backed sources include all upstream `source_timestamps_json` values.
- When upstream timestamp metadata is absent, `as_of` falls back to completion time and source includes `collection_completion_fallback: provider/domain as_of absent`.
- When upstream metadata exists, the collector result `as_of` is retained, including for unavailable results, and provenance identifies the upstream timestamp basis.

Regression: `test_sections_stamp_completion_time_and_preserve_timestamp_provenance` verifies six distinct ordered completion timestamps, upstream metadata retention, upstream `as_of` retention on an unavailable result, and explicit analysis/decision fallback provenance.

### 4. `create_new` uniqueness depended on one service instance

Root cause: bundle idempotency was derived from the fixed capture clock. `_last_capture_at` only made repeat calls unique within one `AnalysisBundleCaptureService`; two instances with the same clock resolved to the same persisted bundle.

Fix: each `create_new` ensure operation generates a UUID discriminator. The repository persists it as part of the bundle's unique `idempotency_key`. The discriminator is excluded from the frozen analysis document, so identical captured content still has the same canonical payload hash and can reuse the same immutable snapshot row while receiving a distinct bundle identity. Instance-local `_last_capture_at` was removed.

Regression: `test_two_capture_service_instances_with_fixed_clock_create_unique_bundles` constructs two services with the identical fixed clock and asserts different bundle UUIDs plus identical content hashes.

## TDD evidence

Initial RED command:

```text
uv run pytest \
  tests/services/action_report/snapshot_backed/test_collectors.py::test_production_registry_preserves_altseason_error_diagnostic \
  tests/services/analysis_snapshot_bundle/test_capture.py::test_two_capture_service_instances_with_fixed_clock_create_unique_bundles \
  tests/services/analysis_snapshot_bundle/test_capture.py::test_frozen_collector_never_overlaps_shared_session_reads \
  tests/services/analysis_snapshot_bundle/test_capture.py::test_sections_stamp_completion_time_and_preserve_timestamp_provenance -q
```

Observed RED: 4 failed. The failures were exactly: market freshness `fresh` instead of `partial`; equal bundle UUIDs; maximum active reads `5` instead of `1`; one section timestamp instead of six.

Initial GREEN: the same four-test command completed with `4 passed`.

Additional provenance RED: after extending the timing test to cover an unavailable collector result with upstream timestamp metadata, the stored `as_of` was completion time (`03:00:05Z`) instead of provider time (`03:00:00Z`). After allowing unavailable sections to retain an explicit upstream `as_of`, the test completed with `1 passed`.

## Final verification

Focused analysis, collector, policy, persistence, and MCP gate:

```text
uv run pytest tests/services/analysis_snapshot_bundle \
  tests/services/action_report/snapshot_backed/test_collectors.py \
  tests/services/investment_snapshots/test_policy.py \
  tests/services/investment_snapshots/test_bundle_ensure_service.py \
  tests/services/investment_snapshots/test_repository.py \
  tests/mcp_server/test_analysis_bundle_tools.py \
  tests/mcp_server/test_investment_snapshots_tools.py -q
```

Result: `161 passed, 2 warnings in 4.73s`. Both warnings are pre-existing Pydantic v2 class-config deprecations from `app/auth/schemas.py`.

Full static gate:

```text
make lint
```

Result:

- Ruff check: all checks passed.
- Ruff format check: 2522 files already formatted.
- ty with warnings as errors: all checks passed.

## Residual concerns

No new functional concern identified. Serialization intentionally trades capture latency for AsyncSession correctness. The UUID discriminator is audit-visible through the persisted bundle idempotency key but intentionally absent from canonical content, preserving content-addressed snapshot semantics.

---

## Follow-up final review fixes

### 5. Production service clock and upstream provenance

Root cause: `AnalysisBundleCaptureService` passed `lambda: captured_at` into `AnalysisInputFrozenCollector`, defeating the collector's completion-time stamping in real service wiring. Collector section provenance also omitted `result.source_kind` and only considered `source_timestamps_json`, even though production payloads carry timestamps such as `quote.as_of`, index `quote_asof`, portfolio `provenance.fetched_at`, and investor-flow `snapshot_date`.

Fix:

- The service passes the live `self._clock` into the frozen collector and separately passes the capture-start value for document identity.
- Each collector section stores its result source kind and preserves the full upstream `source_timestamps_json` list.
- Timestamp-shaped production payload leaves are copied verbatim into `payload_timestamp_metadata` with JSON paths.
- Parseable upstream datetimes drive section `as_of`; non-parseable upstream metadata remains preserved and avoids a false fallback note. Completion fallback is used only when neither upstream source nor payload timestamp metadata exists.

Regressions:

- `test_service_wiring_uses_live_clock_for_section_completion_times` proves all six persisted section completion timestamps differ through actual `AnalysisBundleCaptureService` wiring.
- `test_collector_source_preserves_kind_and_payload_timestamp_metadata` proves source kind, empty source timestamps, nested quote `as_of`, and the actual section `as_of` survive without recomputation.

### 6. ROB-287 shared market compatibility

Root cause: preserving the altseason error had also changed the shared `MarketEventsSnapshotCollector` freshness from legacy `fresh` to `partial`.

Fix: altseason failure retains exact `errors_json.altseason` and the legacy fresh status. `AnalysisInputFrozenCollector` independently maps any non-empty collector errors to a partial frozen section, so ROB-838 still exposes the degraded market-gate input.

Regressions:

- Both direct and production-registry market tests assert fresh status plus the exact altseason error.
- `test_market_error_marks_frozen_section_partial_even_when_collector_is_fresh` proves the analysis bundle remains partial with the original diagnostic.

### 7. Section-local timeout isolation

Root cause: serialized sections had no local timeout. The only guard was the 60-second outer frozen-collector timeout, so one blocked source canceled the entire collector and no other evidence was persisted.

Fix: every serialized section has an explicit local budget (30 seconds, with 60 seconds for full analysis). Timeout is caught at the section boundary and persisted as `TimeoutError: <section> collection timed out after <seconds>s`; collection then proceeds to later sections. The total local budget is 210 seconds. The outer `analysis_snapshot_bundle_v1` timeout is 225 seconds, leaving headroom inside the 240-second MCP budget.

Regression: `test_slow_section_times_out_locally_and_other_evidence_persists` reduces the portfolio timeout to 0.01 seconds, verifies a partial capture with the exact timeout diagnostic, and verifies quote/orderbook evidence still persists.

## Follow-up TDD evidence

Targeted RED command covered the production clock, provenance, frozen partial mapping, slow-section isolation, production registry, and outer policy timeout. Observed result: `4 failed, 2 passed`. Exact missing behaviors were one shared service timestamp instead of six, absent `source_kind`, no local timeout (`complete` instead of `partial`), and a 60-second outer timeout instead of 225 seconds.

The broader compatibility run then exposed the remaining legacy expectation directly: `test_market_collector_altseason_failure_is_soft` failed because production returned the restored `fresh` status while the test still expected `partial`. Updating that compatibility assertion completed the ROB-287 restoration.

Targeted GREEN result: `6 passed, 2 warnings`.

Final focused command:

```text
uv run pytest tests/services/analysis_snapshot_bundle \
  tests/services/action_report/snapshot_backed/test_collectors.py \
  tests/services/investment_snapshots/test_policy.py \
  tests/services/investment_snapshots/test_bundle_ensure_service.py \
  tests/services/investment_snapshots/test_repository.py \
  tests/mcp_server/test_analysis_bundle_tools.py \
  tests/mcp_server/test_investment_snapshots_tools.py -q
```

Final result: `166 passed, 2 warnings in 4.79s`. The warnings remain the pre-existing Pydantic class-config deprecations in `app/auth/schemas.py`.

Final static command: `make lint`.

Final result: Ruff passed, all 2522 files are formatted, and ty passed with warnings treated as errors.

## Follow-up residual concerns

No functional blocker remains. The timestamp extractor intentionally recognizes a narrow set of timestamp-shaped keys and preserves their exact values; new provider payload conventions will need an explicit key addition rather than heuristic conversion. The 15-second gap between summed local budgets and the outer guard is reserved for section transitions and persistence within the 240-second MCP limit.
