# Sentry profiling collection-path runbook

## Scope and what "proof" means here

This runbook covers the diagnosis work for the near-zero-profile-events
observation (last-90-days profiles dataset: 0 events; last-30-days spans:
~482,200 rows / 14,564 traces). It draws a hard line between two kinds of
proof:

- **Repository-time proof** (this PR): hermetic tests using a fake DSN and
  an in-memory transport prove the *code path* is sound — a sampled
  transaction, run through the real `app.monitoring.sentry.init_sentry()`
  seam, produces a linked `profile` + `transaction` envelope item pair, the
  transaction is scrubbed, and the `mcp.server`/`tools/call` span survives.
  This does **not** prove Sentry's ingest actually accepts and stores
  profiles for this org/project/plan in production.
- **Operator post-deploy proof** (out of scope for this PR): actually
  running the canary CLI (`scripts/sentry_profiling_canary.py --send
  --confirm`) against a deployed process with a real `SENTRY_DSN`, then
  checking the Sentry UI for a profile linked to that transaction. This PR
  does not do this — no real DSN, no real send, no deploy/activation.

## What repository-time tests actually prove

- `tests/test_sentry_profile_envelope_contract.py` — routes through the real
  `init_sentry()` (not a hand-rolled duplicate of its kwargs) with a fake DSN
  and an in-memory transport. Confirms:
  - a transaction sampled with `traces_sample_rate=1.0,
    profiles_sample_rate=1.0` yields one envelope with both a `transaction`
    and a `profile` item. The matching `profile.transactions[]` record has
    `id == transaction.event_id`, `trace_id == transaction.contexts.trace.trace_id`,
    and `name == transaction.transaction` after final scrubbing. These are
    the actual sentry-sdk 2.x linkage fields — **not** a `contexts.profile`
    field, which this SDK version does not populate on the transaction event.
  - `profiles_sample_rate` alone is **not sufficient**: with
    `traces_sample_rate=0.0`, no profile is produced even at
    `profiles_sample_rate=1.0`. The transaction profiler piggybacks on trace
    sampling in sentry-sdk 2.57.0.
  - an MCP `tools/call` transaction with a secret-KEY-named span field
    (`mcp.request.argument.api_key`) is scrubbed to `[Filtered]`, the
    `mcp.server` span and the `tools/call <tool>` transaction rename survive,
    and the profile item is still present and linked by id, trace id, and final
    transaction name.
  - the ROB-1880 repo-wide socket guard reports zero blocked network
    attempts for every test in this file (`ROB-1296 external HTTP boundary:
    0 blocked requests` / `ROB-1880 socket guard: ... blocked_attempts=0`).
- `tests/test_sentry_mcp_payload_contract.py` — uses serialized in-memory
  events and envelopes to prove that `SENTRY_MCP_INCLUDE_PROMPTS=false`
  removes generic MCP request arguments and tool/prompt result content from
  spans, contexts, tags, breadcrumbs, extras, and MCP error-event context,
  while preserving `mcp.server`, `tools/call <tool>`, lane/profile metadata,
  timing/status, and measurements. Explicit `true` permits non-sensitive
  payload but leaves the credential/PII scrubber in force.
- `tests/test_sentry_process_config_seam.py` — AST-extracts the actual
  `init_sentry(...)` call from each of the four process entrypoints
  (`app/main.py`, `app/core/taskiq_broker.py` ×2 for worker/scheduler,
  `app/mcp_server/main.py`) rather than hand-copying kwargs, so a deleted or
  changed call fails this test. Replays each extracted call and confirms all
  four forward the identical settings-derived `traces_sample_rate` /
  `profiles_sample_rate` / `send_default_pii` to `sentry_sdk.init`. Also
  statically forbids any entrypoint from passing those three kwargs inline
  (which would fork the seam even though it currently reads one shared
  `settings` object).
- `tests/test_sentry_diagnostics.py` / `app/monitoring/sentry_diagnostics.py`
  — a bounded, non-secret diagnostics surface (`process_kind`, `sdk_version`,
  `enabled`, `traces_sample_rate`, `profiles_sample_rate`, `profiler_ready`).
  `profiler_ready` requires DSN configured **and** `traces_sample_rate > 0`
  **and** `profiles_sample_rate > 0` — reflecting the piggyback finding
  above. `process_kind` is validated against a fixed allowlist
  (`KNOWN_PROCESS_KINDS`); an unknown value raises `ValueError` rather than
  being echoed back, so this function can never become a channel for
  leaking arbitrary caller-supplied text.
- `tests/test_sentry_pii_docs_default_off.py` — pins that every committed
  env example/doc (`env.example`, `env.prod.example`, `MONITORING_README.md`,
  `ERROR_REPORTING_README.md`, `LOGGING_SETUP.md`, `DEPLOYMENT.md`) advertises
  `SENTRY_SEND_DEFAULT_PII=false` and `SENTRY_MCP_INCLUDE_PROMPTS=false`,
  matching the actual ROB-1305 runtime default in `app/core/config.py`.

None of the above required activation, a real DSN, or a network call — every
test uses a syntactically valid but non-routable fixture DSN
(`fake.invalid.example`) and an in-memory capturing transport, verified
against the ROB-1880 socket guard's zero-blocked-attempts report.

## Known limitation, verified against installed sentry-sdk 2.57.0

Reading `sentry_sdk/client.py::Client.capture_event` shows
`profile = event.pop("profile", None)` happens **before**
`self._prepare_event(event, hint, scope)` — which is what invokes
`before_send_transaction`. The profile payload is therefore never passed to
`before_send_transaction`, and `sentry_sdk/consts.py`'s public `init()`
options expose no `before_send_profile` or equivalent hook in this SDK
version. Consequence, confirmed empirically
(`test_profile_frames_carry_unscrubbed_filesystem_paths`):

- Profile item frames (`profile.frames[]`) carry real `abs_path` /
  `filename` / `module` / `function` values. These paths may expose host,
  user, repository/project, or sensitive path-component names; this runbook
  does **not** classify them as harmless or universally non-PII.
- The profiler is a statistical stack sampler and does not place ordinary
  local-variable values in frame metadata (verified by the test above), but
  that does not remove the residual path-disclosure risk. Public sentry-sdk
  2.57.0 has no `before_send_profile` or equivalent profile-item scrub hook.
- Therefore production profiling requires operator risk acceptance of this
  residual and a fixed synthetic canary only. Do not put user, broker, MCP,
  prompt, or other sensitive workload data into the canary. Removing or
  rewriting profile paths would require an explicit separate decision and a
  public SDK capability; this repository does not use private SDK hooks or
  envelope monkeypatching.

## MCP payload collection contract (ROB-1305 R6)

At the shared public `before_send`, `before_send_transaction`,
`before_send_log`, and `before_breadcrumb` seams, the default-deny transform
removes every `mcp.request.argument.*` value and generic tool/prompt result
content when `SENTRY_MCP_INCLUDE_PROMPTS=false`. It applies recursively across
span data, contexts, tags, breadcrumbs, extras, and MCP error events. The
`mcp.server` span, `tools/call <tool>` name, method/tool identity,
lane/profile metadata, timing/status, and measurements remain usable.

An explicit `SENTRY_MCP_INCLUDE_PROMPTS=true` permits non-sensitive MCP
payload collection, but the existing key-name, value-shape, query/header, and
symbol scrubbers still run and remove credential/PII fixtures. This gate does
not use private SDK hooks, delete MCP spans, or rename away `tools/call`.

## Diagnosing after deploy (operator-only)

1. Confirm which of the four processes (API, TaskIQ worker, TaskIQ
   scheduler, MCP) is under investigation, and run its non-secret
   diagnostics: `uv run python -m scripts.sentry_profiling_canary` (default
   dry mode — prints `process_kind`, `sdk_version`, `enabled`,
   `traces_sample_rate`, `profiles_sample_rate`, `profiler_ready`; no init,
   no network call).
2. If `enabled=false`: `SENTRY_DSN` is not configured in that process's
   deployed environment — check the deploy env file, not the code path.
3. If `enabled=true` but `profiler_ready=false`: check whether
   `traces_sample_rate` or `profiles_sample_rate` is `0` in that
   environment — both must be `> 0` per the piggyback finding above.
4. If `profiler_ready=true` and Sentry still shows zero profiles: run the
   canary explicitly — `uv run python -m scripts.sentry_profiling_canary
   --send --confirm` (both flags required; omitting either fails closed,
   exit code 2). This sends exactly one fixed transaction
   (`sentry-profiling-canary`, op `canary.cpu_probe`) with a fixed
   deterministic CPU workload — no user input, no free text, no MCP
   prompt/result, no account/broker data. The canary is a dedicated non-MCP
   transaction: it must never use `/mcp`, `mcp.server`, or `tools/call`, so it
   cannot contaminate MCP usage telemetry.
5. Check the Sentry UI for a transaction named `sentry-profiling-canary` and
   whether it has a linked profile. Absence at this point means the gap is
   in one of: Sentry org/project entitlement (profiling not enabled on the
   plan), ingest-side sampling/rate-limiting, or something specific to the
   deployed environment that repository-time tests cannot observe (network
   egress, SDK version mismatch, a startup ordering issue). Classify which
   before making further changes — do not guess.
6. **Disable/rollback**: setting `SENTRY_PROFILES_SAMPLE_RATE=0` in that
   process's env is sufficient to stop profile collection without touching
   tracing or error reporting; no code change or redeploy of this repo's
   scrubber/init code is required for that rollback.

## What this PR explicitly does not do

- No production deploy, activation, or real canary send.
- No Sentry org/plan changes, event deletion, or credential rotation.
- No sampling-rate/cost policy decision.
- No scheduler/cron/TaskIQ/Prefect registration for the canary CLI — it is
  operator-invoked only.
