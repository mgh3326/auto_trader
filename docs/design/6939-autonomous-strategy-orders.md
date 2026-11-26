# 6939 — Autonomous strategy orders (Fillwire signal → E1 gateway → live and mock venues)

Status: design only. This document changes no code, config or env.
Authority: operator decision `decision/2026-11-27/at-autonomous-strategy-orders` (hk 6939, 2026-11-27 07:17 KST).
Context: hk 6667 comment 37005 (dots pilot rules), comment 35372 (what exists).
Pins: auto_trader `d3e8c77ed`, fillwire `b257db5` (read only).

## 0. Summary

Strategy orders leave without a human in the loop, on `kis_live` (KR NXT+KRX, US), `toss_live` (KR, US), `upbit_live`
(KRW) and on every mock/demo venue, inside the envelope of the decision. The shape is the one the E1 gateway already
has for `kis_mock` and `upbit_live`:

```
AT strategy plan (vN, immutable)  ──publish──▶  E1 intent (immutable, caps)  ◀──child request──  Fillwire executor
          ▲                                          │ journal (write-ahead, one send per key)          ▲
          │ supersede vN→vN+1, budget re-reserve      ▼                                                 │ quotes/fills (WS)
   ledger evidence (execution_ledger, order ledgers) ◀── reconcile ── broker ◀── forwarder (per account)
```

- AT owns **what** (plan, caps, invalidation, budget, kill). Fillwire owns **when/at what price inside the plan**.
- Fillwire never talks to a broker or DB for orders; it posts child requests to the loopback E1 route
  (`app/routers/execution_intent_gateway.py:207`, `fillwire internal/exec/e1/client.go:305`).
- Every guard that exists today stays. New guards are added at the gateway (`decisions.py` / per-account decision module)
  and re-checked in the forwarder's pre-send hook, the same two places `upbit_live` uses
  (`upbit_decisions.py:608 decide_upbit_child`, `upbit_decisions.py:687 check_before_send`).

## 1. Data shape and field ownership

### 1.1 Strategy plan (AT source of truth) — new table `review.strategy_order_plans` (+ `..._plan_versions`, append-only)

| Field | Type | Owner | Notes |
|---|---|---|---|
| `plan_id` | uuid | AT | stable across versions |
| `version` | int, +1 only | AT | DB trigger: previous+1; publishing vN+1 supersedes vN atomically (§3 I9) |
| `account` | enum `kis_live`/`toss_live`/`upbit_live`/mock accounts | AT | binds forwarder |
| `market` / `venue` | `equity_kr`(KRX/NXT) / `equity_us` / `crypto` | AT | |
| `symbol` | DB format (`app/core/symbol.py`) | AT | |
| `side_program` | entry buy, then exit sell legs | AT | cash only; no short/margin on live |
| `sessions` | subset of {`nxt_pre`,`regular`,`nxt_after`,`krx_after`,`us_pre`,`us_regular`,`us_post`,`crypto_24h`} | AT | §2 table decides what is legal |
| `valid_from`/`valid_until` | timestamptz | AT | ≤ intent TTL ceiling |
| `entry_price_cap` | exact decimal | AT | limit ceiling for buys |
| `quantity_cap` / `notional_cap` | exact decimal | AT | must fit §3 I1 |
| `invalidation_price` | exact decimal, required for entry | AT | recorded at entry; the only loss-sell exception (§3 I4) |
| `target_price` | exact decimal, optional | AT | take-profit; not a loss sell |
| `arming_state_at_publish` | enum | AT | copied from arming table (§2.2) |
| `plan_sha256` | hex | AT | canonical JSON hash, bound into intent |

### 1.2 E1 intent (existing tables, new `schema_version` 3)

`review.execution_intents` stays immutable (`docs/design/1440-intent-gateway-contract.md`). v3 adds
`plan_id`, `plan_version`, `plan_sha256`, `invalidation_price`, `target_price`, `session_set` and per-account caps.
Owner: AT (written by the plan publisher, never by Fillwire). The CHECK that binds `schema_version` ↔ account
(`20261101_rob1440_intent_gw`, `20261120_hk5441_upbit_gw`) gains v3 rows for the new accounts.

### 1.3 Fillwire child request (existing wire)

`fillwire internal/exec/machine.go:45 Child` / `internal/exec/upbit/plan.go:29 Child`: `idempotency_key`, `intent_id`,
kind submit/amend/cancel, limit price, quantity, `reason` (new closed value `invalidation_break` / `target_hit`).
Owner: Fillwire. It may choose price and quantity only inside the intent; it cannot name a broker order id
(amend/cancel target by journal key, as today).

### 1.4 Gateway journal (existing)

`review.execution_child_requests` write-ahead, `sending` committed before the broker call, `accepted`/`uncertain`/
`refused`/`not_sent`. Owner: gateway store (`store.py`, `upbit_store.py`). Unchanged rules: same key replays first answer,
`uncertain` is never resent.

### 1.5 Ledger evidence (existing)

Fills only from `execution_ledger_in_effect()` and the per-broker order ledgers through their services
(`kis_live_order_ledger`, `live_order_ledger`, `toss_live_order_ledger`, Upbit order evidence `order_evidence.py`,
mock ledgers). Owner: reconcilers. Fillwire reads fills through `GET /execution-intents/intents/<id>/fills`
(`fillwire internal/exec/e1/reads.go:59`). `accepted` is never a fill.

## 2. Account × market × session table

Legend — Path: G = new gateway forwarder calling the existing send function; `G*` = existing gateway path.
Order forms are per cell (operator correction 2026-11-27: limit-only/integer/no-amount applies to Toss US pre/post only). Gates: Q = fresh best bid **and** ask present (age ≤ 5 s,
reuse `proposal_gate.ASK_MAX_AGE`), S = spread gate in bps (regular / extended), H = halt/VI/expected-price block.
Arming: MF = mock-first (10 clean mock round trips then live full cap), LH = live half cap (1,000,000 KRW) for 10 round trips.

| Account / market / session | Send path (existing function behind G) | Order rule | Carry / expiry | Gates | Arming |
|---|---|---|---|---|---|
| kis_live KR KRX regular 09:00–15:30 | G → `_place_order_impl` (`order_execution.py:2066`) with `order_send_intents` reservation | code sends `ORD_DVSN` 00 limit / 01 market, integer quantity only (`domestic_orders.py:386,394`); no amount order in KIS client | DAY; SOR buy conservative 20:00 (`live_order_expiry.py:140`) | Q, S≤regular, H | MF (kis_mock KR regular via `G*` `kis_mock_forwarder.py:159`) |
| kis_live KR NXT pre 08:00–08:50 | G → `_place_order_impl` (NXT route, `nxt_tradable` only; routing `SOR`/`KRX` `domestic_orders.py:65`) | code: 00 limit / 01 market, int qty. Market and other NXT `ORD_DVSN` codes on pre-session [unverified: check KIS official `examples_llm/domestic_stock/order_cash` + KIS NXT notice; until confirmed send 00 limit]. KIS has no amount order field — NXT amount orders (operator note) not reachable via KIS | DAY; carries to 20:00 if SOR [unverified: confirm KIS ODNO survives 08:50→09:00 boundary via `kis_live_get_daily_orders` on first live day] | Q, S≤ext, H, NXT eligibility | LH (no mock session) |
| kis_live KR NXT after 15:30–20:00 (continuous from 16:00) | G → `_place_order_impl` | same as NXT pre [unverified: market order acceptance in NXT after] | expires 20:00 (`live_order_expiry.py`) | Q, S≤ext, H; 15:30–16:00 refused (`session_segment_of` UNKNOWN) | LH |
| kis_live KR KRX after-market 16:00–20:00 | G → `_place_order_impl` KRX-after route | 00 limit, int [unverified: market order in KRX after-market — KRX rulebook]; listed-eligible only (`approval_window.py:339 resolve_krx_after_capability`) | expires 20:00 (`REASON_KRX_AFTER_CLOSE`, `live_order_expiry.py:83`) | Q, S≤ext, H, eligibility fresh ≤7d | LH |
| kis_live US regular | G → `_place_order_impl` (`equity_us`) | limit; int; KIS overseas market order [unverified: overseas `ORD_DVSN` 00 only in client — check `overseas_orders.py` + KIS sample] | DAY, US trading day rolls 20:00 ET (`us_trading_day_window`) | Q, S≤regular, H | MF (kis_mock US via kis_mock path; see T5) |
| kis_live US pre/post | G → `_place_order_impl` | limit, int [unverified] | [unverified: KIS overseas pre/after order TR & DAY carry — verify from KIS official sample `examples_llm/overseas_stock/order*` before T11; if unsupported, cell is closed `unsupported`] | Q, S≤ext, H | LH |
| toss_live KR regular / NXT / after | G → `toss_place_order` (`orders_toss_variants.py:2183`) w/ `nxt_preflight` | quantity or `orderAmount` (amount order, `orders_toss_variants.py:409-416`) in all KR sessions per operator note; limit/market per session [unverified: Toss OpenAPI 1.2.28 order schema per NXT session — check `openapi.json` `orderType` enum; first live NXT amount order recorded] | DAY per Toss; NXT carry [unverified: Toss KR SOR carry 15:30→20:00 — verify by `toss_get_order_history` on first after-session fill] | Q, S, H, `krx_after` preflight (#969) | LH |
| toss_live US regular | G → `toss_place_order` | limit/market, quantity incl. fractional, or `orderAmount` | DAY | Q, S, H | LH |
| toss_live US pre/post | G → `toss_place_order`; requires policy key `toss_live_us_sessions` include pre/post (`config/trading_policy.yaml:458`) | **only cell with limit-only, integer qty, no `orderAmount`** (`approval_window.py:448-466`) | pre: expires next Toss regular close (doc, `measured=false`); post: [unverified] measure per #908/#1032 | Q, S≤ext, H | LH |
| toss_live US day market | — | closed `unsupported` (not in decision) | — | — | — |
| upbit_live KRW 24h | `G*` `upbit_live_forwarder.py` | Upbit accepts `limit`, market buy by KRW amount (`price`), market sell by `volume` (`upbit/orders.py:138,180`); gateway path today sends limit only (tick-validated `app/core/upbit_tick.py`); keep limit for autonomous v1 | GTC until cancel; intent TTL ≤12 h (`mandates.py:61`) | Q, S, market_event warning/unknown/stale block | LH |
| kis_mock KR regular | `G*` `kis_mock_forwarder.py:185` (`is_mock=True`) | L/M; attribution required (hard rule 7) | DAY | existing | mock — no arming |
| kis_mock US | G (new v3 kis_mock US forwarder) → `_place_order_impl(is_mock=True)` | L; int | DAY ET | existing | mock |
| kiwoom_mock KR / US | G → `place_kiwoom_mock_order` / `place_kiwoom_mock_us_order` (`kiwoom_mock_place.py:737`, `:1293`) | KRX regular / XNYS regular only; attribution (hk 4723) | DAY | existing preflight | mock |
| alpaca_paper (US, ext hours LIMIT+day) | G → existing paper submit | L, `extended_hours` per hk 6497 | DAY | paper URL + account suffix | mock |
| binance spot / futures demo | G → demo execution clients | L/M per client; futures 1x, reduceOnly exits | GTC | demo host allowlist | mock |
| nh_mock KR | G → `nhplug_mock/operations.py` (only `_dispatch_new_intent`) | exact `"limit"`, KRX non-SOR | DAY | Stage 2 gates all true (hard rule 14) | mock |
| kis_futures_mock day | G → `kis_futures_mock/place.py` | MF200 limit 1 contract (`constants.py:91 LOCKED_LIMITS`) | day session only | locked consts | mock |

Spread gate values (S): regular ≤ 30 bp, extended ≤ 15 bp is the recommended start (Q3); policy key, not code constant.

### 2.2 Arming state machine (per account × market × session-class)

`frozen` (default) → `mock_proving` (MF cells) or `live_half` (LH cells) → `live_full`.
- MF: 10 consecutive mock round trips (entry fill + exit fill) through the **same** gateway path with zero
  `uncertain`/`refused(error)`/cancel failure/reconcile mismatch → `live_full`. Any error resets the counter.
- LH: live with per-order cap 1,000,000 KRW for 10 clean live round trips → `live_full`. Per the decision text "(1,000,000 KRW; one-share exception unchanged)", a qty=1 order whose one share costs more than 1,000,000 KRW may still go up to 10,000,000 KRW during `live_half`; this is the operator's explicit choice (Q7 offers a stricter alternative). T2 tests pin the order: I1b half cap first, then I1a evaluated against the cap in force.
- Stored in new append-only `review.autonomous_arming_events`; state is a fold. Kill freeze does not reset arming.

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
| I7 | Plan supersession: old version never sends after new is published | plan_version | publish vN+1 appends a plan_versions row; intent rows stay immutable (trigger `20261101_rob1440_intent_gateway.py:53`) — an intent is live only if its `plan_version` equals the latest published version of its `plan_id` | gateway decision reads the plan head under the plan advisory lock (taken by publisher too) + pre-send hook re-read | refused `plan_superseded`; cancels of vN orders still allowed |
| I8 | Partial fill → exit only ledger-confirmed net quantity | `GET .../fills` lineage `filled_qty` (in-effect, ROB-1379) | exit child | gateway decision (as `upbit` S-rules: sell ≤ proven own holding) | refused `exit_exceeds_confirmed_qty` |
| I9 | Budget re-reservation | plan notional − filled − working | supersede / partial fill | AT publisher under sleeve lock; gateway high-water cap per intent (`decisions.py` existing) | new intent budget = remaining only; cancel never releases (D-4) |
| I10 | Halt / stale / expected-price block | `halted_suspect`, `kr_symbol_trading_flags`, `price_kind=expected`, quote age | child reserve + pre-send | gateway decision + pre-send hook (reuse `halt_detection.py`, `kr_trading_flags/gate.py`, `kr_quote_price.py`) | refused `halt_suspect`/`quote_stale`/`expected_price` |
| I11 | Per venue×session order shape (§2 column), both sides fresh in all sessions, stricter spread when thin; Toss US pre/post limit-int-no-amount only | session segment, book, order form | child reserve | gateway decision via `session_segment_of` (`session_segment.py:258`) + per-cell allow-list; Toss US shape `apply_toss_us_extended_order_shape` | refused `order_form_not_accepted` / `spread_too_wide` |
| I12 | Discord alerts for every live order, fill, auto stop-loss, freeze | journal/ledger/kill transitions | after commit | new alert emitter (ops webhook `config.py:561 discord_webhook_alerts`), fail-open | message per event; alert failure never blocks |
| I13 | Kill switch default frozen | missing/unreadable row | — | `account_control.py:47 read` (existing semantics) + migration seeds `GLOBAL_FREEZE` per new account | no send until operator sets ACTIVE |
| I14 | Live-only scope: cash buy/sell; no short/margin/futures/options | intent side/product | intent store | intent CHECK + decision | refused at store |
| I15 | Existing manual gates unchanged (approval_hash, W5, kis_mock attribution) | — | — | untouched paths | golden tests byte-identical |

## 4. AGENTS.md hard-rule-4 exception paragraph (to be committed by T3)

> hk 6939 의 명시 운영자 결정(2026-11-27, `decision/2026-11-27/at-autonomous-strategy-orders`)은 의도 게이트웨이의 전략 주문 forwarder(`kis_live` KR(KRX·NXT)/US, `toss_live` KR/US, `upbit_live` 원화, 그리고 기존 게이트 아래의 모의·데모 계좌)만 예외로 허용한다 — `EXECUTION_INTENT_GATEWAY_ENABLED` AND 계좌별 `EXECUTION_INTENT_GATEWAY_KIS_LIVE_ENABLED`·`EXECUTION_INTENT_GATEWAY_TOSS_LIVE_ENABLED`·`EXECUTION_INTENT_GATEWAY_UPBIT_LIVE_ENABLED`(모두 기본 false, 정확히 `"true"`) AND `EXECUTION_INTENT_GATEWAY_ROUTE_ENABLED` AND 계좌 kill 행 `ACTIVE`(부재·판독 실패·일일 손실 미측정은 freeze) AND 계좌×시장×세션 arming 상태 AND 발행된 최신 계획 버전 AND 호출별 confirm 헤더가 모두 필요하다. 잠금 상수는 주문당 2,000,000원(미국은 원화 환산), 1주가 그보다 비쌀 때만 1주·10,000,000원, 계좌당 미체결 4건, 일일 손실 500,000원 이상이면 자동 freeze, 미모의 시장의 첫 10회 왕복은 주문당 1,000,000원(1주 예외는 유지)이며 어떤 CLI·env 로도 올릴 수 없다. 손실매도 가드는 그대로이고 예외는 계획에 진입 시 기록된 무효화 가격을 실제 가격이 이탈한 기계식 매도 하나뿐이다(그 밖의 손실 매도는 사람 카드). 주문 형태는 브로커·세션별 허용 목록을 따르며(토스 미국 프리·애프터만 지정가·정수 수량·금액 주문 금지), 모든 세션에서 신선한 양쪽 호가와 얇은 호가의 더 엄격한 스프레드를 요구한다. Upbit 손절 쿨다운·`loss_cut` 상수·`LOCKED_ENVELOPE`(5,000,000원)·`MAX_INTENT_NOTIONAL_KRW` 를 포함한 기존 게이트, 서비스 경유 원장 쓰기·증거 우선 체결·불명 송신 재전송 금지·kis_mock 귀속·W5 inbox·수동 경로 approval_hash 는 바뀌지 않으며, 공매도·신용·선물·옵션 실계좌와 스케줄러 등록은 허용되지 않는다.

## 5. Tickets (ordered "possible first")

Tier T3 = order path / guard wiring → strong model. M = needs migration.

| # | Repo | Title | Tier/Grade | M | Blocks on |
|---|---|---|---|---|---|
| T1 | AT | Plan tables + publisher + supersession | T3/S | M | — |
| T2 | AT | Autonomous decision module (I1–I2, I7–I11, I14) pure + tests | T3/S | — | T1 |
| T3 | AT | AGENTS.md exception + contracts + `LOCKED_ENVELOPE`/intent ceiling raise for 1-share | T3/S | — | T2 |
| T4 | AT | Daily-loss monitor + kill freeze writer + per-account kill rows | T3/S | M | T1 |
| T5 | AT | Arming fold + kis_mock US forwarder (v3) | T3/A | M | T2 |
| T6 | AT | Discord alert emitter | T2/B | — | T1 |
| T7 | AT | Gateway deploy on pi (loopback server unit file copy, env names, runbook) | T2/A | — | T3 |
| T8 | FW | hk 5874 fillwire serve install (pi) | T2/A | — | — |
| T9 | FW | Fillwire plan follower: reads intent v3, emits children (entry/target/invalidation) | T3/S | — | T2, T8 |
| T10 | AT | kis_live KR forwarder (KRX regular, NXT pre/after, KRX after) | T3/S | — | T3, T4, T5 |
| T11 | AT | kis_live US forwarder (regular, pre/post if supported) | T3/S | — | T10 |
| T12 | AT | toss_live KR/US forwarder (+ policy key pre/post via operator PR) | T3/S | — | T10 |
| T13 | AT | upbit_live: widen mandate ceiling to envelope, invalidation exception | T3/S | M | T3, T4 |
| T14 | AT | Mock forwarders: kiwoom KR/US, alpaca, binance, NH, KIS futures mock | T3/A | M | T2 |

Per ticket (Current / Desired / Acceptance / Out of scope / evidence):

- **T1** C: no plan entity; intents written by hand (`scripts/execution_mandates.py`). D: append-only plan + versions; publisher writes the new version and its v3 intent in one tx; intents stay immutable. A: DB trigger refuses non +1 version and UPDATE/DELETE; a vN intent's child is refused `plan_superseded` once vN+1 exists. O: no sending. Ev: `app/services/execution_intent_gateway/store.py`, `app/schemas/execution_intent.py:43`.
- **T2** C: caps per account scattered (`decisions.py:215`, `mandates.py:58`). D: one pure module for envelope/arming/session/quote rules. A: table-driven tests per I1–I2, I7–I11, I14. O: wiring to forwarders. Ev: `app/services/execution_intent_gateway/decisions.py:215`.
- **T3** C: hard rule 4 has no autonomous exception; `LOCKED_ENVELOPE` 5,000,000. D: §4 paragraph, contracts section; new separate locked `ONE_SHARE_CEILING_KRW` (qty==1). A: static test pins paragraph switches; test pins `LOCKED_ENVELOPE` and `MAX_INTENT_NOTIONAL_KRW` unchanged at 5,000,000. O: forwarders. Ev: `app/services/kis_mock_runner/envelope.py:51`.
- **T4** C: kill rows only for upbit (`account_control.py:78`). D: rows for kis_live/toss_live seeded `GLOBAL_FREEZE`; loss monitor freezes at ≥500k or unknown. A: unknown P&L → freeze test; freeze is append-only + Discord. O: unfreeze automation (operator only). Ev: `app/services/execution_intent_gateway/account_control.py:47`.
- **T5** C: kis_mock forwarder KR only (`kis_mock_forwarder.py:159`). D: arming events table + fold; kis_mock US child path. A: 10 clean round trips → `live_full`; any error resets. O: live forwarders. Ev: `app/services/execution_intent_gateway/kis_mock_forwarder.py:185`.
- **T6** C: no per-order Discord for gateway. D: post-commit emitter, fail-open. A: alert failure never changes journal outcome. O: Telegram. Ev: `app/core/config.py:561`.
- **T7** C: gateway not on pi. D: unit-file copy + runbook, env names only. A: runbook lists every switch default false. O: enabling. Ev: `scripts/run_execution_intent_gateway.py:31`.
- **T8** C: pi runs only old KIS fill ingest + Upbit recorder. D: hk 5874 serve install. A: serve health on pi. O: exec mode Send. Ev: fillwire `cmd/fillwire`, `internal/exec/e1/client.go:127 ExecutorSends`.
- **T9** C: execmock/execsim/execupbit drive fixed intents. D: follower that maps plan v3 boundaries (entry cap, target, invalidation) to children; never amends above cap. A: replay tests; dry-run by default. O: plan creation. Ev: fillwire `internal/exec/intent.go:18`, `internal/exec/exposure.go:10`.
- **T10** C: kis_live only via MCP/proposal (`orders_kis_variants.py:530`). D: gateway forwarder calling `_place_order_impl` with gateway pre-send hook. A: no send while kill/arming/plan stale; uncertain never resent; NXT/KRX-after session rules tested. O: US. Ev: `app/mcp_server/tooling/order_execution.py:2066`.
- **T11** C: US live only manual. D: US forwarder; pre/post closed `unsupported` until KIS TR verified. A: XNYS + ET day tests. O: Toss. Ev: same `:2066`.
- **T12** C: Toss via MCP with `approval_hash` (`orders_toss_variants.py:1310`). D: forwarder passing gateway-issued hash; US ext shape enforced. A: day-market refused; pre/post only if policy key lists them. O: policy flip. Ev: `app/mcp_server/tooling/orders_toss_variants.py:2183`.
- **T13** C: mandate per-intent ceiling 480,000 (`mandates.py:58`). D: ceiling = envelope (2,000,000 / half cap), invalidation exception on top of `LOSS_SELL_FLOOR_FACTOR`. A: other loss sells still `loss_sell_requires_card`. O: cooldown change. Ev: `app/services/execution_intent_gateway/upbit_decisions.py:65`.
- **T14** C: mock venues have their own send functions, no gateway. D: one forwarder each under existing gates. A: attribution/kiwoom/NH/futures locked rules unchanged. O: live. Ev: `app/services/kiwoom_mock_place.py:737`, `app/services/kis_futures_mock/constants.py:91`.

## 6. Open questions (recommended answers)

1. Q1 FX for US cap: which rate binds? **Rec:** `exchange_rate_service` midRate snapshot stored in intent at publish; refuse if unavailable (no fallback rate).
2. Q2 Daily-loss measure: realized only or realized + MTM? **Rec:** realized + MTM of open lots, KST day; unknown = frozen.
3. Q3 Spread gate values. **Rec:** 30 bp regular / 15 bp extended / Upbit 20 bp, policy keys, operator PR.
4. Q4 Do mock round trips for KIS KR regular count NXT? **Rec:** no; NXT/after cells are LH.
5. Q5 KIS US pre/post TR availability [unverified]. **Rec:** keep `unsupported` until KIS official sample confirms; then LH.
6. Q6 Does a Toss/KIS cancel in kill state remain allowed? **Rec:** yes, cancel ignores kill (ROB-1440 rule) but checks account fingerprint.
7. Q7 One-share exception during `live_half`: the decision keeps it at 10,000,000 KRW. **Rec:** keep as decided; if the operator wants a stricter start, cap it at 2,000,000 KRW while `live_half` (one policy constant, T2).
