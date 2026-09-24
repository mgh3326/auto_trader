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
`one_share_exception_rung_limit`, `deep_limit_violation`,
`loss_guard_violation`, `below_min_order_amount`,
`same_day_chain_or_opposite_order`, `sector_concentration`, and
`invalid_parent_correlation_id`. v1 is accepted only with the advisory
`schema_version_deprecated_v1` until 2026-09-12; any other schema version
gets the blocking `schema_version_mismatch`.

`unknown_top_level_key`, `extensions_entry_absent`, and
`sector_concentration` are advisory; every other listed rule blocks.

**KR one-share exception (§664).** `sizing_band_violation` checks each buy
rung's `price_min × qty` against `buy.per_symbol_notional_krw_range`
([200,000, 400,000]). Its `one_share_exception` admits an over-band KR buy
rung only when **all** hold:

1. `qty == 1`, the single share (`price_min`) exceeds 400,000, and
   `max(price_min, price_max) <= absolute_ceiling_krw` (10,000,000,
   inclusive; an unreadable or overflowing `price_max` is judged above it).
2. The row's `symbols` list is exactly one canonical KRX code (six ASCII
   `[0-9A-Z]`, no padding) that is not a cash-parking allowlist symbol
   (459580/357870 — their raised parking per-order cap would otherwise
   auto-approve a one-share buy above 2,000,000).
3. The row stays inside the **strict no-free-text grammar**
   (`app/services/decision_table_validate/one_share_exception.py`). Every
   string the row carries is a closed enum or an exact template; every other
   value is a typed number or bool:
   - no non-ASCII and no Unicode control/format (Cc/Cf, e.g. zero-width)
     character anywhere in the row, keys included;
   - row keys ⊆ `scenario_id, priority, symbols, conditions, action,
     invalidation, sector_concentration`; `priority` is an int;
   - `scenario_id` is exactly `one-share-entry-<symbol>` or
     `one-share-entry-<symbol>-<1..3 digits>`;
   - `invalidation` is absent or `[]`;
   - `sector_concentration` is absent or a dict with numeric values under
     `projected_pct`, `projected_percent`, `current_pct`, `cap_pct` only;
   - the action is `proposal_action: place`, `side: buy`, `order_type: limit`,
     `account_mode` ∈ `kis_live, toss_live, kis_mock, kiwoom_mock`, with optional
     `time_in_force: DAY`, `apply_kind: proposal`, positive numeric
     `reference_price` / `minimum_order_amount`, and `required_thesis_fields`
     ⊆ `scenario_id, decision_table_hash, policy_version` (no duplicates);
   - rungs carry exactly the integer keys `rung, price_min, price_max, qty,
     tick` (no `formula`);
   - every condition carries exactly `metric, source, operator, value,
     max_age_seconds` (`max_age_seconds` an int in [1, 86400]);
   - **exactly one** condition is the flat proof: `metric: position_quantity`,
     `operator: eq`, numeric `value: 0`, and `source` **equal to**
     `get_holdings.accounts[<action.account_mode>].positions[<symbol>].quantity`;
   - every other condition's `(metric, source)` **equals** one template:

     | metric | exact `source` | operators | value |
     |---|---|---|---|
     | `live_price_band` | `get_quote(symbol,market='kr').price` | `between` | numeric range `{min,max}_{inclusive,exclusive}` |
     | `krx_previous_close` | `get_quote(symbol,market='kr').previous_close` | `eq lt lte gt gte` | number |
     | `nxt_tradable` | `get_quote(symbol,market='kr').nxt_tradable` | `eq` | bool |
     | `premarket_session_change_pct` | `(get_quote(symbol,market='kr').price / get_quote(symbol,market='kr').previous_close - 1) * 100` | `lt lte gt gte` | number |
     | `rsi_14_last_completed_daily_bar` | `analyze_stock_batch(symbol,quick=false).indicators.rsi.14` | `lt lte gt gte` | number |
     | `fresh_support_s1_price` | `analyze_stock_batch(symbol,quick=false).support_resistance.supports[0].price` | `eq lt lte gt gte` | number |
     | `fresh_resistance_r1_price` | `analyze_stock_batch(symbol,quick=false).support_resistance.resistances[0].price` | `eq lt lte gt gte` | number |
     | `toss_open_orders_count_same_symbol` | `toss_get_order_history(status="open").orders[symbol==sym].length` | `eq` | int |
     | `kis_open_orders_count_same_symbol` | `kis_live_get_order_history(status="pending",market="kr").orders[symbol==sym].length` | `eq` | int |

   Numbers are JSON numbers, never strings. A new metric or spelling needs a
   code change to `_MARKET_CONDITIONS` before it can appear on an exception
   row.

**Product cost:** exception rows cannot carry prose invalidation, rung
formulas, a matched tier, or any condition outside the template table.
**Residual limit:** a table that *only lies* — it declares a held symbol flat
and says nothing else — is not detectable by a pure validator.

Silence about holdings is **not** a new entry. A denied rung keeps
`sizing_band_violation` with the denial reason in `expected`
(`non_ascii_or_control_character`, `row_field_not_in_grammar`,
`scenario_id_not_template`, `invalidation_not_empty`,
`sector_concentration_not_numeric_closed`, `action_not_in_grammar`,
`rung_not_in_grammar`, `thesis_field_not_in_enum`, `condition_not_a_template`,
`no_bound_flat_position_condition`, `cash_parking_symbol`, …). Once any rung of a symbol
uses the exception, that symbol may have at most `max_deep_rungs` (1) buy
rungs across the whole table — every row and account, counted on an
NFKC/strip/upper-normalized key — else each involved row gets the blocking
`one_share_exception_rung_limit`. Sells never consult the exception; the US
band's exception is not honoured by this validator (unchanged). Auto-approval
is untouched: a one-share order above the KR per-order cap (2,000,000) is
demoted to a human card as `per_order_cap_exceeded`. Limit: the validator is
pure and cannot read holdings, so it trusts the declared flat proof.
`decision_table_apply` does not evaluate row conditions either; the live check
is the helmsman session's condition match before `apply(dry_run=false)` (spec
§3 workflow). Each
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
tool. It creates only proposals, watches, and one session-context summary
through their existing writers. Apply v1 has proposal and watch
writers only: a forecast row is skipped, and the session must call
`forecast_save` directly if it has a real target and probability. It is
deliberately absent from
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
6. Derive the table-scoped apply-record key
   `kr-nxt-apply-<YYYY-MM-DD>:<parent_artifact_uuid>:<table_hash>` and read
   that record. An exact completed `(parent_artifact_uuid, table_hash)` match
   returns `already_applied=true` without invoking any writer. A pre-scope,
   date-only record is read only as a legacy fallback when its payload proves
   that same exact identity.

The prep artifact is immutable. The resume state is a separate analysis
artifact with
`correlation_id="kr-nxt-apply-<YYYY-MM-DD>:<parent_artifact_uuid>:<table_hash>"`
and this payload:

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

`rows` may instead hold `watch_id`. The tool lists metadata for that exact,
table-scoped correlation ID and resumes only when both parent UUID and hash
match. A changed hash is a new table and starts with no row markers. After every
successful row it updates the separate record; failed rows remain unmarked,
while later rows continue in original table order.

### Multiple tables on one trading date

Two prep artifacts on the same trading date always receive different apply
records because both the prep artifact UUID and the exact table hash are part of
the correlation key. Applying table B cannot overwrite or hide table A's row
markers; replaying A reads A's own record and produces no duplicate proposal or
Telegram approval card. The old date-only key is only a read-only compatibility
fallback for a payload that proves the same table identity, and new saves never
write it.

`action.apply_kind` is the v1.1 additive canonical row discriminator:
`proposal`, `watch`, or `forecast`; `schema_version` remains v1.1. Omission
means proposal for legacy rows. A canonical `action.watch` or
`action.forecast` payload without `apply_kind` is instead
`ambiguous_apply_kind`: apply does not invoke a row writer and never silently
turns that intent into a proposal. v1.2 mandatory-discriminator work is
separate.

The only canonical auxiliary payloads are
`action.watch{symbol,watch_condition,valid_until,trigger_checklist?}` (where
`watch_condition` uses the existing playbook schema) and
`action.forecast{symbol,direction,horizon,decision_bucket,review_date}`.
`kind`, `action_type`, `type`, `watch_config`, and `forecast_config` are not
accepted aliases. There are two apply-v1 row writers—proposal and watch.
`session_context_append` is not a row kind: it records exactly one summary per
apply invocation after the rows have been processed.

`forecast_save` additionally requires an `instrument_type`, typed
`forecast_target`, and `probability`. The v1.1 additive forecast payload has
no ratified deterministic mapping for the target or probability. ESC-4
therefore excludes forecast from apply v1: an `apply_kind=forecast` row is
`skipped` with `reason="unsupported_apply_kind"`, its `scenario_id`, and a
hint to call `forecast_save` directly from the current session. It never calls
that writer, creates no row marker, does not count as an unmarked resume
remainder, and does not block `complete=true` for supported rows.
`invalid_row_mapping` remains reserved for malformed proposal/watch mappings,
not for this unsupported apply kind.

Operators should first use the default `dry_run=true` and review the row
statuses. For an accepted table call again with `dry_run=false, confirm=true`.
If any supported row reports `failed` or `complete=false`, correct only the
external writer problem and repeat the identical artifact ID and table hash:
durable markers cause completed rows to be reported as `skipped`, and only
unmarked supported rows are attempted again. An
`unsupported_apply_kind` forecast skip is not a retry target; create it with a
direct `forecast_save` session call only when the session has a genuine typed
target and probability. Do not edit or resave the prep artifact to force a
retry.
