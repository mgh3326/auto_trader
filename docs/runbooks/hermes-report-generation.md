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
