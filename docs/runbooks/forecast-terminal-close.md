# Terminal-close forecast runbook (ROB-1038)

ROB-1038 prevents a closing-price claim from being scored as a window touch.
It adds no database migration: the contract lives in the existing
`forecast_target` JSONB and successful evidence lives in `resolution_detail`.

## Versioned target contracts

New window-touch forecasts must use:

```json
{
  "kind": "price_target",
  "direction": "at_or_above",
  "target_price": 130.0,
  "outcome_rule_version": "window-touch-v1-high-gte-low-lte"
}
```

The behavior is unchanged: `at_or_above` is `max(high) >= target`, and
`at_or_below` is `min(low) <= target`.

A terminal-close claim must use a new forecast ID and this target shape:

```json
{
  "kind": "terminal_close",
  "direction": "up",
  "target_price": 130.0,
  "outcome_rule_version": "terminal-close-v1-up-gte-down-lt"
}
```

The V1 terminal events are complementary:

```text
up   = review-date close >= target
down = review-date close <  target
```

Equality belongs only to `up`. Terminal resolution reads only `close`; it never
uses `high`, `low`, `adj_close`, or an extended-hours source.

## Legacy quarantine

A versionless `price_target` does not say whether its author intended a window
touch or a terminal close. The resolver therefore leaves it open and returns
`quarantined_legacy_price_target` before any candle lookup or backfill.

The due batch obtains quarantined rows separately, so they remain visible but
do not consume the normal due limit. Unknown typed rule versions return
`quarantined_invalid_target` and also remain open.

Do not edit or reinterpret a legacy row. If its original event was a terminal
close, preregister a new typed `terminal_close` forecast with a new
`forecast_id`; retain the legacy row in quarantine. ROB-1038 does not provide
touch attestation, automatic supersession, or durable cross-row links. Those
invariants belong to ROB-1041.

## Candle acceptance boundary

The resolver first requires the review date to be a regular exchange session
and requires the exchange-calendar final-session gate to have passed. It then
requires exactly one review-date daily row with a positive finite `close`.

The reduced ROB-1038 source allowlist is:

| Source label | Recorded close basis |
|---|---|
| `kis` | provider-adjusted |
| `toss`, `toss_fallback` | provider-adjusted |
| `yahoo`, `yahoo_fallback` | raw |

Missing, stale-only, duplicate, untrusted/extended, invalid-close, holiday, or
not-yet-final-session data leaves the forecast open. The result status is,
respectively, `unresolved_no_review_candle`, `unresolved_stale_data`,
`unresolved_ambiguous_review_candle`, `unresolved_untrusted_source`,
`unresolved_invalid_close`, or `unresolved_session_not_final`. Successful
`resolution_detail` records the target kind, rule version, direction, target,
comparison operator, review/source date, source timestamp, source
label/partition, source price, `source_price_field="close"`, and the
source-basis label.

This PR deliberately does not add row-level upstream finality, source identity,
ingestion-version, or actor-bound provenance. The allowlisted source label plus
calendar gate is the ROB-1038 boundary; ROB-1043 owns stronger upstream
finality and evidence binding.

## Corporate actions are unsupported

ROB-1038 does not apply split, reverse-split, or price-basis conversion factors.
`price_adjustment_policy`, `target_to_close_factor`, and
`adjustment_provenance` are rejected at save time. Existing stored terminal
targets carrying those fields fail closed during resolution.

Do not register or resolve a terminal target that needs corporate-action or
price-basis adjustment. ROB-1043 will add the authoritative corporate-action
evidence ledger and actor binding.

## Review and persist

For a genuinely read-only preview:

```text
forecast_resolve(
  forecast_id="...",
  dry_run=true,
  backfill_missing=false
)
```

`dry_run=true` prevents a forecast outcome write. The default
`backfill_missing=true` may still fetch and persist daily candles, so it is not
a fully read-only inspection mode.

After reviewing the target contract and selected close evidence, use
`dry_run=false` to persist. ROB-1038 does not provide preview/persist
fingerprint CAS, row locks, or stale-batch protection; ROB-1042 owns those
concurrency guarantees.

Forecast resolution does not mutate broker orders, watches, proposals, or
order intents.

## Deployment and follow-up

There is no schema migration and no deployment ordering beyond deploying the
application code. Rollback is an application rollback; no database downgrade
is needed.

- ROB-1041: claim immutability, DB transitions, and supersession invariants.
- ROB-1042: resolver concurrency, stale batch identity, and lock predicates.
- ROB-1043: upstream finality, corporate-action evidence, and actor binding.
