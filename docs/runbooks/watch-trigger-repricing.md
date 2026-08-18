# 워치 발화 트리거 구동 재판정 (ROB-1286 / §93차 A안)

워치 발화(`review.investment_watch_events`)를 장중에 폴링해, 미소비
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

자동승인까지 도달하려면 아래가 **전부** 참이어야 한다 (전부 코드 확인):

| # | 조건 | 위치 |
|---|---|---|
| 1 | `ORDER_PROPOSALS_AUTO_APPROVE=true` (기본 false) | `app/services/order_proposals/dispatch.py:841` |
| 2 | `ORDER_PROPOSALS_AUTO_APPROVE_MODE="expanded"` (기본 `off`) | `app/core/config.py:853` |
| 3 | toss_live 는 추가로 `ORDER_PROPOSALS_TOSS_LIVE_VETO_ENABLED=true` (기본 false) | `auto_approve.py:_is_veto_capable_account_market` |
| 4 | `(account_mode, market)` ∈ `_VETO_CAPABLE_ACCOUNT_MARKETS` | `auto_approve.py:_VETO_CAPABLE_ACCOUNT_MARKETS` |
| 5 | 같은 세션 Toss 체결 freeze 없음 | `dispatch.py::active_toss_auto_submission_freeze` |
| 6 | auto-veto 카드용 thesis 존재 | `auto_approve.py::auto_veto_thesis_summary` |
| 7 | `policy_deviation` / `table_disagreement` 태그 없음 | `auto_approve.py::_APPROVAL_REQUIRED_TAGS` |
| 8 | rung 이 non-marketable (매도는 시장가보다 엄격히 위) | `auto_approve.py` 모듈 docstring |
| 9 | 매도는 왕복비용 차감 후 실현손익 > 0, ±`breakeven_band_pct` 밴드 밖 | `auto_approve.py` |
| 10 | `loss_cut` 등 exit_intent 아님 | `auto_approve.py` |
| 11 | `per_order_cap` / 당일 누적 `daily_cap` 미초과 | `dispatch.py::auto_approved_daily_notional` |

**이 PR 은 위 11개 중 무엇도 건드리지 않는다.** 정적 가드가 강제한다 —
`tests/services/watch_trigger_repricing/test_invariants.py::test_approval_gate_settings_are_never_referenced`
는 문자열 리터럴까지 스캔하므로 `getattr` 우회도 잡힌다.

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

**3값이다.** `UNKNOWN` 은 `UNCLAIMED` 이 아니다. claim store 가 답을 못 하면
다른 소비자가 이미 작업 중인지 알 수 없고, 그걸 "미소비" 로 추측하는 것이
발화 하나를 매도 제안 둘로 만드는 경로다. `may_consume()` 은 **증명된
`UNCLAIMED` 에만** 통과한다.

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
닫는다** — 만료된 claim 은 재취득 가능이라 발화가 다시 떠오른다. 정상적인 spawn
실패는 `release(reason=...)` 로 즉시 반납한다. 즉 **지연은 유계, 소실은 없다.**

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

## 5. 🔴 arm 전 차단 항목 (전부 미해소)

1. **내구 claim store 부재 — migration 필요, 승인 대기.**
   레포의 유일한 구현은 프로세스 로컬(`InMemoryClaimStore`)이다. 프로덕션에서는
   tick 이 별개 Prefect flow run(별 프로세스)이라 **tick 간 dedup 이 성립하지
   않는다.** 제안 DDL (이 PR 에 **미포함**):

   ```sql
   CREATE TABLE review.watch_event_repricing_claims (
       id            BIGSERIAL PRIMARY KEY,
       event_uuid    UUID        NOT NULL,
       symbol        TEXT        NOT NULL,
       claimed_by    TEXT        NOT NULL,
       claimed_at    TIMESTAMPTZ NOT NULL DEFAULT now(),
       expires_at    TIMESTAMPTZ NOT NULL,
       released_at   TIMESTAMPTZ,
       release_reason TEXT,
       CONSTRAINT uq_watch_event_repricing_claims_event
           UNIQUE (event_uuid)
   );
   ```
   UNIQUE 충돌 자체가 상호배제여야 한다 — read-then-write 로 구현하면 이 테이블이
   막으려던 경쟁을 그대로 되살린다. lease 만료 재취득은 만료 조건부 UPDATE 로.

2. **B안 쪽 read surface 미배선.**
   `investment_watch_events_list_recent` 는 현재 **소비 필터가 없다**
   (`delivery_status='delivered'` + `delivered_at>=since` 뿐). 즉 claim 이 생겨도
   B안 세션은 그것을 볼 수 없다.
   🔴 **그래서 이 상태로 A안을 arm 하면 A 가 소비한 발화를 B 가 다시 판정할 수
   있다** — 같은 종목 매도 제안 2건. 1번과 함께 additive 필드
   (`repricing_claim: claimed|unclaimed|unknown`) 로 노출해야 한다.
   ⚠️ 필드를 붙이되 항상 `null` 인 상태로 두는 것은 **더 나쁘다** — 읽는 쪽이
   "미소비" 로 단정한다. 그래서 이 PR 은 일부러 배선하지 않았다.
   🔴 operator repo 프롬프트는 **고치지 않는다** (소유권 밖). additive 필드면
   현행 문언("미소비 `review_required` 가 있으면")이 그대로 작동한다.

3. **live spawner 부재.** 레포에 구현이 없다. `DrySessionSpawner` 만 있다.

4. **Prefect 배포·스케줄 미등록.** 상류 착지 검수 담당.

---

## 6. 휴장일 게이트 (AC3)

`app/services/market_events/session_calendar.trading_session_status` (오프라인
XKRX) 재사용. 새 휴일 판정을 만들지 않았다.

🔴 **ROB-1280 캘린더 endpoint 를 쓰지 않았다.** 그 표면의 `is_open` 은
`toss_api_disabled` / `toss_calendar_unavailable` / `date_out_of_calendar_window`
세 가지 **일상적인** 사유로 `null` 이 된다 — 플래그가 꺼져 있거나 벤더가 흔들리면
평범한 거래일이 "불확정" 으로 보인다. XKRX 는 정적·오프라인이라 `unknown` 이
드물고, 나오면 그건 설정 상태가 아니라 실제 고장이다.

**불확정 시 동작 = 미가동** (`closed` 와 동일하게 스킵, 단 사유는 구분).

- 선택 비용: 분류 못 한 날에는 이 flow 가 지연 단축을 못 한다. **발화는 잃지
  않는다** — 미소비로 남아 B안이 세션 말미에 집는다. 지연이지 소실이 아니다.
- 반대 선택 비용: 거래일인지 확인 못 한 날에 제안을 만든다. 제안은 무해하지
  않다 — §1 의 자동승인 레인으로 들어갈 수 있다.

프로그램의 rep 스폰 레인은 같은 상황에서 fail-**open** 인데, 그쪽 저울은
"세션 낭비 vs 거래일 누락" 이다. **이 flow 는 주문을 만든다.** 그 저울을
물려받지 않는다.

장중 창은 09:00–15:30 KST (마감 배타), KST 로 평가한다.

---

## 7. 안전 경계 요약

- 브로커 mutation 0 · 주문 제출 0 · 승인 0 · 워치 alert mutation 0
- 실행 경계 `order_proposal_create` 까지. 그 너머 토큰(`place_order`,
  `record_auto_approval`, `revalidate_and_submit` …)은 정적 가드로 금지
- KR 만. US/crypto 는 별건 (`test_market_scope_stays_kr`)
- 신규 recurring job 은 이 flow 하나 (`test_this_change_adds_exactly_one_flow`)
- `WATCH_TRIGGER_REPRICING_ENABLED` 기본 false
- migration 0 (`test_no_migration_defines_a_consumption_marker`)
