# Terminal-close forecast runbook (ROB-1038)

Use this runbook only when the event is the official regular-session close on
the review date. Resolution semantics are explicit and never inferred from
prose, a symbol, or a forecast ID.

## Event contracts

- `price_target` is a window touch. Every new row must carry
  `outcome_rule_version="window-touch-v1-high-gte-low-lte"`.
  `at_or_above` uses `max(high) >= target`; `at_or_below` uses
  `min(low) <= target`.
- `terminal_close` uses exactly one final review-date regular-session `close`
  under `outcome_rule_version="terminal-close-v1-up-gte-down-lt"`.
  `up` is `close >= target`; its non-overlapping `down` complement is
  `close < target`.

Terminal resolution never uses a window extreme, `adj_close`, or an
extended-hours price. Free-form manual resolution is forbidden for any row
whose immutable original kind is `terminal_close`.

## Legacy `price_target` quarantine

A versionless legacy `price_target` contains too little information to
distinguish touch intent from a terminal-close forecast saved under the wrong
kind. It is quarantined before candle lookup in both single-ID and due-batch
resolution. Quarantined rows use a separate diagnostic selector and do not
consume the eligible due limit.

Start with a genuinely read-only inspection:

```text
forecast_resolve(
  forecast_id="<legacy-id>",
  dry_run=true,
  backfill_missing=false
)
```

The result is `quarantined_legacy_price_target` and includes the canonical
`target_hash`. Do not update the row based on prose or a heuristic. Choose one
of the following evidence-backed paths.

### Attest genuine window-touch intent

Replay the exact stored claim, add the touch rule version, require
`expected_target_version=0`, and provide:

```json
{
  "contract_version": "forecast-semantics-attestation-v1",
  "authority_type": "service",
  "actor_principal": "service:forecast-evidence-operator",
  "authentication_method": "mcp_bearer",
  "source_target_sha256": "<target_hash from quarantine preview>",
  "evidence_sha256": "<sha256 of retained review artifact>",
  "evidence_ref": "artifact://forecast-semantics/<artifact-id>",
  "reason": "operator verified the original event was a window touch",
  "attested_at": "2026-07-23T15:30:00+09:00"
}
```

The write creates an immutable claim/hash, sets target version 1, and stores
the attestation. It cannot change the threshold, direction, probability,
dates, attribution, or origin/evidence cutoff.

### Supersede terminal intent

Create a new ID with a typed `terminal_close` target and
`supersedes_forecast_id="<legacy-id>"`. Preserve the legacy claim fields and map
`at_or_above -> up` or `at_or_below -> down` without changing the threshold.
Provide the same evidence shape with
`contract_version="forecast-semantics-supersession-v1"`.

The transaction locks both rows and stores:

- new row `supersedes_forecast_id`;
- legacy row `superseded_by_forecast_id`;
- matching from/to IDs, source target hash, actor, time, reason, and semantics
  versions on both rows.

The legacy row becomes durably `superseded` and cannot resolve. Never create a
replacement terminal row without this link.

## Authenticated evidence boundary

Corporate-action promotion, legacy touch attestation, and terminal
supersession require an application-authenticated actor. For MCP writes:

```text
MCP_AUTH_TOKEN=<non-empty bearer token>
FORECAST_EVIDENCE_AUTHENTICATED_ACTOR_ID=service:forecast-evidence-operator
```

The payload's `actor_principal` must equal the configured principal and its
`authentication_method` must be `mcp_bearer`. Caller headers and forecast JSON
never establish identity. Without both the active bearer and configured
principal, evidence-bearing writes fail closed. Trusted in-process callers
must pass an `AuthenticatedForecastActor` with `service_identity`; they may
not reuse payload self-attestation.

## Immutable terminal claim and factor promotion

Preregister the claim before resolution when factor evidence is not ready:

```json
{
  "kind": "terminal_close",
  "direction": "up",
  "target_price": 30.56,
  "outcome_rule_version": "terminal-close-v1-up-gte-down-lt",
  "price_adjustment_policy": "unverified_fail_closed"
}
```

This row remains quarantined with `requires_adjustment_evidence`. Its kind,
instrument, symbol, direction, original target, probability/range,
start/review dates, horizon, creator/model/policy attribution, and
origin/evidence cutoff are immutable. An exact replay is idempotent. The only
target mutation is a compare-and-set transition from
`unverified_fail_closed` to `explicit-factor-v1`, using the currently stored
`expected_target_version`.

Corporate-action provenance must use this complete contract:

```json
{
  "kind": "terminal_close",
  "direction": "up",
  "target_price": 30.56,
  "outcome_rule_version": "terminal-close-v1-up-gte-down-lt",
  "price_adjustment_policy": "explicit-factor-v1",
  "target_to_close_factor": 1.0,
  "adjustment_provenance": {
    "contract_version": "corporate-action-adjustment-v1",
    "authority_type": "licensed_data_vendor",
    "authority_id": "KIS",
    "actor_principal": "service:forecast-evidence-operator",
    "authentication_method": "mcp_bearer",
    "symbol": "SMCI",
    "action_type": "none",
    "action_ratio": 1.0,
    "effective_date": "YYYY-MM-DD",
    "verified_through_date": "YYYY-MM-DD",
    "source": "KIS corporate-action feed",
    "source_ref": "artifact://corporate-actions/SMCI/YYYY-MM-DD",
    "source_sha256": "<64 lowercase hex>",
    "source_price_basis": "provider_adjusted"
  }
}
```

Allowed authorities are typed exchange, regulator, issuer filing, and
licensed-data-vendor IDs maintained in code. Symbol, effective/review dates,
source reference/hash, and price basis are validated. Factor 1 is not a
shortcut: `action_type="none"` still requires authoritative evidence with
ratio 1 and factor 1.

`action_ratio` means new units per old unit and:

```text
target_to_close_factor = 1 / action_ratio
effective_target = original_target * target_to_close_factor
```

Thus a 2-for-1 split uses ratio 2/factor 0.5, while a 1-for-10 reverse split
uses ratio 0.1/factor 10. If the conversion cannot be proved, leave the row
`unverified_fail_closed`.

## Daily close provenance

Only newly written KR/US daily rows with all of the following can resolve a
terminal claim:

- `is_final=true` and `session_scope="regular"`;
- actual `ingested_at` after the market final gate;
- content-addressed `source_row_id` and exact `source_row_version`;
- source-specific `price_basis`;
- positive finite `close`.

Existing rows with null provenance fail closed. The resolver also checks the
exchange calendar and wall clock. KR uses the 15:35 KST cutoff. US uses the
XNYS session schedule, including holidays, early closes, and DST, and requires
ingestion after the scheduled close.

Source contracts are:

| Stored source | Row version | `close` basis |
|---|---|---|
| `kis` | `kis-adjusted-daily-v1` | `provider_adjusted` |
| `toss`, `toss_fallback` | `toss-adjusted-daily-v1` | `provider_adjusted` |
| `yahoo`, `yahoo_fallback` | `yahoo-raw-daily-v1` | `raw` |

The corporate evidence basis must equal the selected candle basis. Provider
corrections change the content-addressed row identity and therefore invalidate
an earlier preview.

## Preview and persist

`dry_run=true` preserves the historical default. With the also-default
`backfill_missing=true`, it may fetch and commit shared daily-candle rows in a
separate transaction. For a truly read-only operator review always use:

```text
forecast_resolve(
  forecast_id="<terminal-id>",
  dry_run=true,
  backfill_missing=false
)
```

A successful typed preview returns a `resolution_contract` containing
`target_kind`, `outcome_rule_version`, `target_version`,
`immutable_claim_hash`, `target_hash`, `evidence_fingerprint`, and
`resolution_fingerprint`. Verify the comparison, original/effective target,
factor, selected source date/price/basis, final/session fields, ingestion time,
source row identity/version, and adjustment provenance.

Persist exactly that reviewed snapshot:

```text
forecast_resolve(
  forecast_id="<terminal-id>",
  dry_run=false,
  backfill_missing=false,
  expected_target_version=<preview target_version>,
  expected_claim_hash="<preview immutable_claim_hash>",
  expected_resolution_fingerprint="<preview resolution_fingerprint>"
)
```

The persist path re-locks the forecast and candle rows and recomputes the
fingerprint. Any target, evidence, candle, source, or ingestion change returns
`resolution_cas_mismatch` and leaves the row open. Batch persist uses the same
three values under `expected_resolutions[forecast_id]`.

## Deployment and rollback

1. Stop forecast resolution writers.
2. Apply the additive ROB-1038 migration.
3. Deploy the matching daily writers and resolver.
4. Let trusted writers populate new provenance; legacy candles are not
   backfilled by inference.
5. Review quarantine rows and explicitly attest or supersede each one.
6. Use read-only preview and CAS-bound persist.

The migration's DB trigger blocks legacy touch resolution, terminal identity
mutation, manual terminal resolution, and unbound adjustment evidence during
mixed deployment. Application rollback requires stopping the new resolver
first. Database downgrade deliberately refuses while typed forecast evidence
or candle provenance exists, because dropping it would destroy the safety
record.

No step in this runbook creates or mutates broker orders, watches, proposals,
or order intents. A future issue may replace operator-attested factors with a
first-class corporate-action ledger; until then, unverifiable conversion stays
fail-closed.
