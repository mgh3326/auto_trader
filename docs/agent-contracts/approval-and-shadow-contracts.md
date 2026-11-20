# 승인·shadow 계약

이 파일은 작업 분야와 무관하게 작업 시작 전에 끝까지 읽어야 하는 필독 계약의 일부다. 찾아보기는 탐색 보조일 뿐 선택 읽기 면제가 아니다.

## 목차

- Telegram 승인 콜백 durable inbox (W5)
- 매수 게이트 A/B shadow (ROB-1301)
- /trader 승인 대기함 (task 890 PR A)
- 자동승인 가격 폴백 (#1067)
- 주차자산 proposal-bound 자동 매도 (task 817)
- Telegram 알림 분리 + 자동승인 다이제스트 (ROB-1052)
- Toss US 확장세션 승인 창 (#1116)
- 전략 슬리브 — 실계좌 안의 전략별 예산·손실 한도 (ROB-1470)
- Kiwoom 모의 제안 집행 (hk 4732/4754 / 3797 T-P5/T-P8)

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

### /trader 승인 대기함 (task 890 PR A)

trader.robinco.dev `/trader` 페이지의 승인/기각/손절 2클릭 버튼은 **새 승인 경로가
아니다** — 기존 `/invest/api/approvals/{id}/{approve|deny|loss-cut-confirm}`
(`handle_web_approval`)만 호출하며, 그 함수는 Telegram 콜백과 같은
`_handle_approve`/`_handle_deny`/`_handle_loss_cut_first_click` 을 부른다
(`tests/services/order_proposals/test_trader_page_same_approval_path.py`).
🔴 `/trading/` 은 CSRF 면제 경로이므로 이 페이지용 상태 변경 라우트를 `/trading/api/trader/`
에 추가하지 마라. 목록 포함 규칙의 단일 정본은
`app/services/trader_page/approval_inbox.py::inbox_block_reason` 이다(자동승인·만료·
nonce 소비·미게시·알림 카드 제외). `INVEST_APPROVALS_ENABLED`/
`INVEST_LOSS_CUT_APPROVAL_ENABLED` 기본 false 유지. 런북: `docs/runbooks/trader-page.md`.

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

### Approval batch health (ROB-1488)

수동 ROB-870 batch의 member writer 시각은 `added_at`이며 `processed_at`은
callback 결과 시각이다. ROB-1052 auto digest는 이 batch/member 테이블을 쓰지 않는다.
`scripts.diagnose_approval_batch_health`는 READ ONLY·8초 timeout의 SELECT 집계만 한다.
member 없는 batch는 `gap`이지만, manual/reconfirm 발송 후 member가 없는 경우는
설계상 제외도 가능하므로 `review`이지 writer 정지 증명이 아니다. 다른 member가 있어도
미등록 published card 또는 frozen unsent summary가 하나라도 있으면 `review`다.
skip/summary dispatch 증거를 확인하며, member 없는 batch의 `gap`이 우선한다. 등록 skip 로그는
닫힌 category만 남기고 UUID/승인창의 동적 상세를 reason 필드에 넣지 않는다.
등록·승인·callback의 판정과 반환값, 스케줄은 변경하지 않는다.

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

### 전략 슬리브 — 실계좌 안의 전략별 예산·손실 한도 (ROB-1470)

기존 실계좌(KIS·Toss·Upbit) 안에서 ROB-1458 규칙별 카나리의 돈과 결과를 **DB 에서** 분리한다.
새 계좌 없음. 🔴 **모든 금액은 운영자가 정한다** — 코드·정책·문서 예시에 금액 없음(자리표시자만).

- **모델**: `app/models/strategy_sleeves.py` — `review.strategy_sleeves`(불변 정체성: 규칙·계좌·시장·통화)
  + `review.strategy_sleeve_versions`(append-only: 상태 `shadow|live|paused|stopped`·예산·손실 한도·사유·설정자).
  `review.order_proposals.sleeve_id`(nullable, 기존 흐름은 NULL). 마이그레이션 `20261102_rob1470_sleeves`.
- **서비스**: `app/services/strategy_sleeves/` — `store`(두 테이블의 유일한 writer, 귀속), `book`(순수 FIFO·P&L·사용액),
  `gate`(순수 판정), `dispatch_gate`(dispatch 배선), `report`, `marks`(DB 일봉 종가). **CLI**: `scripts/strategy_sleeves.py`.
- **런북**: `docs/runbooks/strategy-sleeves.md`
- 🔴 **게이트**: 태그된 제안은 `dispatch_proposal` 에서 revalidation(브로커 호출) **이전**에, 트랜잭션 끝까지 잡는 슬리브
  advisory lock 아래 판정된다. live 아님·손실 ≥ 한도(→ `stopped` 버전 append, actor `system:sleeve_loss_gate`)·P&L 미측정·
  매수 사용액+주문 > 예산·사용액 미측정·판독 실패는 전부 **사람 승인 카드**로 강등되고 카드에 사유 코드가 남는다
  (`sleeve_*`, `auto_approve_audit` 어휘). 거절이 아니라 강등이다. 🔴 `sleeve_id IS NULL` 제안은 이 분기에 들어가지 않는다 —
  태그 없는 주문 동작은 바이트 동일. 이 게이트를 revalidation 뒤로 옮기거나 미측정을 0 으로 취급하지 마라.
- 🔴 **DB 가드**: 두 테이블 UPDATE/DELETE/TRUNCATE 거부, 버전은 직전+1 만, `sleeve_id` 는 INSERT 때만(같은 계좌·시장 슬리브)
  설정되고 이후 변경 거부. 제안 트리거는 함수 호출 없는 WHEN 이라 태그 없는 쓰기는 트리거 코드를 실행하지 않는다.
  downgrade 는 슬리브나 태그된 제안이 있으면 거부. 이 트리거를 완화하지 마라.
- **귀속**: fill 은 `execution_ledger_in_effect()` + authoritative 소스만, 슬리브 자기 rung 의 브로커 주문번호·side·종목·통화,
  제안 생성 이후, KIS 는 rung 자기 주문의 거래일만(그 날짜는 rung 의 idempotency key·correlation id **와** 주문번호가 일치하는 송신 원장 행에서 — 주문번호만으로는 정하지 않는다. 날짜를 못 정하면 체결을 붙이지 않고 P&L 미측정). websocket 행은 reconcile 전까지 P&L 에 안 들어가지만
  열린 주문으로 사용액에는 남는다. 사용액 = 보유 원가 + 열린 매수 주문 + 원장 미기록 체결.
- **사전등록 (ROB-1468)**: 버전에 `prereg_metric`·`prereg_min_effect_pct`(예산 대비 %)·`prereg_horizon_sessions`(H)·
  `prereg_max_duration_days` — 값은 운영자만. `live` 버전은 넷 다 필요(DB CHECK), 한 번 live 였던 슬리브의 사전등록은
  변경 거부(store + 트리거) — 바꾸려면 새 슬리브(새 epoch).
- **리포트 통계**: 표본 = 에피소드 1개(신호 = 제안 lineage root 의 rung·래더·리비전·부분체결; 같은 종목 보유 중에 낸
  후속 신호 — 물타기·후속 래더·청산 — 는 첫 신호의 에피소드에 합류, flat 에서 낸 신호는 새 에피소드). 첫 신호 후 H 번째
  완성 종가로 MTM 한 비용 차감 결과 ÷ 에피소드 시작 시점 예산. 미체결은 결과 0 으로 코호트에 남고, H 종가 없음은
  `unmeasured`(0 으로 평균 금지).
  비교 기준선은 auto_trader 에 없어 `baseline=not_recorded`, D 는 비움. 통화별 합계만. 🔴 안전 상태(손실 한도·예산·정지)와
  통계 판정은 별개 필드이고 판정은 "통과"를 내지 않는다(`final_date_reached` = 사전등록 대비 채점 대상).
- **키움 모의 조합 (hk 5637, 3795 T1m-b)**: sleeve 계좌 조합에 `kiwoom_mock`/`equity_kr` 하나를 모의 전용으로 더했다(DB CHECK `ck_strategy_sleeves_account_market`, `SLEEVE_ACCOUNT_MARKETS`, `create_sleeve` 검사, `LEDGER_IDENTITY["kiwoom_mock"] = ("kiwoom","mock")`, 마이그레이션 `20261120_hk5637_sleeve_kiwoom` — 추가형, 새 객체 없음, downgrade는 `kiwoom_mock` sleeve가 있으면 거부). 키움 US·실계좌·다른 모의 계좌 조합은 없고 live 조합·게이트는 불변이다. 사용액·손익은 T1m-a의 키움 체결 귀속(계좌·거래일·주문번호·rung 키)으로만 측정되고 모호하면 미측정이다.
- 직접 주문 도구는 슬리브를 받지 않는다(슬리브 주문은 제안 경로만). 스케줄러 연결 없음. ROB-1460 exit plan 연결은 #2266 머지 후 후속.

### 위임장 모델 — 조항·승인·활성화 프로필·분할 계획 (hk 3795 T1, hk 5528)

설계 정본 `docs/design/3795-mandate.md`. T1은 **데이터 모델과 유일 writer만**이다. 주문·승인 게이트·분류기·스케줄러·env는 바뀌지 않고, 계획 결속 자동승인(T5)과 활성화는 별도 승인이다(하드룰 4).

- **테이블**: `review.mandate_{approval_requests,approvals,terms,activation_profiles,plans,plan_tranches,plan_events,symbol_reservations,symbol_reservation_releases}` + `order_proposals.mandate_plan_id`/`mandate_tranche_index`(nullable, INSERT 때만 설정). 마이그레이션 `20261120_hk5528_mandate`. 🔴 아홉 테이블 모두 UPDATE·DELETE·TRUNCATE 거부(모든 역할), at_app 은 SELECT·INSERT 만(하드룰 18), downgrade 는 행이 있으면 거부. 새 열이 NULL 인 제안 쓰기는 트리거 코드를 실행하지 않는다(WHEN 에 함수 호출 없음).
- **코드**: `app/services/mandate/` — `vocabulary`(닫힌 어휘·전이표, 마이그레이션이 같은 표를 리터럴로 복사하고 테스트가 일치를 고정) · `fold`(`fold_plan_events`/`apply_event`: 계획 상태는 이벤트 스트림 전체의 fold 이고 마지막 이벤트 이름이 아니다) · `invariants`(I2·I3·I4·I5·I6·I7·I15·I17 순수 함수) · `hashing`(조항·프로필 내용 해시, 십진·시각·집합 정규화) · `locks`(L1 종목 → L2 바구니 → L3 sleeve) · `store`(유일 writer). 주문·브로커 모듈 import 없음(`tests/services/mandate/test_static_boundary.py`).
- **바구니·영역**: 바구니의 정체성은 (영역, `basket_key`), 영역은 계좌 모드에서 정해진다(`kis_live`·`toss_live`=live, `kiwoom_mock`=mock). 그 밖의 계좌 모드(Upbit·KIS mock 등)는 영역이 없어 거부한다. 같은 이름이라도 영역이 다르면 다른 바구니이고 종목 예약도 영역별이다.
- **승인 기록(I21)**: 조항·프로필 행은 내용 해시가 같은 `approved` 승인 기록 없이는 INSERT 되지 않는다(트리거). 승인 요청의 `content_sha256`을 DB 열에서 다시 계산한 값과 대조한다. 운영자 principal 대조(`MANDATE_APPROVER_TELEGRAM_USER_IDS`)와 nonce 소비는 콜백(T3b)의 몫이고 store 는 `principal` 의 형식(정확한 십진 문자열, ≤2^52−1)만 검증한다.
- **계획 등록**: 종목 lock(L1) → 바구니 lock(L2) → sleeve lock(L3) 뒤에 I1(영역 안 종목당 미해제 계획 1개, 예약 INSERT 트리거도 같은 lock 을 스스로 잡는다)·I2·I3·I4(계획 금액 기준, §3.8)·I17·열린 `loss_cut` 제안 거부(`mandate_open_loss_cut`, 실계좌 영역만)·회차 금액이 등록 시점 1주 값 이상인지를 판정한다. 계좌 참조(`account_ref`)와 보유 범위 참조(`scope_account_refs`)와 1주 값은 호출자가 읽어 넘기는 값이다(store 는 브로커를 읽지 않는다).
- **이벤트 트리거(I7·I9·I16·I19·I24)**: `seq` 는 직전+1, 권한 상태 전이표·회차 순서·회차당 열린 제안 1개를 DB 가 강제한다. 권한 상태가 `approved`/`active` 가 아닌 계획에는 전송 fence(`tranche_send_fenced`)가 INSERT 되지 않고, `released` 는 `orders_settled` → `holdings_disposed` 뒤 닫히지 않은 fence 0일 때만, 그리고 그 INSERT 가 같은 트랜잭션에서 예약 해제 행을 쓴다. fence 영역 lock(F1~F4)·권한 행·mutex·자동 fence 의 최신 프로필 트리거는 T4c/T5 몫이다.
- **sleeve 규칙 id**: `at3795.basket1_value_split_entry`(KR·US)를 `CANARY_RULES`에 더했다. metric·horizon 은 참고 문구이고 구속력 있는 사전등록은 sleeve 버전마다 운영자가 정한다.
- **운영자 CLI (hk 5638 T2)**: `scripts/mandate.py`는 `request-terms`·`request-profile`(승인 요청 스냅샷)·`register-plan`(계획 등록)·`event`(운영자 계획 이벤트)·`status`(읽기)만 노출한다. 쓰기는 `--commit`에서만 일어나고 미리보기는 같은 저장소 검사를 돌린 뒤 롤백한다. actor는 `operator:<이름>`이어야 하며 `system:` 등은 DB 접근 전에 거부한다. `event`의 목록은 `suspended`·`resumed`·`retired`·`tranche_closed_partial`·`thesis_broken`·`thesis_evidence_refreshed`·`holdings_disposed`(`kept_outside_basket`만)뿐이다. `approved`·`rejected`·fence·체결·`released`는 이 CLI에 없다. `resume_target`·1차 `first_fill_*`·`thesis_seq`는 상태에서 정하고 운영자가 입력하지 않는다. `thesis_broken`·`thesis_evidence_refreshed`는 `--retrospective-id`·`--approval-issue-id`가 필수다(§5.2 증거 순서). 편입 보유 증거와 계좌 참조는 운영자 입력이며 CLI는 브로커를 읽지 않는다. 승인 기록은 만들지 않는다(T3b). 명령·플래그는 `docs/runbooks/mandate-cli.md`이며 테스트가 실제 argparse와 대조한다.
- **아침 묶음 승인 카드 (hk 5648 T3a)**: 테이블 둘 `review.mandate_plan_batches`(바구니 × 대상 거래일당 1개, 승인 마감 = 대상 세션 개장·만료 = 종료)와 `review.mandate_plan_batch_members`(카드 발급 1회당 1행, 카드 1은 배치와 같은 트랜잭션에서만, 재발송은 다음 `card_no`·새 nonce digest·같은 스냅샷)는 모든 역할에 UPDATE·DELETE·TRUNCATE 거부이고 at_app은 SELECT·INSERT만이다(마이그레이션 `20261120_hk5648_mandate_batches`, 추가형). 🔴 I22는 `UNIQUE(realm, basket_key, target_session)`과 배치 INSERT 트리거(바구니 lock L2 아래에서 앞 배치가 열려 있으면 — 만료 전이고 응답 없는 계획이 있으면 — 거부)가 지킨다. 응답은 `batch_member_id`를 단 계획 이벤트 하나이고(검사를 통과한 같은 INSERT에서 DB가 불변 카드 참조 `answered_batch_member_id`를 채우며, 응답 여부·배치 닫힘·재발급·새 resume 하한은 이 참조만 읽는다. 쓰는 쪽은 이 값을 지정할 수 없고 T1 시절 행은 NULL로 남아 응답이 아니다. 배치의 최초 멤버는 DB가 강제하는 생성 트랜잭션 식별자로, 조항·배치 만료는 잠금 뒤 DB 시각으로 다시 확인한다) `mandate_plan_events`의 새 트리거가 가장 새 카드·만료 전·계획당 응답 1개(advisory lock으로 직렬화)·카드 종류와 이벤트(신규 `approved`/`rejected`, 재승인 `terms_rebound`/`retired`)·카드의 조항이 sleeve의 live 조항·효력 세션(마감 전 = 대상 세션, 마감 뒤 = 그 뒤)을 어느 writer에게든 강제한다. 응답 이벤트가 nonce를 소비한다(nonce는 카드 버튼 안에만 있고 DB에는 digest뿐, 출력에도 없다).
  콜백 `mpb`(`mpb:<a|r>:<카드 id base36>:<nonce 22자>`)는 기존 주문 승인 콜백·W5 inbox를 거치지 않는 새 경로다: 라우터가 `mpb:` 접두로만 `app/services/mandate/batch_callback.py`로 보내고 나머지 업데이트는 이전과 같다. 🔴 정적 테스트의 범위는 둘이다: 패키지 전체(`app/services/mandate/`)는 주문 송신·승인·브로커·MCP 모듈 폐쇄 검사(`store.py`가 `order_proposals.state_machine`의 상수를 import하는 것은 허용), 새 batch 모듈과 `scripts/mandate_batch.py`는 `app.services.order_proposals` 전체를 import하지 않는다. 계획 이벤트만 store(`append_plan_event`)로 쓴다. 검사 순서: 허용 chat → 데이터 형식 → 운영자 principal(`MANDATE_APPROVER_TELEGRAM_USER_IDS`, **기본 빈 값 = 아무도 승인 불가**, 항목 하나라도 형식이 틀리면 전체가 빈 값, 목록에 없는 발신자는 DB를 열기 전에 거부되고 행도 nonce 소비도 없다) → nonce digest·카드 최신 여부·만료·이미 응답함·계획/조항 해시·live 조항 여부. 발송 CLI `scripts/mandate_batch.py`(`create`/`resend`/`status`, 기본 dry-run, `--commit`이면 행 커밋 뒤 발송, 발송 실패는 종료 코드 2)와 런북 `docs/runbooks/mandate-batch-card.md`. 스케줄러·주문·게이트 완화 없음. 설계 정본 `docs/design/3795-mandate.md` §5.1·§3.5, 테스트 `tests/services/mandate/test_batch_*.py`·`test_batches_db.py`(CI).
- **매도 1호가 소스 (hk 5652 T4d)**: `app/services/order_proposals/mandate_quote.py`가 계획 결속 제안의 근접가(§4.1 조건 6)가 쓰는 venue 매도 1호가를 읽는 유일한 곳이다(읽기 전용: KIS 호가·현재가 읽기만 부르고 주문·원장·승인 모듈을 import 하지 않는다 — 정적 테스트). 소스가 있는 (계좌 모드, 시장)은 `kiwoom_mock`/`equity_kr`(KRX 호가, 키움 모의는 KRX 전용)와 `kis_live`/`equity_kr`(주문 경로 `_domestic_exchange_route`와 같은 규칙: NXT 적격이면 통합 호가, 아니면 KRX 호가, 라우트를 못 정하면 ask 없음)뿐이다. Toss·US·crypto는 소스가 없어 ask가 없고 **대체가 없다** — `current_price`, Toss의 KIS 현재가 폴백(F7), 다른 venue의 호가를 ask 자리에 쓰지 않는다(`mandate_ask_unavailable`). 레포 Toss 클라이언트에 호가 엔드포인트가 없어 Toss 소스는 검증된 API가 생길 때까지 추가하지 않는다. 계획 결속 제안의 **생성 허용 계좌**는 여전히 `kiwoom_mock`뿐이라 `kis_live` 소스는 E1 측정(probe)에만 쓰인다. 최우선 매도 호가는 가격·수량이 양의 유한값인 가장 낮은 매도 레벨이고, 점유된 레벨(가격 또는 수량이 비어 있지 않거나 0이 아닌 레벨)의 가격이 무효이거나 수량이 판독 불가·음수이면 더 깊은 레벨을 승격하지 않고 `invalid_level`로 ask 없음이다. 매도 호가가 없거나 최우선 매수 호가가 그 이상(locked·crossed)이어도 ask 없음이다. **관측 시각은 브로커의 호가 시각이다**(`aspr_acpt_hour` + 브로커 날짜, 날짜가 없으면 응답 KST 날짜이고 응답보다 앞서면 전날). 방금 도착했지만 1분 전에 찍힌 호가는 1분 묵은 호가로 보인다. 시각이 없거나 해석 불가이거나 응답보다 2초 넘게 앞서면 `no_book_time`으로 ask 없음이다. 신선도 5초(`proposal_gate.ASK_MAX_AGE`, 시험 값 Q10)는 사용 시점의 `check_near_ask`가 판정하며, 판정 시각은 읽기가 끝난 시각 이후로 고정한다(`ask_check_time`) — 읽기 전에 잡은 `now`가 방금 읽은 호가를 미래 관측으로 보이게 하지 않는다. 지정가 상한의 호가 단위는 ask×(1+N bp) 가격대의 KRX 주식 호가 단위다(종목 분류가 없으면 주식 표 유지). 제안 **생성**(`create_proposal`)의 ask는 이 소스에서 오고, dispatch의 `mandate_market_reader` 기본값은 여전히 비어 있어 fail-closed다(그 입력이 쓰는 신선한 preview 가격·전역 누계는 T5 몫이라 이 PR이 채우지 않는다). 모든 읽기는 닫힌 어휘 `QuoteReadRecord`(불투명 `observation_id`·요청·응답 완료 시각·읽기 ms·브로커 호가 시각과 그 나이·호가·예외 클래스, 계좌번호·비밀 없음)를 남기고, 호가를 쓸 때마다 같은 `observation_id`를 담은 `QuoteUseRecord`(단계·판정 시각·나이·판정)를 남긴다(생성 경로가 첫 사용처이고 fence/전송 직전 경계는 같은 `record_quote_use`로 잇는다 — T4c/T5). 기본은 로그 한 줄이고 `scripts/mandate_ask_probe.py`(수동, 스케줄 없음, 주문 없음, 출력은 레포 밖)는 같은 기록을 JSONL로 쌓으며 기록이 유실되거나 0건이면 0이 아닌 코드로 끝난다(서비스 안의 sink는 fail-open). 테스트: `tests/services/order_proposals/test_mandate_quote.py`·`test_mandate_ask_probe.py`.
- **체결 투영 CLI (hk 5650 T4b)**: `scripts/mandate_projection.py`(규칙 `app/services/mandate/projection_rules.py`, 읽기·쓰기 `projection.py`)는 원장의 authoritative 체결과 계획에 결속된 제안의 rung 상태를 읽어 `tranche_filled`·`tranche_partially_filled`·`tranche_unfilled`(§3.2 "회차 기준값": 누계 95% 이상, 또는 체결이 있고 잔여가 제안 지정가 기준 1주 값 미만이면 완료), `first_tranche_settled`·`exhausted`·`closed`·`orders_settled`·`holdings_disposed`·`released`를 `system:mandate_projection`으로 T1 store(`append_plan_event`)를 통해서만 추가한다. 기본은 미리보기(같은 판정·검사 뒤 롤백)이고 `--commit`일 때만 계획마다 한 트랜잭션으로 쓴다. 🔴 원장·주문 표에 쓰지 않고 브로커·주문 모듈을 import하지 않는다(정적 테스트). 🔴 I10: rung이 체결을 보고해도 authoritative 원장 행이 그 수량을 덮지 않으면(websocket 단독 행 포함) 쓰지 않고(`fill_not_booked`), 열린·불명(`unverified`) rung, 귀속 불명 체결, 주문번호 없이 닫히지 않은 fence는 미전송 증거가 아니다. 🔴 I19: `zero_holdings`는 운영자가 `--holdings`로 넘긴 조회(브로커를 읽지 않는다) 중 `scope_account_refs`의 **모든** 계좌와 참조가 일치하고 마지막 체결보다 늦고 신선한 0 조회만 받는다(범위 계좌가 바뀌었거나 빠지면 쓰지 않는다). `closed`는 `thesis_broken` 뒤 모든 매수 주문 종결과 계획 계좌 0 조회, `exhausted` 계획의 정리는 전 계좌 0 조회 뒤에만, `thesis_broken` 계획은 `closed`·`retired` 없이 정리하지 않는다. 종결 이벤트 뒤 늦게 들어온 체결은 `recorded_fill_mismatch`로 정리를 막는다. 스케줄러 연결 없음. 런북 `docs/runbooks/mandate-projection.md`, 테스트 `tests/services/mandate/test_projection_rules.py`·`test_projection_cli.py`(로컬), `test_projection_db.py`(CI).

### Daily strategy forward shadow (ROB-1473 phases 1–2)

`scripts/paper_forward/run.py` → `research/paper_forward/` records hypothetical
B3/US-composite episodes in immutable per-market daily JSON files outside git.
Research replay and forward share `research/strategy_evidence/decision_rules.py`.
No broker/DB/credentials/orders; paper-account and sleeve wiring are later phases.
E is fixed per episode (B3 3u, US 1.5u); missing bars/intent evidence/control limits
are `unverified`, never zero. H uses the independent 24/7 or XNYS calendar.
The normalized intent feed preserves research signal definitions, and the universe
is frozen from its research price manifest. `deploy/dagu/paper-forward.yaml` is
PROPOSAL only, unregistered. Details: `docs/runbooks/paper-forward.md`.
The normalized feed requires an inclusive `covers_from` entry-session bound and
`complete_through`; coverage must span the whole uncommitted interval. Unverified
runs defer new signals without consuming keys and publish reports only. A separate
immutable epoch marker pins the initial date, so corrected input can retry the
same asof or a later catch-up without losing the first interval.
Bar gaps block collection only along new signal paths and existing active or
still-maturing episodes. Unused missing-universe members remain in `data_gaps`
without blocking a commit; `blocking_data_gaps` records the required missing bars.

### Kiwoom 모의 제안 집행 (hk 4732/4754 / 3797 T-P5/T-P8)

`order_proposals` 는 Kiwoom mock **place 제안만** 받는다: `('kiwoom_mock','equity_kr')`(KR, hk 4732)와
`('kiwoom_mock_us','equity_us')`(US, hk 4754)가 `_SUBMITTABLE_ACCOUNT_MODE_MARKETS` 에 있다.
`SUPPORTED_TARGET_ACTIONS`(replace/cancel)는 그대로다 — 교체·취소 제안은 kiwoom 계좌에서
생성되지 않는다(구조화된 `unsupported account_mode/market/action` 거부). US 어댑터는
`equity_us`+`order_type='limit'` 만 허용하고 양의 정수 quantity·양의 decimal price 를 요구한다
(KR 어댑터는 정수 quantity/price).

- **세션**: `resolve_submission_session` 의 **명시 분기**가 KR 은 KRX 정규장(09:00–15:30 KST),
  US 는 XNYS 정규장만 허용한다(`exchange_calendars:XKRX:regular_only` / `exchange_calendars:XNYS`).
  NXT·pre/post 연장은 없다 — KIS/Toss 의 `_resolve_kr_session`/`_resolve_toss_us_session` 을
  타지 않고, 창 밖은 `DEFER_SESSION_CLOSED`(또는 `NO_EXECUTABLE_WINDOW`)로 다음 정규장 시각을
  명시한다.
- **수동 승인 전용**: `_VETO_CAPABLE_ACCOUNT_MARKETS`·자동승인 어휘·`auto_approve_audit`
  `account_mode` 열거에 `kiwoom_mock`/`kiwoom_mock_us` 는 없다. 제안 경유 kiwoom 주문은 전부
  사람 승인 카드다(veto 는 브로커 취소 경로가 필요한데 이 경로는 취소를 열지 않는다). 이 집합들은
  테스트로 고정한다.
- **하나의 송신 경계**: 확정 KR/US kiwoom place 시퀀스(귀속 신호 → preflight → 송신 예약 →
  POST → 원장 행 → 신호 결과 표시)는 `app/services/kiwoom_mock_place.py` 의
  `place_kiwoom_mock_order`/`place_kiwoom_mock_us_order` 에 있고, `kiwoom_mock_place_order`/
  `kiwoom_mock_us_place_order` MCP 도구와 제안 어댑터(`revalidation._default_place_order_fn` 의
  두 kiwoom 분기)가 **같은 함수**를 호출한다. MCP 응답은 골든 테스트로 바이트 동일하게 고정된다.
- **귀속·예약은 송신 전**: 제안의 `strategy`/`correlation_id` 가 귀속을 공급한다.
  `strategy` 가 없으면 `attribution_required`, 신호 행 커밋 실패는
  `signal_record_unavailable`, 예약 실패는 `send_intent_unavailable` 이며 셋 다 **송신 0**.
  같은 rung 의 재디스패치는 결정적 `client_order_id`(`kwoomm-`/`kwoomus-`)로 예약 1회·송신 1회다.
- **응답 매핑(닫힌 3분기)**: 브로커 ACK + 주문번호 증명(KR `ord_no`/`order_no`, US 는
  `validate_us_order_id` 통과 주문번호) → rung `resting`(broker_order_id·idempotency key·
  correlation 기록). 명시적 브로커 거절 또는 증명된 미송신 → `rejected`(`error_code` 를 void
  사유로). 그 밖(`acceptance_uncertain`, 주문번호 없는/불량 ACK, ACK 후 원장 기록 실패, 앞선
  결과가 불명인 중복 키)은 `success` 를 지워 `unverified` 로 남기고 절대 종결 처리하지 않는다.
- **pre-send 훅**: 제안 경로의 `ApprovalWindowPreSendGate` 는 예약 직후·POST 직전에 실행되고,
  차단 시 예약을 해제한 뒤 예외를 그대로 올려 창/지연 결과로 수렴시킨다(증명된 미송신).
- **범위 밖**: 이 제안 집행 절이 직접 쓰는 것은 원장·신호·예약이다. `execution_ledger` 체결
  부킹은 별도 대사 경로(hk 4734)가 담당하고 저널·실현손익 투영은 hk 4802 이다. propose 경유
  modify/cancel과 스케줄러 연결은 없다.

## 유지 규약

새 기능·안전 경계·계약을 추가하거나 변경할 때에는 관련 분야 계약을 같은 PR에서 갱신하고, 필요하면 루트 `AGENTS.md`의 최소 하드룰과 명시적 필독 목록도 함께 갱신한다.
