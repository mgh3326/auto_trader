# 승인·shadow 계약

이 파일은 작업 분야와 무관하게 작업 시작 전에 끝까지 읽어야 하는 필독 계약의 일부다. 찾아보기는 탐색 보조일 뿐 선택 읽기 면제가 아니다.

## 목차

- Telegram 승인 콜백 durable inbox (W5)
- 매수 게이트 A/B shadow (ROB-1301)
- 자동승인 가격 폴백 (#1067)
- 주차자산 proposal-bound 자동 매도 (task 817)
- Telegram 알림 분리 + 자동승인 다이제스트 (ROB-1052)
- Toss US 확장세션 승인 창 (#1116)

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


### 주차자산 proposal-bound 자동 매도 (task 817)

`expanded` 자동 승인에서는 저장된 proposal의 명시적 계좌가 브로커 선택 계좌와
일치하고 계좌별 주차 계측이 유효할 때만, 닫힌 SGOV/BIL/459580/357870
symbol·account-mode·market 튜플의 live 지정가 SELL을 평균단가·손익 증명 없이
허용한다. 일반 주차 매도에는 `funding_target`이 필요하지 않다. 현금 부족 또는
운영자 지시가 운영상 이유이며, 제안에는 이를 설명하는 veto thesis가 있어야 한다.
task 765의 Toss 명시 계좌·브로커 계좌 목록 검증이 선행되어야 한다.

`policy_deviation` 전체 필드 스캔, veto 가능한 계좌·시장, USD 10,000/KRW
10,000,000 단건 상한, 신선한 preview, 현재가 대비 2% 지정가 하한, 보유 수량,
브로커 이중 게이트, 재검증, veto 카드가 유지된다. KR은 XKRX 정규장 안에서만
dispatch와 send가 가능하다. 계좌 누락·불일치 또는 계측 실패는 자동 승인 없이
사람 승인 카드로 간다. 직접 주문 API, 기존 `cash_funding` 증거 계약,
default-disabled 게이트, 스케줄러는 바뀌지 않는다. 자세한 운영 경계는
`docs/runbooks/order-proposal-auto-approve-expand.md` §10을 따른다.

### 자동승인 가격 폴백 (#1067)

`toss_live` preview 의 `current_price` 가 없거나 null/blank 일 때만 자동승인
게이트가 `get_quote` 경로로 KIS 시세 1회를 읽어 **입력만** 대체한다 — 모든
게이트(캡·거리·tier·loss guard)는 그 값으로 그대로 돈다. 채택 조건: 같은
심볼·`instrument_type`, `source == "kis"`, `is_stale_price is False`(부재는
fresh 아님), `data_state == "fresh"`, 유한 양수 가격. US 는 `market_unsupported`.
결정에 `price_source`(`toss_preview`/`kis_quote_fallback`) 기록.

- **모듈**: `app/services/order_proposals/auto_approve_price_fallback.py`,
  `auto_approve.evaluate_auto_approve_eligibility(price_fallback=...)`,
  `dispatch.dispatch_proposal(price_fallback_fn=...)`
- 🔴 폴백 실패/stale 이면 같은 dispatch 에서 즉시 기존 카드(`price_or_quantity_missing`) —
  `price_context_message`(#1053) + 닫힌 `price_fallback_reason` 보존. **지연 재평가·재시도
  없음**(운영자 결정 #1083 B). 스케줄러/TaskIQ/detached task 추가 금지.
- **런북**: `docs/runbooks/order-proposal-auto-approve-expand.md` §11

### Telegram 알림 분리 + 자동승인 다이제스트 (ROB-1052)

승인 카드(사람 판단 필요: manual/reconfirm/loss-cut/batch)는 항상
승인 allowlist 첫 chat으로 간다. 자동승인 카드는 `vc` veto 버튼을
달고 notices 목적지로 가므로, `ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID`가
설정되면 그 chat도 `ORDER_PROPOSALS_TELEGRAM_CHAT_ALLOWLIST_STR`에
들어 있어야 한다 — 빠져 있으면 notices chat에서 온 모든 veto tap이
chat_not_allowed로 거부된다. 자동승인·체결·만료 알림은
`ORDER_PROPOSALS_TELEGRAM_NOTICES_CHAT_ID` /
`ORDER_PROPOSALS_TELEGRAM_NOTICES_THREAD_ID`가 설정됐을 때만 그 목적지로
가며, 미설정이면 종전 팬아웃과 바이트 동일하게 동작한다. thread id는
양의 정수만 유효하고, notices chat 없이 malformed/zero/negative면
전체가 미설정으로 간주된다(notices chat이 있으면 chat-only로
degrade). thread만 설정되면 chat은 allowlist 첫 항목으로 fallback한다.

자동승인 알림의 라운드 키는 `open_auto_digest_round()` 스코프 하나다 —
현재 `support_reserve_net_consume`의 post-commit dispatch 루프와
`apply_decision_table`의 행 루프만 감싼다. 라운드 안의
`dispatch_proposal`은 카드 발송 대신 item을 버퍼하고
`ApprovalDispatchState.PENDING`을 반환하며, 스코프 종료 flush가
Telegram UTF-16 4096 한계 안쪽으로 chunk를 만들어 한 메시지(필요시
연속 chunk)로 보낸다. item 0개면 아무것도 보내지 않고, 라운드 밖
standalone dispatch는 즉시 발송으로 유지된다. flush 실패는 item마다
standalone과 동일한 durable 최종 처리(attempt fence, 브로커 cancel
보상, `record_auto_notification_failure`) + Discord operator alert +
`collector.outcomes` 기록이며, 호출자의 `pending` 결과는 scope 종료 후
실제 outcome으로 reconcile된다. 공유 digest의 veto tap은
`source_asof["auto_digest"]` 멤버를 재조회해 digest를 재렌더하므로
형제 item의 살아있는 `vc` 버튼이 유지된다(standalone 카드는 종전
wipe-edit 그대로). 만료는 승인 카드를 원 위치에서 편집하고, notices가
설정됐을 때만 동일 본문을 별사본으로 보낸다(best-effort, 예외 삼킴).
콜백 인가·nonce 단일소비·loss-cut 2클릭 규칙은 변경되지 않는다.

### Toss US 확장세션 승인 창 (#1116)

정책 키 `order_proposals.approval_window.toss_live_us_sessions`
(`config/trading_policy.yaml`, 스키마 `OrderProposalApprovalWindowPolicy`)가
`toss_live`/`equity_us` 제안이 쓸 수 있는 토스 US 세션을 정한다. 🔴 기본값
`[regular]` 은 기존 하드코딩(정규장 전용)과 결정·policy stamp 가 바이트 동일하다.
운영자가 정책 PR 로 `pre`/`post` 를 더할 때만 LIMIT `place` 제안에 그 세션이 열린다
(`regular` 필수, 토스 데이마켓은 어휘에 없음, MARKET·replace·cancel 은 정규장 전용).

- **모듈**: `app/services/order_proposals/approval_window.py`
  (`_resolve_toss_us_session`, `apply_toss_us_extended_order_shape`),
  읽기 `trading_policy_service.toss_live_us_approval_sessions` — 읽기 실패는
  정규장 전용으로 fail-closed
- 🔴 **정규장 밖은 정수 수량 LIMIT 만**: rung `notional`(금액 주문 = 토스
  `orderAmount`)·소수/비양수 수량·지정가 부재는 `DEFER_SESSION_CLOSED` +
  `toss_us_extended_session_refused:<사유>` 로 거부된다. 판정은
  `evaluate_approval_window_boundary(rungs=...)` 안에 있고 모든 운영 게이트(카드·
  일괄 요약·단건/일괄/손절 콜백·reconfirm·redispatch·revalidation rung 게이트·
  transport hook)가 rungs 를 넘긴다(누락 시 정적 테스트 실패). **모두 브로커
  preview/submit 이전**이며, 거부 멤버 하나가 일괄 승인 전체를 nonce 소비 전에 막는다.
  보호 청산(`exit_intent`)은 기본 키에서 기존 validity-only 면제 그대로(캘린더 I/O
  없음)이고, pre/post 를 켜면 토스 세션을 조회해(fail-open) 조회 뒤의 시각으로
  세션을 판정하고 pre/post 중에는 같은 형태 규칙을 적용하며, 통과한 면제도 현재
  토스 세션 종료 시각까지만 유효해 전송 직전 재표본에서 세션 전환이 fail-closed 된다. `toss_preview_order` 는 이 형태들을 로컬에서
  통과시키지만 실주문은 정규장 밖 422 — **preview 통과 ≠ 접수 가능**
- 🔴 KIS US·KR·crypto 는 이 키를 읽지 않는다. 키 값이 바뀌면 stamp 가 바뀌어 이전
  값으로 발송된 카드는 승인 시 fail-closed
- **DAY 만료 기록**: pre/post 결정의 `session_evidence.day_expiry` 와 발송 카드의
  `source_asof.approval_window_day_expiry`. pre 접수는 다음 토스 정규장 종료(토스
  문서 근거, `measured=false`), post 접수는 `expected_expiry_at=null`(미측정).
  KR 키 `order.day_expiry_kst` 에서 유도하지 않는다. 실측 카드(#908/#1032)가
  채운다
- **금지**: 이 PR 은 키를 켜지 않는다. 스케줄러·자동 플립 없음
- **런북**: `docs/runbooks/order-proposals.md` §Approval-window defense in depth

## 유지 규약

새 기능·안전 경계·계약을 추가하거나 변경할 때에는 관련 분야 계약을 같은 PR에서 갱신하고, 필요하면 두 진입점의 최소 하드룰과 명시적 필독 목록도 함께 갱신한다.
