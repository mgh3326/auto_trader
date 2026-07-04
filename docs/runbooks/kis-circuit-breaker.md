# KIS Client Circuit Breaker (ROB-699)

## What it does

A per-process, in-process circuit breaker guards the single KIS HTTP dispatch
seam (`BaseKISClient._request_with_rate_limit_with_headers`,
`app/services/brokers/kis/base.py`). When the KIS host is unreachable (e.g. a
maintenance window), the breaker opens after N consecutive transport
connect-failures and fails fast with `KISCircuitOpen` — **zero HTTP call, zero
rate-limit wait** — instead of every KIS-dependent `/invest` reader burning the
full connect-timeout × retries on every request.

`KISCircuitOpen` is a plain `Exception` subclass, so it propagates through the
**existing** broad `except Exception` fallbacks with no new wiring:
- `InvestQuoteService._kis_fetch_kr` / `_kis_fetch_us` (per-symbol fetch → `None`)
- `PriceFallbackResolver._apply_layer` (KIS layer fails open → Toss layer fills)
- `KISHomeReader.fetch` (KIS account flow → warning, empty accounts/holdings)

When KIS is healthy the breaker stays closed and is a pure passthrough — this
change does not alter any broker mutation logic, retry classification, or
response parsing.

## What trips it

Trips **only** on transport connect/read-hang failures:
- `httpx.ConnectTimeout`
- `httpx.ConnectError`
- `httpx.PoolTimeout`
- `httpx.ReadTimeout`
- `ConnectionRefusedError`

`ReadTimeout` is included on purpose: during a maintenance window the KIS load
balancer often **accepts the TCP connection** but the backend never responds,
so the outage manifests as a *read* timeout, not a connect error. `ReadTimeout`
and `ConnectTimeout` are sibling `httpx.TimeoutException` subclasses (neither is
a subclass of the other), so a trip set that only lists connect errors would
never open the breaker for this very plausible outage shape.

**Never trips** on: `httpx.WriteTimeout`, `httpx.HTTPStatusError` (including
429), `RateLimitExceededError`, or KIS business `RuntimeError`s — all of these
mean KIS was *reached*, so a normal 2xx/non-2xx round trip resets the failure
count (closed) or closes a half-open probe.

## State machine

```
CLOSED  --(N consecutive connect-failures)-->  OPEN
OPEN    --(before_request while elapsed < cooldown)-->  raise KISCircuitOpen (0 HTTP, 0 wait)
OPEN    --(before_request once elapsed >= cooldown)-->  HALF_OPEN, hand out EXACTLY ONE probe
HALF_OPEN(probe in flight) --(before_request)-->  raise KISCircuitOpen (stampede guard)
HALF_OPEN  --(probe success / probe reached KIS)-->  CLOSED (failures reset)
HALF_OPEN  --(probe connect-failure)-->  OPEN (cooldown restarts from now)
```

State is a **module-level singleton** (`app/services/brokers/kis/circuit_breaker.py`
— `get_kis_circuit_breaker()` / `reset_kis_circuit_breaker()`), shared across
every `BaseKISClient` instance in the process (live and KIS-mock alike). No
Redis — this is a per-process, best-effort fail-fast guard, not a distributed
rate limiter.

## Defaults

| Setting | Default | Env var |
|---|---|---|
| `kis_circuit_breaker_enabled` | `True` | `KIS_CIRCUIT_BREAKER_ENABLED` |
| `kis_circuit_breaker_failure_threshold` | `5` (consecutive connect-failures) | `KIS_CIRCUIT_BREAKER_FAILURE_THRESHOLD` |
| `kis_circuit_breaker_cooldown_seconds` | `45` (open → half-open) | `KIS_CIRCUIT_BREAKER_COOLDOWN_SECONDS` |

Defaults are estimates seeded from the Sentry incident (`/invest/api/home`
averaging 27.5s during a KIS outage, driven by ~5s connect timeout × retries
per call). Tune from production after rollout; all three are env-overridable
with zero deploy.

## How to disable in an incident

Set `KIS_CIRCUIT_BREAKER_ENABLED=false` and restart the process (or, in a
context where settings are mutated at runtime, flip the flag directly). With
the flag off, `before_request()` / `record_*()` are complete no-ops — every
call passes straight through to the unmodified retry/rate-limit dispatch,
byte-identical to pre-ROB-699 behavior.

## Observability — log lines to grep

- `WARNING "KIS circuit OPEN after N consecutive connect-failures; failing fast for Ns"`
  — the breaker just opened.
- `INFO "KIS circuit half-open: allowing one probe request"` — cooldown elapsed,
  a single probe request is being let through.
- `INFO "KIS circuit closed: probe succeeded"` / `"KIS circuit closed: probe
  reached KIS (non-2xx)"` — the probe proved KIS is reachable again; circuit closed.
- `WARNING "KIS circuit re-opened: probe connect-failure"` — the probe itself
  hit a connect-failure; cooldown restarts.

## Scope notes

- **Mock ↔ live share one breaker.** The singleton is process-wide, so a live
  KIS outage will also fail-fast KIS-mock calls in the same process, and vice
  versa. During real KIS maintenance both hosts are typically down together;
  revisit only if false-coupling is observed in practice.
- **The OAuth token endpoint is not breaker-guarded.** `/oauth2/token` is a
  different host path, and the Redis-cached token means the hot price/order
  path does not hit it on every call.
- **First post-outage load still pays the full timeout.** A single `/invest`
  load fires many KIS calls concurrently; they all pass the closed-breaker gate
  before the Nth failure opens it, so the *first* load after an outage begins
  still burns the connect timeout. Only *subsequent* calls fail-fast. This is
  intentional — the breaker is a steady-state guard, not a first-request guard.
- **No broker mutation changes.** The breaker only affects how fast a
  connect-failure surfaces; order/holdings/quote semantics, retry classifiers
  (ROB-270/ROB-645), and 429 handling are unchanged.
