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
| BUY / SELL | `KISClient.buy_overseas_stock` / `sell_overseas_stock` | limit order when `price>0` (`ORD_DVSN=00`); TRs `VTTT1002U` / `VTTT1006U`; order id at `result["odno"]` |
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
bounded holdings-delta poll → **cleanup in a `finally`** (SELL a filled residual,
or CANCEL an unfilled resting BUY) → final open-position/delta verification. It
prints a JSON evidence packet including symbol, exchange, side(s), order id(s),
baseline/post holdings, selected fill signal, cleanup result, and final position
delta vs baseline.

## Exit codes

| code | meaning |
|---|---|
| 0 | success — preflight printed, or confirmed round trip flattened to baseline (final delta 0) |
| 1 | unexpected exception |
| 2 | pre-BUY blocked (no/stale quote, unresolved exchange, size zero, baseline read failed) **or** fill unconfirmed but flat |
| 3 | anomaly — residual position / pending order could not be cleaned up (SELL/CANCEL rejected, missing order id, residual, over-flatten) |
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
- **Alpaca Paper is a separate broker** (`account_scope=alpaca_paper`,
  `scripts/smoke/*alpaca*`). Do not mix its ledger/preflight semantics into this
  KIS-mock evidence; this smoke never touches the dual-paper preview packet.

## Caveats / known unknowns (operator must verify on the confirmed run)

- **Mock fill reflection is unverified.** Whether KIS mock actually fills overseas
  orders and reflects them in `fetch_my_us_stocks(is_mock=True)` is the central
  unknown. If mock does **not** populate overseas holdings on fill, every run is
  fill-unconfirmed (exit 2) — the smoke fails closed and never fabricates a fill.
- **Quote freshness tz.** Minute-chart timestamps are treated as US/Eastern; the
  freshness gate is coarse. `is_market_open("us")` is the primary session gate and
  the CANCEL cleanup is the real safety net for an unfilled resting limit.
- **Attribution boundary (inherited from ROB-341).** Holdings delta attributes any
  same-symbol qty change in the poll window to this order; there is no WS
  serialization here, so keep the confirmed smoke to idle symbols.
- **Overseas mock cancel fields** (forwarding-org code, partial-qty rules) are
  unvalidated until the confirmed run; a cancel rejection is classified exit 3.
