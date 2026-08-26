# 워치 발화 트리거 구동 재판정 (ROB-1286 / ROB-1304)

워치 발화(`review.investment_watch_events`)를 시장별 거래 가능 시간에 폴링해, 미소비
`review_required` 이벤트를 종목 한정 재판정 세션으로 전환한다. 목적은 **제안
생성 지연 단축**이다.

🔴 **현재 상태: 미가동. 배포·스케줄 등록 0.** 아래 §5 의 차단 항목이 해소되기
전에는 arm 하지 않는다.

---

## 1. 왜 이게 위험한 작업인가

이 flow 가 스폰하는 세션은 `order_proposal_create` 에서 멈춘다. 그러나 **생성된
제안은 기존 승인 기계로 흘러간다.** 그 기계에는 §40/51차 자동승인 레인이 있고,
조건이 맞으면 사람 클릭 없이 주문이 접수될 수 있다.

따라서 이 작업은 "지연만 줄이는" 것이 아니다. **제안 생성 빈도를 올리면
자동승인 경로로 들어가는 양도 오른다.** §3 의 dedup·상한은 편의 기능이 아니라
그 빈도를 묶는 안전장치다.

자동승인까지 도달하려면 아래가 **전부** 참이어야 한다 (전부 코드 확인).

⚠️ **r2 / SHOULD-2 정정**: r1 보고서는 이 표를 "11개 조건 전부" 로 제시했으나,
그것은 **자동승인 판정기 내부의 조건만** 센 것이었다. 제안이 그 판정기에 도달하기까지
넘어야 할 **선행 관문**이 빠져 있었다. 아래 §1.1 이 그 누락분이다. 누락은 사슬을
실제보다 **넓게** 주장한 쪽이라 안전 방향이지만, "11개 전부" 라는 서술은 부정확했다.

| # | 조건 | 위치 |
|---|---|---|
| 1 | `ORDER_PROPOSALS_AUTO_APPROVE=true` (기본 false) | `app/services/order_proposals/dispatch.py:841` |
| 2 | `ORDER_PROPOSALS_AUTO_APPROVE_MODE="expanded"` (기본 `off`) | `app/core/config.py:853` |
| 3 | toss_live 는 추가로 `ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED=true` (기본 false) | `auto_approve.py:_is_veto_capable_account_market` |
| 4 | `(account_mode, market)` ∈ `_VETO_CAPABLE_ACCOUNT_MARKETS` | `auto_approve.py:_VETO_CAPABLE_ACCOUNT_MARKETS` |
| 5 | 같은 세션 Toss 체결 freeze 없음 — 해제는 모든 auto rung의 정확한 full fill과, 대응 place ledger의 `filled/FILLED`·`reconciled_at`·무 review/error, 그리고 `original_order_id` 연결 cancel/modify sibling의 clean terminal reconciliation이 **모두** 증명될 때만 | `dispatch.py::active_toss_auto_submission_freeze` |
| 6 | auto-veto 카드용 thesis 존재 | `auto_approve.py::auto_veto_thesis_summary` |
| 7 | `policy_deviation` 태그 없음 (`table_disagreement`는 감사 기록만) | `auto_approve.py::_APPROVAL_REQUIRED_TAGS` |
| 8 | 매수 rung 은 non-marketable; 매도도 기본 non-marketable이며 §156의 fresh `take_profit` 한정만 시장가성 허용 | `auto_approve.py` 모듈 docstring |
| 9 | 매도는 왕복비용 차감 후 실현손익 > 0, ±`breakeven_band_pct` 밴드 밖 | `auto_approve.py` |
| 10 | `loss_cut` 등 exit_intent 아님 | `auto_approve.py` |
| 11 | `per_order_cap` / 당일 누적 `daily_cap` 미초과 | `dispatch.py::auto_approved_daily_notional` |

행 5의 해제 술어는 "체결이 끝난 것처럼 보임"이 아니다. 누락·partial·identity/price/qty
불일치·unreconciled place row·linked cancel/modify anomaly는 모두 계속 `frozen`이다. 따라서
운영 감사에서 오늘 두 번째 자동주문이 관찰되면, 원 place order별 위 연언의 증거와
durable freeze resolution을 함께 확인해야 한다.

### 1.1 r1 표가 누락했던 선행 관문 (r2 보완)

위 11개는 `auto_approve` 판정기 **안**의 조건이다. 제안이 거기까지 가려면 그 전에
다음이 전부 참이어야 한다:

| # | 선행 조건 | 위치 |
|---|---|---|
| P1 | `ORDER_PROPOSALS_ENABLED=true` — 아니면 도구 자체가 등록되지 않음 | `tooling/registry.py`, `tradingcodex_execution_registration.py` |
| P2 | `ORDER_PROPOSALS_TELEGRAM_ENABLED=true` **그리고** chat allowlist 비어있지 않음 — dispatch 자체가 안 일어남 | `order_proposal_tools.py::_dispatch_gate` |
| P3 | Toss 클라이언트 활성 + 자격증명 + `TOSS_LIVE_ORDER_MUTATIONS_ENABLED=true` | `config.py`, `orders_toss_variants.py` |
| P4 | 최종 전송이 `dry_run=False` **그리고** `confirm=True` | `orders_toss_variants.py` |
| P5 | 기존 revalidation/pre-send 가드 통과 — preview/approval hash, high-value, warnings, market/session, approval window | `revalidation.py`, ROB-651/653 |

즉 실제 사슬은 **16단 이상**이다. 이 flow 는 그중 **첫 단(제안 생성)의 빈도**만
올린다 — 그래서 §4 의 dedup·상한과 §5.0 의 내구성 게이트가 이 PR 의 안전 논거다.

**이 PR 은 위 11개 + 선행 5개 중 무엇도 건드리지 않는다.** 정적 가드가 강제한다 —
`tests/services/watch_trigger_repricing/test_invariants.py::test_approval_gate_settings_are_never_referenced`
는 문자열 리터럴까지 스캔하므로 `getattr` 우회도 잡힌다. 여기에 r2 는
**capability allowlist**(§7.1)를 더했다: 스폰되는 세션은 브로커 주문 도구를
애초에 손에 쥐지 못한다.

---

## 2. 소비 마킹의 정본

🔴 **`review.investment_watch_events` 에는 소비 마킹 컬럼이 없다.** 기존 가변
필드는 어느 것도 "어떤 소비자가 이 발화의 재판정 책임을 졌다" 를 뜻하지 않는다:

- `outcome` — 발화 시점 스캐너 분류. `review_required` 는 이 flow 의 **입력**이다.
- `delivery_status` — Hermes 전달 추적. 하류 판정과 무관.
- `follow_up_report_item_id` — **ROB-405 Slice E 가 이미 점유.**
  `app/services/trade_journal/watch_follow_up_service.py:34` 가
  `follow_up_report_item_id IS NULL` 로 스캔한다. 재사용하면 두 기능이 서로의
  쓰기를 자기 작업으로 오인하고 양쪽 스캔이 굶는다.

그래서 정본은 **`event_uuid` 키 claim** 이고, 판정은
`app/services/watch_trigger_repricing/consumption.py` 한 곳에서만 내린다.

**5값이다 (r2).** r1 은 3값이었고 그게 BLOCKER-1 의 뿌리였다 — `CLAIMED` 하나로는
"세션이 돌고 끝났다" 와 "tick 이 lease 를 쥔 채 죽었다" 가 같은 상태라서, 후자를
살리는 lease 만료가 필연적으로 전자도 되살렸다(성공 30분 뒤 재판정).

| 상태 | 뜻 | 만료하나 | 소비 가능 |
|---|---|---|---|
| `UNCLAIMED` | 아무도 안 가짐 | — | ✅ |
| `CLAIMED` | 진행 중 lease | **한다** (홀더 크래시 자가치유) | ❌ |
| `CONSUMED` | 세션 기동이 **증명됨**. 종결 | **안 한다** | ❌ |
| `QUARANTINED` | spawn 결과 불명, 화해 실패. 종결이자 **결함** | **안 한다** | ❌ |
| `UNKNOWN` | store 가 답 못 함 | — | ❌ |

`UNKNOWN` 은 `UNCLAIMED` 이 아니다. store 가 답을 못 하면 다른 소비자가 이미
작업 중인지 알 수 없고, 그걸 "미소비" 로 추측하는 것이 발화 하나를 매도 제안 둘로
만드는 경로다. `may_consume()` 은 **증명된 `UNCLAIMED` 에만** 통과한다.

🔴 **시계가 둘이다.** 이벤트 종결(`CONSUMED`)은 영구, 종목 점유는 lease 유계다.
합치면 09:06 에 005930 발화 하나를 소비한 것 때문에 그날 남은 005930 발화가 전부
묻힌다. `state_for` 는 이벤트 생애주기를, `active_symbols` 는 lease 를 읽으며,
lease 가 지난 consumed claim 에 대해 둘이 **의도적으로 다르게** 답한다.

---

## 3. 두 소비자와 경쟁 조건

| | A안 (이 flow) | B안 (rep 세션 말미) |
|---|---|---|
| 위치 | `app/services/watch_trigger_repricing/orchestrator.py` | operator repo `prompts/kr-open-trade.md` §5 |
| 읽는 표면 | claim store (정본) | `investment_watch_events_list_recent` |
| 소비 기준 | `consumption.may_consume()` | 동일 판정을 read surface 로 전달 (§5 차단항목) |

- **A 가 먼저**: claim 이 남고 B 는 claim 을 보고 물러난다.
- **B 가 먼저**: A 의 `select_candidates` 가 `already_consumed` 로 스킵한다.
- **동시**: `try_claim` 이 원자적이라 정확히 한쪽만 이긴다. 진 쪽은
  `claim_lost_race` 로 **보고된다** (조용히 사라지지 않는다).
- **둘 다 스킵**: 불가능. 미접촉 이벤트는 `UNCLAIMED` 이고 양쪽 모두 소비 자격이
  있다 (`test_neither_consumer_skips_an_untouched_event`).

### claim → spawn 순서와 그 창

claim 이 **먼저**다. 역순(spawn→claim)은 크래시 시 같은 종목에 두 세션이
독립적으로 `order_proposal_create` 까지 가므로 더 나쁘다.

claim-first 의 잔여 창 = claim 과 spawn 사이 하드 크래시. **lease(기본 30분)가
닫는다** — 만료된 **비종결** claim 은 재취득 가능이라 발화가 다시 떠오른다.

### 🔴 spawn 결과 3분기 — 불리언으로는 표현이 안 된다 (r2 / BLOCKER-2)

r1 은 `started: bool` 이었고 **양방향으로** 틀렸다: 모든 예외를 release 로 처리해
세션이 이미 뜬 뒤 예외가 나면 다음 tick 이 중복 스폰했고, 반대로 아무것도 안 띄우고
정상 반환하면 성공으로 세어 lease 동안 발화를 묻었다.

| disposition | 증거 | claim 처리 | 결과 |
|---|---|---|---|
| `STARTED` | 기동 증명됨 | `mark_consumed` (영구) | 재스폰 없음 |
| `NOT_STARTED` | **미기동이 증명됨** (`SpawnNotStarted` raise 또는 명시 반환) | `release` | 다음 tick 재시도 |
| `AMBIGUOUS` | 그 외 모든 예외 · ack 타임아웃 | ↓ 아래 | ↓ |
| `DRY` | 리허설 경로 | `mark_consumed` | `started=False` |

🔴 **`SpawnNotStarted` 만이 깨끗한 실패다.** 일반 예외는 "요청이 안 나갔다" 와
"세션은 떴는데 ack 가 늦었다" 를 구분하지 못하므로 **불명은 '아니오'가 아니다.**

### AMBIGUOUS 의 비용 — 공짜 답이 없다는 것을 명시한다

- claim 을 **놓으면**: 첫 세션이 실제로 떠 있었을 경우 발화 하나가 세션 둘이 되고,
  각각 독립적으로 `order_proposal_create` 에 도달한다 → §1 자동승인 레인.
- claim 을 **쥐면**: 세션이 안 떴을 경우 A는 재취득 안 하고 B는 점유로 읽어
  발화가 미처리로 남는다 → 원래 ROB-1286 사고.

그래서 **추측하지 않고 먼저 판정한다.** spawner 가 `ReconcilableSpawner` 를 구현하면
결정적 `spawn_key`(= `event_uuid` 파생, 시계·시도횟수·호스트 무관)로 자기 백엔드에
"이 키의 세션이 있나" 를 되묻고, 확답이면 위 표대로 종결/반납한다.

**판정 불가일 때만 quarantine 한다.** 선택한 비용 = **이벤트 1건의 지연**, 얻는 것 =
**승인 레인으로의 중복 스폰 0**. 조용하지 않다 — `logger.error` + `TickResult.
needs_reconcile` + 응답 `needsReconcile[]` 로 **운영자 과제로 표면화**된다.
🔴 즉 `reconcile` 미구현 live spawner 는 ambiguous 마다 수동 화해가 필요하다.
이건 누락이 아니라 readback 구현을 강제하는 압력이다.

---

## 4. dedup · 상한 · 초과분

| 장치 | 값 | 막는 것 |
|---|---|---|
| 이벤트 dedup | `event_uuid` 당 1회 | 발화 하나 → 재판정 둘 |
| 종목 동시성 | 심볼당 in-flight 1 | 같은 포지션을 두 세션이 각각 사이징 (08-18 삼성 1·2단 동시발화 형태) |
| 회차 상한 | 기본 3 | 스캐너 오설정·시장 전체 갭 시 한 tick 폭발 반경 |

🔴 **초과분은 버리지 않는다.** `SelectionResult.overflow` 에 심볼·event_uuid·사유가
담기고 `logger.warning` 으로 표면화된다. 다음 tick 에서 다시 후보가 된다
(`test_capped_event_is_still_spawnable_on_a_later_tick`). 조용한 절단은 원래
사고와 같은 계열이므로 금지다.

---

## 5. 소유 분리 (§101차 ⑥)

| | 어디 |
|---|---|
| **스케줄 flow** | `robin-prefect-automations` — **이 레포 밖** |
| **poller/spawner 로직** | `app/services/watch_trigger_repricing/` — 이 레포 |
| **진입점** | `entrypoint.run_watch_repricing_tick()` — 스케줄러가 호출하는 함수 하나 |

🔴 이 레포에 **스케줄 등록이 없다.** Prefect `@flow` 객체도, cron 도, deployment 도
없다 (`test_this_repo_registers_no_schedule_or_deployment`). r2 까지 있던
`app/flows/watch_trigger_repricing_flow.py` 는 §101차 ⑥ 에 따라 **삭제**했다.

`prefect` 는 프로젝트 의존성이 아니므로 로직은 prefect 없이 import·테스트된다
(`test_prefect_is_not_imported_anywhere_in_the_package`).

### 5.1 시장별 게이트 (ROB-1304)

이 체인은 `market` 한 값으로 시장을 구분한다. KR 규칙을 US·crypto에 복사하지 않는다.

| 시장 | 발화 가능 시간 | 거래일/세션 근거 | 심볼·통화 주의 |
|---|---|---|---|
| `kr` | XKRX 정규장 09:00–15:30 KST | 기존 XKRX offline calendar | DB KR 심볼, KRW 가격 |
| `us` | XNYS regular session의 실제 open~close, close 배타 | `exchange_calendars` XNYS; early close 포함 | DB 기준 `BRK.B` 유지(`app/core/symbol.py` 전용 변환), USD 가격 |
| `crypto` | 24/7 | equity calendar을 적용하지 않음 | `KRW-BTC` 같은 거래쌍 유지, quote currency는 쌍에서 읽음 |

US가 KST 23:00에도 실행될 수 있는 것은 XNYS가 열려 있기 때문이다. 반대로 US
after-hours는 이 체인의 범위 밖이다. crypto는 주말·공휴일에도 유효하며
`ok_24x7`로 명시된다. 미지의 market은 fail-closed (`ValueError`)다.

`kstDate`는 기존 감사 호환 필드다. 새 `marketDate`는 KR은 KST, US는 ET,
crypto는 UTC 날짜를 기록하므로 이를 거래일 판단에 혼용하지 않는다.

### 5.2 짧은 알림과 증거 보존

operator/운영 레포는 알림을 한 줄로 렌더링할 수 있다. 그러나 auto_trader가
보존하는 원문 증거를 축약하거나 재계산해 덮어써서는 안 된다. poller는 이벤트의
`market`, `symbol`, `metric`, `operator`, `threshold`, `thresholdHigh`,
`thresholdKey`, `currentValue`, `firedAt`를 `triggerEvidence`로 snapshot한다.
원본은 `review.investment_watch_events`에 남고, proposal 생성 시 같은 snapshot이
`review.order_proposals.rationale.trigger_evidence`에 남는다. 즉 표시 축소와
기록 축소는 별개다.

이 PR은 auto_trader만 바꾼다. 운영 레포에는 다음 후속 변경이 필요하지만 여기서
커밋하거나 적용하지 않는다.

- market별 tick 호출(`market=kr|us|crypto`)과 배포 시 arm 절차
- 한 줄 renderer 및 원본 `triggerEvidence` 열람 링크/첨부
- `eventUuid`/`spawnKey`를 키로 하는 cross-path delivery dedupe와 관측 대시보드

### 5.3 §110차 배포 전환 계획: 기존 watch-alert triage 대체

이 절은 **머지 절차가 아니라 배포 절차**다. 이 PR이 머지되어도 기존
watch-alert triage poller/launchd는 계속 동작한다. 스케줄러·cron·TaskIQ·Prefect
등록과 env arm은 이 PR에 없다.

#### 배포 순서 — 무엇을 언제 끄는가

1. 운영 레포에서 새 market별 consumer와 dedupe 관측을 배포하되, 새 consumer는
   delivery를 기록만 하고 발송은 하지 않는 shadow 상태로 둔다. 기존 triage는 그대로
   실행한다.
2. KR·US·crypto 각각에서 source event, `triggerEvidence`, proposal/decline
   completion, dedupe key가 관측되는지 확인한다. 오류·quarantine·unmapped가 0인지
   확인한다.
3. 동일한 배포 변경에서 기존 watch-alert triage poller/launchd를 **중지**하고,
   새 consumer의 실제 발송을 **활성화**한다. 둘의 순서는 중지 → activation이며,
   activation 전 기존 프로세스의 단일 인스턴스 종료를 확인한다.
4. 전환 뒤 첫 시장 세션(KR/US)과 crypto 24시간 구간을 관측한다. auto_trader의
   `WATCH_TRIGGER_REPRICING_ENABLED`는 운영 승인 전까지 default-off를 유지한다.

#### 롤백 조건과 방법

다음 중 하나면 즉시 새 consumer 발송을 끄고 기존 triage를 재가동한다: 중복
`eventUuid` 발송, 발화 대비 completion 누락/unmapped, `spawn_ambiguous` 또는
quarantine 미화해결, US 세션 밖 실행, crypto 주말 fire 누락, `triggerEvidence`의
조건·값·시각 누락/불일치, 혹은 새 consumer의 전송 실패가 기존 경로보다 지속된다.

롤백은 주문/승인 정책 변경이 아니다. 새 consumer의 send arm을 끄고, 기존 poller를
단일 인스턴스로 복구하고, watermark를 되돌리지 않은 채 `eventUuid` dedupe 상태를
보존한다. 누락/중복 후보는 source event에서 재조사한다.

#### 동시 구간과 중복 방지

shadow 단계에서는 두 경로가 동시에 실행될 수 있지만 **기존 triage만 발송**한다.
새 경로는 `eventUuid`와 결정적 `spawnKey`를 저장해 would-send만 기록한다. 실제
발송 단계에는 동시 구간을 만들지 않는다. 불가피한 겹침(프로세스 종료 지연 등)은
공유 durable dedupe key `eventUuid`를 send 전에 원자적으로 claim하고, 성공 후에만
delivery를 기록해 막는다. 메모리 watermark·표시 텍스트·심볼만을 키로 dedupe하면
재시작·문구 축소·동일 심볼 다중 threshold에서 중복을 막지 못하므로 사용하지 않는다.

---

## 6. 진입점은 스스로 폴링한다 (§101차 ②)

`run_watch_repricing_tick()` 은 `events` 인자를 **받지 않는다.** 기본으로
`DatabaseWatchEventSource` 를 앱의 실 세션 팩토리 위에 만들어
`review.investment_watch_events` 를 직접 읽는다.

r2 지적: 이전 진입점은 `events` 를 필수로 받아 아무것도 폴링하지 않았고, "E2E"
테스트가 fake source 를 손으로 조립해 tick 에 넘겼다. 즉 실제 배포가 틀릴 수 있는
유일한 지점(어떤 행을 보는가)이 검증 밖이었다.
지금은 `test_entrypoint_polls_the_database_itself` 가 **source 주입 없이** 호출해
run-owned DB 에 심은 행을 찾아내는지 본다.

---

## 7. 완료 기준 — 분석-온리는 실패다

운영자 지시(§101차): 「분석이 큰 의미가 없는 것 같아, operator 세션에서 실제
주문까지 필요하면 하게」

🔴 **발화 이벤트 하나하나가** 다음 중 하나로 끝나야 한다:

| terminal | 의미 | 증거 |
|---|---|---|
| `proposal_created` | `order_proposal_create` 로 실제 proposal 행 | `proposal_id` (NOT NULL + 공백거부, DB CHECK) |
| `rejected_with_reason` | 제안하지 않은 **이벤트 귀속** 사유 | `rejection_reason` (동상) |
| `expired_unprocessed` | TTL 만료 — **아무도 판정 못 함** | 🔴 완료로 세지 않는다 |

🔴 **「분석-온리」 terminal 은 enum 에도 DB CHECK 에도 없다.** `analysed` 를 넣으면
`ck_watch_event_repricing_claims_state` 가 거부한다(실측). 총평 한 문단은 사유가
아니다 — 사유는 이벤트에 귀속된 컬럼 값이다.

### 1:1 매핑 표

`run_watch_repricing_tick()` 응답의 `completion` 이 그 표다. 폴링이 본 이벤트 집합
N 은 `polled` 로 먼저 확정되어 나오고, 매핑은 N 의 **모든** 원소를 담는다.

- `completionComplete` — 전부 proposal 또는 사유. **이것이 완료 기준이다.**
- `completionAccounted` — 최소한 아무것도 말없이 사라지지 않았다.
- `completionDeferred` — 이번 tick 이 **알고서 미룬** 것(종목 동시성·회차 상한).

🔴 **deferred 는 완료가 아니다.** 같은 종목 2개 발화는 한 tick 이 하나만 판정할 수
있는데(그게 종목 동시성 규칙의 존재 이유다), 그걸 완료로 쳐주면 "미룸"이 영구
면죄부가 된다. 그래서 `is_complete` 는 deferred 를 인정하지 않고,
`test_the_loop_converges_every_fire_resolves_across_ticks` 가 **여러 tick 에 걸쳐
실제로 전부 해소되는지**로 증명한다.

---

## 8. 권한 배선 (§101차 급소 B)

운영자가 세션에 제안 생성 권한을 주기로 했다. 그래서 목표가 바뀌었다:
**(구)** proposal-only 강제 → **(신)** 승인 기계를 우회할 수 없음 강제.

| # | 주장 | 코드 |
|---|---|---|
| ① | 실 MCP 프로필 | `McpProfile.WATCH_REPRICING` (`app/mcp_server/profiles.py`), registry 분기가 broad 블록 **전에 return** |
| ② | `order_proposal_create` 실제 보유 | `order_proposal_tools.py:486`(함수)·`:1008`(등록). 프로필이 provision 하는 registry 와 allowlist 가 **`==`** |
| ③ | 기존 승인 기계 연결 | `order_proposal_create` → `dispatch_proposal` → `evaluate_auto_approve_eligibility` (토스 자동승인 포함) |

🔴 **exact 등가**: `assert_exact_grant` 는 `==` 다. superset 은 r2 가 뚫은 탈출구,
subset 은 제안 못 하는 세션(=분석-온리 산출)이라 **둘 다 거부**한다.

🔴 **완화 0**: 이 패키지는 `ORDER_PROPOSALS_AUTO_APPROVE*`·cap·`loss_cut` 을
**문자열로도 언급하지 않는다**(`test_this_package_relaxes_none_of_the_approval_gates`,
`test_loss_cut_stays_human_approved`). loss_cut 은 여전히 사람 승인이다.

🔴 **정직한 공백**: 이 레포의 유일한 세션 런처 seam
(`scripts/mock_session_mcp.py`)의 `SAFE_MOCK_PROFILES` 에 `watch_repricing` 이
**없다.** 즉 이 레포만으로는 live 세션을 띄울 수 없고, 띄우려면 그 allowlist 를
고치는 별도 리뷰가 필요하다. 테스트로 박제했다
(`test_no_launcher_in_this_repo_can_start_the_profile`).

### 8.1 스텁이 아니라 실 서버로 증명 (ROB-1290 r3 후속)

r3 실측 결론: **in-process 로는 승인 경계를 닫을 수 없다.** subclass·injected
judge·module-global 교체 앞에서 capability 토큰·`@final`·in-process allowlist 가
전부 무력했고 raw 콜러블이 남았다. 그래서 경계를 **프로세스 경계 = MCP 서버가
내주는 도구 집합**에서 증명한다.

| 증거 | 어떻게 |
|---|---|
| provision | `build_watch_repricing_server()` — `register_all_tools(profile=WATCH_REPRICING)` 로 실 `FastMCP` 를 세운다 (`on_duplicate="error"`, 프로덕션 `main.py` 와 동일) |
| 닫힌 등가 | `assert_provisioned_surface()` — 서버가 **실제로 서빙하는** `tools/list`(15개)를 `PROPOSAL_ONLY_TOOLS` 와 `==` 비교. 등록기의 장부가 아니라 세션이 볼 수 있는 유일한 표면이 비교 대상이다 |
| 서버 거부 | `tools/call place_order` 응답이 `isError=True  "Unknown tool: 'place_order'"`. 클라이언트 필터가 아니다 — 클라이언트는 **받은 적 없는 이름**을 보내고 서버가 거절한다. 레포의 주문 registry 이름 전체를 훑는다 |
| 공허하지 않음 | 같은 경로·허용 이름(`order_proposal_create`)은 `"Unknown tool"` 이 아니라 **인자 검증** 오류로 실패한다. "전부 실패하는 서버" 가 아니라는 대조군 |

테스트 = `tests/services/watch_trigger_repricing/test_real_server_surface.py`.
스텁 기반 `test_live_spawner_contract.py` 는 그대로 둔다 — 스텁에는 `tools/list`
도 호출 경로도 없어서 위 두 질문에 애초에 답할 수 없다.

🔴 **이 절이 증명하지 않는 것**: 스폰된 세션이 *이렇게 세운 서버에* 붙는다는 것.
런처 배선은 여전히 없다(위 정직한 공백 · §11). 여기까지의 주장은 "프로필로 서버를
세우면 그 서버는 주문 도구를 내주지 않는다" 이며, provision 은 arm 이 아니다.

---

## 9. live spawner 는 계약 없이 배선 불가 (§101차 ③)

r2 는 문서 규칙이었고 뚫렸다:

```
B4_IGNORED_PROFILE  status=ok spawned=1 request_has_toss=False actual_has_toss=True
SELF_ATTESTED_DRY   status=ok spawned=1 calls=1
```

r3 는 타입·생성자 강제다:

- `LiveSessionSpawner.__init__` 가 `declared_grant()` 를 **생성 시점에** 검사한다.
  틀린 grant 를 가진 live spawner 는 "나중에 실패"가 아니라 **객체가 만들어지지
  않는다.**
- `reconcile` / `attest_granted_tools` 는 `@abstractmethod` — 없으면 인스턴스화
  자체가 `TypeError`.
- `assert_arming_contract` 는 base class 상속까지 확인한다. 메서드만 duck-type 으로
  갖춘 객체는 거부된다(생성자 검사를 건너뛰었으므로).
- 🔴 **dryness 는 타입이다.** `is_dry` 자기신고를 더 이상 읽지 않는다
  (`DRY_SPAWNER_TYPES` 닫힌 집합).

---

## 10. 동시성 — 이제 DB 제약이다

| r2 결함 | r3 폐쇄 | 실측 |
|---|---|---|
| stale claimant 가 새 claim 을 종결 | `(event_uuid, generation, owner_token)` fencing | 만료 후 gen1 UPDATE → `UPDATE 0` |
| 같은 symbol 다른 event 둘 다 스폰 | `UNIQUE (symbol) WHERE state='started'` | 2번째 INSERT → unique violation |
| 프로세스 싱글톤 dedup | 행 + 제약 | 별 세션 두 개로 검증 |

TTL 롤오버는 `expired_unprocessed` 를 **먼저 기록**해야 symbol 슬롯이 풀린다 —
조용한 재사용이 아니라 감사 행이 남는다.

---

## 11. 🔴 여전히 arm 하면 안 되는 이유

1. **live spawner 부재** — 레포에 구현이 없다. 계약만 있다.
2. **런처 allowlist 미포함** — §8 의 정직한 공백.
3. **스케줄 미등록** — §101차 ⑥ 대로 상류 소관.
4. **마이그레이션 미적용** — run-owned test DB 왕복만 했다. 프로덕션/공유 DB 적용 0.
5. `WATCH_TRIGGER_REPRICING_ENABLED` 기본 false.

---

## 12. 안전 경계 요약

- 브로커 mutation 0 · 주문 제출 0 · 승인 0 · 워치 alert mutation 0
- `review.investment_watch_events` 는 **읽기 전용** — 소비는 claim 테이블에 산다
  (`test_the_package_never_writes_to_investment_watch_events`)
- 쓰기는 `db_claim_store.py` 한 파일, 자기 테이블만
  (`test_only_the_claim_store_writes_and_only_its_own_table`)
- KR 만. US/crypto 는 별건
- migration 1건, additive, 자기 테이블 create 만
