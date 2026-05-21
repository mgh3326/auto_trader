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
| `hermes_bundle_preparation_flow` (Prefect) | Calls `SnapshotBundleEnsureService.ensure(...)` on a cadence so Hermes finds a fresh bundle to pull from. | `app/flows/hermes_bundle_preparation_flow.py` |
| `HermesContextExporter` / `HermesCompositionIngestService` / `HermesStageArtifactsIngestService` | Service layer the MCP + HTTP routes both call. | `app/services/investment_stages/hermes_context.py` / `hermes_ingest.py` |

---

## 2. Env / config matrix

| Var | Default | Set on | Notes |
|---|---|---|---|
| `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED` | `false` | auto_trader app + MCP server + Prefect worker | Must be `true` for any Hermes endpoint or the prep flow to do real work. Same gate as the legacy `investment_report_generate_from_bundle` MCP tool. |
| `HERMES_INGEST_TOKEN` | empty | auto_trader app (HTTP route only) | Shared secret for the HTTP family. Token unset → all four HTTP endpoints respond `403 "Hermes ingest token not configured"`. **Do NOT commit a real value to this repo** — operator places it in the deployment secret store. |
| `HERMES_INGEST_TOKEN_HEADER` | `X-Hermes-Ingest-Token` | auto_trader app + Hermes side | Header name Hermes must use. Default rarely needs changing. |
| `HERMES_BUNDLE_PREPARATION_ENABLED` | `false` | Prefect worker | Activation gate for the bundle-preparation flow. `false` → flow exits with `{"status": "disabled", ...}` and zero side effects. `true` → flow calls `SnapshotBundleEnsureService.ensure` on its schedule. |

The four vars are independent gates:

```
HTTP endpoint reachable  ← token configured + correct header
HTTP endpoint produces  ← generator flag on
Prep flow does work     ← generator flag on AND prep-flow flag on
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
4. **Prefect worker env**: set `HERMES_BUNDLE_PREPARATION_ENABLED=true` on the worker that runs the `rob-287 hermes bundle preparation` flow. Initial invocation should land an `InvestmentSnapshotBundle` row and return `{"status": "ok", ...}`.
5. **Prefect deployment registration**: register a paused deployment of `hermes_bundle_preparation_flow` in `robin-prefect-automations`. Suggested cadence: KR equity preset every 5 minutes during 09:00–15:30 KST. Unpause manually after a clean dry-run.
6. **End-to-end smoke**: ask Hermes to drive a single cycle against the non-prod auto_trader instance. Expected timeline:
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
    auto_trader hostname BUT with HERMES_BUNDLE_PREPARATION_ENABLED
    still false. This verifies the HTTP surface, NOT the flow.
[ ] Register the Prefect deployment in robin-prefect-automations,
    paused=true. Do NOT unpause yet.
[ ] Set HERMES_BUNDLE_PREPARATION_ENABLED=true on the prod Prefect
    worker. Restart the worker to pick up the env change.
[ ] Manually trigger a single run of the paused deployment. Verify the
    InvestmentSnapshotBundle row landed and the return value is
    {"status": "ok", ...}.
[ ] Ask Hermes to perform a single end-to-end cycle. Inspect:
       - One investment_stage_run row with status='completed'.
       - One investment_report row referencing the same snapshot bundle.
       - No anomalies in Sentry from app/services/investment_stages/.
[ ] Unpause the Prefect deployment.
[ ] Monitor for one trading session before considering ROB-287 closed.
```

Until the unpause step is signed off by an operator, ROB-287 stays
**In Progress** in Linear. Done transition is the final action after
the round-trip + a clean session.

---

## 5. Rollback

Every gate has an off-switch:

* Hermes producing bad reports → flip `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=false`. All four HTTP endpoints + the prep flow return 503/disabled in one round trip.
* HTTP surface specifically misbehaving → flip `HERMES_INGEST_TOKEN` to empty. The middleware returns 403 to every call; Hermes will fail fast.
* Prep flow over-running → pause the deployment in Prefect; flip `HERMES_BUNDLE_PREPARATION_ENABLED=false` as belt-and-braces.

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
| Prep flow `"status": "disabled"` on the worker | `HERMES_BUNDLE_PREPARATION_ENABLED=false` (default). | Flip the env var; restart the Prefect worker. |
| Hermes never sees a fresh bundle | Either the flow is disabled, the deployment is paused, or the cadence is too sparse for Hermes' poll interval. | Manually run the flow once and check `created=true` in the return. |

The middleware token branches are at `app/middleware/auth.py`. The flow body is at `app/flows/hermes_bundle_preparation_flow.py`. The service layer that all four endpoints share is at `app/services/investment_stages/hermes_context.py` + `hermes_ingest.py`.

---

## 7. Phase C smoke — operator round-trip verification (ROB-287)

After the activation steps in §3 / §4 land but **before** flipping ROB-287 to Done, the operator drives a single Hermes-shaped round-trip to confirm the wire contract end-to-end. The fixtures + CLI for this live in this repo and are pinned in lock-step with the contract tests so the smoke does not double as fixture-syntax validation.

### 7.1 Pre-conditions

- `SNAPSHOT_BACKED_REPORT_GENERATOR_ENABLED=true` on the target auto_trader app.
- `HERMES_INGEST_TOKEN` configured on the target app, value known to the operator.
- One existing `InvestmentSnapshotBundle` row (use the prep flow once or seed manually). The smoke does NOT call `/prepare-bundle` — that's a separate concern.

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

The CLI loads Hermes-shaped payloads from `tests/fixtures/hermes/*.json` and substitutes the two placeholder UUIDs (`{{run_uuid}}`, `{{snapshot_bundle_uuid}}`) at runtime:

1. `POST /trading/api/investment-reports/hermes/context` — returns `HermesContextPayload` (5 deterministic stages render; missing snapshots surface as UNAVAILABLE without aborting).
2. `POST /trading/api/investment-reports/hermes/stage-artifacts` — append-only ingest of 5 stage artifacts under one `run_uuid`. Re-running the same CLI invocation returns 200 OK with `idempotent_existing=true` on every artifact.
3. `POST /trading/api/investment-reports/hermes/composition` — final composition + auto-finalize the stage run. The Investment­Report row lands with `metadata.hermes_composition.hermes_run_id="hermes-smoke-001"`.

### 7.4 Expected output

```
... INFO base_url: https://<host>
... INFO bundle_uuid: <uuid>
... INFO run_uuid: <uuid>
... INFO --- step 1/3: context export ---
... INFO POST .../context → 200  (keys: bundle_status, constraints, ...)
... INFO --- step 2/3: stage-artifacts ingest ---
... INFO POST .../stage-artifacts → 200  (keys: ...)
... INFO   artifacts: 5 (idempotent reuse: 0) run_status: running
... INFO --- step 3/3: composition ingest ---
... INFO POST .../composition → 200  (keys: ...)
... INFO   report_uuid: <uuid>  items: 2  status: draft
... INFO --- round-trip OK ---
```

Exit code 0 = full chain succeeded. Exit code 1 = some step returned a 4xx/5xx; stderr carries the body for triage. Re-runnable: stage-artifacts ingest is idempotent on `(run_uuid, stage_type)`, so an aborted run can be safely retried with the same `--run-uuid`.

### 7.5 Hard invariants the smoke is allowed to assume

- No external LLM is called — Hermes payloads come from the JSON fixtures.
- No broker / order / watch / order-intent mutation reachable from any endpoint the smoke hits.
- Token is never logged or printed; only its presence/absence is surfaced via `_redact_token`.
- The CLI refuses to invent bundle UUIDs (`--bundle-uuid` is required).

### 7.6 Closing ROB-287

Done transition is approved after:

- The smoke exits 0 against the production auto_trader instance with a fresh bundle UUID,
- The InvestmentReport row is visible in the prod DB with the expected `hermes_composition` metadata,
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
  (`tests/fixtures/hermes/{stage_artifacts_request_us,composition_request_us}.json`).
  These are pinned to `market="us"`, `account_scope="alpaca_paper"`,
  `status="draft"`, with example tickers `AAPL` + `MSFT`. The fixture
  lock test (`test_us_fixtures_pin_alpaca_paper_and_draft_and_us_symbols`)
  guards against scope drift.
- Default `--fixture-set kr` matches PR #910 behaviour — operators
  not interested in the US smoke don't need to change anything.

### 8.4 Expected outcome

Three POSTs in order (same chain as §7.3, but on US fixtures):

1. `/context` — returns a `HermesContextPayload` with
   `market="us"`, `account_scope="alpaca_paper"`. The 5 deterministic
   stages render even with no items in the bundle (UNAVAILABLE).
2. `/stage-artifacts` — 5 artifact rows persisted under one
   `run_uuid`, `run_status="running"`.
3. `/composition` — returns a 200 envelope with `status="draft"`,
   `items_count=3`. The matching `InvestmentStageRun` row is
   auto-finalised to `status="completed"` (§D4).

### 8.5 DB-level invariants the operator confirms

After the CLI exits 0:

- `investment_stage_runs.status='completed'` for the run UUID the
  CLI printed.
- 5 `investment_stage_artifacts` rows linked to that `run_uuid`.
- One `investment_reports` row with:
  - `snapshot_bundle_uuid` = the US bundle UUID,
  - `status='draft'` (**must not be `published`**),
  - `market='us'`,
  - `account_scope='alpaca_paper'`,
  - `report_metadata.hermes_composition.hermes_run_id="hermes-smoke-us-001"`.

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
