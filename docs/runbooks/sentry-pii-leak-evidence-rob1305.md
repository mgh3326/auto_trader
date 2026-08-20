# Sentry leak-evidence inventory (ROB-1305)

## Scope and method

This inventory documents what the **pre-fix** Sentry scrubbing gap (ROB-1305 W0)
allowed into Sentry, using **aggregate-only** Sentry search queries against the
`auto_trader` project (org `mgh3326-daum`). No row below contains a credential
value, a masked fragment, a hash, a URL, a header, or a screenshot — every row
is limited to: credential category, a bounded observation period, an event or
span count, and an optional safe source-class label. This matches ROB-1305 D6/
AC-D6 exactly; do not add columns beyond these when updating this table.

Queries were run 2026-08-20 via the Sentry MCP `search_events` tool
(`dataset=spans|errors|logs`, aggregate `count()` only — no individual event
bodies were opened or copied).

## Evidence table

| Credential category | Observation period | Count | Source class |
|---|---|---|---|
| Telegram bot API token (embedded in URL path, e.g. `.../bot<token>/sendMessage`) | 2026-08-06 → 2026-08-20 (14d) | 548 | transaction span (`span.description`, httpx spans) |
| Telegram bot API token (embedded in URL path) | 2026-08-06 → 2026-08-20 (14d) | 20 | error event message |
| Telegram bot API token (embedded in URL path) | 2026-08-06 → 2026-08-20 (14d) | 20 | log entry message |
| Broker/KIS API key or app-secret in a URL query string (`app_key=`, `app_secret=`, `api_key=`, `access_token=`) | 2026-08-06 → 2026-08-20 (14d) | 0 | transaction span (`span.description`) |

## Blocked / undetermined rows

- **Broker/KIS `appkey`/`appsecret` exposure via HTTP request headers on httpx
  spans** (as opposed to URL query strings, which are ruled out above at 0):
  KIS sends these as headers (`app/services/brokers/kis/base.py`), and Sentry's
  httpx integration can attach request headers as span *data* attributes when
  `SENTRY_SEND_DEFAULT_PII` is true (the pre-fix repo default). No safe
  aggregate query was found that counts spans by a specific header-attribute
  key without a per-project schema lookup that risks surfacing raw attribute
  values. This row is reported as **blocked**, not fabricated as zero.
  Consequence: treat KIS `appkey`/`appsecret` as **suspect for header-path
  exposure** in the rotation runbook even though this specific count is
  unresolved.
- Only the `auto_trader` Sentry project was queried (the only project this
  repository's Sentry integration writes to). `brewdial-node` /
  `brewdial-react` are unrelated projects and were not queried.
- 14 days was chosen as the longest single bounded window the Sentry MCP
  `search_events` tool accepts in one call (`period` enum tops out at `90d`;
  14d was used to match the ROB-1305 investigation's original observation
  window cited in Linear). A longer-window re-run is safe to do later with the
  same aggregate-only method.

## What this evidence means (context, not a value)

- The counts above are **consistent with the code-level root cause fixed in
  this PR**: `before_send_transaction` did not scrub non-`mcp.server` span
  descriptions/data before this change, so any httpx span whose URL embedded a
  Telegram bot token (`app/monitoring/trade_notifier/transports.py` builds
  URLs as `https://api.telegram.org/bot{bot_token}/sendMessage`) reached
  Sentry unredacted. The fix in `app/monitoring/sentry.py`
  (`_scrub_secret_shapes_in_string`, applied to every span and to the
  transaction `request` block) closes this specific path going forward.
- The KIS query-string count of 0 is consistent with KIS sending its
  `appkey`/`appsecret` as HTTP headers, not query parameters — this is why the
  blocked header-path row above is flagged as suspect rather than cleared.
- This table does **not** state whether the exposed events still exist in
  Sentry, whether the org's data retention window has already aged them out,
  or whether any external party accessed them. That determination and any
  purge/retention decision is out of scope for this document (see the
  companion rotation runbook, `sentry-credential-rotation.md`).

## Credentials that should be treated as rotation candidates

Derived from the evidence above — see `sentry-credential-rotation.md` for the
runbook. Rotation scope, timing, and execution remain **operator decisions**;
nothing in this document performs or schedules a rotation.

1. Telegram bot token (`TELEGRAM_TOKEN`) — **confirmed** exposure (548 span +
   20 error + 20 log occurrences, 14d window).
2. KIS `app_key` / `app_secret` (`KIS_APP_KEY`, `KIS_APP_SECRET`, and mock
   variants) — **suspect, unconfirmed** (header-path query blocked; ruled out
   via query string).
