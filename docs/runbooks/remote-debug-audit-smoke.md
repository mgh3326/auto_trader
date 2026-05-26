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

Output envelope (ROB-325):

```json
{
  "step": "audit",
  "target_kind": "report",        // or "bundle"
  "report_uuid": "...",            // null when --bundle-uuid was used
  "bundle_uuid": "...",
  "checked_symbols": 5,
  "symbols_resolved": 3,           // Naver resolved a price (ok|mismatch)
  "audit": { "source": "naver_remote_debug", "findings": [], "gaps": [], "affects_report_generation": false }
}
```

`audit.gaps` severities are `info`/`warning` only — this audit never gates
report generation or publish.

### Acceptance (live smoke)

The fix landed correctly when `checked_symbols > 0` **and**
`symbols_resolved > 0` (i.e. not every symbol comes back
`naver_symbol_unresolved`). `symbols_resolved` counts symbols Naver actually
resolved, independent of the auto_trader side, so it is the direct signal that
the CDP render-wait works. The CLI exit code mirrors this (see below).

## Render wait (ROB-325)

`CdpClient.fetch_rendered` enables the Page + Runtime domains, then polls a
price-selector readiness check (`NAVER_READY_JS`) before the final extraction.
The poll is bounded by the per-symbol timeout (15s) and a hard max-poll cap, so
a slow/blocked page can never loop forever — it falls open to `unavailable`.
Extraction tries an ordered list of Naver selector variants
(`NAVER_PRICE_SELECTORS` / `NAVER_NAME_SELECTORS`); if Naver changes its DOM,
add the new selector to those lists.

## Safety

- Host-locked to `127.0.0.1:9222` (strict equality).
- Read-only: zero DB writes; no broker/order/watch/order-intent.
- Naver access happens only here — never in the frontend request path or
  server-side `ensure()`. The `ensure()` registry stubs are unchanged.
- Exit codes:
  - `0` audit completed and at least one symbol resolved on Naver.
  - `2` disabled / no Chrome at 127.0.0.1:9222 / bundle not found.
  - `3` audit completed but zero symbols resolved — operator-actionable
    (env/config or external Naver page change), **not** a report-generation
    failure (generation never calls this audit).

## Scope

KR symbols only (Naver = KRX). US / Toss / Upbit / browser_probe and persisted
audit results are future slices.
