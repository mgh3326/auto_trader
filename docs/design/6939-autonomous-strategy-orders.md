# 6939 — Autonomous strategy orders (Fillwire signal → E1 gateway → live and mock venues)

Status: design only. This document changes no code, config or env.
Authority: operator decision `decision/2026-11-27/at-autonomous-strategy-orders` (hk 6939, 2026-11-27 07:17 KST).
Context: hk 6667 comment 37005 (dots pilot rules), comment 35372 (what exists); operator/dots direction 11-27 (hk 6667).
Revision: hk 6942 (one plan store = daily_plan, supersession timing, session acceptance/fill table, retro linkage, ticket reuse).
Pins: auto_trader `a30bd7d27`, fillwire `b257db5` (read only).

**Scope of this phase (hk 6942).** Design closure plus replay, mock and shadow verification. Live arming stays governed by the
decision record above and its arming rules (§2.2); this document and its PR change no live switch, cap or gate.

## 0. Summary

Strategy orders leave without a per-order human approval, on `kis_live` (KR NXT+KRX, US), `toss_live` (KR, US),
`upbit_live` (KRW) and on every mock/demo venue, inside the envelope of the decision.

```
Claude/dots (propose/change/stop, retro) ──▶ AT daily_plan vN (validate, publish; budget/risk state per account)
                                                   │ AT intent issuer (binds head, seq, row_key)
                                                   ▼
Fillwire detector (socket quotes → condition met) ──child request──▶ AT E1 gateway (permission checks, journal, broker send)
                                                   ▲                                    │
                         ledger evidence (execution_ledger, order ledgers) ◀── reconcile ◀── broker
```

### 0.1 Roles (hard rule 4 framing)

| Actor | Does | Never does |
|---|---|---|
| Claude / dots (out-of-process) | Propose, change or stop a strategy from price and non-price evidence (news, disclosures, flow); write the retro | Hold broker credentials; send orders; write ledgers; bypass a daily_plan validation |
| AT daily_plan (hk 6751/6790/6902) | Validate and publish plan versions; own per-account/currency budget and risk state; score and compare budgets | Create proposals, orders, watches or mandates from publish itself (5750 §8.2) |
| AT intent issuer + E1 gateway (ROB-1440, hk 5441) | Turn a published row into an immutable intent; every permission check (kill, arming, caps, session, quote, supersession); journal; the only broker access | Choose when a condition is met |
| Fillwire | Detect conditions from socket quotes; post child requests to the loopback E1 route | Talk to a broker or DB for orders; name a broker order id; resend an uncertain child |

Manual paths (MCP order tools, proposals, Telegram/web approval) keep their order approval (`approval_hash`), ledger and
duplicate-send rules unchanged. The **E1 confirm header** is a service-to-service idempotency echo, not an order approval.

## 1. One plan store: daily_plan is the canonical plan

The 6939 first draft created `review.strategy_order_plans` (+ versions). hk 6942 drops it. The canonical plan is the
daily_plan head/version/row store (hk 6751, `app/models/daily_plan.py`), with inputs from the bundle (hk 6902) and the
scoring/budget comparison from hk 6790. Execution binding is the 6728 intent reference `(plan_head_id, plan_version, row_key)`.

### 1.1 Field map — every 6939 §1 execution field to its single owner

| 6939 draft field | Owner table / column | Notes |
|---|---|---|
| `plan_id` | `review.daily_plan_heads.id` | one head per (market, session_date) |
| `version` (+1 only) | `review.daily_plan_versions.seq` (`uq_daily_plan_versions_head_seq`, CAS `expected_prev_seq`) | append-only, triggers refuse UPDATE/DELETE |
| `plan_sha256` | `daily_plan_versions.request_sha256` + `input_bundle_sha256` | bound into intent with seq |
| `account` | row `account_ref` / `would_be_account_ref` (+ `routable_account_modes`) | `derive_account_ref`; E1 intent carries the forwarder account |
| `market` / `venue` / `sessions` | row `market`, body `venue`, `session` (validator `VENUE_SESSIONS`) | one row = one venue×session |
| `symbol` | row `symbol` (`app/core/symbol.py`) | |
| `side_program` | row `action` ∈ new/add/trim/risk_sell/hold/watch | buy vs sell is the action, not a program field |
| `valid_from`/`valid_until` | body `valid_until` (≤ session close), version `asof` | effective expiry = min(plan, approval, session) (6728 §1) |
| `entry_price_cap` | body `max_buy_price`, `would_be_limit_price` | |
| `quantity_cap` / `notional_cap` | body `would_be_quantity` xor `would_be_notional` | E1 intent caps are the binding copy |
| `invalidation_price` | body `invalidation_conditions[]` (`price_at_or_below`) | required for an autonomous entry row |
| `target_price` | body `trigger` on a `trim` row (`price_at_or_above`) | own intent bound to the trim row_key (§1.3); not a loss sell |
| `arming_state_at_publish` | **not stored in the plan**: read live by E1 from `review.autonomous_arming_events` fold | arming is gateway state, not plan content |
| budget per account/currency | 6790 `daily_plan_versions.metadata.allocation_comparison` (W/N) | operator-only, not served to sessions |
| remaining qty / budget after fills | E1 fills projection (6728 §3) | never written back into the plan |
| journal / send state | `review.execution_child_requests` (E1) | unchanged |
| fills | `execution_ledger_in_effect()` + order ledgers | unchanged |

Required widenings (additive CHECK changes, owned by ticket N1 below, not a new store): `daily_plan_heads` market `kr`→
`kr|us|crypto` and `mode` `plan_only`→`plan_only|executable`; `daily_plan_rows.execution_state` `plan_only`→
`plan_only|executable` (an `executable` row is the only kind the intent issuer may read); `ACCOUNT_MODES` gains `upbit_live`
and the mock accounts; symbol CHECK per market; validator `VENUE_SESSIONS` gains the US venues (`NASD`/`NYSE`/`AMEX`, sessions
`us_pre`/`us_regular`/`us_post` as §2 allows per broker) and `UPBIT` (`crypto_24h`); `normalize_symbol`'s market guard admits
`us` and `crypto` so those rows reach the venue check. No field above needs its own table.

### 1.2 Scoring and budget (reuse 6790, extend)

- New buy, add-on buy and trim/sell are scored as **separate cohorts** (row `action`), each with its own rank list; no cohort
  is fixed to three symbols — the size cap is `daily_plan.max_adopt` per cohort and the budget, not a symbol count.
- Ranking A (price only) and ranking B (price + news/disclosure/flow) stay side by side in every version (6790). A missing
  input is the closed `unavailable` reason (`input_missing`, `input_stale`, `observed_time_unproven`, …), never 0 (hk 6769).
- Policy W (wait for a top candidate) and N (allow the next one) are both computed per account and currency, cash vs parking.
  **Orderable cash** = broker-confirmed cash only. An assumed parking sale (`would_sell_parking`) and an unconfirmed fill
  amount are recorded beside the allocation and never added to orderable cash; a buy that needs them carries the 6790
  precondition and is not issued until the broker's orderable cash has risen.
- A fill updates only remaining quantity and remaining budget (6728 §3 projection). Strategy re-evaluation happens only at
  named events — premarket, disclosure, VI, halt/resume, session change, operator/dots request — and each produces a new
  version (`VERSION_KINDS` premarket/fill/event/session/close). A `fill` version records observation only: it never changes a row's execution fields (trigger, invalidation, price caps,
  size, venue/session, account), so it never makes an intent stale (§5.1); the validator rejects a `fill`-kind version that does.

### 1.3 E1 intent (existing tables, new `schema_version` 3)

`review.execution_intents` stays immutable. v3 adds `plan_head_id`, `plan_version` (= `daily_plan_versions.seq`),
`row_key`, `invalidation_price`, `session`, per-account caps and the FX snapshot. These are copies bound at
issue time for the gateway; the daily_plan row stays the source. Every copied value comes from the bound row only: an entry
(`new`/`add`) intent carries the entry row's invalidation; a take-profit exit is a separate intent bound to its own `trim`
row_key (its `trigger` is the target), checked against that row's supersession. `invalidation_break` children belong to the
entry intent's lineage; `target_hit` children to the trim intent. Owner: AT intent issuer, never Fillwire.

### 1.4 Fillwire child request, gateway journal, ledger evidence (existing, unchanged)

Child: `idempotency_key`, `intent_id`, submit/amend/cancel, limit price, quantity, `reason` (`entry_condition` /
`invalidation_break` / `target_hit`). Journal: `sending` committed before the broker call; same key replays the first answer;
`uncertain` is never resent. Fills only from `execution_ledger_in_effect()` and the order-ledger services; `accepted` is never a fill.

## 2. Broker × market × session table

Columns: **Supported** = path exists in code today (`yes`) or must be confirmed (`[unverified]`, with how it is checked).
**Acceptance** = when the broker accepts a new order. **Fill** = when matching can occur. They differ in several cells.
Gates for every cell: fresh best bid and ask (≤ 5 s, `proposal_gate.ASK_MAX_AGE`), spread gate (regular / extended policy
key), halt/VI/expected-price block. Arming: MF = mock-first, LH = live half cap (§2.2).

| Broker / market / session | Supported | Acceptance (KST unless noted) | Fill | Order forms | How checked | Arming |
|---|---|---|---|---|---|---|
| KIS KR KRX regular | yes (`_place_order_impl`, `order_send_intents`) | 08:30–15:30 (from 08:30 auction) | 09:00–15:20 continuous, 15:20–15:30 closing auction | `ORD_DVSN` 00 limit / 01 market, integer qty; no amount field | code `domestic_orders.py:386,394` | MF (kis_mock KR) |
| KIS KR NXT pre | yes for limit (NXT route, `nxt_tradable`) | 08:00–08:50 | 08:00–08:50 | 00 limit int; market [unverified] | KIS official `examples_llm/domestic_stock/order_cash` + NXT notice; until confirmed send 00 only | LH |
| KIS KR NXT after | yes for limit | **from 15:30** (orders accepted for the after session) | **15:40–20:00** continuous (15:30–15:40 no NXT matching) | 00 limit int; market [unverified] | same as pre; first live day `kis_live_get_daily_orders` shows ODNO/fill time | LH; entries refused 15:30–15:40 only (`entry_no_fill_window`) |
| KIS KR KRX after-market | yes, listed-eligible only (`resolve_krx_after_capability`) | 16:00–20:00 | 16:00–20:00 | 00 limit int; market [unverified] | KRX rulebook + first live order | LH |
| KIS KR NXT amount order | **no** — KIS client has no amount field | — | — | — | not reachable via KIS; not claimed | — |
| KIS US regular | yes (`equity_us`) | ET 09:30–16:00 (KIS accepts earlier reservation) | ET 09:30–16:00 | limit int | `overseas_orders.py` | MF (kis_mock US) |
| KIS US pre/post | [unverified] | [unverified] | [unverified] | limit int [unverified] | KIS `examples_llm/overseas_stock/order*`; cell closed `unsupported` until confirmed | LH |
| Toss KR regular | yes (`toss_place_order`) | 08:30–15:30 | 09:00–15:30 | qty or `orderAmount` | `orders_toss_variants.py:409-416` | LH |
| Toss KR NXT pre/after | yes for qty limit with `nxt_preflight`; **amount orders [unverified]** | pre 08:00–08:50; after from 15:30 | pre 08:00–08:50; after 15:40–20:00 | qty limit; `orderAmount` [unverified] | Toss OpenAPI 1.2.28 `orderType`/amount per NXT session; first live NXT amount order recorded; until then qty only | LH |
| Toss KR KRX after | yes, `krx_after` preflight (#969) | 16:00–20:00 | 16:00–20:00 | qty limit | `nxt_preflight_krx_after.py` | LH |
| Toss US regular | yes | ET 09:30–16:00 | ET 09:30–16:00 | limit/market, fractional qty, `orderAmount` | code | LH |
| Toss US pre/post | yes only if `toss_live_us_sessions` lists them | Toss calendar pre/after windows | same windows | **limit, integer qty, no `orderAmount` — this rule is Toss US pre/post only; it is not applied to KR NXT** | `apply_toss_us_extended_order_shape` | LH |
| Toss US day market | no | — | — | — | closed `unsupported` | — |
| Upbit KRW | yes (`upbit_live_forwarder.py`) | 24h | 24h | limit (tick-validated) for v1 | code | LH |
| kis_mock KR / US | KR yes (`kis_mock_forwarder.py`), US new forwarder | KRX regular / XNYS regular | same | limit/market; attribution required | existing | mock |
| kiwoom_mock KR / US | yes (`kiwoom_mock_place.py`) | KRX regular / XNYS regular | same | limit | existing | mock |
| alpaca_paper | yes | ET regular + ext (limit+day, hk 6497) | same | limit | existing | mock |
| binance spot / futures demo | yes | 24h | 24h | per client; futures 1x reduceOnly exits | existing | mock |
| nh_mock KR | yes (`_dispatch_new_intent`) | KRX regular | same | exact `"limit"`, non-SOR | existing | mock |
| kis_futures_mock day | yes | 08:45–15:45 | same | MF200 limit 1 contract | `LOCKED_LIMITS` | mock |

**Toss on the autonomous path (hk 7118, ESC 703702):** autonomous Toss KR and US orders are integer-quantity limit orders
only, in every session, until a broker-enforced quantity bound exists. `orderAmount` is refused (closed code
`order_form_not_accepted`), because the broker chooses the quantity, so the intent `max_quantity` and the position bound
cannot be proven. The table rows above describe what Toss accepts; manual Toss paths are unchanged (Toss still accepts amount
orders in KR regular and US regular, and fractional quantity in US regular; the no-amount rule there is US pre/post only).

Session source: the gateway classifies sessions with the quote/session classifier that `get_quote` uses (NXT after continuous
from 15:40 KST), **not** research `session_segment_of` (which labels `NXT_POST` only from 16:00, hk 6298). Toss KR NXT after uses the
same boundary. Rule: the gateway refuses a new entry when the current time is inside acceptance but outside fill for that cell
(`entry_no_fill_window`), so an accepted-but-unmatchable order is never left resting by the autonomous path.

### 2.2 Arming state machine (per account × market × session-class)

`frozen` (default) → `mock_proving` (MF cells) or `live_half` (LH cells) → `live_full`.
- MF: 10 consecutive mock round trips through the **same** gateway path with zero `uncertain`/`refused(error)`/cancel
  failure/reconcile mismatch → `live_full`. Any error resets the counter.
- LH: live with per-order cap 1,000,000 KRW for 10 clean live round trips → `live_full`. A qty=1 order whose one share costs
  more than the cap in force may go up to `ONE_SHARE_CEILING_KRW` (10,000,000 KRW) (decision text; Q7 offers a stricter start).
- Envelope scope: for v3 intents on `kis_live`, `toss_live` and `upbit_live` the per-order cap is decided by the autonomous
  decision module (I1/I1a/I1b) only; `decisions._caps_refusal`'s `LOCKED_ENVELOPE.max_single_notional_krw` check stays as is and
  keeps bounding kis_mock and v1/v2 intents, and is not applied to v3 live intents (a 6,000,000 KRW one-share v3 order is allowed
  by I1a; the same order on kis_mock is still refused). `MAX_INTENT_NOTIONAL_KRW` stays the v1/v2 schema bound; v3 has its own
  schema bound `ONE_SHARE_CEILING_KRW`. Neither 5,000,000 constant changes.
- Stored in append-only `review.autonomous_arming_events`; state is a fold. Kill freeze does not reset arming.

## 3. Invariant table

| # | Invariant (decision item) | Input | Transition | Enforcement location | Observable result |
|---|---|---|---|---|---|
| I1 | Per-order cap 2,000,000 KRW (US: KRW-equivalent USD at publish FX) | child qty×limit, FX snapshot in intent | child reserve | gateway decision (new `autonomous_decisions.py`, called from `decide_*`); intent CHECK; pre-send hook | refused `order_cap_exceeded`, no journal send |
| I1a | One-share exception ≤ 10,000,000 KRW, only when one share > the cap in force (full 2,000,000 or half 1,000,000) and quantity exactly 1 | 1-share price | same | new locked constant `ONE_SHARE_CEILING_KRW=10,000,000` in the new autonomous decision module and the v3 intent CHECK only; `LOCKED_ENVELOPE.max_single_notional_krw` (`envelope.py:51`) and `MAX_INTENT_NOTIONAL_KRW` (`execution_intent.py:48`) stay 5,000,000 and keep bounding kis_mock and v1/v2 intents | refused `one_share_exception_invalid` |
| I1b | Half cap 1,000,000 KRW while `live_half` | arming fold | — | gateway decision reads arming fold under account lock | refused `arming_half_cap` |
| I2 | ≤ 4 working orders per account | journal rows without terminal evidence | child reserve under account advisory lock | gateway decision (count across all intents of the account, like `MAX_OPEN_INTENTS` `mandates.py:59`) | refused `working_orders_limit` |
| I3 | Daily loss ≥ 500,000 KRW → account kill freezes | realized P&L from ledger fills (FIFO, fees) + mark-to-market of open lots at fresh quote, per KST day (US: KRW at fill FX) | `ACTIVE → DAILY_LOSS_FREEZE` | new loss monitor writing `account_control` (`account_control.py:78 set_mode`); gateway reads kill on every submit/amend | freeze row + Discord; **unknown loss (unreconciled fill, stale mark, read fail) = frozen** |
| I4 | Stop-loss exception only for invalidation break | plan `invalidation_price`, fresh trade price (not bid) below it, ledger-confirmed lot | sell child `reason=invalidation_break` | gateway decision: loss-sell guard (`upbit_decisions.py:65` factor 1.01 and KIS/Toss guard in `order_execution.py`) bypass only when intent v3 carries invalidation and price evidence ≤ it; same check in pre-send hook | other loss sells → `loss_sell_requires_card` (existing human card) |
| I5 | Upbit 8-day cooldown, buy-only | `crypto_trade_cooldown_service.py:18` | buy child | existing `upbit_decisions.py:672` | `cooldown_active` |
| I6 | One send per claim; uncertain never resent | journal key | `sending` committed before POST | existing gateway store + forwarders (ROB-1440); `order_send_intents` for KIS; Toss `clientOrderId` | replay first answer; `uncertain` stays until reconcile |
| I7 | Plan supersession: old version never sends after new is published (§5) | `(plan_head_id, plan_version, row_key)` in the intent; `daily_plan_versions.seq` | daily_plan publish commits seq N+1 under the (account, symbol, venue) lock; intent rows stay immutable | gateway reservation + pre-send re-read under the same lock (§5.1) | refused `plan_version_stale`; cancels of vN orders still allowed |
| I8 | Partial fill → exit only ledger-confirmed net quantity | `GET .../fills` lineage `filled_qty` (in-effect, ROB-1379) | exit child | gateway decision (as `upbit` S-rules: sell ≤ proven own holding) | refused `exit_exceeds_confirmed_qty` |
| I9 | Budget re-reservation | approved budget − ledger-confirmed fills − working (6728 §3) | supersede / partial fill | AT intent issuer under the same lock; gateway high-water cap per intent (`decisions.py` existing) | new intent budget = remaining only; cancel never releases (D-4); a fill never rewrites the plan |
| I10 | Halt / stale / expected-price block | `halted_suspect`, `kr_symbol_trading_flags`, `price_kind=expected`, quote age | child reserve + pre-send | gateway decision + pre-send hook (reuse `halt_detection.py`, `kr_trading_flags/gate.py`, `kr_quote_price.py`) | refused `halt_suspect`/`quote_stale`/`expected_price` |
| I11 | Per venue×session order shape (§2 column), both sides fresh in all sessions, stricter spread when thin; Toss US pre/post limit-int-no-amount only | session segment, book, order form | child reserve | gateway decision via `session_segment_of` (`session_segment.py:258`) + per-cell allow-list; Toss US shape `apply_toss_us_extended_order_shape` | refused `order_form_not_accepted` / `spread_too_wide` |
| I12 | Discord alerts for every live order, fill, auto stop-loss, freeze | journal/ledger/kill transitions | after commit | new alert emitter (ops webhook `config.py:561 discord_webhook_alerts`), fail-open | message per event; alert failure never blocks |
| I13 | Kill switch default frozen | missing/unreadable row | — | `account_control.py:47 read` (existing semantics) + migration seeds `GLOBAL_FREEZE` per new account | no send until operator sets ACTIVE |
| I14 | Live-only scope: cash buy/sell; no short/margin/futures/options | intent side/product | intent store | intent CHECK + decision | refused at store |
| I15 | Existing manual gates unchanged (approval_hash, W5, kis_mock attribution) | — | — | untouched paths | golden tests byte-identical |

## 4. AGENTS.md hard-rule-4 exception paragraph (to be committed by N4)

Wording: the service-to-service header is the **E1 confirm header**; the operator's per-order decision is **order approval** (`approval_hash`, card). The autonomous path has no order approval; manual paths keep theirs.

> hk 6939 의 명시 운영자 결정(2026-11-27, `decision/2026-11-27/at-autonomous-strategy-orders`)은 의도 게이트웨이의 전략 주문 forwarder(`kis_live` KR(KRX·NXT)/US, `toss_live` KR/US, `upbit_live` 원화, 그리고 기존 게이트 아래의 모의·데모 계좌)만 예외로 허용한다 — `EXECUTION_INTENT_GATEWAY_ENABLED` AND 계좌별 `EXECUTION_INTENT_GATEWAY_KIS_LIVE_ENABLED`·`EXECUTION_INTENT_GATEWAY_TOSS_LIVE_ENABLED`·`EXECUTION_INTENT_GATEWAY_UPBIT_LIVE_ENABLED`(모두 기본 false, 정확히 `"true"`) AND `EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED` AND 계좌 kill 행 `ACTIVE`(부재·판독 실패·일일 손실 미측정은 freeze) AND 계좌×시장×세션 arming 상태 AND daily_plan 의 최신 발행 버전 AND 호출별 E1 확인 헤더(서비스 간 confirm, 운영자 주문 승인과 별개)가 모두 필요하다. 잠금 상수는 주문당 2,000,000원(미국은 원화 환산), 1주가 그보다 비쌀 때만 수량 1주·10,000,000원(별도 잠금 상수 `ONE_SHARE_CEILING_KRW`; `LOCKED_ENVELOPE`·`MAX_INTENT_NOTIONAL_KRW` 5,000,000원은 그대로), 계좌당 미체결 4건, 일일 손실 500,000원 이상이면 자동 freeze, 미모의 시장의 첫 10회 왕복은 주문당 1,000,000원(1주 예외는 유지)이며 어떤 CLI·env 로도 올릴 수 없다. 손실매도 가드는 그대로이고 예외는 계획에 진입 시 기록된 무효화 가격을 실제 가격이 이탈한 기계식 매도 하나뿐이다(그 밖의 손실 매도는 사람 카드). 주문 형태는 브로커·세션별 허용 목록을 따르며(토스 미국 프리·애프터만 지정가·정수 수량·금액 주문 금지), 모든 세션에서 신선한 양쪽 호가와 얇은 호가의 더 엄격한 스프레드를 요구한다. Upbit 손절 쿨다운·`loss_cut` 상수·`LOCKED_ENVELOPE`(5,000,000원)·`MAX_INTENT_NOTIONAL_KRW` 를 포함한 기존 게이트, 서비스 경유 원장 쓰기·증거 우선 체결·불명 송신 재전송 금지·kis_mock 귀속·W5 inbox·수동 경로 approval_hash 는 바뀌지 않으며, 공매도·신용·선물·옵션 실계좌와 스케줄러 등록은 허용되지 않는다.

## 5. Supersession: when a previous version's requests are refused

### 5.1 The deciding lock and timestamp

The order is decided by **commit order under one lock**, not by wall-clock timestamps. The lock is the 6728 shared
serialization boundary: a transaction-scoped advisory lock keyed on (account, normalized symbol, venue), taken by
(a) daily_plan publish for every row it changes, (b) the E1 child reservation, (c) the pre-send re-read, and (d) the event
block writer (VI/halt/session). Because only a publish that changes a row takes that row's lock, and only such a publish can
make the row stale, every supersession of a row is serialized against its reservation and pre-send. `daily_plan_versions.created_at` is recorded for audit only; it never decides.

- **At reservation.** Inside the reservation transaction, after replay/conflict on the key, the gateway reads, under the lock, the
  latest `PUBLISHED` seq **in which this intent's `row_key` changed** (the daily_plan fold already tracks the last change per
  row key). Staleness is per row, not per head: a publish that changes other rows, or a `fill`-kind version, does not
  supersede this intent. If that row seq is greater than the intent's `plan_version`, the submit/amend is
  `refused/plan_version_stale`, no journal `sending`, broker calls 0.
- **At the pre-send re-read.** After the `sending` row is committed and immediately before the adapter's first send, the
  forwarder's pre-send hook takes the same lock, **holds it through the adapter's first send call (bounded by the adapter timeout)**,
  and re-reads the same per-row latest seq and block state. If a newer version or block
  committed in between, the child ends `not_sent` (proven unsent, reason `plan_version_stale`) and the reservation is
  released. Because a publish that changes the row needs that lock, it cannot commit between the final check and the first send; it
  waits and commits after the send entered, which is case R1. If the hook returned, the send happened or may have happened:
  later errors are `uncertain`.
- **Already accepted orders** of the old version stay tracked: cancel (own key, kill/supersession not checked, account
  fingerprint checked — ROB-1440 rule) and reconcile continue. A new version never cancels or replaces an order by itself.
- **Uncertain** is never resent, under any version. A new submit on the same (account, symbol, venue) is
  `refused/prior_intent_unsettled` while any prior intent has an open order, an uncertain send or unreconciled fills.

### 5.2 Replay cases (fake clock, fake broker spy; extends 6728 §6)

| # | Case | Input | Expected journal | Expected ledger / projection |
|---|---|---|---|---|
| R1 | Send just before the version switch | v1 child reserved; hook returns and broker accepts 10 shares; then v2 publish commits | v1 row `sending→accepted`; a v1 retry key replays `accepted` (sends 0); a new v1 child → `refused/plan_version_stale`; v2 child for same symbol → `refused/prior_intent_unsettled` until v1 settles | `kis_live_order_ledger` 1 accepted row; no fill row; broker sends = 1 |
| R1b | Publish wins the lock first | v2 commits after v1 reservation, before hook | v1 row `sending→not_sent` (`plan_version_stale`) | no ledger row; broker sends = 0 |
| R1c | Publish attempted between final check and first send | v2 publish for the row starts while the v1 hook holds the lock | v2 waits on the lock; v1 sends once (`accepted` or `uncertain`); v2 commits after; outcome = R1 | broker sends = 1; no send after v2 commit |
| R2 | Version switch after a partial fill | v1 10 shares, 4 filled (in-effect ledger), v2 publishes with a smaller size | v1 open 6 remain cancellable: cancel child accepted when `broker_cancel_confirmed`; v2 submit refused `prior_intent_unsettled` until cancel + fills reconcile, then allowed for remaining budget only | projection 4 held / 0 open, cancelled 6; v2 intent budget = approved − 4×price; budget not reset by v2 |
| R3 | Socket disconnect and reconnect | quotes stream, gap, reconnect, resubscribe ACK, REST snapshot, then new-generation WS tick | during gap and until resubscribe ACK + fresh WS book: children 0 (Fillwire `blocked`); REST snapshot alone: 0; backlog touches: 0; first eligible new tick: 1 child | broker sends = 1 after recovery; an `uncertain` child from before the disconnect is not resent |

Tests: AT `tests/services/execution_intent_gateway/test_plan_version.py` (R1, R1b, R2), FW
`./internal/exec/plandetector -run TestEntryBlockedUntilSubscriptionReplayAndFreshQuote` (R3).

## 6. Retro linkage: why a buy did not happen

Every candidate row of a published version keeps a link chain `plan head + seq + row_key + 6790 score snapshot
(ranking A/B ranks, inputs_used, W/N allocation)` → intent → child → fill or block code. The retro (Claude/dots) reads it
through `retro_timeline` (hk 6171, one row per symbol per day) extended with a `plan` source, and through `block_reason`
(hk 5610) where the stop happened at proposal/approval.

| Reason (closed) | Evidence | Reused surface |
|---|---|---|
| `candidate_not_captured` | symbol absent from the bundle candidate set (`candidate_set_sha256`) | `retro_timeline` `not_observed`/`observed_only` |
| `condition_not_reached` | row published `executable`, no child before expiry, detector saw no qualifying tick | new `plan` event kind in `retro_timeline` |
| `data_delayed_or_missing` | input `unavailable` reason in score snapshot, or detector `blocked` (gap/stale quote) | 6790 `unavailable`; FW block log |
| `budget_or_guard_block` | E1 refusal code (`order_cap_exceeded`, `working_orders_limit`, kill, arming, `plan_version_stale`, `prior_intent_unsettled`, `entry_no_fill_window`) or 6790 `reserved_waiting`/`skipped_waiting` | E1 journal; `block_reason` vocabulary extended by these codes |
| `unfilled` | child `accepted`, ledger shows 0 fill at expiry/cancel | existing chain `order_expired`/`order_cancelled` |

Unknown evidence is reported as unknown (`source_unavailable`), never collapsed into one of the five.

## 7. Tickets

Status columns: Design = design doc merged; Impl = implementation merged on main; Pi = deployed on pi; Mock = verified on a
mock/replay path. `—` = not applicable or not yet. Statuses read from `origin/main` at `a30bd7d27`; pi/mock columns marked
[unverified] are to be confirmed by the design session.

### 7.1 Existing tasks (reused, not re-ticketed)

| hk | What | Design | Impl | Pi | Mock |
|---|---|---|---|---|---|
| 6751 | daily_plan store/publish (KR) | done (5750) | merged #2632 | [unverified] | — |
| 6790 | ranking A/B + W/N budget comparison | done | merged #2658 | [unverified] | — |
| 6902 | premarket input bundle | done | merged #2661 | not deployed (DAG copy unregistered) | — |
| 6728 | plan → E1 → mock execution design | done #2620 | — (tickets A–F) | — | — |
| 6939 | this design | done #2663, revised by 6942 | — | — | — |
| 5874 | Fillwire serve install on pi (FW) | done | [unverified] | [unverified] | — |
| 6346 | Fillwire socket subscription/continuity (FW) | done | [unverified] | — | — |

### 7.2 New tickets (missing only)

Tier T3 = order path / guard wiring. M = migration.

| # | Repo | Title | Tier/Grade | M | Blocks on | Design | Impl | Pi | Mock |
|---|---|---|---|---|---|---|---|---|---|
| N1 | AT | daily_plan widening: markets us/crypto, `executable` rows, account modes (no new plan store) | T3/S | M | 6751 | done (§1.1) | — | — | — |
| N2 | AT | Intent issuer + v3 intent binding `(head, seq, row_key)` + supersession at reservation/pre-send (§5) = 6728 B | T3/S | M | N1 | done | — | — | — |
| N3 | AT | Autonomous decision module (I1–I2, I7–I11, I14) pure + tests | T3/S | — | N2 | done | — | — | — |
| N4 | AT | AGENTS.md exception + contracts + separate `ONE_SHARE_CEILING_KRW` (qty==1); `LOCKED_ENVELOPE` and `MAX_INTENT_NOTIONAL_KRW` stay 5,000,000 | T3/S | — | N3 | done (§4) | — | — | — |
| N5 | AT | Daily-loss monitor + kill freeze writer + per-account kill rows | T3/S | M | N2 | done | — | — | — |
| N6 | AT | Arming fold + kis_mock US forwarder (v3) | T3/A | M | N3 | done | — | — | — |
| N7 | AT | Discord alert emitter | T2/B | — | N2 | done | — | — | — |
| N8 | AT | Gateway deploy on pi (unit-file copy, env names, runbook) | T2/A | — | N4 | done | — | — | — |
| N9 | FW | Plan follower: reads intent v3, emits children (entry/target/invalidation) = 6728 E | T3/S | — | N3, 5874, 6346 | done | — | — | — |
| N10 | AT | kis_live KR forwarder (KRX regular, NXT pre/after, KRX after) | T3/S | — | N4, N5, N6 | done | — | — | — |
| N11 | AT | kis_live US forwarder (regular; pre/post only after TR verified) | T3/S | — | N10 | done | — | — | — |
| N12 | AT | toss_live KR/US forwarder (+ policy key pre/post via operator PR) | T3/S | — | N10 | done | — | — | — |
| N13 | AT | upbit_live: widen mandate ceiling to envelope, invalidation exception | T3/S | M | N4, N5 | done | — | — | — |
| N14 | AT | Mock forwarders: kiwoom KR/US, alpaca, binance, NH, KIS futures mock | T3/A | M | N3 | done | — | — | — |
| N15 | AT | Retro linkage: `plan` source in `retro_timeline`, block codes in `block_reason` (§6) | T2/A | — | N2 | done | — | — | — |

6728 tickets A (input→publish read), C (ledger→remaining), D (event→re-evaluation), P (Fillwire subscription evidence) and
F (mock retro) stay under hk 6728; N2/N9 are its B/E and are not duplicated there.

### 7.3 Per-ticket notes

- **N1** C: daily_plan is KR `plan_only` only (`daily_plan.py:66,135`). D: additive CHECK widening plus validator `VENUE_SESSIONS`/market-guard widening (§1.1), no new table. A: existing
  KR tests byte-identical; accepts a valid US row and a valid crypto row, rejects an unlisted venue/session pair;
  `executable` row requires invalidation condition. O: sending.
- **N2** C: no plan reference in intents; the journal trigger (`20261120_hk5441_upbit_intent_gateway.py:591-596`) allows
  `not_sent` only for `schema_version` 2 with reasons in the closed `NOT_SENT_REASONS` list. D: v3 binding; per-row §5 lock
  at publish, reservation, pre-send; migration widens that trigger to allow `not_sent` for `schema_version` 3 and adds the v3
  pre-send codes `plan_version_stale` and `prior_intent_unsettled` to the closed list, with `upbit_decisions.NOT_SENT_REASONS`
  (or a v3 sibling list) and its pin test kept in sync. A: §5.2 R1, R1b, R2; a fill-kind version or an unrelated-row publish
  leaves an outstanding intent (incl. its `invalidation_break` child) sendable. O: forwarders.
- **N3** C: caps scattered (`decisions.py:215`, `mandates.py:58`). D: one pure module. A: table-driven tests per invariant.
- **N4 (formerly T3)** Title: "AGENTS.md exception + contracts + separate `ONE_SHARE_CEILING_KRW` (qty==1)". C: hard rule 4 has
  no autonomous exception. D: §4 paragraph and contract section; one new locked constant `ONE_SHARE_CEILING_KRW =
  10,000,000` applied only when quantity == 1 and the share price exceeds the cap in force. A: tests
  `tests/services/execution_intent_gateway/test_autonomous_envelope.py::test_locked_envelope_unchanged_at_5m` (pins
  `LOCKED_ENVELOPE.max_single_notional_krw == 5_000_000`), `::test_max_intent_notional_unchanged_at_5m` (pins
  `MAX_INTENT_NOTIONAL_KRW == 5_000_000`), `::test_one_share_ceiling_only_for_qty_one`; static test pins the paragraph's
  switch list. O: forwarders; no raise of any existing constant. Ev: `envelope.py:51`, `execution_intent.py:48`.
- **N5..N14**: as in the 6939 first draft (T4..T14), with plan reads replaced by the daily_plan head/seq.
- **N15** C: `retro_timeline` has no plan source. D: add plan events and the five reasons (§6). A: one fixture per reason.

## 8. Open questions (recommended answers)

1. Q1 FX for US cap: which rate binds? **Rec:** `exchange_rate_service` midRate snapshot stored in intent at publish; refuse if unavailable (no fallback rate).
2. Q2 Daily-loss measure: realized only or realized + MTM? **Rec:** realized + MTM of open lots, KST day; unknown = frozen.
3. Q3 Spread gate values. **Rec:** 30 bp regular / 15 bp extended / Upbit 20 bp, policy keys, operator PR.
4. Q4 Do mock round trips for KIS KR regular count NXT? **Rec:** no; NXT/after cells are LH.
5. Q5 KIS US pre/post TR availability [unverified]. **Rec:** keep `unsupported` until KIS official sample confirms; then LH.
6. Q6 Does a Toss/KIS cancel in kill state remain allowed? **Rec:** yes, cancel ignores kill (ROB-1440 rule) but checks account fingerprint.
7. Q7 One-share exception during `live_half`: the decision keeps it at 10,000,000 KRW. **Rec:** keep as decided; if the operator wants a stricter start, cap it at 2,000,000 KRW while `live_half` (one policy constant, N3).
