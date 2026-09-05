# Decision-table validate runbook

## 1. Scope and safety boundary

`decision_table_validate(table, market)` is a pure MCP validation tool. It has
no database, network, broker, proposal, or order side effect. A response with
one or more `severity="block"` violations has `valid=false` and must not be
passed to an execution consumer.

## 2. Operator use

The v1.1 enum intentionally excludes `binance_demo` and
`binance_futures_demo` account modes. This is a KR decision-table contract and
there is no crypto decision-table variant; such input therefore fails closed as
`invalid_enum_value`. A future crypto contract may add them in its own PR
without changing the ROB-285 Binance audit guard.

Submit the complete prep envelope, including
`schema_version="kr-nxt-decision-table/v1.1"`, `market`, `decision_table`,
and the SHA-256 `decision_table_hash`; the tool hashes only the nested
`decision_table` object. Its `policy` response is the current
`trading_policy_service.policy_version_stamp()`. For v1.1 rungs,
`recomputed.rows` reports the deterministic lower price boundary
`price_min` and `qty`; both bounds, the declared tick, and KRX's reused tick
grid are separately checked.

The tool reports these rules: `table_not_object`, `missing_decision_table`,
`schema_version_mismatch`, `decision_table_hash_mismatch`, `non_finite_number`,
`duplicate_scenario_id`, `unsupported_table_shape`, `unknown_top_level_key`,
`extensions_entry_absent`, `invalid_condition_operator`,
`condition_missing_source`, `condition_missing_max_age_seconds`,
`invalid_enum_value`, `price_qty_not_machine_recomputable`,
`unsupported_rungs_encoding`, `rungs_field_not_integer`,
`rungs_price_bounds_inverted`, `rungs_price_not_tick_aligned`,
`rungs_missing_field`, `price_recompute_mismatch`,
`qty_recompute_mismatch`, `tick_grid_violation`, `sizing_band_violation`,
`deep_limit_violation`, `loss_guard_violation`, `below_min_order_amount`,
`same_day_chain_or_opposite_order`, `sector_concentration`, and
`invalid_parent_correlation_id`. v1 is accepted only with the advisory
`schema_version_deprecated_v1` until 2026-09-12; any other schema version
gets the blocking `schema_version_mismatch`.

`unknown_top_level_key`, `extensions_entry_absent`, and
`sector_concentration` are advisory; every other listed rule blocks. Each
violation contains the detected table shape and a link
to the [canonical v1.1 shape](../specs/mcp-session-tools-v1.md#canonical-decision-table-shape-v11).

To correct a table, use row-object rows and make every `action.rungs` value an
array of objects with `rung`, integer `price_min`, `price_max`, `qty`, and
`tick` (and optional text `formula`). Do not use prose, a scalar rung object,
or `prices`/other parallel arrays. Keep price bounds ordered and both prices
on the declared and KRX tick grids. List every top-level extension in
`decision_table.extensions`; remove a stale list entry or add the named field.
Then recompute `decision_table_hash` from the nested object. Correct duplicate
scenario IDs, stale/missing condition sources, enum values, undersized/deep buy
rows, loss-selling rows, and opposite same-account rows before resubmission. A
sector concentration result is recorded for operator review but does not make an
otherwise valid table invalid.

After a valid result, the operator uses `recomputed.hash` to perform the
required three-way procedural comparison: report-header hash,
`analysis_artifact_save` payload hash/object, and audit-evidence hash/object
must all be the same table. This tool does not access artifact storage or compare
those external records.

## 3. Applying a validated table (ROB-1349)

이 도구는 원자적이지 않다. 부분 적용은 정상 상태이며 재호출로 완주한다. 텔레그램 승인 카드는 되돌릴 수 없다.

`decision_table_apply(artifact_id, table_hash, dry_run=true, confirm=false)` is
a default-profile helmsman/navigator persistence coordinator, not a broker
tool. It creates only proposals, watches, forecasts, and one session-context
summary through their existing writers. It is deliberately absent from
read-only, auto-spawned closed-world, and external BrokerAdapter profiles. A
proposal writer can independently commit and then perform post-commit Telegram
work, so this tool must never promise a cross-writer rollback.

Before any write it performs this fail-closed sequence:

1. Fetch the artifact with `analysis_artifact_get`; a missing artifact returns
   `artifact_not_found`.
2. Require a decision-table envelope in its payload; otherwise it returns
   `not_a_decision_table`.
3. Require the argument hash, payload `decision_table_hash`, and canonical
   recomputation to match; otherwise it returns `table_hash_mismatch` with all
   three values.
4. Re-run `decision_table_validate(payload, market)`; a non-valid result returns
   `table_invalid` and its violations unchanged.
5. For real application require literal `confirm=true`; otherwise return
   `confirm_required`.
6. Read the newest apply record for the date. An exact completed
   `(parent_artifact_uuid, table_hash)` match returns `already_applied=true`
   without invoking any writer.

The prep artifact is immutable. The resume state is a separate analysis
artifact with `correlation_id="kr-nxt-apply-<YYYY-MM-DD>"` and this payload:

```json
{
  "schema": "kr-nxt-apply-record/v1",
  "parent_artifact_uuid": "...",
  "table_hash": "...",
  "rows": {
    "scenario-id": {"proposal_id": "...", "at": "..."}
  },
  "complete": false,
  "at": "..."
}
```

`rows` may instead hold `watch_id` or `forecast_id`. The tool lists metadata
for that correlation ID newest-first, gets the newest payload, and resumes only
when both parent UUID and hash match. A changed hash is a new table and starts
with no row markers. After every successful row it updates the separate record;
failed rows remain unmarked, while later rows continue in original table order.

The v1.1 action keeps its machine-validated proposal fields and rungs. The
apply discriminator is `action.apply_kind` (`proposal`, `watch`, or
`forecast`; `kind`, `action_type`, and `type` are compatibility aliases).
Watch and forecast rows place their target-writer input in `action.watch` or
`action.forecast` respectively (`watch_config` and `forecast_config` are
compatibility aliases). This keeps the validator as the sole admission gate
while making the selected existing persistence writer explicit.

Operators should first use the default `dry_run=true` and review the row
statuses. For an accepted table call again with `dry_run=false, confirm=true`.
If any row reports `failed` or `complete=false`, correct only the external
writer problem and repeat the identical artifact ID and table hash: durable
markers cause completed rows to be reported as `skipped`, and only unmarked
rows are attempted again. Do not edit or resave the prep artifact to force a
retry.
