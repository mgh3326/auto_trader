# Sentry leak-evidence inventory (ROB-1305)

## Scope and method

This inventory documents what the **pre-fix** Sentry scrubbing gap (ROB-1305 W0)
allowed into Sentry, using **aggregate-only** Sentry search queries against the
project this repository's Sentry integration writes to. No row below contains
a credential value, a masked or partial value, a URL, a URL template, a query
or header parameter name, an environment variable name, or a screenshot —
every row is limited to: leak category (high-level), a bounded observation
period, an event/span/log count with its unit, and an optional safe source
class. This matches ROB-1305's hard invariant on the evidence table exactly;
do not add columns or reintroduce identifying detail when updating this table.

Queries were run 2026-08-20 using aggregate-count-only searches — no
individual event body was opened or copied.

## Evidence table

| Leak category | Observation period | Count | Source class |
|---|---|---|---|
| Messaging-bot credential embedded in an outbound notification URL | 2026-08-06 → 2026-08-20 (14d) | 548 spans | transaction span |
| Messaging-bot credential embedded in an outbound notification URL | 2026-08-06 → 2026-08-20 (14d) | 20 events | error event |
| Messaging-bot credential embedded in an outbound notification URL | 2026-08-06 → 2026-08-20 (14d) | 20 entries | log entry |
| Broker API credential in a URL query string | 2026-08-06 → 2026-08-20 (14d) | 0 spans | transaction span |
| Broker API credential attached as an HTTP request header on a span | 2026-08-06 → 2026-08-20 | 집계 불가/미확인 (not aggregated / undetermined) | — |

The last row's reason is limited to: no safe aggregate query available for
this shape without risking exposure of the underlying attribute. It is
reported as undetermined rather than assigned a fabricated count of zero.

## Notes

- Only this repository's own Sentry project was queried. Unrelated projects
  in the same organization were not queried.
- 14 days was the observation window used; a longer-window re-run with the
  same aggregate-only method is safe to do later.
- This table does not state whether the counted events still exist in
  Sentry, whether the org's data retention window has already aged them out,
  or whether any external party accessed them. Retention/purge decisions and
  rotation candidates, dependency mapping, and environment-variable-level
  detail belong only in the companion rotation runbook
  (`sentry-credential-rotation.md`), not in this evidence document.
