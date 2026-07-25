# Crash-Day Advisory Policy — Manual kis_mock Reps (ROB-932)

## What this is

`config/trading_policy.yaml` now carries a `crash_day` advisory block
(ROB-932, epic ROB-927). It is **judgment guidance only** — there is no code
enforcement anywhere in the order/execution path, and this PR does not touch
`order_execution` / `order_validation` / any `orders_*_variants` module. A
session reads `get_trading_policy(...)`'s echoed `crash_day` section the same
way it reads any other advisory threshold, and decides for itself whether to
act on it.

```yaml
crash_day:
  trigger:
    index_symbol: "069500"        # KODEX200
    index_gap_pct_max: -3.0       # open vs prev close, 09:00-09:05 판정
  actions:
    new_entry_hold: true
    deep_rung_reprice_to_band_floor: true
    profit_trim_marketable_allowed: true
    defensive_brief_cross_check: true
```

## Confirmed decisions (do not re-litigate)

- **Trigger = gap only.** KODEX200(069500) open vs prior close, judged in the
  09:00–09:05 KST window. There is **no intraday trigger** in this version.
- **LIMITATION**: intraday-only crashes are NOT covered. 2026-07-13 is the
  reference case — gap was only -0.8% at the open (would not have fired) but
  the index fell to -9.8% intraday. This is a known, documented gap in this
  minimal version, not an oversight. A future version may add an intraday
  trigger; that is out of scope here.
- **`new_entry_hold` = NEW entries only.** Averaging-down / adding to an
  existing position at a deeper rung is explicitly **exempt** — the
  2026-07-16 midday dip-buys were measured effective and this policy must not
  discourage that pattern.
- **`defensive_trim` execution support is deferred.** This PR does not wire
  any exit_intent / defensive_trim execution path. `profit_trim_marketable_allowed`
  here means: an operator or session *may* choose to use the existing
  ROB-912 marketable-sell band (-2%) for a profit-side trim during a crash
  day, exactly as they could on any other day — the policy block does not
  grant a new capability, it just flags that doing so is advisory-sanctioned
  during a crash day. Whether to build dedicated defensive_trim execution
  support is a separate, still-open design question.
- **Cross-check, not override.** `defensive_brief_cross_check` means: when
  `crash_day` fires, cross-reference the ROB-930 preopen shadow §0-R
  `gap_risk` verdict (already wired into the preopen session) rather than
  treating this trigger as a standalone signal.

## Why manual reps first

Per this repo's "verify manually before automating" convention, no
scheduler, cron, or TaskIQ task is introduced by this policy addition, and
none should be until a human has watched the trigger behave correctly across
a real sample of trading days. **This runbook is for a session or operator
to run by hand, once per trading day, for 4 weeks.** There is no code path
that calls `get_trading_policy` on a timer for this purpose.

## Daily procedure (repeat once per trading day, ~09:05 KST)

1. **Judge the trigger.** Get KODEX200 (069500) today's open and yesterday's
   close (any existing quote/candle MCP tool is fine — this is a read, no
   new tool is introduced by ROB-932). Compute:

   ```
   gap_pct = (open - prev_close) / prev_close * 100
   fired = gap_pct <= -3.0
   ```

2. **Record one line** (append to your own working log — this repo does not
   ship a dedicated log file for this):

   ```
   [ROB-932-mock] date=YYYY-MM-DD gap=X.X% fired=yes|no | actions_taken(mock): ... | roundtrip_cost: ...
   ```

3. **If `fired=no`:** log the line with `actions_taken(mock): none` and stop.
   This is the expected outcome most days — confirming **zero false-fires**
   on non-crash days is itself part of what these reps are measuring.

4. **If `fired=yes`:** using `account_mode="kis_mock"` (the shared
   `place_order` / `modify_order` / `cancel_order` MCP tools' KIS mock
   routing — `app/mcp_server/tooling/orders_registration.py`, backed by
   `review.kis_mock_order_ledger`; never `kis_live`), perform up to three
   mock actions and note each in the log line:

   - **(a) Profit-side mock trim, marketable.** Pick one held KR position
     that is in profit. Preview + place a marketable limit sell
     (`account_mode="kis_mock"`) for a small trim, referencing the ROB-912
     -2% band. Record the ledger id and round-trip cost (fees/slippage) in
     the log line.
   - **(b) New-entry hold, logged not executed.** For any candidate that
     would otherwise be a NEW entry today, log
     `actions_taken(mock): new_entry_held symbol=<X>` — do **not** place a
     mock buy for it. (A candidate that is an averaging-down add to an
     *existing* position is explicitly exempt from this hold — proceed with
     that mock buy normally and log it as `deep_rung_add` instead.)
   - **(c) One deep-rung reprice.** For one existing deep-rung resting order
     (buy-side ladder), mock-modify (`modify_order`, `account_mode="kis_mock"`)
     its price toward the `buy.deep_limit_pct_range` floor (-12%). Record
     the old/new price and the ledger id.

   See `docs/runbooks/kis-mock-scalping-smoke.md` / `kis-mock-reconciliation.md`
   for the underlying `kis_mock` account-mode mechanics (gates, ledger,
   reconcile) these reps ride on unmodified.

   Cap this at one action per category per day — these are reps to observe
   behavior, not a full defensive playbook execution (which remains
   deferred).

## 4-week scoring (after ~20 trading days)

At the end of the 4-week window, an operator/session reviews the accumulated
log lines and scores:

- **`fired` day count** — how many days actually crossed the -3% gap
  threshold, vs. how many days a session *felt* like it should have fired
  (this is where the intraday-crash limitation, e.g. 2026-07-13-shaped days,
  should surface as a gap in coverage, not a bug in this trigger).
- **Defense effectiveness (%p)** — for `fired` days where a mock profit trim
  (action a) was taken, compare the trim's realized price vs. the position's
  price at end-of-day / next-day-open, expressed as percentage points of
  downside avoided (or given up, if the trim turned out to be premature).
- **Round-trip cost** — cumulative fees/slippage across all mock actions
  taken during the window, to weigh against the defense effectiveness above.

This scoring is the input to the still-open decision of whether to build
real `defensive_trim` execution support (separate issue) and whether an
intraday trigger is worth adding in a follow-up.

## Explicitly out of scope for this policy addition

- **No scheduler / automation wiring.** Nothing in `app/tasks/`,
  `app/flows/`, or TaskIQ registers a periodic call for this trigger. All
  reps above are manual, session/operator-initiated.
- **No code enforcement.** `crash_day` is not read by any fail-closed guard,
  order-validation path, or execution client. It is advisory data returned
  by `get_trading_policy(market, lane)` alongside the existing
  `{version, content_hash}` stamp.
- **No `defensive_trim` exit_intent path.** See "Confirmed decisions" above.
- **No live orders.** All reps in this runbook use `account_mode="kis_mock"`;
  do not substitute a live order tool or `kis_live` account mode.

## Related docs

- `docs/runbooks/kis-mock-scalping-smoke.md`,
  `docs/runbooks/kis-mock-reconciliation.md` — the KR `kis_mock` order
  lifecycle/ledger/reconcile mechanics used for reps (a)/(c) above.
- ROB-912 — marketable sell band (-2%) referenced by
  `profit_trim_marketable_allowed`.
- ROB-930 — preopen shadow §0-R `gap_risk` verdict referenced by
  `defensive_brief_cross_check`.
- `config/trading_policy.yaml` §`crash_day` — the source values this runbook
  exercises.
