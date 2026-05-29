# KIS mock overseas/US holdings-delta smoke (ROB-364)

Operator runbook for the KIS official-mock **overseas/US stock** holdings-delta
fill-confirmation smoke — the US counterpart to the domestic ROB-358 smoke
(`docs/runbooks/kis-mock-scalping-smoke.md` / `scripts/kis_mock_holdings_delta_smoke.py`).
**Mock/paper only, default-disabled, no automatic submit.** This validates the
US *execution + cleanup plumbing*; it is **not** a live-trading recommendation.

Script: `scripts/kis_mock_overseas_holdings_delta_smoke.py`

## Why this is a separate smoke (not the domestic one with a US symbol)

| Concern | Domestic (ROB-358) | Overseas/US (this smoke) |
|---|---|---|
| Execution path | `KisMockBroker` (`mock_scalping_exec/adapters.py`) | overseas order client **directly** (`buy/sell/cancel_overseas_stock`) — there is **no** overseas `KisMockBroker` |
| Cleanup SELL gate | `KIS_MOCK_SCALPING_ENABLED` + an allowed `ScalpingExitContext` reason | **none** — that validator is KR-only; cleanup is a plain overseas SELL/CANCEL |
| Cash / fill price | mock domestic cash **is** readable → cash-delta price | USD margin is **OPSQ0002-blocked** → cash-delta unavailable → `fill_price_source="limit_fallback"` |
| Same-day history | domestic daily-ccld empty (non-gating) | overseas daily-ccld likely empty + pending-orders (TTTS3018R) **unavailable in mock** → both non-gating |
| Fill gate | holdings delta (primary) | holdings delta (**sole** gate) |

## Capability surfaces (inventory)

| Capability | Surface (`is_mock=True`) | Notes |
|---|---|---|
| Account holdings | `KISClient.fetch_my_us_stocks(exchange=…)` | rows keyed `ovrs_pdno` / `ovrs_cblc_qty`; pre-filtered to nonzero |
| USD cash / margin | `KISClient.inquire_overseas_margin()` | **OPSQ0002** in mock → treated as unavailable (best-effort only) |
| Quote (sizing) | `KISClient.inquire_overseas_minute_chart(symbol, exchange, n=1)` | latest candle = **last** row (`close`, `datetime`); accepts the 4-digit order code |
| BUY / SELL | `KISClient.buy_overseas_stock` / `sell_overseas_stock` | limit order when `price>0` (`ORD_DVSN=00`); US mock TRs `VTTT1002U` / `VTTT1001U`; order id at `result["odno"]` |
| CANCEL | `KISClient.cancel_overseas_order(order_number, symbol, exchange, qty)` | `order_number` = prior BUY `odno`; TR `VTTT1004U` |
| Pending orders | `inquire_overseas_orders` | **RuntimeError in mock** — not used here |
| Filled history | `inquire_daily_order_overseas` | supplementary / non-gating (may be empty same-day) |
| Exchange resolution | `us_symbol_universe_service.get_us_exchange_by_symbol` | order/cancel use 4-digit `NASD/NYSE/AMEX` |

## Fill-evidence hierarchy

1. **GATING — overseas holdings delta.** Baseline `fetch_my_us_stocks` qty vs
   post-submit qty, matched per-symbol (`to_db_symbol` normalizes `BRK/B`↔`BRK.B`),
   via the shared `classify_fill_by_delta` kernel. Only a **full** directional
   delta confirms; partial / zero / wrong-direction fail closed.
2. **PRICE only (not gating) — `derive_fill_price`.** Cash is OPSQ2-blocked, so
   `cash=None` → `(limit_price, "limit_fallback")`.
3. **DIAGNOSTIC (not gating) — `inquire_daily_order_overseas`.** Best-effort,
   attached if present; empty neither confirms nor denies.

## Gates (all default off)

| env | effect |
|-----|--------|
| `KIS_MOCK_OVERSEAS_SMOKE_ENABLED` | runs the smoke at all (read directly from env — **not** a persistent Settings flag); unset → no-op exit 4 |
| `KIS_MOCK_APP_KEY` / `KIS_MOCK_APP_SECRET` / `KIS_MOCK_ACCOUNT_NO` | KIS mock credentials (existing) |

There is intentionally **no** new persistent config field and **no**
`KIS_MOCK_SCALPING_*` reuse — set the enable flag ephemerally for one run.

## Pre-BUY fail-closed gates (`--confirm` stops before any order if any fail)

- smoke disabled / KIS mock not configured → exit 4
- `KIS_MOCK_ACCOUNT_NO` < 10 digits (cleanup SELL/CANCEL un-submittable) → exit 4
- US market not open right now (`is_market_open("us")`, XNYS) → exit 4
- exchange unresolved → exit 2
- no quote / stale quote / size zero / baseline read failed → exit 2

## Step 1 — read-only preflight (no order)

```bash
KIS_MOCK_OVERSEAS_SMOKE_ENABLED=true uv run python -m \
    scripts.kis_mock_overseas_holdings_delta_smoke --preflight --symbol AAPL
```

Expect a JSON line with `mode=preflight`, resolved `exchange`, `holdings_qty`,
and `cash_usd=null` / `cash_source=unavailable_opsq0002` (expected in mock).
**No order is placed.** Run this first and confirm the symbol/exchange/holdings.

## Step 2 — small confirmed round trip (operator-gated, US RTH only)

Run only during US regular trading hours (so a marketable limit can fill), on an
**idle** symbol with no concurrent manual activity (see attribution caveat).

```bash
KIS_MOCK_OVERSEAS_SMOKE_ENABLED=true uv run python -m \
    scripts.kis_mock_overseas_holdings_delta_smoke \
    --confirm --symbol AAPL --exchange NASD --notional-usd 20
```

The script: sizes a marketable limit from the latest minute close → BUY →
bounded holdings-delta poll → **cleanup in a `finally`** → final open-position /
delta verification. It prints a JSON evidence packet (see *Evidence packet* below).

## Fill verdict & cleanup policy (fail-closed)

The entry fill verdict is one of `filled` (full directional delta — the **only**
confirmation), `partial` (some but not the full ordered qty), `none`, or
`read_failed`. The cleanup branches on whether the entry was a **confirmed full
fill**:

- **Confirmed full fill** → no resting remainder exists; SELL the delta to flatten.
  A clean flatten to baseline (final delta 0) is the **only** path to **exit 0**.
- **Partial / unconfirmed entry with a positive delta** → the original BUY may
  have an unfilled resting remainder. KIS overseas mock has **no authoritative
  open-order query** (`inquire_overseas_orders` raises in mock), so the smoke
  **cancels the original BUY** to remove that risk, then SELLs the filled delta.
  Even when it ends flat, this is **exit 2 (never exit 0)** — the entry was never
  a clean confirmed full fill.
- **Cannot authoritatively cancel the resting BUY** (cancel rejected or response
  missing `odno`) → **fail closed, exit 3**, and the smoke does **not** SELL.
- **No fill / nothing acked** → exit 2 (fill-unconfirmed) — never exit 0.

So a partial fill can never be silently reported as success, and a flat-but-
unconfirmed run is never mistaken for a clean round trip.

## Evidence packet (every `--confirm` run logs these)

`symbol`, `exchange`, `buy_limit_price`, `baseline_holdings_qty`, `quantity`,
`buy_order_id` / `entry_order_id`, `entry_fill_verdict`, `entry_fill_confirmed`,
`entry_filled`, `fill_price_source`, `cleanup_current_delta`,
`buy_cancel_attempted`, `buy_cancel_order_id` / `buy_cancel_status`,
`open_order_check_status`, `cleanup_sell_order_id` / `cleanup_sell_status`,
`cleanup`, `final_position_delta_vs_baseline`, `final_exit_reason`, `exit_code`.
On a BUY submit failure: `entry="BUY_submit_rejected"` + `buy_submit_exception`
+ `entry_order_id=null`, with a non-zero `exit_code` consistent with the process.

## Exit codes

| code | meaning |
|---|---|
| 0 | **confirmed full fill** flattened to baseline (final delta 0) — the only clean success; or `--preflight` printed |
| 1 | unexpected exception |
| 2 | pre-BUY blocked (no/stale quote, unresolved exchange, size zero, baseline read failed); **or** partial/unconfirmed fill that ended flat (resting BUY cancelled but entry never confirmed full); **or** BUY submit rejected with nothing acquired |
| 3 | anomaly — could not authoritatively clean up: resting BUY un-cancellable, residual after SELL, over-flatten/below-baseline, missing SELL/CANCEL order id |
| 4 | disabled / KIS mock not configured / account un-submittable / US market closed (no order placed) |

## Safety boundaries

- KIS official **mock** overseas/US only. No KIS live. No market orders. No
  shorting. No automatic submit. No scheduler / Prefect / TaskIQ / cron / launchd.
  No persistent env / flag / secret changes. No production DB backfill/delete.
- Cleanup goes through the overseas order client directly; SELL/CANCEL responses
  are inspected — a missing `odno` or a submit rejection is an explicit anomaly
  (exit 3), never a silent success.
- Negative / below-baseline / over-flatten deltas are **always** anomalies, never
  a clean exit (final clean exit requires delta exactly 0).
- A **partial / unconfirmed** entry never reports exit 0: the resting BUY is
  cancelled (fail-closed if that cancel can't be authoritatively confirmed) and a
  flat outcome is exit 2, not exit 0.
- **Confirmed smoke is operator-approved only**, after the read-only `--preflight`
  passes — one-off, minimal, LIMIT-only, on an idle symbol. No automatic `--confirm`.
- **Alpaca Paper is a separate broker** (`account_scope=alpaca_paper`,
  `scripts/smoke/*alpaca*`). Do not mix its ledger/preflight semantics into this
  KIS-mock evidence; this smoke never touches the dual-paper preview packet.

## Caveats / known unknowns (operator must verify on the confirmed run)

- **Open-order query is NOT authoritative in mock.** `inquire_overseas_orders`
  (pending, TTTS3018R) raises in mock, so the smoke cannot *read* whether a resting
  BUY exists. Instead it removes the risk by **cancelling** the BUY whenever the
  entry was not a confirmed full fill; if that cancel can't be confirmed, it fails
  closed (exit 3). `open_order_check_status` in the evidence records which applied.
- **Mock fill reflection is unverified.** Whether KIS mock actually fills overseas
  orders and reflects them in `fetch_my_us_stocks(is_mock=True)` is the central
  unknown. If mock does **not** populate overseas holdings on fill, every run is
  fill-unconfirmed (exit 2) — the smoke fails closed and never fabricates a fill.
- **ROB-364 is NOT Done until the operator confirmed run.** This PR hardens the
  code/tests/docs only; the issue stays open until an operator-approved confirmed
  smoke (creds + US RTH) actually exercises the round trip and resolves the
  unknowns above. This runbook makes no live-trading recommendation.
- **Quote freshness tz.** Minute-chart timestamps are treated as US/Eastern; the
  freshness gate is coarse. `is_market_open("us")` is the primary session gate and
  the CANCEL cleanup is the real safety net for an unfilled resting limit.
- **Attribution boundary (inherited from ROB-341).** Holdings delta attributes any
  same-symbol qty change in the poll window to this order; there is no WS
  serialization here, so keep the confirmed smoke to idle symbols.
- **Overseas mock cancel fields** (forwarding-org code, partial-qty rules) are
  unvalidated until the confirmed run; a cancel rejection is classified exit 3.
