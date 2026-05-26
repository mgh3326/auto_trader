# Remote-Debug Data-Quality Audit Smoke (ROB-323)

Operator-only, default-disabled, read-only. Cross-checks a report/bundle's KR
symbols against the logged-in Naver finance pages via the operator's Chrome at
`127.0.0.1:9222`. Prints a JSON audit to stdout. **No DB writes, no orders.**

## 1. Launch the logged-in Chrome (operator macbook)

```bash
open -na "Google Chrome" --args \
  --remote-debugging-address=127.0.0.1 \
  --remote-debugging-port=9222 \
  --user-data-dir="$HOME/.hermes/chrome-toss-debug"
```

This profile keeps Naver/Toss/Upbit/TradingView logins. Do NOT use a fresh or
default profile.

## 2. Enable + preflight

```bash
export REMOTE_DEBUG_AUDIT_ENABLED=true
uv run python -m scripts.remote_debug_audit_smoke --mode preflight
```

`ok=false` lists missing env KEY names only (never values).

## 3. Audit a bundle (or report)

```bash
uv run python -m scripts.remote_debug_audit_smoke --mode audit \
  --bundle-uuid <uuid> --max-symbols 10
# or
uv run python -m scripts.remote_debug_audit_smoke --mode audit \
  --report-uuid <uuid> --max-symbols 10
```

Output: `{source, snapshot_bundle_uuid, findings[], gaps[], affects_report_generation:false}`.
`gaps` severities are `info`/`warning` only — this audit never gates report
generation or publish.

## Safety

- Host-locked to `127.0.0.1:9222` (strict equality).
- Read-only: zero DB writes; no broker/order/watch/order-intent.
- Naver access happens only here — never in the frontend request path or
  server-side `ensure()`. The `ensure()` registry stubs are unchanged.
- Exit codes: `0` audit completed (any number of gaps); `2` disabled / no Chrome
  at 127.0.0.1:9222 / bundle not found.

## Scope

KR symbols only (Naver = KRX). US / Toss / Upbit / browser_probe and persisted
audit results are future slices.
