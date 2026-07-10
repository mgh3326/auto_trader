# ROB-810 — get_holdings Toss sellable cache opt-in + skip discarded buying_power

**Status:** design approved
**Date:** 2026-07-10
**Issue:** [ROB-810](https://linear.app/mgh3326/issue/ROB-810) (High)
**Related:** ROB-701 (45s sellable cache, /invest home only), ROB-685 (sellable N+1 fanout removal), ROB-549 (sellable display accuracy gate), ROB-707 (concurrent cash task)

## Problem

MCP `get_holdings` is the current top single latency source (Sentry 24h, 2026-07-10):
**46 calls/day × avg 12.5s (sum 573s/day)**. Child-span breakdown:

- `invest.home.toss_api.sellable_quantity` — **42 spans, avg 10.7s, 86%**. Raw
  `GET /api/v1/sellable-quantity` runs **2,135 calls/day**; it is in the Toss
  ORDER_INFO rate-limit group (6 TPS), so the per-holding fanout serializes to
  ~N/6 s.
- `invest.home.toss_api.buying_power` — avg **3.1s**, even though the raw GET is
  74ms. The wait is the Toss ACCOUNT 1-TPS limiter. **The get_holdings path
  discards this cash entirely** (`_collect_toss_api_positions` only consumes
  `snapshot.positions` and `snapshot.errors`).

ROB-701 built a process-global 45s TTL sellable cache
(`app/services/toss_sellable_cache.py`) but deliberately wired it only to the
/invest home reader (`app/services/invest_home_readers.py`), keeping the MCP
`get_holdings` path fresh on a sell-sizing-safety argument. But ROB-701's own
safety argument was "real sells re-validate sellable at submit" — which applies
equally to get_holdings' **display** sellable. get_holdings is called 46×/day and
re-runs the full fanout every time. This is the last uncovered path of the
ROB-685/701 sweep.

## Scope

- **In scope:** issue items #1 (sellable cache opt-in) and #2 (skip discarded
  buying_power fanout).
- **Out of scope:** issue item #3 (`inquire-daily-itemchartprice` per-KR-holding
  refresh). See "Deferred" below.
- **migration-0.** No DB schema change. No new config key.

## Change 1 — sellable cache opt-in (issue #1)

`fetch_toss_portfolio_snapshot(..., sellable_cache=...)` already exists (ROB-701).
Only the MCP call chain needs wiring plus an explicit fresh escape hatch.

Thread a new `fresh_sellable: bool = False` parameter through:

```
get_holdings (MCP tool)
  -> _get_holdings_impl
    -> _collect_portfolio_positions
      -> _collect_toss_api_positions
        -> fetch_toss_portfolio_snapshot(need_sellable=..., sellable_cache=...)
```

Behavior:

- **Default (`fresh_sellable=False`):** `_collect_toss_api_positions` passes
  `sellable_cache=get_shared_sellable_cache()`. This is the SAME process-global
  cache the /invest home reader warms, so a warm entry serves get_holdings and
  vice versa. Cache hit ⇒ **0** `sellable_quantity` calls; the cached Decimal is
  re-wrapped as `TossSellableQuantity` by the existing snapshot loop.
- **`fresh_sellable=True`:** pass `sellable_cache=None` ⇒ today's fresh
  per-symbol fanout (operator escape hatch when a caller needs authoritative
  sellable for display).

`need_sellable` semantics are unchanged: get_holdings still requests sellable
(it surfaces `sellable_quantity`), so `need_sellable=True` stays. The existing
`need_sellable=False` skip path (ROB-685) is untouched.

### Safety (ROB-549 / ROB-701)

- Sell **sizing** never depends on this display value — `toss_place_order` /
  `toss_preview_order` re-validate sellable at the broker at submit time.
- Display staleness is bounded by the cache TTL (45s), the tradeoff ROB-701
  already approved for the home/account-panel readers.
- The cache honors `toss_sellable_cache_enabled` (default True) and
  `toss_sellable_cache_ttl_seconds` (default 45) — no new config surface.

## Change 2 — skip the discarded buying_power fanout (issue #2)

The get_holdings path never reads `snapshot.cash_krw`/`cash_usd`, yet
`fetch_toss_portfolio_snapshot` always kicks off `fetch_toss_cash_snapshot`
(the KRW+USD `buying_power` calls that serialize on the ACCOUNT 1-TPS limiter,
~3.1s). Rather than cache that value, **skip it** on the path that discards it.

Add `need_cash: bool = True` to `fetch_toss_portfolio_snapshot`:

- **`need_cash=False`:** do not create `cash_task`; return
  `cash_krw=None, cash_usd=None`, no cash errors. `_collect_toss_api_positions`
  passes `need_cash=False`.
- **`need_cash=True` (default):** unchanged — `invest_home_readers` (consumes
  `snapshot.cash_krw/usd`) and any other future caller keep today's behavior.

The dedicated cash tools (`portfolio_cash.py`) call `fetch_toss_cash_snapshot`
directly and are unaffected.

## Callers audited

`fetch_toss_portfolio_snapshot` has exactly two callers:

1. `app/mcp_server/tooling/portfolio_holdings.py:559` (get_holdings) — discards
   cash ⇒ `need_cash=False`, and `sellable_cache` per `fresh_sellable`.
2. `app/services/invest_home_readers.py:555` — consumes cash (lines ~690-708)
   ⇒ keeps `need_cash=True` default; already passes its own `sellable_cache`.

## Deferred — issue #3 (`inquire-daily-itemchartprice`, ~1,170 calls/day, 155s)

Inside get_holdings, `include_current_price=True` triggers
`_fetch_price_map_for_positions`, which for every KR-held symbol that
`_position_needs_current_price_refresh` calls `_fetch_quote_equity_kr` →
`KISClient.inquire_daily_itemchartprice` (KR daily candle). Toss KR positions
already carry a Toss `last_price`, but the refresh overwrites it with the KIS
daily close. Skipping or caching this refresh would change the **displayed**
current price (Toss last_price vs KIS daily close) — a display-semantics change
that needs its own validation. **No code change in this PR.** Findings recorded
on the issue; a follow-up can decide (short-TTL cache vs. skip-when-toss-price-
present).

## Testing (TDD)

New/extended unit tests, `migration-0`:

1. **Cache hit ⇒ 0 sellable calls.** Pre-warm `get_shared_sellable_cache()`, run
   get_holdings default; assert the fake Toss client's `sellable_quantity` is
   called 0 times and `sellable_quantity` values still populate positions.
2. **`fresh_sellable=True` ⇒ cache bypassed.** Even with a warm cache, assert
   per-symbol `sellable_quantity` is re-fetched.
3. **`need_cash=False` ⇒ no buying_power.** Assert the fake client's
   `buying_power` is called 0 times on the get_holdings path; positions/errors
   unchanged; `cash_krw/cash_usd` None on the snapshot.
4. **`need_cash=True` default regression guard.** `invest_home_readers` path (or
   a direct `fetch_toss_portfolio_snapshot()` call) still fetches cash.
5. **Response-shape stability.** get_holdings output unchanged (cash is not
   surfaced today).

## Expected effect

get_holdings avg **12.5s → ~2s**; Toss ORDER_INFO `sellable-quantity` calls
**2,135/day → tens**; buying_power ACCOUNT-limiter wait removed from the
get_holdings path. Closes the last uncovered path of the ROB-685/701 sweep.
