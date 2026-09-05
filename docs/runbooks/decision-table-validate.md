# Decision-table validate runbook

## 1. Scope and safety boundary

`decision_table_validate(table, market)` is a pure MCP validation tool. It has
no database, network, broker, proposal, or order side effect. A response with
one or more `severity="block"` violations has `valid=false` and must not be
passed to an execution consumer.

## 2. Operator use

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

## 3. Reserved for ROB-1349
