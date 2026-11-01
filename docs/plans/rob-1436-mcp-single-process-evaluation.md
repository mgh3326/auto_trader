# Serving several low-traffic MCP profiles from one process (ROB-1436)

Status: evaluation only. Nothing here is implemented. It needs an operator
decision before any code changes.

## Question

After ROB-1436, NCP runs three fixed MCP units next to the DEFAULT blue/green
pair: kiwoom (8772), h3-crypto-paper (8776) and h3-us-paper (8777). Each is its
own Python process from the same image. Could one process serve all three
endpoints and still keep each endpoint's closed tool set (#1171
h3-crypto-paper, #1257 h3-us-paper)?

## Shape it would take

One Python process builds three `FastMCP` instances. Each instance gets
`register_all_tools(instance, profile=...)`, its own auth provider and token,
and its own middleware stack, and is mounted on its own port (or path) behind
the existing HAProxy frontends. Registration is already per instance: the
closed-world registrars (`h3_crypto_paper_registration`,
`h3_us_paper_registration`, and `kiwoom_kr_registration` if a unit moves to
that profile) wrap the instance they are given in an exact-set
proxy and fail the boot when the registered set differs from the reviewed
list. Each endpoint's tool set therefore stays closed by construction. A tool
object registered on one instance is not reachable from another instance's
`tools/list` or `tools/call`.

## Memory estimate

Measured on a developer Mac (macOS physical footprint), one process registering
real `FastMCP` instances in turn, with no server running and no tool called:

| Registered so far in the process | Footprint |
|---|---:|
| h3-us-paper | 220.7 MB |
| + h3-crypto-paper | 221.2 MB |
| + kiwoom (134 tools) | 223.9 MB |

Three separate processes at about 221 MB each come to about 663 MB, so at boot
one process saves about 440 MB. Each extra profile costs under 3 MB.

Reading: the shared part (interpreter, fastmcp, pydantic, SQLAlchemy,
the app's service modules and, once used, pandas and yfinance) is paid once
instead of three times. Each extra profile adds only its registration objects.
The prod figures in the brief (about 250 MB per unit at baseline, about 600 MB
for h3-us-paper after use) include per-process request-time state: caches,
pooled connections and data frames. Those do not shrink when processes merge;
they add up inside one heap. So, as an estimate rather than a measurement:
three units at about 250 MB baseline (about 750 MB today) become one process
at about 250-300 MB baseline, plus the sum of the working sets. That saves on
the order of 450-500 MB of baseline on NCP. Confirm with `docker stats` on the
host before deciding (the measurement gaps are listed in
[rob-1436-go-port-candidates.md](rob-1436-go-port-candidates.md)).

## What breaks or must change

1. **Process-global profile state.** `app/monitoring/sentry.py:1563-1616`
   tags every span `mcp.profile` from the process env `MCP_PROFILE`. One
   process would tag every endpoint's spans the same, and the per-profile
   Sentry counts this decision was based on would stop working. The tag must
   come from the serving instance, for example a middleware that sets it per
   request, as `ToolCallLogMiddleware(profile=...)` already does for logs.
2. **Startup validation is per profile.** `main.py`
   (`_validate_profile_auth_token`, `_validate_profile_runtime_settings`)
   checks one profile per process: a token for network transports, and the
   Kiwoom mock host and credentials for `kiwoom_kr`. It would have to run
   once per endpoint, and one endpoint's misconfiguration would then block
   the boot of all three (or each endpoint must fail alone, which is new
   code).
3. **Lazy imports stop paying off.** The ROB-1436 deferral holds only until
   any endpoint touches pandas. With three endpoints in one process, the first
   h3 session loads it for all of them.
4. **Fault isolation.** All endpoints share one event loop. A blocking call
   or a CPU-heavy tool on one endpoint stalls the other two (the ROB-469
   single-point-of-failure finding). `ToolTimeoutMiddleware` bounds awaited
   time, not a blocked loop. Today a kiwoom stall cannot touch the h3 pilots.
5. **Credentials in one address space.** `h3-us-paper` runs every tool body
   inside `broker_credentials_blocked` (a ContextVar, so per call and safe to
   share). But one process would hold all three endpoints' bearer tokens and
   the broker settings the kiwoom endpoint needs. Today every unit already
   gets both env files (`ENV_FILE_ARGS` in `scripts/deploy-ncp-pull.sh`), so
   the token-isolation loss is smaller than it looks. It is still a larger
   blast radius for a remote-code bug in any one endpoint.
6. **Deploy and rollback granularity.** `MCP_UNITS_SKIP`, per-unit digest
   capture, per-unit rollback and the per-unit tailnet route probe all assume
   one container per profile. Merging means one restart for all three, and one
   failed health check rolls back all three.
7. **Closed-world proof.** The h3 contract tests (`test_h3_crypto_paper_profile.py`,
   `test_h3_us_paper_profile.py`) prove the closed set per registration. A
   merged process also needs a test that endpoint A's transport cannot
   dispatch endpoint B's tool names (routing isolation), which does not exist
   today.

## Recommendation

Not now. The saving is real (an estimated 450-500 MB baseline), but the
risks fall on the paths the operator cares about: the h3 pilots' isolation,
per-profile Sentry evidence and per-unit rollback. Cheaper steps first:

1. Retire unused units (done in ROB-1436).
2. Defer heavy imports on idle units (done in ROB-1436).
3. Measure real per-container memory on NCP for a week with `docker stats`.
4. If memory is still the constraint, merge the two h3 pilots first: they
   share a lifecycle and are both paper-only. Keep kiwoom separate, and move
   the Sentry profile tag to per-instance first.
