# KIS Client Circuit Breaker (ROB-699 + ROB-700)

## What it does

A per-process, in-process circuit breaker guards **two** KIS HTTP seams,
sharing the **one** module singleton:
- the data dispatch (`BaseKISClient._request_with_rate_limit_with_headers`,
  `app/services/brokers/kis/base.py`) — ROB-699.
- the OAuth token POST (`BaseKISClient._fetch_token`,
  `app/services/brokers/kis/base.py`) — ROB-700.

When the KIS host is unreachable (e.g. a maintenance window), the breaker
opens after N consecutive transport connect-failures on *either* seam and
fails fast with `KISCircuitOpen` — **zero HTTP call, zero rate-limit wait** —
instead of every KIS-dependent `/invest` reader burning the full
connect-timeout × retries on every request.

`KISCircuitOpen` is a plain `Exception` subclass, so it propagates through the
**existing** broad `except Exception` fallbacks with no new wiring:
- `InvestQuoteService._kis_fetch_kr` / `_kis_fetch_us` (per-symbol fetch → `None`)
- `PriceFallbackResolver._apply_layer` (KIS layer fails open → Toss layer fills)
- `KISHomeReader.fetch` (KIS account flow → warning, empty accounts/holdings)

When KIS is healthy the breaker stays closed and is a pure passthrough — this
change does not alter any broker mutation logic, retry classification, or
response parsing.

## Why the token fetch is also guarded (ROB-700)

Live measurement during the 2026-07-04 KIS maintenance window proved the
ROB-699 data-dispatch guard **never opened**: 0 circuit-breaker log lines. The
only KIS HTTP calls made were `POST /oauth2/token` (half of them connect-timing
out at ~5s) — **zero** data calls (`dailyprice`/`inquire-price`/
`inquire-balance`) were ever attempted. The reason: `_fetch_token` called
`httpx` directly, bypassing the breaker entirely, and the token fetch
connect-times-out *first*, so no token is ever obtained, so no data call is
ever dispatched, so the data-dispatch guard never sees a failure to record.

ROB-700 closes this gap by guarding `_fetch_token`'s network POST with the
**same** singleton: N consecutive **token** connect-failures now open the
breaker, after which both the token fetch and the data dispatch fail-fast with
`KISCircuitOpen`. The data-dispatch gate fail-fasts in ~0ms; the open-breaker
token fetch fail-fasts inside `_fetch_token`, but the call still runs *inside*
the token single-flight (`refresh_token_with_lock`), so on an empty token
cache it still pays the single-flight's pre-lock cache double-checks (~100ms —
2× `asyncio.sleep(0.05)` + a few Redis round-trips), never the full 5s connect
wait. Either way, `/invest` KIS readers hit their existing Toss fallback /
warning promptly instead of every reader independently burning the connect
timeout.

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
the flag off, `before_request()` / `record_*()` are complete no-ops on **both**
guarded seams — every data call passes straight through to the unmodified
retry/rate-limit dispatch, and every token fetch passes straight through to
the unmodified `_fetch_token` network POST, byte-identical to pre-ROB-699/
pre-ROB-700 behavior.

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
- **The OAuth token POST is breaker-guarded (ROB-700).** `_fetch_token`'s
  network POST shares the same singleton as the data dispatch — see "Why the
  token fetch is also guarded" above. The **cached-token fast path is
  byte-identical**: a cache hit in `_ensure_token` never calls `_fetch_token`,
  so the breaker is never touched on the hot, Redis-cached-token path. The
  token **single-flight / Redis-cache semantics are unchanged** — the breaker
  check is per-attempt, inside `_fetch_token`, before the POST, so
  `refresh_token_with_lock` still collapses a concurrent `/invest` burst into
  exactly one fetch attempt. **401 / invalid-key / non-JSON** token responses
  are *reachable* (KIS responded) and do **not** trip the breaker — only
  transport connect/read-hang failures do. Because token and data share one
  singleton, a token success (or a reachable non-2xx) resets the failure count
  for both surfaces, and vice versa.
- **First post-outage load still pays the full timeout.** A single `/invest`
  load fires many KIS calls concurrently; they all pass the closed-breaker gate
  before the Nth failure opens it, so the *first* load after an outage begins
  still burns the connect timeout. Only *subsequent* calls fail-fast. This is
  intentional — the breaker is a steady-state guard, not a first-request guard.
- **No broker mutation changes.** The breaker only affects how fast a
  connect-failure surfaces; order/holdings/quote semantics, retry classifiers
  (ROB-270/ROB-645), and 429 handling are unchanged.
