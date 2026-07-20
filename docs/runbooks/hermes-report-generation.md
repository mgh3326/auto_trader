# Hermes report-generation activation (ROB-287)

This runbook covers turning the Hermes pull-driven `/invest/reports`
report-generation cycle on in non-production, then in production. The
code shipped in PRs #898 / #901 / #905 / #907 already gives Hermes
every endpoint it needs; this runbook is the operator playbook for
flipping the gates.

> **Boundary**: nothing in this repo schedules the Hermes flow on its
> own. Prefect deployment registration lives in
> `robin-prefect-automations` and ships paused. The auto_trader-side
> changes are all gated by env vars that default off.

---

## 1. Components

| Surface | Role | Lives in |
|---|---|---|
| MCP tools (`investment_report_prepare_bundle` / `..._get_hermes_context` / `..._create_from_hermes_composition` / `investment_stage_artifacts_ingest_from_hermes`) | Hermes pulls context + posts artifacts/composition back over MCP. | `app/mcp_server/tooling/investment_hermes_handlers.py` |
| HTTP routes (`/trading/api/investment-reports/hermes/{prepare-bundle, context, stage-artifacts, composition}`) | Same surface as MCP, exposed over HTTP for Hermes if it prefers HTTP transport. | `app/routers/investment_hermes_http.py` |
| AuthMiddleware token branch | Shared-secret auth for the HTTP family (prefix-match). | `app/middleware/auth.py` |
| `HermesContextExporter` / `HermesCompositionIngestService` / `HermesStageArtifactsIngestService` | Service layer the MCP + HTTP routes both call. | `app/services/investment_stages/hermes_context.py` / `hermes_ingest.py` |

> **ROB-986 update**: the `hermes_bundle_preparation_flow` Prefect flow (bundle-preparation cadence) was removed — it was never deployed (no `robin-prefect-automations` registration, zero invocation history). Bundle freshness for a Hermes pull is now produced ad hoc via `investment_report_prepare_bundle` (MCP) / HTTP `prepare-bundle` (§1) at pull time; there is no scheduled cadence.

---

## 2. Env / config matrix

| Var | Default | Set on | Notes |
|---|---|---|---|
| `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` | `false` | auto_trader app + MCP server | Must be `true` for any Hermes endpoint to do real work. Same gate as the legacy `investment_report_generate_from_bundle` MCP tool. |
| `HERMES_INGEST_TOKEN` | empty | auto_trader app (HTTP route only) | Shared secret for the HTTP family. Token unset → all four HTTP endpoints respond `403 "Hermes ingest token not configured"`. **Do NOT commit a real value to this repo** — operator places it in the deployment secret store. |
| `HERMES_INGEST_TOKEN_HEADER` | `X-Hermes-Ingest-Token` | auto_trader app + Hermes side | Header name Hermes must use. Default rarely needs changing. |

The vars are independent gates:

```
HTTP endpoint reachable  ← token configured + correct header
HTTP endpoint produces  ← generator flag on
```

---

## 3. Non-production activation

Order matters: confirm each step before moving on.

1. **Generator flag**: set `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true` on the auto_trader app server and the MCP server. Verify with a single MCP call:
   ```
   investment_report_prepare_bundle(market="kr", account_scope="kis_live")
   ```
   Expected: `{"success": true, "bundle_uuid": "...", ...}`. If it returns `success=false / error=snapshot_backed_report_generator_disabled`, the env var did not propagate to the running process.
2. **HTTP token (auto_trader side)**: place a non-trivial secret in `HERMES_INGEST_TOKEN` on the auto_trader app server. Default header `X-Hermes-Ingest-Token` is fine unless you have a reason to change it.
3. **HTTP token (Hermes side)**: configure Hermes with the *same* secret. Smoke-test:
   ```
   curl -sSf \
     -H "X-Hermes-Ingest-Token: <secret>" \
     -H "Content-Type: application/json" \
     -d '{"snapshot_bundle_uuid": "<bundle-uuid>"}' \
     https://<auto_trader>/trading/api/investment-reports/hermes/context
   ```
   Expected: 200 with the frozen Hermes context payload. 401 → header/value mismatch. 403 → token not configured on the server side. 503 → generator flag is off.
4. **End-to-end smoke**: ask Hermes to drive a single cycle against the non-prod auto_trader instance. Expected timeline:
   1. Hermes calls `POST /prepare-bundle` (or polls existing bundles).
   2. Hermes calls `POST /context` with a `snapshot_bundle_uuid`.
   3. Hermes ingests stage artifacts as it computes them (`POST /stage-artifacts`).
   4. Hermes posts the final composition (`POST /composition`). The composition ingest auto-finalises the matching stage run from `running` to `completed`.

If any of those steps surfaces an error envelope or non-2xx status, stop and inspect the corresponding service-level test — the unit suite covers the wire shape exhaustively.

---

## 4. Production cutover (operator-gated, NOT triggered by merging this PR)

```
[ ] Backup the prod DB (logical or vendor equivalent).
[ ] Confirm SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true is staged in
    the production secret manager (NOT committed to this repo).
[ ] Confirm HERMES_INGEST_TOKEN is staged in the production secret
    manager with a fresh value (NOT committed to this repo).
[ ] Confirm HERMES_INGEST_TOKEN_HEADER agrees with Hermes' production
    configuration.
[ ] Run the smoke CLI / curl checks from §3 against the prod
    auto_trader hostname. This verifies the HTTP surface.
[ ] Ask Hermes to perform a single end-to-end cycle. Inspect:
       - One investment_stage_run row with status='completed'.
       - One investment_report row referencing the same snapshot bundle.
       - No anomalies in Sentry from app/services/investment_stages/.
[ ] Monitor for one trading session before considering ROB-287 closed.
```

Until the unpause step is signed off by an operator, ROB-287 stays
**In Progress** in Linear. Done transition is the final action after
the round-trip + a clean session.

---

## 5. Rollback

Every gate has an off-switch:

* Hermes producing bad reports → flip `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=false`. All four HTTP endpoints return 503/disabled in one round trip.
* HTTP surface specifically misbehaving → flip `HERMES_INGEST_TOKEN` to empty. The middleware returns 403 to every call; Hermes will fail fast.

Rolling back any of these is a config-only change; no auto_trader
redeploy is required.

---

## 6. Diagnostics

| Symptom | Likely cause | Quick check |
|---|---|---|
| HTTP `403 "Hermes ingest token not configured"` | `HERMES_INGEST_TOKEN` empty on server. | Check the running process env, not the deployment manifest. |
| HTTP `401 "Invalid Hermes ingest token"` | Hermes is sending a stale or wrong-shape header. | Compare `HERMES_INGEST_TOKEN_HEADER` on both sides. |
| HTTP `503` with `snapshot_backed_report_generator_disabled` | `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` is false on the auto_trader app server. | Restart the process if the env was changed without restart. |
| HTTP `409` with `artifact_content_conflict` | Hermes re-ingested the same `(run_uuid, stage_type)` with a different payload. | Hermes-side bug — auto_trader's append-only contract is doing its job. |
| HTTP `409` with `run_envelope_mismatch` | A second ingest under the same `run_uuid` carries different `market/account_scope/policy_version`. | Hermes-side bug — fix the envelope. |
| Hermes never sees a fresh bundle | No bundle has been prepared for this identity tuple yet. | Manually call `investment_report_prepare_bundle` (MCP) / HTTP `prepare-bundle` and check `created=true` in the return. |

The middleware token branches are at `app/middleware/auth.py`. The service layer that all four endpoints share is at `app/services/investment_stages/hermes_context.py` + `hermes_ingest.py`.

---

## 7. Phase C smoke — operator round-trip verification (ROB-287)

After the activation steps in §3 / §4 land but **before** flipping ROB-287 to Done, the operator drives a single Hermes-shaped round-trip to confirm the wire contract end-to-end. The fixtures + CLI for this live in this repo and are pinned in lock-step with the contract tests so the smoke does not double as fixture-syntax validation.

### 7.1 Pre-conditions

- `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true` on the target auto_trader app.
- `HERMES_INGEST_TOKEN` configured on the target app, value known to the operator.
- One existing `InvestmentSnapshotBundle` row (call `investment_report_prepare_bundle` once or seed manually). The smoke does NOT call `/prepare-bundle` — that's a separate concern.

### 7.2 CLI usage

```bash
HERMES_INGEST_TOKEN="<value>" \
uv run python -m scripts.hermes_roundtrip_smoke \
  --base-url https://<auto_trader-host> \
  --bundle-uuid <existing-bundle-uuid>
```

Optional flags:

- `--token <value>` — bypass the env var (the env var is the default; never log either).
- `--token-header <header>` — when the deployment uses a non-default header name.
- `--run-uuid <uuid>` — fix the `run_uuid` for replay; default is a fresh UUID4 per invocation.
- `--verbose` — full response bodies per step.

### 7.3 Steps the CLI drives

The CLI loads Hermes-shaped payloads from `tests/fixtures/hermes/*.json` and substitutes the placeholder UUIDs (`{{run_uuid}}`, `{{snapshot_bundle_uuid}}` always; `{{symbol_report_uuid}}` / `{{dimension_report_uuid}}` are filled in from the symbol-/dimension-reports responses before the composition POST). As of ROB-309 a single run exercises the **full current contract** — ROB-287 baseline + ROB-301 symbol-reports + ROB-306 dimension-reports + ROB-308 final-synthesis fields:

1. `POST /trading/api/investment-reports/hermes/context` — returns `HermesContextPayload` (5 deterministic stages render; missing snapshots surface as UNAVAILABLE without aborting). On this first pull `dimension_reports` / `symbol_intermediate_reports` are empty.
2. `POST /trading/api/investment-reports/hermes/stage-artifacts` — append-only ingest of 5 stage artifacts under one `run_uuid`. Re-running the same CLI invocation returns 200 OK with `idempotent_existing=true` on every artifact.
3. `POST /trading/api/investment-reports/hermes/symbol-reports` (ROB-301) — per-symbol reductions for the same `run_uuid`. The CLI captures the first returned `symbol_report_uuid`.
4. `POST /trading/api/investment-reports/hermes/dimension-reports` (ROB-306) — per-dimension analyst reports for the same `run_uuid`. The CLI captures the `market` dimension's `dimension_report_uuid`.
5. `POST /trading/api/investment-reports/hermes/context` (re-pull) — the CLI asserts the response now carries **non-empty** `dimension_reports` and `symbol_intermediate_reports`, confirming the run linkage surfaces back to Hermes.
6. `POST /trading/api/investment-reports/hermes/composition` (ROB-308) — final composition + auto-finalize the stage run. The payload threads `symbol_intermediate_report_uuids` + `dimension_report_uuids` (validated for existence + run membership) and includes one `decision_bucket="new_buy_candidate"` item with `cited_symbol_report_uuid` / `cited_dimension_report_uuids`. The Investment­Report row lands with `metadata.hermes_composition.hermes_run_id="hermes-smoke-001"` plus `metadata.symbol_intermediate_report_uuids` / `metadata.dimension_report_uuids`.
7. `GET /trading/api/investment-reports/runs/{run_uuid}/dimension-reports?dimension=market` — read surface; the CLI asserts the market dimension report renders (stance `bullish`). **Session-authed** (see §7.4 note).
8. `GET /trading/api/investment-reports/{report_uuid}` — final report bundle; the CLI asserts `decision_rollup.new_candidate` is non-empty and `item_groups` carries the `new_buy_candidate` group alongside the held/unclassified items (ROB-308 C5 held-action vs new-candidate split). **Session-authed.**

### 7.4 Expected output

The two GET read surfaces (steps 7–8) are authenticated by the **operator session cookie**, NOT the Hermes ingest token (only the five `…/hermes/*` POSTs use the token). Supply the cookie via `--session-cookie "<cookie>"` or `HERMES_SMOKE_SESSION_COOKIE`. When the cookie is omitted the CLI runs the ingest chain (steps 1–6), logs a warning, and exits 0 without the read-surface assertions — verify the grouping manually in that case (see §7.4 note below).

```
... INFO base_url: https://<host>
... INFO bundle_uuid: <uuid>
... INFO run_uuid: <uuid>
... INFO read-surface GETs: enabled (session cookie supplied)
... INFO --- step 1/8: context export ---
... INFO POST .../context → 200  (keys: bundle_status, constraints, dimension_reports, ...)
... INFO --- step 2/8: stage-artifacts ingest ---
... INFO POST .../stage-artifacts → 200  (keys: ...)
... INFO   artifacts: 5 (idempotent reuse: 0) run_status: running
... INFO --- step 3/8: symbol-reports ingest ---
... INFO POST .../symbol-reports → 200  (keys: ...)
... INFO   symbol_reports: 2  first symbol_report_uuid: <uuid>
... INFO --- step 4/8: dimension-reports ingest ---
... INFO POST .../dimension-reports → 200  (keys: ...)
... INFO   dimension_reports: 2  market dimension_report_uuid: <uuid>
... INFO --- step 5/8: context re-pull (carries reports) ---
... INFO POST .../context → 200  (keys: ...)
... INFO   context carries dimension_reports: 2  symbol_intermediate_reports: 2
... INFO --- step 6/8: composition ingest ---
... INFO POST .../composition → 200  (keys: ...)
... INFO   report_uuid: <uuid>  items: 3  status: draft
... INFO --- step 7/8: dimension-reports read surface ---
... INFO GET .../runs/<uuid>/dimension-reports?dimension=market → 200  (keys: ...)
... INFO   read-surface market reports: 1  stance: bullish
... INFO --- step 8/8: final report bundle grouping ---
... INFO GET .../<report_uuid> → 200  (keys: ...)
... INFO   decision_rollup: new_candidate=1 held_action=0  groups: new_buy_candidate, unclassified
... INFO --- round-trip OK ---
```

(The US fixture set — `--fixture-set us` — drives the same 8 steps but composes 4 items and tags `hermes-smoke-us-001`.)

Exit code 0 = full chain succeeded. Exit code 1 = some step returned a 4xx/5xx (or a contract assertion failed, e.g. the context re-pull did not carry the reports, or the bundle had no `new_candidate`); stderr carries the body for triage. Re-runnable: the stage-artifacts / symbol-reports / dimension-reports ingests are content-idempotent, so an aborted run can be safely retried with the same `--run-uuid`.

> **Note — verifying the new legs during the real-Hermes operator run.** After the smoke exits 0, confirm in the prod DB / read API that (a) the `investment_dimension_reports` row for the `market` dimension exists for the run (or `GET …/runs/{run_uuid}/dimension-reports?dimension=market` returns it), and (b) the final bundle's `decision_rollup` splits the items correctly — the `new_buy_candidate` item lands under `new_candidate` while held/risk/completed items land under `held_action`. If you ran without `--session-cookie`, drive those two GETs by hand with an authenticated session to close the verification.

### 7.5 Hard invariants the smoke is allowed to assume

- No external LLM is called — Hermes payloads come from the JSON fixtures.
- No broker / order / watch / order-intent mutation reachable from any endpoint the smoke hits (the symbol-/dimension-reports and composition are advisory-only; items stay `operation ∈ {review, cancel, keep}` + `apply_policy=requires_user_approval`).
- Token is never logged or printed; only its presence/absence is surfaced via `_redact_token`. The `--session-cookie` value is likewise never logged.
- The CLI refuses to invent bundle UUIDs (`--bundle-uuid` is required).

### 7.6 Closing ROB-287

Done transition is approved after:

- The smoke exits 0 against the production auto_trader instance with a fresh bundle UUID,
- The InvestmentReport row is visible in the prod DB with the expected `hermes_composition` metadata **and** the consumed `symbol_intermediate_report_uuids` / `dimension_report_uuids`,
- The dimension report read surface returns the `market` dimension row and the final bundle splits held-action vs new-candidate (§7.4 note),
- The InvestmentStageRun row is `status='completed'` (auto-finalized by the composition step),
- No anomalies in Sentry from `app/services/investment_stages/` for one trading session.

Until those are signed off, ROB-287 stays In Progress.

---

## 8. US narrow smoke (ROB-287 follow-up)

A narrow, **non-prod** verification that the Hermes-first contract
(four endpoints in §1) accepts a `market="us"` snapshot bundle and
produces a **draft** `InvestmentReport` linked back to it. This is
NOT a "US production support" milestone — it is a contract-shape
check for the Hermes path on US-shaped payloads.

### 8.1 Scope and boundaries

- **In scope**: Hermes context export / stage-artifacts ingest /
  composition ingest on a `market="us"`, `account_scope="alpaca_paper"`
  snapshot bundle, producing a `status="draft"` report row.
- **Out of scope** (do NOT execute as part of this smoke):
  - Published US reports (`status="published"`) — operator review
    required.
  - The legacy `ReportGenerationRequest` snapshot-backed generator
    (`/trading/api/investment-reports/snapshot-backed`) — its
    request schema still only accepts `market` ∈ `{"kr", "crypto"}`
    paired with `kis_live` / `upbit_live`. The Hermes-first
    endpoints take `market` as a plain string and therefore work
    on US bundles without that legacy validator firing.
  - `auto_emit_from_evidence` — same legacy path, US bundles aren't
    accepted there. The Hermes flow does not pass through that
    proposer.
  - Real-money broker mutation — every Hermes endpoint is
    structurally read/persist-only on the report-database side; no
    code path reaches `submit_order` / `cancel_order` /
    `create_watch_intent` / order-intent shapes from any of the
    four endpoints. The static guard from PR #898
    auto-scans the staged path for re-introductions.
  - Production env / secret application, Prefect registration or
    unpause, production deploy, ROB-287 → Done reconciliation.

### 8.2 Pre-conditions

Same as §7.1 plus:

- An existing `InvestmentSnapshotBundle` row with `market="us"` and
  `account_scope="alpaca_paper"` in the non-prod DB. The smoke does
  not call `/prepare-bundle`; seed the bundle row via whatever
  out-of-band mechanism non-prod already has (or by calling the
  `investment_snapshots_refresh_flow` once after the env var flip
  in §3 — `purpose` field is opaque to the Hermes path).
- Hermes will reach the endpoints from a host that has connectivity
  to the non-prod auto_trader and a valid `HERMES_INGEST_TOKEN`
  header. **Do not stage production tokens into a non-prod
  environment.**

### 8.3 CLI invocation (operator runs)

```bash
HERMES_INGEST_TOKEN="<value-from-non-prod-secret-manager>" \
uv run python -m scripts.hermes_roundtrip_smoke \
  --base-url https://<non-prod-auto_trader-host> \
  --bundle-uuid <existing-us-bundle-uuid> \
  --fixture-set us
```

- `--fixture-set us` switches the CLI to the US fixtures
  (`tests/fixtures/hermes/{stage_artifacts_request_us,symbol_reports_request_us,dimension_reports_request_us,composition_request_us}.json`).
  These are pinned to `market="us"`, `account_scope="alpaca_paper"`,
  `status="draft"`, with example tickers `AAPL` + `MSFT`. The fixture
  lock test (`test_us_fixtures_pin_alpaca_paper_and_draft_and_us_symbols`)
  guards against scope drift.
- Default `--fixture-set kr` matches PR #910 behaviour — operators
  not interested in the US smoke don't need to change anything.

### 8.4 Expected outcome

Same 8-step chain as §7.3, on the US fixtures (the read-surface GETs in
steps 7–8 still need `--session-cookie`):

1. `/context` — returns a `HermesContextPayload` with
   `market="us"`, `account_scope="alpaca_paper"`. The 5 deterministic
   stages render even with no items in the bundle (UNAVAILABLE);
   `dimension_reports` / `symbol_intermediate_reports` start empty.
2. `/stage-artifacts` — 5 artifact rows persisted under one
   `run_uuid`, `run_status="running"`.
3. `/symbol-reports` — 2 per-symbol reductions (AAPL/MSFT).
4. `/dimension-reports` — 2 per-dimension reports (market/news).
5. `/context` (re-pull) — now carries 2 `dimension_reports` + 2
   `symbol_intermediate_reports`.
6. `/composition` — returns a 200 envelope with `status="draft"`,
   `items_count=4` (the extra item is the `new_buy_candidate` on AAPL).
   The matching `InvestmentStageRun` row is auto-finalised to
   `status="completed"` (§D4).
7. `/runs/{run_uuid}/dimension-reports?dimension=market` — the US
   market dimension report renders.
8. `/{report_uuid}` — `decision_rollup.new_candidate` is non-empty.

### 8.5 DB-level invariants the operator confirms

After the CLI exits 0:

- `investment_stage_runs.status='completed'` for the run UUID the
  CLI printed.
- 5 `investment_stage_artifacts` rows linked to that `run_uuid`.
- 2 `investment_symbol_intermediate_reports` + 2
  `investment_dimension_reports` rows linked to that `run_uuid`.
- One `investment_reports` row with:
  - `snapshot_bundle_uuid` = the US bundle UUID,
  - `status='draft'` (**must not be `published`**),
  - `market='us'`,
  - `account_scope='alpaca_paper'`,
  - `report_metadata.hermes_composition.hermes_run_id="hermes-smoke-us-001"`,
  - `report_metadata.symbol_intermediate_report_uuids` /
    `report_metadata.dimension_report_uuids` populated with the
    consumed report UUIDs.

If any of those fail, treat the smoke as failed and surface the
exact mismatch on the ROB-287 ticket — do NOT attempt to publish or
remediate the row.

### 8.6 Closing the US narrow smoke

The US narrow smoke does NOT close ROB-287. It is a contract-shape
check that the Hermes-first endpoints round-trip on US bundles.
ROB-287 → Done still requires the operator-driven prod-side cycle
listed in §7.6 (and KR is the default fixture for that prod cycle
until a separate decision approves US for production Hermes
composition).

If the US narrow smoke passes, the operator may flag the contract
as "US-ready (advisory-only)" on the ROB-287 thread and decide
separately whether to:

- Expand `ReportGenerationRequest` to accept `market="us"` (out of
  scope here — that's a different code change with its own
  approval gate).
- Schedule a US Prefect deployment (separate `robin-prefect-automations`
  PR, separate operator approval).
- Add US to the prod cutover checklist (operator decision).

---

## 9. ROB-314 — production-collector bundle preparation (real KR/kis_live evidence)

The two report-generation entrypoints now inject `production_collector_registry(session)` and accept a `user_id`:

- MCP `investment_report_prepare_bundle` (`user_id` is an optional tool arg)
- HTTP `POST /trading/api/investment-reports/hermes/prepare-bundle` (`user_id` is an optional body field)

Because these paths are token-authed (HTTP) or have no user context (MCP), `user_id`
must be supplied explicitly by the caller — it is never derived from an authenticated
session here. The REST `SnapshotBackedReportGenerator` path keeps deriving it from its
own request.

**Behaviour change — prepare is no longer DB-only.** With production collectors injected,
prepare-bundle now performs live, read-only external calls in addition to DB reads:
KIS quote/orderbook (`SymbolSnapshotCollector`) and KIS/Upbit open-orders
(`PendingOrdersSnapshotCollector`). No order/watch/order-intent mutation occurs. Live
read credentials must be present on the host; absent/misconfigured credentials make those
collectors emit per-source `unavailable` rather than crashing. Expect added latency and
broker rate-limit sensitivity.

**Operator smoke (read-only, placeholders only — do not paste real secrets):**

```bash
# Gate must be on for the endpoint to do anything.
export SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true
# HTTP transport (token-authed):
curl -sS -X POST "$HOST/trading/api/investment-reports/hermes/prepare-bundle" \
  -H "X-Hermes-Ingest-Token: $HERMES_INGEST_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"market":"kr","account_scope":"kis_live","symbols":["<HELD_SYMBOL>","<CANDIDATE>"],"user_id":<USER_ID>}'
```

Inspect `coverage_summary` / `missing_sources` in the response to confirm portfolio,
market, journal, and watch coverage.

**Diagnostics ladder — interpreting a still-empty / blocked bundle:**

| Symptom | Likely cause | Operator action |
|---|---|---|
| 503 `snapshot_backed_report_generator_disabled` | feature flag off | set `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true` |
| 403 / 401 on HTTP | token misconfig / wrong token | check `HERMES_INGEST_TOKEN[_HEADER]` |
| portfolio `unavailable` but holdings exist | `user_id` not supplied to this entrypoint | pass `user_id` in the request |
| all required sources `unavailable` | wrong/empty collector registry (regression) | confirm this entrypoint injects `production_collector_registry` |
| specific source `unavailable` w/ credentials absent | missing live read precondition | provision read creds for that broker/source |
| source present but `hard_stale` | stale data precondition | refresh upstream data; not a code bug |
| `complete`/`partial` with a real no-action verdict | genuine no-action report | none — this is a valid outcome |

**Deferred (ROB-314 scope decision):** `investment_snapshots_refresh_flow` and the generic
MCP `investment_snapshot_bundle_ensure` tool intentionally stay on the empty default
registry; the refresh path is part of the separate scheduler-activation track. Locked by
`tests/test_rob314_deferred_call_sites.py`.
