# ROB-87 Alpaca Paper roundtrip retrospective

- 작성일: 2026-05-03
- 범위: [ROB-84](/ROB/issues/ROB-84) → [ROB-88](/ROB/issues/ROB-88), parent [ROB-40](/ROB/issues/ROB-40)
- 런타임 SHA: `b5340f6c1d5ec651951f0e055480f44bef3c8df1`
- Roundtrip client order ids: `rob87-buy-20260503160600`, `rob87-sell-20260503160600`
- 작성 범위: read-only retrospective. 이 문서는 브로커 주문, DB 직접 수정, 스케줄러, watch/order-intent, live/generic/KIS/Upbit 경로를 추가하거나 실행하지 않는다.

## 1. TL;DR

ROB-87에서 한 번의 bounded Alpaca Paper crypto buy/sell roundtrip이 완료됐다. 신호 provenance는 `Upbit:KRW-ETH`, 실행 provenance는 `Alpaca Paper:ETH/USD`로 분리됐고, preview → `confirm=False` gate → buy fill/reconcile → sell close/reconcile → 최종 no-ETH residual 상태까지 연결됐다.

Core plumbing은 작동한다. ROB-84 ledger, ROB-85 buy fill/reconcile, ROB-86 guarded sell/close contract, ROB-87 final reconcile이 모두 이어졌다. 다만 이 결과는 “자동화 가능” 신호가 아니라 “다음에 무엇을 표준화해야 하는지 보이는 첫 표본”으로 해석해야 한다. 다음 slice는 스케줄러/확대 실행보다 data/ledger 정규화, approval packet hardening, operator report/read view를 먼저 다루는 쪽이 안전하다.

## 2. Context

ROB-84..ROB-87은 preopen/crypto thesis를 Alpaca Paper 실행·회수까지 연결하기 위한 최소 체인을 순서대로 만들었다.

| Issue | 산출물 | ROB-88에 남긴 의미 |
|---|---|---|
| ROB-84 | `review.alpaca_paper_order_ledger`, `AlpacaPaperLedgerService`, read-only FastAPI/MCP ledger paths. PR #652 merged/deployed at `f0cf279072aa5df99a6812321866b6eb60b160cf`. | 실행 lifecycle과 signal/execution provenance를 audit row로 읽을 수 있게 됐다. |
| ROB-85 | Guarded Alpaca Paper buy fill/reconcile smoke. PR #653 merged/deployed at `9b6304ad204f00934c292424d55d60c30b2f4e4f`. | `rob85-fill-20260503033724` (`KRW-BTC` → `BTC/USD`)와 `rob85-fill-20260503035400` (`KRW-SOL` → `SOL/USD`)이 `filled_position_matched`까지 검증됐다. |
| ROB-86 | Guarded Alpaca Paper sell/close safety contract. PR #654 merged/deployed at `b5340f6c1d5ec651951f0e055480f44bef3c8df1`. | sell/close는 `close_position`, `close_all`, liquidation, generic route, bulk behavior 없이 explicit Alpaca Paper crypto sell-limit path로 제한됐다. |
| ROB-87 | ETH/USD buy/sell roundtrip smoke. | 처음으로 buy fill과 sell close가 같은 paper cycle에서 최종 no-residual 상태로 연결됐다. |

기존 `BTCUSD`, `SOLUSD` paper positions는 ROB-85에서 생긴 out-of-scope context다. ROB-88은 해당 잔여 포지션을 닫거나 변경하기 위한 작업이 아니다.

## 3. What worked

1. Ledger lifecycle이 buy와 sell 양쪽을 연결했다.
   - Buy row `rob87-buy-20260503160600`: `filled`, `filled_position_matched`.
   - Sell row `rob87-sell-20260503160600`: `filled`, `closed_position_matched`, post-position qty `0`.
2. Signal venue와 execution venue가 분리됐다.
   - Signal: `Upbit:KRW-ETH`.
   - Execution: `Alpaca Paper:ETH/USD`.
3. Preview와 `confirm=False` gate가 의도대로 비제출 경로를 증명했다.
   - Buy와 sell 모두 `confirmation_required` gate가 기록됐다.
4. Sell/close helper가 과다 수량 source-filled-qty 시도를 mutation 전에 fail-closed했다.
   - 초기 sell dry-run은 현재 matching position qty가 요청 sell qty보다 작아 중단했다.
   - 이후 reconciled buy position qty `0.003929098`로 close leg를 재실행했다.
5. 최종 reconciliation이 깨끗했다.
   - Open Alpaca Paper orders count: `0`.
   - `ETH/USD` / `ETHUSD` residual position: 없음.
   - Recent fills에는 ROB-87 ETH buy/sell이 모두 `filled`, `leaves_qty=0`으로 남았다.
6. Production/deploy gate가 최종 runtime SHA 기준으로 정리됐다.
   - ROB-86 contract가 production current SHA `b5340f6c1d5ec651951f0e055480f44bef3c8df1`에 올라간 뒤 ROB-87 K3가 재개됐다.

## 4. What was manual

1. Linear/Discord-facing audit report는 수동 조립이었다.
   - ROB-84..ROB-87 comments, ledger rows, fills, positions, PR/deploy state를 사람이 합쳤다.
2. Preopen/QA/bridge/briefing evidence가 한 번에 보이는 read view가 없었다.
   - ROB-87 evidence에는 signal/execution, preview, fill, reconcile은 있지만 QA/bridge/artifact fields가 complete packet으로 고정되어 있지 않다.
3. PR/deploy gate 해소와 K3 unblock이 운영자 triage를 요구했다.
   - PR #654 deploy 전에는 confirm=True roundtrip이 중단됐고, deploy 후 별도 unblock/resume comment가 필요했다.
4. Sell rerun에서 수량 선택이 수동 판단이었다.
   - `filled_qty`를 그대로 close qty로 쓰려던 첫 path가 fail-closed했고, reconciled position qty를 사람이 확인해 재시도했다.
5. 최종 report의 compliance bullets가 반복 작성됐다.
   - 금지된 live/generic/KIS/Upbit/bulk/watch/order-intent/scheduler/DB-direct side effects를 매번 수동으로 나열했다.

## 5. What was fragile / nearly blocked

| 지점 | 관찰 | 왜 fragile인가 |
|---|---|---|
| PR/deploy prerequisite | ROB-87 K3는 PR #654가 아직 main/production에 없어서 `--execute` 전에 멈췄다. | 정상적인 code/deploy gate였지만, block/unblock classification이 수동이었다. |
| Opus/Claude auth lane | 일부 upstream review lane에서 Claude Code auth unavailable로 manual review fallback이 발생했다. | 모델-lane 요구사항과 실제 worker auth 상태가 어긋나면 board가 불필요하게 대기할 수 있다. |
| Sell close qty source | Source filled qty 기준 sell dry-run이 fail-closed했고, reconciled position qty로 재시도했다. | crypto fee/settlement delta를 report/ledger가 first-class로 설명하지 않으면 정상 delta가 오류처럼 보인다. |
| `validation_summary.close_intent=reduce` artifact | 최종 close가 성공했는데 sell row에 초기 dry-run path artifact가 남았다. | attempt별 validation history가 없으면 최종 상태와 과거 실패 artifact가 혼동된다. |
| Venue taxonomy drift | buy/sell rows에서 execution venue taxonomy가 `alpaca_paper` / `alpaca_paper_crypto`로 다르게 보인다. | 같은 venue/asset class라도 audit table과 report에서 일관성이 떨어진다. |
| QA/bridge/artifact packet | ROB-87 final evidence에는 QA/bridge/briefing 상태가 roundtrip table에 완결 형태로 캡처되지 않았다. | parent ROB-40이 “전략 품질 → 실행 결과”를 한 화면에서 판단하기 어렵다. |

## 6. End-to-end chain table

| Chain step | Evidence captured | Status | Gap / note |
|---|---|---|---|
| Thesis / signal | Signal venue `Upbit`, signal symbol `KRW-ETH`. | Captured | KRW signal → USD execution FX/proxy disclosure는 별도 first-class field로 남기면 좋다. |
| QA evaluator | ROB-87 final comments에는 quantitative QA status가 roundtrip packet으로 명시되지 않았다. | Missing / not captured in ROB-87 evidence | QA verdict와 paper outcome을 나중에 비교하려면 approval/report packet에 고정 필드가 필요하다. |
| Approval bridge / briefing artifact | ROB-84 ledger schema는 fields를 지원하지만 ROB-87 final report에는 artifact/bridge status가 완결 table로 나타나지 않았다. | Missing / not captured in ROB-87 evidence | operator가 Linear comments를 수동 join해야 한다. |
| Execution request — buy | Alpaca Paper `ETH/USD`, asset class crypto, side buy, limit, notional `$10`, limit `2538.75`, TIF `gtc`, client_order_id `rob87-buy-20260503160600`. | Captured | Raw broker order id와 account identifiers는 의도적으로 생략. |
| Preview / confirm-false — buy | Preview OK, `confirm=False` blocked with `confirmation_required`. | Passed | 비제출 gate가 정상 동작. |
| Submit / fill — buy | One paper buy submitted via explicit Alpaca Paper path, order filled. | Passed | Live/generic route 미사용. |
| Position / reconcile — buy | `filled_position_matched`. | Passed | 이후 sell source qty 선택에서 received-position qty delta가 드러남. |
| Execution request — sell | Alpaca Paper `ETH/USD`, side sell, limit `2077.16`, TIF `gtc`, client_order_id `rob87-sell-20260503160600`, source buy client order id. | Captured | 첫 source-filled-qty attempt는 fail-closed, 실제 close는 reconciled position qty `0.003929098` 사용. |
| Preview / confirm-false — sell | Preview OK, `confirm=False` gate OK / `confirmation_required`. | Passed | 과다 qty path가 mutation 전에 중단된 점이 안전하게 작동. |
| Submit / fill — sell | One paper sell submitted via explicit Alpaca Paper sell-limit path, order filled. | Passed | `close_position`/bulk/generic route 미사용. |
| Final position / reconcile | `closed_position_matched`, post-position qty `0`, open orders `0`, no `ETH/USD` residual position. | Passed | BTCUSD/SOLUSD는 pre-existing out-of-scope positions. |

## 7. Gap classification and next issue candidates

| Gap ID | Class | Evidence | Severity | Proposed next issue candidate | Recommended priority |
|---|---|---|---|---|---|
| D1 | data/ledger | ROB-87 caveat: `alpaca_paper` vs `alpaca_paper_crypto` venue taxonomy drift. | blocker before automation | Normalize Alpaca Paper execution venue taxonomy for ledger audit reports. | High |
| D2 | data/ledger | Buy filled qty와 sell closed qty 사이에 expected crypto received-position / fee-settlement delta가 있었다. | blocker before automation | Add fee/settlement delta labeling to Alpaca Paper ledger reports. | High |
| D3 | data/ledger | Sell row에 초기 dry-run `validation_summary.close_intent=reduce` artifact가 남았다. | blocker before automation | Version validation summaries by attempt for paper sell/close ledger rows. | High |
| D4 | data/ledger | Reports rely on strings such as `filled_position_matched`, `closed_position_matched`. | soft → monitoring prerequisite | Define canonical paper reconcile status enum and report mapping. | Medium-high |
| S1 | safety | Exact approval packet was assembled as free text across comments. | blocker before automation | Define JSON schema for bounded Alpaca Paper execute approvals. | High |
| S2 | safety | ROB-87 used fixed buy/sell client_order_ids across dry-run/execute/retry phases. | blocker before repeated runs | Reject unsafe client_order_id reuse across paper smoke attempts. | High |
| S3 | safety | Each smoke script performs its own account/open-order/position/symbol guard checks. | soft | Extract reusable Alpaca Paper preflight guard for bounded smoke scripts. | Medium-high |
| S4 | safety | PR/deploy gates changed between preview and execute phases. | soft | Expire bounded paper execution approvals after configurable TTL. | Medium |
| U1 | operator UX | ROB-87 report manually joined Linear, ledger, fills, positions, and git/deploy state. | blocker before automation | Add read-only bundled Alpaca Paper roundtrip report command/MCP tool. | High after D1/D2/D3 |
| U2 | operator UX | Final audit comments repeat the same safety/compliance bullets. | soft | Create static paper roundtrip retrospective template for Linear/Discord. | Medium |
| U3 | operator UX | Operator needs a read path without scraping comments. | soft | Add paper ledger lifecycle browser to trading decision dashboard. | Medium |
| M1 | monitoring/automation | Smoke scripts can produce `unexpected_state`; no alerting is defined. | soft → automation prerequisite | Alert on Alpaca Paper ledger unexpected lifecycle state. | Medium-high after D4 |
| M2 | monitoring/automation | `submitted` / `open` / `partially_filled` rows need TTL monitoring before unattended runs. | soft → automation prerequisite | Alert on stale Alpaca Paper submitted/open/partial rows. | Medium-high |
| M3 | monitoring/automation | ROB-40 needs trends after more cycles; one cycle is not a trend. | soft | Daily paper roundtrip summary grouped by QA verdict and symbol. | Medium after N≥3 / N≥10 |
| P1 | product | ROB-87 exercised crypto only. | soft | Run guarded US equity Alpaca Paper buy/sell roundtrip during market hours. | Medium after safety/report hardening |
| P2 | product | ROB-86/87 cover one close leg, not partial exits. | soft | Design multi-leg Alpaca Paper sell/reduce ledger contract. | Low-medium |
| P3 | product / operator UX | Upbit KRW signal mapped to Alpaca USD execution without first-class FX disclosure in final report. | soft | Add KRW signal to USD execution FX provenance in approval reports. | Medium |
| Q1 | strategy quality | One smoke proves plumbing, not trade quality. | soft | Define Alpaca Paper fill slippage quality metrics. | Medium after more samples |
| Q2 | strategy quality | QA status was not quantitatively linked to paper outcome. | soft | Compare preopen QA verdicts against paper roundtrip outcomes. | Medium after N≥10 |
| Q3 | strategy quality | ROB-87 observed a small crypto qty delta. | soft | Benchmark crypto received-position deltas across paper roundtrips. | Medium after D2 and N≥10 |

Recommended first three roadmap candidates:

1. D1 + D3 combined: normalize venue taxonomy and version/clear validation artifacts by attempt.
2. S1 + S2 combined: formal approval packet schema and client_order_id idempotency guard.
3. U1: bundled read-only roundtrip report command/MCP view, after D1/D2/D3 definitions are stable.

## 8. What not to do next

- Do not enable scheduler-driven paper trading from one successful roundtrip.
- Do not widen notional limits or move toward live/generic routes based on this result.
- Do not close, cancel, or otherwise touch `BTCUSD` / `SOLUSD` residual positions as part of ROB-88.
- Do not add `close_all`, broad liquidation, by-symbol bulk close/cancel, or generic broker mutation paths.
- Do not add direct DB maintenance scripts, backfills, or ad-hoc SQL updates for the ledger.
- Do not auto-post to Linear/Discord with new tokens or webhooks as part of this retrospective.
- Do not collapse signal venue and execution venue into a single symbol/venue field.
- Do not treat strategy quality as proven until there are repeated samples with QA verdict and outcome linkage.

## 9. ROB-40 decision points

1. Next cycle shape: run another bounded crypto roundtrip first, or harden ledger/reporting before more broker-side tests?
2. Venue taxonomy: normalize `alpaca_paper` / `alpaca_paper_crypto` now, or tolerate drift until N≥3?
3. Approval schema: require a structured JSON approval packet before any further `confirm=True` paper execution?
4. Market expansion: stay in crypto sandbox, or add one US equity market-hours paper roundtrip after safety/report hardening?
5. Monitoring minimum: what alerts must exist before scheduler-driven paper execution is even considered?
6. Operator UX: is a read-only roundtrip report command enough, or should the dashboard ledger lifecycle view come first?
7. Strategy quality: how many samples are needed before QA verdict vs paper outcome analysis becomes meaningful?

## 10. Linear / Discord-ready summary

ROB-88 retrospective draft is ready.

Summary:
- ROB-87 completed one bounded Alpaca Paper crypto roundtrip: signal `Upbit:KRW-ETH`, execution `Alpaca Paper:ETH/USD`.
- Buy `rob87-buy-20260503160600` reached `filled` / `filled_position_matched`; sell `rob87-sell-20260503160600` reached `filled` / `closed_position_matched` with post-position qty `0`.
- Final read-only state: open orders `0`, no `ETH/USD` residual position; pre-existing `BTCUSD` and `SOLUSD` remain out of scope.
- Core plumbing works, but automation is not ready from one sample.

Top next candidates:
1. Normalize Alpaca Paper venue taxonomy and version validation artifacts by attempt.
2. Add fee/settlement delta labeling and a canonical reconcile status mapping.
3. Define structured bounded paper approval packets plus client_order_id idempotency rules.
4. Add a bundled read-only roundtrip report command/MCP view after the data definitions are stable.
5. Add lifecycle/stale-row monitoring before any scheduler-driven paper cycle.

Safety statement:
- This retrospective is docs-only/read-only.
- No broker/order mutation, live/generic/KIS/Upbit/bulk/watch/order-intent/scheduler change, or direct DB update/delete/backfill was performed by ROB-88 writer scope.
- Secrets, credentials, Authorization headers, account identifiers, raw broker order ids, and raw asset ids are intentionally omitted.
