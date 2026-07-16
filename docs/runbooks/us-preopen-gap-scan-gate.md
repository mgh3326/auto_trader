# US Pre-Open Gap Scan Gate (ROB-924)

## What this is

A **manual, operator-run** gate for the 21:30–22:25 KST window (US premarket)
that reads premarket price reaction to overnight catalysts and filters it down
to a small set of `alpaca_paper`-only mock reps. It exists because the
07-16 실측 showed that "did the company announce something" is the wrong
filter — 8/11 catalysts were already public before the open, and the signal
that actually separated MAN (+11.7% premarket, +10.5% at the open) from a
gap-down miss (TSM/UAL) was the **premarket price reaction itself**, not the
announcement.

This is **not a new automation**. There is no schedule, cron, TaskIQ task, or
Prefect flow attached to this gate — per this repo's "자동화 전 수동 검증"
policy, it runs as a human-triggered session until ≥10 `alpaca_paper` reps
produce a performance report (AC #3) and a separate promotion decision is
made.

**Scope boundary:** `alpaca_paper` only. `kis_live` / `toss_live` / any other
live order surface is never touched by this gate. `order_proposal` rows are
never created by this procedure — it is a standalone mock rep loop, not part
of the ROB-816 proposal/approval pipeline.

## Preconditions

- `ALPACA_PAPER_DEFAULT_TOOLS_ENABLED=true` — DEFAULT-profile exposure gate
  for the `alpaca_paper_*` MCP tools used below (see
  `docs/runbooks/alpaca-paper-ledger.md` §"Profile exposure (ROB-908 —
  DEFAULT-profile flag)"). Without it the tools in Step 4/6 are not visible
  on the operator's MCP profile.
- ROB-922 (`include_extended_hours` premarket/afterhours overlay on
  `get_quote`) is deployed — production commit `77333e7e`. This gate depends
  on `price_source="yahoo_prepost_last"` being available; if the deployed
  build predates this, Step 1 cannot label premarket quotes and the gate
  cannot run.
- Familiarity with `docs/runbooks/alpaca-paper-ledger.md` (ledger states,
  read-only surfaces) and `docs/runbooks/alpaca-paper-roundtrip-report.md`
  (roundtrip audit) is assumed — this runbook does not repeat that material.
- Operator has whatever MCP client session the alpaca_paper tools are
  normally driven from (Hermes / MCP consumer session), with confirm-gated
  submit available.

## Trigger

**Manual only, 21:30–22:25 KST** (US premarket window, i.e. roughly 08:30–
09:25 ET premarket per US_SESSION_PREMARKET). Operator starts a session and
works through Steps 1–6 below. **Do not wire this to a schedule, cron,
TaskIQ task, or Prefect deployment** — that decision is explicitly deferred
past the ≥10-rep performance report (AC #3) and requires a separate issue +
user decision.

## Step 1 — Gap measurement

1. Call `get_earnings_calendar(market="us", from_date=<yesterday's date>,
   to_date=<today's date>)` (no `symbol` — sweep the window) to pull:
   - Today's **BMO** (before-market-open) reporters.
   - Yesterday's **AMC** (after-market-close) reporters.

   (Tool: `app/mcp_server/tooling/fundamentals_handlers.py` `get_earnings_calendar`,
   impl `handle_get_earnings_calendar` in
   `app/mcp_server/tooling/fundamentals/_financials.py`. Korean equities are
   **not** in scope for this gate — always pass `market="us"`.)

2. For each candidate symbol from the calendar sweep (plus any other symbol
   flagged by overnight news you're independently tracking), call
   `get_quote(symbol, market="us", include_extended_hours=True)`.

   - **Check `price_source` before trusting the number.** ROB-922's overlay
     only fires when the tagged `session` is `premarket` or `afterhours` —
     if `price_source` comes back as anything other than
     `"yahoo_prepost_last"` (e.g. still `"yahoo_fast_info_close"` or
     `"kis_overseas_last"`), **the premarket overlay did not apply** — most
     likely because the session was not tagged premarket/afterhours, or the
     Yahoo prepost fetch failed and the quote silently kept its prior
     labeling (never lies about `price_source` — see
     `_apply_extended_hours_overlay` in
     `app/mcp_server/tooling/market_data_quotes.py`). **Do not compute a gap
     off a non-`yahoo_prepost_last` quote during this window** — record it in
     the reps log as "gap 판정 불가" instead of fabricating a number.
   - Record `quote_asof` alongside the price — it is the timestamp label for
     the premarket tick, not a request timestamp.
   - **Gap % formula**: `(price - previous_close) / previous_close * 100`,
     using the `previous_close` field on the same quote payload (KRX-style —
     this field is preserved through the overlay specifically so gap math
     stays correct).

## Step 2 — U3 filter

Apply, in order, to every symbol measured in Step 1:

| Filter | Threshold | On fail |
|---|---|---|
| Gap % | `+5%` to `+20%` (inclusive) | Record in **배제 로그** as `gap_out_of_range` (note whether `>20%` or `<5%`, and gap-down cases separately as `gap_down` — these are your gap-down-exclusion-hit-rate denominator for AC #3) |
| Market cap | `> $1B` | Record as `micro_cap_excluded` |
| Volume | `> 500,000 shares` (premarket/cumulative volume from the quote payload where available; note the source if it's a partial-session volume) | Record as `low_volume_excluded` |

Additional exclusions recorded but not separately thresholded: inverse ETFs,
and any symbol whose **nominal** market cap clears $1B but whose premarket
**거래대금** (dollar volume) is thin (07-16 MAAS-style trap) — flag these in
the notes column rather than silently passing them.

U3-passing symbols typically number ≤5. Everything that fails any filter goes
into the **배제 로그 (exclusion log)** table in the reps log (see below) with
its exclusion reason — this table is what AC #3's "U3 필터 유효성" and
"갭다운 배제 적중률" get computed from later, so record it even though these
symbols never reach Step 3/4.

## Step 3 — Catalyst classification

For symbols that passed Step 2 (U3), call `get_news(symbol, market="us")` to
pull recent headlines (Finnhub-backed for US). Keep this call scoped to the
≤5 U3-survivors only — `get_news` is a per-symbol call and Finnhub rate
limits make sweeping the full pre-filter candidate list wasteful and
unnecessary.

Classify each:

- **실적 (earnings)** — confirmed by the Step 1 earnings-calendar hit →
  candidate for Step 4 entry.
- **M&A / 인수가 pin** — deal-priced stock pinned near an announced offer
  price → **기각 (reject)**, do not enter. Record in reps log with reason
  `ma_pin_rejected`.
- **Sympathy move** (moving on a peer's news, no own-name catalyst) →
  기록만 (record only), do not enter unless you have independent conviction;
  default is no entry.

Only "실적" classification (or another catalyst you can articulate and are
willing to defend in the reps log's 비고 column) proceeds to Step 4.

## Step 4 — Mock entry (alpaca_paper only)

For each Step-3-approved candidate:

1. `market_quote_snapshot_ensure(market="us", symbol=<symbol>)` — builds a
   fresh (<5m) trusted snapshot if one doesn't already exist.
2. `market_quote_snapshot_latest(market="us", symbol=<symbol>)` — confirm
   `submit_ready: true` and note the returned `id` (this is the
   `quote_snapshot_id` Step 4.3 needs).
3. `alpaca_paper_submit_order(symbol=<symbol>, side="buy", type="market" |
   "limit", quote_snapshot_id=<id from 4.2>, qty=... or notional=...,
   asset_class="us_equity", confirm=True)`.
   - `confirm=True` is the same operator gate documented in
     `docs/runbooks/alpaca-paper-ledger.md` — **do not bypass or script
     around it**; this procedure runs it manually every rep.
   - Strict caps apply regardless (per the tool's own description):
     `us_equity` `qty<=5` / `notional<=$1000` / `qty*limit_price<=$1000`.
   - **Sizing rule (fixed, not discretionary):** use a fixed notional per
     candidate for the whole rep series so P&L is comparable across reps.
     Operator picks the value once (subject to the cap above) and records it
     at the top of the reps log; do not vary size rep-to-rep.
4. Record the returned order/ledger identifiers in the reps log's entry
   column.

## Step 5 — Exit rule (pre-defined, no discretion)

**-3% stop-loss OR same-day close-out** — whichever triggers first. This is
fixed up front specifically so exit decisions don't become a second,
undocumented judgment call layered on top of the entry filter this gate is
trying to validate.

1. During the session (or a follow-up check before 05:00 KST US market
   close), call `alpaca_paper_ledger_get(client_order_id)` or
   `alpaca_paper_ledger_list_recent(lifecycle_state="filled")` to check the
   fill and current mark.
2. If unrealized P&L on the position hits **-3%**, submit a closing
   `alpaca_paper_submit_order(..., side="sell", ...)` immediately (same
   confirm-gate procedure as Step 4.3) — do not wait for close.
3. Otherwise, hold to the close. Since US regular-session close is ~05:00
   KST (or NXT/pre-market timing depending on session), plan a same-day-close
   or next-session check-in to submit the close-out sell once the position
   should be flat. Use `get_quote(symbol, market="us")` (extended-hours flag
   not needed once in regular session) to confirm the close print before
   submitting the close order.
4. Record exit price/time and the ledger id of the closing order in the reps
   log.

## Step 6 — Ledger reconciliation

For every rep (entry + exit), confirm both legs landed in the ledger before
marking the rep row complete:

- `alpaca_paper_ledger_list_recent(limit=50, lifecycle_state=<state>)` or
  `alpaca_paper_ledger_get(client_order_id)` for the specific order.
- `alpaca_paper_roundtrip_report(client_order_id=<id>)` (or
  `lifecycle_correlation_id=` / `candidate_uuid=` if you threaded one through)
  for the consolidated entry→exit audit view — see
  `docs/runbooks/alpaca-paper-roundtrip-report.md` for field meanings.

Only once both entry and exit ledger rows are confirmed does the rep count
toward the ≥10-rep total in AC #2. Record the ledger id(s) in the reps log row
— a rep without a confirmed ledger id is not done.

## Boundaries (do not cross)

- **Live surfaces: zero.** `kis_live_place_order`, `toss_place_order`, and
  every other live-order MCP tool are never called by this procedure, for any
  symbol, at any step.
- **No `order_proposal` usage.** This gate does not create, approve, or read
  `trading_decision_proposals` rows — it is a standalone mock loop, separate
  from the ROB-816 proposal/telegram-approval pipeline.
- **No automation beyond this document.** No schedule, cron, TaskIQ task, or
  Prefect deployment may be attached to any step above without a separate
  issue and an explicit user decision, and only after the ≥10-rep performance
  report (AC #3) exists.
- **Promotion is out of scope here.** Whether this gate's candidates ever
  feed a live or proposal-based path is a decision for *after* the reps +
  report, made by the user in a follow-up issue — not something this runbook
  or its reps imply.

## Reps log

See `docs/runbooks/us-preopen-gap-scan-reps-log.md` for the entry
format, exclusion-log format, and performance-report template that this
procedure's reps get appended to.
