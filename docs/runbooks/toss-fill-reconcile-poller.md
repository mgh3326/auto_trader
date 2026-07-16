# Toss Fill Reconcile Poller — Manual Runner (ROB-925)

## Problem

Toss has no broker websocket. The only path that books a confirmed fill into
`review.execution_ledger` / `review.trades` / trade journals is
`toss_reconcile_orders` (MCP) or the equivalent service kernel
(`toss_reconcile_orders_impl`). Absent a live session calling that manually,
a fill sits unbooked indefinitely — a 6월 fill was only discovered and
backfilled in 7월 (~24 days later).

`app/tasks/toss_live_reconcile_tasks.py::toss_live_poll_fills_periodic` (ROB-757)
already implements the discover+reconcile kernels with a market-session gate
and a kill switch, but it is a TaskIQ task: its in-repo `schedule=` is only
non-empty when `TOSS_FILL_POLL_ENABLED=true`, and turning that cadence on
requires the TaskIQ worker + scheduler infra to be live. Per this repo's
"manual verification before automation" convention, ROB-925 ships a
standalone CLI **before** that promotion so an operator can run reps by hand
first.

`scripts/toss_fill_reconcile_poller.py` wraps the exact same kernels
(`TossFillPollerService.discover_external_orders` +
`toss_reconcile_orders_impl`) as a single-shot process. It introduces **no
new booking logic and no new broker mutation path** — order place / modify /
cancel code is untouched, and every write goes through the same
already-idempotent reconcile kernel used by `toss_reconcile_orders` today.

## Modes

| Mode | Flag | Broker calls | DB writes | What it does |
|---|---|---|---|---|
| Preview (default) | *(none)* / `--dry-run` | 0 | 0 | Lists the current open `toss_live_order_ledger` rows a real run would scan (`TossLiveOrderLedgerService.list_open`). |
| Commit | `--commit` | yes (read + write) | yes | Runs `discover_external_orders(dry_run=False)` then `toss_reconcile_orders_impl(dry_run=False)` — identical to what the TaskIQ poller does per cycle. |

Both modes short-circuit to **zero broker calls** before touching Toss if
either gate below is closed — this holds for `--commit` too, not just
preview.

## Gates (checked in this order, before any broker call)

1. **Kill switch** — `TOSS_FILL_POLL_ENABLED` (default `false`). This is the
   same flag that gates the TaskIQ poller's schedule (ROB-757) — intentionally
   reused rather than adding a second flag, so there is one on/off decision
   for "should the fill poller run at all" regardless of which process runs
   it. `false` → `{"status": "disabled", ...}`, exits 0, no I/O.
2. **Market-session gate** — `_toss_fill_poll_market_gate()`
   (`app/tasks/toss_live_reconcile_tasks.py`, reused as-is): active when the
   KRX regular session (09:00–20:00 KST) is open **or** the US session
   (pre-market/regular/after-hours, `us_market_session`) is open. Gated by
   `TOSS_FILL_POLL_MARKET_GATE_ENABLED` (default `true`). Inactive →
   `{"status": "skipped", "gate": {...}}`, exits 0, no broker call.

## Env / config knobs

| Env var | Default | Purpose |
|---|---|---|
| `TOSS_FILL_POLL_ENABLED` | `false` | Kill switch — must be `true` for `--commit` (or preview's gate check) to proceed past the first gate. |
| `TOSS_FILL_POLL_MARKET_GATE_ENABLED` | `true` | Set `false` only to force a run outside session hours for testing; do not disable in production. |
| `TOSS_FILL_POLL_LOOKBACK_DAYS` | `7` | How far back `discover_external_orders` scans closed orders when no prior watermark exists. |
| `TOSS_FILL_POLL_CLOSED_PAGE_CAP` | `20` | Bounded pagination cap on the closed-order scan (no unbounded loop). |
| `TOSS_FILL_POLL_RECONCILE_LIMIT` | `100` | Per-run cap on the number of open ledger rows reconciled (order/symbol cap for AC5). |
| `TOSS_API_ENABLED` + Toss credentials | — | Required by `TossReadClient.from_settings()`; unrelated to this poller's own gates. |

Per-request rate limiting (TPS, including the 09:00–09:10 KST order-window
throttle) is enforced by `TossReadClient`'s shared process-global rate
limiter (`app/services/brokers/toss/rate_limiter.py`) — this script does not
add its own sleep/backoff, it just reuses the same client every other Toss
caller uses, so it inherits the same limits without a second, potentially
divergent implementation.

## Manual reps procedure

Run from the repo root with `TOSS_FILL_POLL_ENABLED=true` exported (or
inline per-command as below). All commands are safe to re-run.

**1. DRY_RUN — confirm the scan target list, zero broker calls:**

```bash
TOSS_FILL_POLL_ENABLED=true uv run python -m scripts.toss_fill_reconcile_poller
```

Expect `{"status": "preview", "target_count": N, "targets": [...]}` (or
`{"status": "skipped", ...}` outside market hours — this is correct, not a
failure). Inspect `targets` against what you expect to be open right now.

**2. Single real run:**

```bash
TOSS_FILL_POLL_ENABLED=true uv run python -m scripts.toss_fill_reconcile_poller --commit
```

Expect `{"status": "ran", "success": true, "discover": {...}, "reconcile": {...}, "booked_symbols": [...]}`.
Re-running this immediately is safe — the second pass should report
`booked_symbols: []` and `reconcile.counts` dominated by
`noop_already_booked` for rows the first pass already booked (idempotent by
construction: `_reconcile_one_toss_row` in
`app/mcp_server/tooling/toss_live_ledger.py` compares `delta = broker_cum -
already_filled_qty` and no-ops when `delta <= 0`).

**3. Verification query** (run against the DB the process pointed at):

```sql
SELECT id, market, symbol, broker_order_id, status, filled_qty,
       avg_fill_price, trade_id, journal_id, updated_at
FROM review.toss_live_order_ledger
WHERE updated_at >= now() - interval '15 minutes'
ORDER BY updated_at DESC;
```

Confirm newly `filled`/`partial` rows carry a non-null `trade_id` /
`journal_id` (buy) and that no row was booked twice (compare `filled_qty`
against the known broker quantity — it should match, not double).

Optional scope narrowing: `--market kr` / `--market us` limits both the
preview list and the commit-mode reconcile pass to one market (the discover
scan itself is always both-market, matching the existing TaskIQ poller).

## Exit codes / output contract

- Always prints one JSON line to stdout, `status` ∈
  `{disabled, skipped, preview, ran, error}`.
- Exit `0` for `disabled` / `skipped` / `preview` / `ran` (all are expected
  no-op-or-success outcomes — a closed market window is not a failure).
- Exit `1` only when an unhandled exception occurs (`status: "error"` with
  `error.type` / `error.message`); nothing is swallowed.

## Future: launchd promotion (not applied by this issue)

This issue ships the runner + docs only. Registering a recurring schedule is
a separate operator decision, mirroring the existing
`~/ops/fill-event-triage/poller.sh` + launchd pattern
(`docs/runbooks/fill-event-claude-triage.md` §4). A draft plist for when that
decision is made:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.operator.toss-fill-reconcile-poller</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/env</string>
    <string>uv</string>
    <string>run</string>
    <string>python</string>
    <string>-m</string>
    <string>scripts.toss_fill_reconcile_poller</string>
    <string>--commit</string>
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>TOSS_FILL_POLL_ENABLED</key><string>true</string>
  </dict>
  <key>WorkingDirectory</key><string>/Users/USERNAME/work/auto_trader</string>
  <key>StartInterval</key><integer>120</integer>
  <key>StandardOutPath</key><string>/Users/USERNAME/.local/state/toss-fill-reconcile-poller/stdout.log</string>
  <key>StandardErrorPath</key><string>/Users/USERNAME/.local/state/toss-fill-reconcile-poller/stderr.log</string>
</dict></plist>
```

`StartInterval=120` (2 minutes) matches the existing
`TOSS_FILL_POLL_CRON` default (`*/2 * * * *`, see `app/core/config.py`) so
both vehicles imply the same target cadence. Do not register this plist
until: (a) several manual reps above have been observed clean, (b) the
production `.env` has `TOSS_FILL_POLL_ENABLED=true` and valid Toss
credentials, and (c) an operator has reviewed log rotation for the state
dir. Registration itself (`launchctl bootstrap gui/$UID ...`) is out of
scope for this issue — see `docs/runbooks/fill-event-claude-triage.md` §4
for the exact bootstrap/bootout commands, which apply unchanged to this
plist.

## Relationship to other Toss reconcile docs

- `docs/runbooks/toss-live-order-reconcile.md` — the underlying
  `toss_reconcile_orders` contract, status semantics, and the ROB-757 fill
  poller's TaskIQ-task framing. Read this first for what "reconcile" means.
- `docs/runbooks/fill-event-claude-triage.md` — the downstream consumer:
  once a fill lands in `review.execution_ledger` (source=`reconciler`,
  broker=`toss`), a separately-scheduled poller triages it (ROB-755/ROB-926).
  This runner's job ends at booking; it does not triage or notify beyond the
  existing `TOSS_FILL_NOTIFY_ENABLED` fill-notification path inside
  `toss_reconcile_orders_impl`.
