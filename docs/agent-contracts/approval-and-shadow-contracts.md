# 승인·shadow 계약

이 파일은 작업 분야와 무관하게 작업 시작 전에 끝까지 읽어야 하는 필독 계약의 일부다. 찾아보기는 탐색 보조일 뿐 선택 읽기 면제가 아니다.

## 목차

- Telegram 승인 콜백 durable inbox (W5)
- 매수 게이트 A/B shadow (ROB-1301)

## 기준 원문 계약

### Telegram 승인 콜백 durable inbox (W5)

`POST /trading/api/telegram/callback` 은 지금까지 승인 워크플로 전체(재검증 →
브로커 제출 → Telegram 메시지 수정)를 **요청 스레드 안에서 인라인 실행**했다.
Sentry 프로덕션 7일 실측: n=44, avg 3.365s / p50 2.738s / p95 12.707s / max
13.593s, child 집계는 `http.client`(359 spans, 86.90s)가 DB(3,106 spans, 4.47s)를
압도. 더 큰 문제는 지연이 아니라 **내구성**이다 — taskiq-redis `ListQueueBroker`
는 LPUSH/BRPOP 이라 워커가 죽기 전에 메시지가 Redis 에서 이미 사라진다.

**PostgreSQL 이 권한자, TaskIQ 는 opaque job UUID 를 나르는 best-effort 깨우기.**
Redis 를 잃으면 지연을 잃지 클릭을 잃지 않는다.

- **테이블**: `review.telegram_callback_inbox` and
  `review.telegram_callback_recovery_cursor` (`app/models/telegram_callback_inbox.py`)
- **마이그레이션**: `alembic/versions/20260821_w5_telegram_callback_inbox.py` (additive)
- **패키지**: `app/services/order_proposals/callback_inbox/` — `contracts`(닫힌 어휘·digest·lock key) ·
  `repository`(service 전용) · `service`(유일한 writer) · `locks`(job advisory lock) ·
  `ingress` · `worker` · `recovery` · `observability`(텔레메트리 allowlist) ·
  `result_boundary.py` · `taskiq_receiver_boundary.py`
- **TaskIQ**: `app/tasks/telegram_callback_inbox_tasks.py` — `order_proposals.telegram_callback_job`
  (per-job) + `order_proposals.telegram_callback_recovery` (**scheduleless 출고**)
- **런북**: `docs/runbooks/telegram-callback-durable-inbox.md`

Accepted canonical inputs retain the post-normalization execution core and the
pre-existing downstream authorization gates. Normalization now also adds the
R37 exact numeric identifier trust boundary before durable authority or core
execution. Published-binding preflight, single-use nonce, commit lease,
target-mutation lock, approval hash, and fresh preview remain downstream gates.

**게이트 3개 전부 default false**:
- `ORDER_PROPOSALS_TELEGRAM_CALLBACK_DURABLE_ENABLED` — durable ingress
- `ORDER_PROPOSALS_TELEGRAM_CALLBACK_WORKER_ENABLED` — per-job 워커
- `ORDER_PROPOSALS_TELEGRAM_CALLBACK_RECOVERY_SCHEDULE_ENABLED` — 복구 스윕 cron **및** 실행

🔴 durable ingress 는 worker/recovery 게이트가 **둘 다** 켜져 있지 않으면 sanitized
503 으로 트래픽을 거부한다(설정 레벨 가드 — **프로세스 생존 확인은 런북 §4 절차**).
활성화 순서: 마이그레이션 → 코드 배포 → worker 게이트 → recovery 게이트 + **스케줄러
재시작**(schedule 라벨은 import 시점 고정) → ingress 게이트.

**재시도 대수 (🔴 이걸 바꾸기 전에 런북 §5 를 읽을 것)**:
콜백 코어는 모든 예외를 `{"handled": False, "reason": "internal_error"}` 로 삼킨다.
그 문자열은 **브로커 leg 가 시작되지 않았다는 증거가 아니다** —
`revalidate_and_submit` 가 브로커에 닿은 뒤 commit 전에 던져진 예외도 같은 결과이고,
롤백은 nonce 를 **미소비** 상태로, published binding 을 **유효** 상태로 남긴다.
재시도하면 합법으로 보이면서 두 번 제출된다.

따라서 재실행 가능한 유일한 부류는 **코어에 진입하지 않았음이 증명된 실패**뿐:
- `retry_wait` ← **worker-owned pre-core phase 실패만**. 현재 명시적으로
  `PreCoreFailure` 를 만드는 경로는 코어 진입 전 notifier 해석 실패뿐이고,
  `schedule_retry` 가 조건부 UPDATE 로 DB 에서 `state='processing'` +
  `handler_entered_at`/`handler_completed_at`/`terminal_state_pending` 전부 NULL
  임을 재확인해야 기록된다. 🔴 **핸들러가 반환하는
  `mutation_not_started`/`retry`/`retryable`/`safe_to_retry` 는 진단용이며 재실행
  권한을 전혀 만들지 못한다**(`IGNORED_HANDLER_RETRY_KEYS`) — 이미 mutate 한
  핸들러도 똑같이 반환할 수 있기 때문이다. 코어 진입 마커 이후의 모든
  예외·결과·reason 은 terminal(succeeded/discarded/dead_letter)일 뿐 재시도 없음.
  저장 envelope 복원이 불가능하면 `discarded/envelope_invalid`, 현재 chat
  allowlist 에서 빠졌으면 `discarded/chat_revoked` 이며 둘 다 재시도하지 않는다
- `succeeded` ← `handled=True` (`results: ["unverified"]` 포함 — 모호한 *전송*은
  proposal/order 상태머신 소관이고, 콜백 재실행은 해결이 아니라 중복 위험)
- `discarded` ← 명시적 비즈니스 거부(nonce_replay/expired/guard_blocked/…), chat 취소,
  복원 불가 envelope
- `dead_letter` ← 코어 진입 후 `internal_error`·크래시·contract 위반, 또는 3회 소진.
  **자동 replay 없음. 권한 필드 스크럽됨. 운영자가 새 승인 카드를 발급해야 한다.**

내구 마커 3개가 프로세스가 죽은 뒤에도 이 판정을 가능하게 한다:
`handler_entered_at`(코어 호출 직전 단독 커밋 — "진입 전 사망"과 "진입 후 사망"의
유일한 내구 차이) / `handler_completed_at` + `terminal_state_pending`(코어 반환 직후
단독 커밋 — 마지막 커밋을 잃으면 recovery 가 **재실행이 아니라 스크럽 보수**를 한다).
🔴 세 마커는 **인과 순서**이고(완료⇒진입, 판정⇒진입+완료) DB CHECK
(`ck_..._handler_marker_order`)가 강제하며, **단조(monotonic)** 다 — 어떤 API 도
NULL 로 되돌리지 못한다. 재시도가 합법인 이유는 마커를 지웠기 때문이 아니라 CAS
술어가 애초에 NULL 이었음을 증명했기 때문이다. 또한 `processing` 행은
`started_at` 이 NOT NULL 이어야 한다(`ck_..._processing_started_at`) — NULL 이면
staleness 비교가 영원히 거짓이라 복구 스캔에서 보이지 않는다.

**데이터 최소화 = 스키마 속성**: raw Telegram `Update` 저장 안 함(JSON/JSONB/ARRAY
컬럼 자체가 없음). terminal 도달 즉시 권한/PII 11개 컬럼 NULL — DB CHECK 2개가 강제
(`terminal_scrubbed` / `active_reconstructable`, 둘 다 `CASE WHEN … THEN … ELSE true END`
+ `IS NULL`/`IS NOT NULL` 이라 SQL `UNKNOWN` 이 통과 못 함). 살아남는 건 `update_digest`
(도메인 분리 일방향 dedupe tombstone) · closed-category `outcome`(unknown raw reason은
고정 `unclassified`) · 닫힌 `error_class` 뿐.
W5 애플리케이션 payload에는 canonical job UUID만 들어간다. producer wire
shape는 테스트로 기계 검증되며, 결과·로그·Sentry에는 endpoint별 닫힌
안전 projection만 남는다.

**R37 identifier boundary**: `callback_query.from` 자체가 exact built-in
`dict`여야 하며, `from.id`는 필수 exact built-in `int`이고
`1..2**52-1`만 허용한다. `update_id`는 `None` 또는 exact built-in `int`이며
`1..2_147_483_647`만 허용한다. callback id가 primary여도 present update id는
항상 검증한다. bool·subclass·string·coercible 값은 durable authority 전에
거부한다. DB user id는 canonical decimal `Text`이고 worker는 exact `int`로만
복원하며 active row는 이를 필수로 한다. `callback_query_id`가 있으면
delivery identity primary이고 valid `update_id`는 callback id가 없을 때만
fallback이다. `update_identity_digest`는 별도 active 검증 필드라 같은
callback id의 변경된 update id는 second job이 아니라 conflict이다. terminal
scrub은 `update_identity_digest`를 제거하고 `update_digest` one-way dedupe
tombstone은 남긴다.

**TaskIQ receiver/result boundary**: job wire는 canonical lowercase hyphenated
UUID string 하나의 positional arg와 빈 kwargs, recovery wire는 완전한 empty
envelope다. exact 두 W5 task name은 formatter-load 단계에서 첫 decoded-message
debug/Sentry surface 전에 sanitize되고 마지막 middleware가 다시 sanitize한다.
incoming labels/label-type metadata는 버려지며 SmartRetry는 이 callback들의
retry authority가 아니다. malformed job은 fixed `invalid_job_id`, malformed
recovery는 fixed `error`이고 result/status/worker/recovery projection은 닫힌
어휘다. Untrusted inbound args/kwargs/labels are collapsed on formatter load
before the first decoded-message log/Sentry surface; only canonical producer
envelopes are intentionally emitted. Worker/recovery extras and exception
strings never enter the W5 result backend or W5 logs/Sentry. task body는 exact CancelledError/KeyboardInterrupt/SystemExit만
private category-only signal로 축약해 SmartRetry에 exception을 주지 않는다. final W5
post_execute는 Receiver의 task-exception catch 이후, result save 이전에 fresh safe
exact control을 raise하며 result backend save 및 ack-capable broker의 WHEN_SAVED
ACK 단계에 도달하지 않고 Receiver error log도 없다. `CancelledError`는 해당
callback만 끝내며, TaskIQ result save/ACK와 독립적인 durable
판정은 3개 DB marker와 recovery가 맡는다. shared `auto-trader` worker process에는
신호를 보내지 않는다. 나머지 failure는 fixed safe result로 collapse된다.

Recovery UUID materialization은 exact stdlib `uuid.UUID` 또는 exact
`asyncpg.pgproto.UUID`만 허용한다. owning base descriptor를 통해 fresh stdlib
UUID로 복사하고 subclass/spoof/malformed 값은 render 없이 거부한다. asyncpg는
stdlib fast path 뒤에 lazy import하며 malformed candidate는 bounded
scanned/claimed error slot 하나를 소비한 뒤 sweep을 계속한다.

🔴 **advisory lock 은 브로커 fencing 이 아니고**, PostgreSQL 재시작을 가로지르는 분산
락도 아니다. pending/processing 행은 그 수명 동안 최소 PII 를 보유한다. 실제 프로세스
활성화는 post-deploy 리스크다. 한계는 런북 §7.

### 매수 게이트 A/B shadow (ROB-1301)

KR/US 매수 스크리닝의 **variant B(moderate+ 지지)** 는 계좌 불사용 shadow
실험이다. Variant A(strong 지지 필수)가 라이브 게이트이며 문언·집행은 불변.

- **패키지**: `app/services/buy_gate_ab_shadow/` — 사전등록 스펙·A/B 대칭
  평가·`shadow_buy` forecast 태깅·4주 채점 공식. DB/브로커/제안/워치 import 없음.
- **MCP**: `evaluate_buy_gate_ab_shadow` — observation-only. B-only 후보는
  `forecast_save` kwargs만 반환하고 도구 자체는 쓰지 않는다.
- **금지**: shadow → 제안·주문·워치 승격 0. 채점 전 중간값으로 정책 변경 0.
  승격·자동화 트리거 0. 스케줄러 등록 0.
- **런북**: `docs/runbooks/buy-gate-ab-shadow.md`
- **플레이북**: `docs/playbooks/trading-decision-playbook.md` §3.2 (lane
  sequence 아님)
- **Q6 activation epoch (ROB-1331)**: versioned addendum
  `rob-1331-q6-activation-epoch.v1` + immutable singleton
  `review.buy_gate_ab_collection_epoch`. `collection_armed_at=2026-08-30T09:17:36+09:00`
  → 다음 공통 완전 세션 `collection_start=2026-08-31` → 28일 고정 창
  (`collection_end_exclusive=2026-09-28`). `first_valid_record_at`은 nullable
  관측값이며 경계 입력이 아니다. 0건도 `INSUFFICIENT_SAMPLE / NO_FIRING`으로
  종료한다. `scoring_ready = collection_window_closed AND all_events_matured`.
  policy projection SHA-256은 spec/forecast/DB marker에 동일하게 봉인된다.
  🔴 caller wiring은 marker의 merge·배포·migration·독립 리뷰 뒤 별도 PR에서만 한다.
  ROB-1331 PR 자체에는 caller/scheduler/실행 배선이 없다.


## 유지 규약

새 기능·안전 경계·계약을 추가하거나 변경할 때에는 관련 분야 계약을 같은 PR에서 갱신하고, 필요하면 두 진입점의 최소 하드룰과 명시적 필독 목록도 함께 갱신한다.
