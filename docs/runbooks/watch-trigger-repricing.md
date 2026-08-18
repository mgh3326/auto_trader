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
| 5 | 같은 세션 Toss 체결 freeze 없음 | `dispatch.py::active_toss_auto_submission_freeze` |
| 6 | auto-veto 카드용 thesis 존재 | `auto_approve.py::auto_veto_thesis_summary` |
| 7 | `policy_deviation` / `table_disagreement` 태그 없음 | `auto_approve.py::_APPROVAL_REQUIRED_TAGS` |
| 8 | rung 이 non-marketable (매도는 시장가보다 엄격히 위) | `auto_approve.py` 모듈 docstring |
| 9 | 매도는 왕복비용 차감 후 실현손익 > 0, ±`breakeven_band_pct` 밴드 밖 | `auto_approve.py` |
| 10 | `loss_cut` 등 exit_intent 아님 | `auto_approve.py` |
| 11 | `per_order_cap` / 당일 누적 `daily_cap` 미초과 | `dispatch.py::auto_approved_daily_notional` |

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

## 5. 🔴 arm 전 차단 항목

### 5.0 r2 에서 닫은 것 / migration 없이는 못 닫는 것 — 구분해서 적는다

| 항목 | migration 없이 닫았나 | 근거 |
|---|---|---|
| 같은 프로세스 내 flow run 간 dedup | ✅ 닫음 | `run_gated_tick` 이 프로세스 싱글턴 store 사용 |
| 성공 claim 의 만료 재취득 금지 | ✅ 닫음 | 종결 상태(`CONSUMED`/`QUARANTINED`)는 lease 무관 |
| claim 원자성 (스레드 경합) | ✅ 닫음 | `try_claim` 이 check+write 를 lock 으로 감쌈 |
| spawn 양방향 누수 | ✅ 닫음 | 3분기 + reconcile readback + quarantine |
| 실행 경계 강제 | ✅ 닫음 | capability allowlist, 요청 생성 시점 검증 |
| **프로세스 간(= 실제 Prefect) 내구 dedup** | 🔴 **못 닫음** | 아래 1번 — **migration 필수** |
| **B안 read surface 소비 projection** | 🔴 **못 닫음** | 아래 2번 — migration + MCP 변경 필수 |

🔴 **못 닫은 두 항목이 arm 을 막는다.** 그리고 그것을 주석이 아니라 **코드**가 막는다:
`run_gated_tick` 은 `is_dry=False` spawner 를 `is_durable=False` store 와 함께
받으면 `status="blocked", reason="non_durable_claim_store"` 로 거부한다. 두 속성 모두
**fail-closed** 로 읽는다 — 답하지 않는 spawner 는 live 로, 답하지 않는 store 는
휘발성으로 간주한다. 즉 레포에 실린 in-memory store 로는 **누가 live spawner 를
배선하더라도** 기동할 수 없다.

1. **내구 claim store 부재 — migration 필요, 승인 대기.**
   레포의 유일한 구현은 프로세스 로컬(`InMemoryClaimStore`)이다. 프로덕션에서는
   tick 이 별개 Prefect flow run(별 프로세스)이라 **프로세스 간 dedup 이 성립하지
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
       -- r2: 종결 상태는 lease 와 별개 컬럼이어야 한다. 단일 expires_at 으로
       -- 성공까지 재취득하게 두면 BLOCKER-1 이 그대로 남는다.
       terminal_state  TEXT
           CHECK (terminal_state IN ('consumed', 'quarantined')),
       terminal_reason TEXT,
       terminal_at     TIMESTAMPTZ,
       -- 결정적 spawn 정체성. ambiguous 결과의 readback 화해 키.
       spawn_key     TEXT        NOT NULL,
       CONSTRAINT uq_watch_event_repricing_claims_event
           UNIQUE (event_uuid)
   );
   ```
   UNIQUE 충돌 자체가 상호배제여야 한다 — read-then-write 로 구현하면 이 테이블이
   막으려던 경쟁을 그대로 되살린다. lease 만료 재취득은 **`terminal_state IS NULL`
   AND 만료** 조건부 UPDATE 로. 🔴 `terminal_state` 조건을 빼면 r1 버그가 DB 로
   이사할 뿐이다.

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
   붙일 때 요구사항: ① `is_dry=False` 선언 ② `ReconcilableSpawner.reconcile`
   구현(`spawn_key` 로 세션 레지스트리 readback) ③ 세션에 부여하는 MCP 프로필은
   `capability.PROPOSAL_ONLY_TOOLS` — **`tradingcodex_execution` 재사용 금지**
   (§7.1 참고).

4. **Prefect 배포·스케줄 미등록.** 상류 착지 검수 담당.

5. 🔴 **flow 소유 repo 미확정 (r2 / SHOULD-3) — 운영자 결정 대기.**
   Linear 설계는 flow 위치를 `robin-prefect-automations` 로 적었는데 이 PR 은
   `app/flows/watch_trigger_repricing_flow.py` 를 auto_trader 에 넣었다. 또한
   `prefect` 는 이 프로젝트 의존성이 아니라 **이 venv 에서 import 되지 않는다**
   (기존 `app/flows/*_flow.py` 관례와는 일치). 선택지:

   - **(A) auto_trader 유지** — 로직·테스트와 같은 repo, 리뷰 1회. 대신 import 불가
     scaffold 가 남고 Linear 문언과 어긋난다.
   - **(B) `robin-prefect-automations` 이관** — Linear 문언과 일치, 실제 배포 위치와
     동일. 대신 2-repo 변경이 되고 `run_gated_tick` 을 넘는 얇은 shell 만 옮긴다.

   🔴 **이 라운드에서 임의 결정하지 않았고, 다른 repo 를 건드리지 않았다.** 결정이
   내려오면 파일 1개 이동이면 된다(로직은 전부 `orchestrator.run_gated_tick`).

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

### 7.1 🔴 실행 경계 = capability, 문자열 선언이 아니다 (r2 / BLOCKER-4)

r1 은 `SpawnRequest.execution_boundary` 라는 **문자열 필드**에
`"order_proposal_create"` 를 담았다. 아무도 그걸 읽지 않았고, live spawner 가 가장
자연스럽게 재사용했을 MCP 프로필(`tradingcodex_execution`)에는 브로커 직접 제출·취소
도구가 등록돼 있다. 즉 요청은 proposal-only 라고 **말하면서** 세션에는 직접 주문
표면을 쥐여 줬을 것이다. 선언은 사슬을 끊지 못한다.

r2 의 `capability.py` 는 이것을 **allowlist** 로 바꿨다:

- `PROPOSAL_ONLY_TOOLS` 에 없는 도구는 전부 거부 — **아직 존재하지 않는 도구 포함.**
  deny-list 였다면 다음 분기에 추가된 브로커 도구가 기본 허용됐을 것이다.
- 강제 지점은 **`SpawnRequest` 생성자**다. 경계를 넘는 프로필을 담은 요청은
  spawner 가 보기 전에 `CapabilityBoundaryViolation` 으로 죽는다.
- `order_proposals` 도 통째로 안전하지 않다. `order_proposal_redispatch`(거절된
  제안을 승인 레인에 재투입) · `order_proposal_void` · `support_reserve_net_consume`
  는 **제외**했다.
- 검증은 **실제 레지스트리 상수와 대조**한다(`test_capability_profile.py`):
  `ORDER_TOOL_NAMES` · `KIS_LIVE/MOCK_ORDER_TOOL_NAMES` ·
  `TOSS_LIVE_ORDER_TOOL_NAMES` · `KIWOOM_MOCK_EXECUTION_TOOL_NAMES` 와 교집합 0,
  그리고 그 각 도구를 프로필에 밀어넣는 뮤턴트가 전부 RED.

### 7.2 요약

- 브로커 mutation 0 · 주문 제출 0 · 승인 0 · 워치 alert mutation 0
- 실행 경계 `order_proposal_create` 까지. 그 너머 토큰(`place_order`,
  `record_auto_approval`, `revalidate_and_submit` …)은 정적 가드로 금지이고,
  **capability allowlist 로 구조적으로도 부재**
- DB 쓰기 0 — 패키지 어느 파일도 session/repository 에 `add`/`commit`/`flush`/
  `delete`/`execute` 를 호출하지 않는다(`test_no_package_file_writes_through_a_
  session_or_repository`). 읽기 seam(`event_source.py`)은 repository 의
  `list_events_by_delivery_status` **한 메서드만** 호출 가능
- KR 만. US/crypto 는 별건 (`test_market_scope_stays_kr`)
- 신규 recurring job 은 이 flow 하나 (`test_this_change_adds_exactly_one_flow`)
- `WATCH_TRIGGER_REPRICING_ENABLED` 기본 false
- **내구성 게이트**: live spawner + 휘발성 store 조합은 코드가 거부 (§5.0)
- migration 0 (`test_no_migration_defines_a_consumption_marker`)
