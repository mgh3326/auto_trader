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
    and a `profile` item, linked via `profile.transactions[0].id ==
    transaction.event_id` (the actual sentry-sdk 2.x linkage mechanism —
    **not** a `contexts.profile` field, which this SDK version does not
    populate on the transaction event).
  - `profiles_sample_rate` alone is **not sufficient**: with
    `traces_sample_rate=0.0`, no profile is produced even at
    `profiles_sample_rate=1.0`. The transaction profiler piggybacks on trace
    sampling in sentry-sdk 2.57.0.
  - an MCP `tools/call` transaction with a secret-KEY-named span field
    (`mcp.request.argument.api_key`) is scrubbed to `[Filtered]`, the
    `mcp.server` span and the `tools/call <tool>` transaction rename survive,
    and the profile item is still present and linked.
  - the ROB-1880 repo-wide socket guard reports zero blocked network
    attempts for every test in this file (`ROB-1296 external HTTP boundary:
    0 blocked requests` / `ROB-1880 socket guard: ... blocked_attempts=0`).
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
  `filename` / `module` / `function` values — e.g. this repo's own source
  tree path on whatever host runs the process (locally, or `/app/...` in the
  production container image, per `_BUILD_VCS_REF_PATH`).
- This is **not** a secret-value leak: sentry-sdk's transaction profiler is
  a statistical stack sampler — it records which function/file/line was
  executing, never local variable values. A fake secret placed in a local
  variable during the sampled call never appears in the frame data (verified
  in the same test). This differs from *exception* stack traces with
  captured locals, which this repo's existing `_before_send`/
  `_sanitize_in_place` scrubber does cover.
- This is architecturally consistent with how this repo already treats
  frame metadata for regular error events — `abs_path`/`filename` on
  exception frames are not redacted there either; only known-sensitive
  keys/values are.
- No public-SDK remediation path exists today. If the operator later decides
  container filesystem paths in profile data are unacceptable disclosure,
  that is a **separate decision** requiring either a private-SDK-internals
  workaround (against this repo's "public SDK APIs only" invariant) or
  waiting on upstream sentry-sdk to add a profile-scrubbing hook — it is
  called out here, not silently left as an unstated assumption.

## MCP tool-argument scrubbing: existing scope, not changed here

`app/monitoring/sentry.py::_safe_mcp_span_argument` scrubs
`mcp.request.argument.*` span data by known sensitive key name/shape
(`_SENSITIVE_KEYWORDS`) and known high-cardinality symbol field names
(`_SYMBOL_FIELD_NAMES`) — it is not a blanket suppression of every argument
value. Free-text tool arguments that don't match either list are recorded
(truncated to 1024 chars) as span data by design (ROB-1305). This PR
verifies the existing key-name-based scrub (a secret-named field like
`api_key` is filtered) and leaves that scope untouched — widening it to
suppress arbitrary free-text argument content is a separate decision outside
this diagnosis brief, and the hard invariant here is to *preserve* the
ROB-1305 scrubber, not redesign it. `SENTRY_MCP_INCLUDE_PROMPTS` (the SDK's
own MCP-integration prompt/result capture toggle, independent of the above)
remains default `false` everywhere, verified by the doc-drift tests above.

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
   prompt/result, no account/broker data.
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
