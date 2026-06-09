# ROB-463 — kis_live_place_order: NXT venue · TIF · 예약주문 (spec + gated surface)

Status: **Phase 1 shipped (gated typed surface).** Enablement blocked on operator
KIS-API wire-code confirmation. Live money — change with care.

## Problem (2026-06-08~09 live operation, real losses)

`kis_live_place_order` only supports **KRX 정규장 day orders**, so:
- NAVER trims (286k/289k, KRX day) went unfilled → expired at close → the next-day
  **08:02 NXT 신고가 300k gap-up was missed** (operator filled manually via HTS).
- New buys / 물타기 unfilled and expired at close (reconcile: `would_mark_cancelled`).
- Pre-market: a 113k buy limit was **rejected** because the order path used the KRX
  previous close (112.4k) as "current price" instead of the NXT price (114.3k).

## Current code (main @ dd3f1b83)

- Venue is **auto-resolved**, not operator-selectable:
  `app/services/brokers/kis/domestic_orders.py:291` —
  `nxt = await is_nxt_eligible(stock_code); excg_id_dvsn_cd = "SOR" if nxt else "KRX"`.
  Codes in live use today: **`SOR`** (smart routing, NXT-eligible), **`KRX`**, and
  **`ALL`** (one branch). A pure **`NXT`** code is never sent.
- `ORD_DVSN` only `00`(지정가)/`01`(시장가). No TIF / order-validity / reserved fields
  (`ORD_COND_DVSN_CD`, `RSV_ORD_TIME`) are present in any request body.
- `KISLiveOrderLedger` has no venue / order_validity / reserved columns.
- Pre-market "current price" comes from `_get_current_price_for_order`
  (`order_execution.py`), which for KR resolves the KRX daily/quote close → during
  pre-market this is the prior close, not the NXT price.

## Phase 1 (this PR) — gated typed surface, zero live risk

`kis_live_place_order` gains `venue` / `order_validity` / `reserved_time` params,
documented in the tool schema. `_venue_tif_gate` (orders_kis_variants.py) **fails
closed** for any value beyond the verified `venue∈{None,"auto"}`,
`order_validity∈{None,"day"}`, `reserved_time=None` — returning
`error="venue_tif_pending_operator_confirmation"` (+ `linear: "ROB-463"`) and placing
**no live order, even in dry_run**. Default behaviour (auto-route SOR/KRX, day) is
byte-identical. No migration, no new wire codes sent.

Rationale: the operator previously had **no way to express** "route to NXT" / "survive
past close" and got silent non-support + real losses. An explicit, actionable, tracked
error is strictly better, and it lays the typed contract so enablement is a gate-flip.

## Blocked on operator KIS-API confirmation (enablement questions)

1. **NXT venue:** is `EXCG_ID_DVSN_CD="NXT"` the correct code for a KRX-listed symbol,
   and how does it differ from `SOR`/`ALL` during pre/after-market? Does `SOR` already
   reach NXT when KRX is closed, or is explicit `NXT` required?
2. **TIF / order validity:** exact field + values for day vs 예약주문 vs GTC-equivalent
   (`ORD_COND_DVSN_CD`? values?). Does KR domestic support GTC at all, and with what
   expiry semantics?
3. **예약주문 (reserved):** field name + format for the reserved time (`RSV_ORD_TIME`,
   `HHMMSS`?), and the TR_ID — is it the same place-order TR or a separate reserved TR?
4. **Pre-market pricing:** during 08:00–09:00 KST, does `inquire_price` for an
   NXT-eligible symbol return the NXT session price or the KRX previous close? This
   determines whether the rejected-113k-buy fix is "route current-price to NXT" vs
   "relax the pre-market price guard".
5. **Cancel/modify:** do reserved / non-default-venue orders need different
   cancel/modify handling (e.g., cancel a reserved order before its scheduled time)?

## Phase 2 (after confirmation) — enablement plan

1. Thread `venue` → `order_korea_stock` (map to confirmed `EXCG_ID_DVSN_CD`); add
   `ORD_COND_DVSN_CD` / `RSV_ORD_TIME` to the request body per the confirmed spec.
2. Additive `KISLiveOrderLedger` columns (`venue`, `order_validity`, `reserved_time`),
   nullable, + alembic migration; record resolved venue on every live order (also fixes
   the "couldn't tell the order missed NXT" visibility gap).
3. Pre-market pricing: session-aware current-price (reuse
   `app/mcp_server/tooling/market_session.kr_market_data_state` from ROB-464) — route to
   NXT quote during pre-market or relax the guard per Q4.
4. Flip `_venue_tif_gate` to accept confirmed values; keep unsupported ones fail-closed.

## Safety

- Phase 1: no broker mutation change, no migration, default path unchanged.
- Shares the session-awareness root cause with **ROB-464** (quote-side pre-market) —
  reuse `market_session` rather than duplicate.
