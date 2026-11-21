# hk 6038 매도 제안 승인·전송 시계 진단과 수정 설계

상태: 설계 초안(문서만). 코드·DB·주문·스케줄러 변경은 이 문서의 범위 밖이고, 구현은 §6의 티켓으로 나눈다.

근거 표기: 코드 근거는 `auto_trader@d24be1eec path:line`(이 PR의 base)이다. DB에 접근하지 않았으므로 운영 행(1623·1624·1625·1626·1629·1630)과 호스트 상태는 코드로 설명되는 원인만 적고, 확인이 필요한 곳은 `[실측 필요]`로 표시한다. 코드에서 확인하지 못한 것은 `[모름]`, 추정은 `[추정]`이다.

## 1. 한눈에 보기

| 증상 | 원인(코드 근거로 확정) | 실측으로 확정할 것 |
|---|---|---|
| 장외 생성 제안이 `expired`, `approval_dispatch_state` NULL, `DEFER_*` 코드 없음 | 자동승인 켜짐 경로(`dispatch_proposal`)가 창 차단 결정을 **원장에 기록하지 않고** 반환한다. 수동 경로(`send_proposal_for_approval`)만 기록한다. 이후 세션 개장 때 다시 보내는 주체가 없어 `valid_until`이 지나 만료된다 | 1623·1624·1625·1626의 `source_asof.approval_dispatch_alert`, 루프 `void_reason`, `ORDER_PROPOSALS_AUTO_APPROVE` 운영 값 |
| 만료가 지난 `proposed` 잔존(1629·1630) | 시간 만료를 쓰는 주체는 야간 스윕·수동 도구뿐인데, 스윕은 기본 꺼져 있고 스케줄은 저장소가 등록하지 않는다 | Dagu 등록 여부와 두 플래그 값 |
| 11-19 에스엠 "거래일 캘린더 불일치" 전송 차단 | 11-19는 수능일이다. XKRX 라이브러리는 그날을 09:00–15:30으로 보고(로컬 확인), 토스 통합 캘린더는 정규장을 다르게 줄 수 있다. 두 소스가 어긋나면 `regular_calendar_disagreement`로 `CALENDAR_UNKNOWN`이 된다 | 그날 토스 `integrated.regularMarket` 창, 실제 차단 문구 |
| US 개장 전(08:39 ET) 생성 | 기본 `valid_until`이 시장과 무관하게 KST 자정이라, 개장 전에 만든 US 제안은 개장 직후 30분(EST) 안에 만료된다. 제안에 "어느 세션을 겨냥했는가"가 없다 | — |
| KR 16:08 생성이 NXT 가격으로 판단됨 | 제안·미리보기에는 가격의 세션·거래소 표지가 없다. `current_price`는 값 하나이고, 장중 `high`는 정규장 값인데 이를 구분하는 필드가 없다 | 그 제안이 쓴 기준가의 출처 |

핵심 결론: **개장 시점에 제안을 다시 확인하는 코드는 저장소에 처음부터 없었다.** at-scheduler(TaskIQ)·at-worker가 은퇴해서 생긴 공백이 아니다(§2.4).

## 2. 원인

### 2.1 장외 생성 제안이 카드 없이 만료되는 경로

1. 생성 직후 MCP 경계가 `dispatch_proposal`을 부른다(`app/mcp_server/tooling/order_proposal_tools.py:292`~`:330`, 결과 처리 `:418`).
2. 기본 `valid_until`은 호출 시각의 다음 KST 자정이다. 시장·세션을 보지 않는다(`app/services/order_proposals/service.py:1012`~`:1015`).
3. `ORDER_PROPOSALS_AUTO_APPROVE`가 꺼져 있으면 `send_proposal_for_approval`로 간다(`app/services/order_proposals/dispatch.py:1095`~`:1103`). 이 경로는 창이 막히면 `_record_approval_window_block`으로 `failed` + `DEFER_SESSION_CLOSED/…` 같은 코드를 **먼저 기록**한다(`dispatch.py:464`, 호출 `:768`~`:780`). 테스트가 이를 고정한다(`tests/services/order_proposals/test_dispatch.py:365`~`:420`).
4. 자동승인이 켜져 있으면 `dispatch_proposal`이 직접 창을 평가한다. 막히면 `EXPIRED`일 때만 루프를 만료시키고 **아무 원장도 쓰지 않고** `return window`한다(`dispatch.py:1124`~`:1135`). `_record_approval_window_block`은 이 경로에 없다. 결과: `approval_dispatch_state` NULL, 실패 코드 없음, 루프 `pending_approval`, 카드·nonce 없음.
5. MCP 경계는 반환된 `ApprovalWindowDecision`으로 Discord 알림만 보낸다(`order_proposal_tools.py:419`~`:430`). 이 알림은 `source_asof.approval_dispatch_alert`에 쓰이고(쓰기 `service.py:2255`~`:2285`, 발송 `alerts.py:101`), 문구는 "다음 주문 가능 세션에서 `order_proposal_redispatch`로 재검증하라"(`alerts.py:48`~`:52`)이다. 즉 사람이 도구를 직접 불러야 한다.
6. 개장 시점에 이를 대신 부르는 주체가 없다(§2.4). `valid_until`이 지나면 `expired`가 된다. 이 전이를 쓰는 곳은 야간 스윕·수동 스윕·승인 콜백·재발송 시도뿐이다(`service.py:1830`, `:1866`, `:4851`~`:4893`, `dispatch.py:598`~`:882`).

판정: NULL + 코드 없음 + `expired`는 "수동 경로는 기록한다"(3)와 "자동 경로는 기록하지 않는다"(4)의 차이로 설명된다. 수동 경로였다면 `failed` + 코드가 남았을 것이다. `[실측 필요]` 네 행에서 `source_asof.approval_dispatch_alert`가 있고, `rung.void_reason`이 `expired_valid_until_night_sweep`(야간 스윕)인지 NULL(그 밖의 경로)인지 본다. 운영 `ORDER_PROPOSALS_AUTO_APPROVE` 값도 확인한다.

### 2.2 만료가 지난 `proposed` 잔존

- 시간 만료를 쓰는 주기 작업은 `order_proposal.night_sweep`(`app/tasks/order_proposal_expiry_tasks.py:51`~`:75`)과 `order_proposal.expire_sweep`(`:22`~)이다. 둘 다 기본 꺼짐이다(`app/core/config.py:1132`~`:1142`: `order_proposal_expire_sweep_enabled`, `order_proposal_night_sweep_enabled`, `order_proposal_night_sweep_schedule_enabled` 전부 `False`). 코드는 크론을 선언만 한다.
- at-scheduler 은퇴 후 대체는 Dagu 제안 사본 `deploy/dagu/at-batch2/at-batch2-order-proposal-night-sweep.yaml`이다. README는 "저장소가 등록하지 않는다. 머지 후 별도 운영 절차"라고 적는다(`deploy/dagu/at-batch2/README.md:1`~`:3`). `scripts/order_proposal_night_sweep.py:14`~`:17`은 **두 플래그가 모두 켜지지 않으면** `{"status":"skipped"}`로 성공 종료한다. DAG가 등록돼 있어도 플래그가 꺼져 있으면 아무것도 하지 않고 실패도 하지 않는다.
- 스윕 시각은 월–금 16:30·07:00 KST뿐이라, 금요일 밤에 만든 제안은 월요일 07:00까지 `proposed`로 남을 수 있다.

판정: 1629·1630은 코드상 "스윕이 한 번도 돌지 않았거나 플래그가 꺼져 있었다"와 일치한다. `[실측 필요]` Dagu에 DAG가 등록돼 있는지, 두 플래그 값이 무엇인지, 최근 실행 로그가 `skipped`인지.

### 2.3 11-19 에스엠 "거래일 캘린더 불일치"

- 정확한 문구 "거래일 캘린더 불일치"는 저장소에 없다. 운영자 문구는 `CALENDAR_UNKNOWN`이 "시장 세션 확인 불가 — 승인 차단"(`approval_window.py:1362`~`:1363`)이다. 가장 가까운 사유는 `regular_calendar_disagreement`이다(`approval_window.py:970`~`:979`): XKRX 라이브러리 기준 정규장이 **아닌데** 토스 통합 캘린더가 지금을 `regular`로 말하면 `known=False`로 막는다.
- `_resolve_kr_session`은 정규장 판정을 XKRX 라이브러리 경계로 한다(`approval_window.py:880`~`:884`, 경계 출처 `app/services/market_events/session_calendar.py:82`~`:110`). 로컬에서 확인했다(`exchange_calendars` 4.13.2): 2026-11-19의 XKRX 세션은 09:00–15:30 KST이다. 11-19는 수능일이라 개장이 10:00이고 마감이 16:30으로 알려져 있다. `[모름]` 이 사실은 이 레포에서 코드로 확인되지 않는다. 같은 한계가 이미 계약 문서에 적혀 있다(`docs/agent-contracts/broker-and-ledger-contracts.md`의 #1112 "알려진 한계": 수능일 16:30 마감을 모델링하지 않는다).
- 그래서 11-19의 15:30–16:30에는 XKRX는 정규장 아님, 토스는 정규장일 수 있고, 이때 전송이 막힌다. 반대 방향 위험도 있다: 09:00–10:00에는 XKRX만 정규장이라 개장 전인데 허용될 수 있다.
- `[실측 필요]` 11-19 에스엠 제안의 실제 차단 코드와 `evidence.detail`, 그날 토스 `market_calendar_kr` 응답의 `integrated.regularMarket`.

### 2.4 개장 때 다시 보내는 주체가 있었는가

- 승인 카드를 (재)발송하는 호출부는 앱·스크립트·배포 파일 전체에서 세 곳뿐이다: 생성 직후 MCP 경계(`order_proposal_tools.py:307`), MCP 도구 `order_proposal_redispatch`(`order_proposal_tools.py:1288`~`:1370`), 기계식 손절 live 경로(`app/services/mechanical_loss_cut/live.py:288`). TaskIQ 태스크·Prefect flow·Dagu DAG·스크립트 중 이를 부르는 것은 없다.
- 이력으로도 같다. `redispatch`는 도구로 처음 들어온 뒤(`461e8f5b5`, `fb06b2b84`) 주기 작업으로 부른 적이 없고, hk 5753이 지운 `ops_task_kick` 레지스트리에도 승인 카드 재발송 항목이 없다(`git show 0e74900c7^:app/services/ops_task_kick/registry.py`에서 `redispatch`·`night`·`expire` 검색 결과 없음, `order_proposal`은 비관련 목록 한 줄만).
- 따라서 hk 5101·5753이 이 기능을 없앤 것이 아니다. 은퇴로 달라진 것은 `order_proposal.night_sweep` 크론이 TaskIQ에서 Dagu 제안 사본으로 넘어갔고(등록은 운영자 몫), W5 durable ingress의 TaskIQ kick이 빠졌다는 점뿐이다(`docs/runbooks/pi-shiftgate-deploy.md` §hk 5753). 승인 콜백은 W5 게이트가 꺼진 인라인 경로로 동작한다.
- `order_proposal_redispatch`가 개장 시점에 쓰이려면 사람(또는 세션)이 직접 불러야 하고, 가격을 다시 정하지 않는다: 지정가·수량이 예전 값과 정확히 같아야 하고(`redispatch.py:187`), 매도 지정가가 현재가보다 낮으면 `redispatch_price_crossed_market`으로 막힌다(`redispatch.py:214`).

## 3. 수정 설계

원칙: 기존 승인 게이트·nonce·손실매도 가드·승인 창 규칙(`docs/agent-contracts/approval-and-shadow-contracts.md`)은 바꾸지 않는다. 아래는 **기록을 더하거나 거부를 더하는** 방향이며, 어떤 것도 주문·정정·취소·자동승인 판정을 풀지 않는다.

### 3.1 (a) 장외 생성 제안을 개장 때 다시 확인하는 레버

#### 3.1.1 기록 공백 메우기

`dispatch_proposal`의 창 차단 반환(`dispatch.py:1131`~`:1135`)이 수동 경로와 같이 `_record_approval_window_block`을 쓰도록 한다. 효과: 장외 생성 제안이 항상 `failed` + `DEFER_SESSION_CLOSED/…` 같은 코드를 남기고, `order_proposal_redispatch`·재확인 레버가 자격을 가진다(`_REDISPATCHABLE_STATES`에는 이미 NULL과 `failed`가 있다, `redispatch.py:30`~`:36`). 만료(`EXPIRED`) 처리 뒤 기록하는 순서도 수동 경로와 같게 한다(`dispatch.py:768`~`:780`).

#### 3.1.2 명시 레버(CLI)

`scripts/order_proposal_session_recheck.py`(이름은 구현 때 확정)를 새로 만든다. `scripts/order_proposal_night_sweep.py`와 같은 모양이다: 앱 로직 함수 하나를 부르는 얇은 CLI, 기본 미리보기, `--commit`이 있어야 쓰고, 새 게이트 `ORDER_PROPOSAL_SESSION_RECHECK_ENABLED`(기본 false, 정확히 `true`)가 꺼져 있으면 `{"status":"skipped"}`로 성공 종료한다.

한 번의 실행 순서:

1. 먼저 만료: 시간이 지난 `proposed` 제안을 기존 `run_order_proposal_expire_sweep`(`order_proposal_tools.py:1124`)로 닫는다. 새 사유 `void_reason=expired_valid_until_session_recheck`를 쓴다(기존 `expired_` 접두 규칙, `night_sweep.py:25`).
2. 대상 선정(읽기): `lifecycle_state='proposed'`, `action='place'`, `order_type='limit'`, nonce 없음·미사용, 모든 루프가 `pending_approval`/`needs_reconfirm`, `approval_dispatch_state ∈ {NULL, failed, partial_failed}`이고 실패 코드가 `DEFER_SESSION_CLOSED/`·`NO_EXECUTABLE_WINDOW/`·`CALENDAR_UNKNOWN/`로 시작하거나 NULL인 것. 그 밖의 `failed`(텔레그램 실패 등)는 대상이 아니다(기존 수동 판단).
3. 대상마다 기존 `validate_proposal_redispatch`(`redispatch.py:65`)를 그대로 부른다. 이것이 개장 시점 재확인이다: 현재 승인 창(`evaluate_approval_window_boundary`), 신선한 `dry_run` 미리보기, 가격·수량 일치, 시장 교차 검사. 통과한 대상만 `send_proposal_for_approval(..., redispatch=True)`로 **수동 승인 카드**를 보낸다. 이 함수는 브로커에 닿지 않는다(`mechanical_loss_cut/live.py:271` 주석, `dispatch.py:707`~`:945`).
4. 통과하지 못한 대상은 닫힌 사유 코드(`redispatch_price_changed`, `redispatch_price_crossed_market`, 창 코드 등)와 함께 보고만 한다. 자동으로 가격을 고치거나 새 제안을 만들지 않는다. 가격 재결정은 제안자의 판단이고, 정책이 필요하다(§7 Q3).
5. 자동승인 경로는 이 레버가 열지 않는다: 카드는 항상 수동이다. 자동승인 제안이 개장 때 자동 제출까지 이어지려면 `dispatch_proposal`을 부르는 별도 결정이 필요하고, 그것은 주문 경로 변경이다(§7 Q2).

#### 3.1.3 실행 시각과 스케줄러(승인 필요)

기존 Dagu 패턴(`deploy/dagu/at-batch2/*.yaml`)을 따라 제안 사본 `deploy/dagu/at-batch3/at-order-proposal-session-recheck.yaml`을 둔다. 장소·이미지·env는 night-sweep DAG와 같다. 권장 시각(각 개장 5분 뒤, 시각은 DAG 안의 `CRON_TZ`로 표기):

| 시장 | 시각 | 이유 |
|---|---|---|
| KR 정규장 | 월–금 09:05 KST | 개장 직후 |
| KR NXT/KRX 애프터 | 월–금 16:05 KST | 15:30 NXT 연장 뒤, KRX 애프터 16:00 개장 직후(§3.2) |
| US 정규장 | 월–금 09:35 America/New_York | DST를 `CRON_TZ`로 처리, KST 고정값은 쓰지 않는다 |

**승인이 필요한 것**(AGENTS.md 하드룰 6, 이 PR은 아무것도 등록하지 않는다): (1) Dagu DAG 등록, (2) `ORDER_PROPOSAL_SESSION_RECHECK_ENABLED` 운영 설정, (3) 이미 있는 야간 스윕 DAG 등록과 두 플래그(§2.2)를 켤 것인가의 결정. 등록 전에는 기존 `order_proposal_redispatch`를 개장 직후 사람이 부르는 것이 유일한 경로다. 각 개장 5분이 늦다면 KR은 08:55(NXT 08:00은 NXT 전용이라 대상이 아니다)처럼 조정은 Q1.

### 3.2 (b) KR: 정규장과 NXT/KRX 애프터의 가격·유효시간 분리

문제: 가격·유효기간이 세션과 무관한 값 하나다. `get_quote`는 NXT 세션에서 `price`를 NXT 호가 값으로 덮고 `price_source`·`session_state`·`regular_session_data_state`를 붙이지만(`app/mcp_server/tooling/market_data_quotes.py:504`~`:561`), 같은 응답의 `high`는 일봉 값이다(`:885`, `:939`). 토스 미리보기의 `current_price`는 `prices().last_price` 하나이며 세션·거래소 표지가 없다(`app/mcp_server/tooling/orders_toss_variants.py:548`~`:558`). 제안·판단 근거에는 가격의 세션을 담는 필드가 없다(`app/models/order_proposals.py:106`~`:203`, `source_asof` 자유 JSONB뿐).

설계:

1. **`target_session` 기록(마이그레이션 없음)**: 생성 시 서버가 `source_asof.target_session`을 찍는다. 값 `{market, session, session_date, window_start, window_end}`, 세션은 닫힌 어휘 `kr_regular`·`kr_nxt_after`·`kr_krx_after`·`us_regular`(필요 시 `us_pre`·`us_post`, 기존 `toss_live_us_sessions` 키의 범위 안에서만). 호출자가 넣은 값은 `creator_agent_id`처럼 먼저 지우고 서버 값으로 덮는다(`service.py:916`~`:922`). 입력은 선택 인자 `target_session`(없으면 생성 시각의 현재 세션, 닫혀 있으면 다음 개장 세션).
2. **기본 `valid_until`을 세션 끝에 묶는다**: `target_session.window_end`(KR 정규장 15:30, NXT 애프터 20:00, KRX 애프터 20:00 — 기존 상수 `live_order_expiry`의 값과 같은 표, `app/services/brokers/kis/live_order_expiry.py:103`~`:186`)로 하되, 호출자가 더 짧은 값을 주면 짧은 쪽이다. 지금처럼 자정까지 두지 않는다. 방어 청산(`DEFENSIVE_EXIT_INTENTS`)의 기존 하한(`service.py:1028`)은 그대로 둔다.
3. **창 평가는 거부만 추가한다**: `evaluate_approval_window`는 `target_session`이 있으면 그 세션 창 안에서만 `ALLOW`, 현재가 다른 세션이면 기존 `DEFER_SESSION_CLOSED`/`NO_EXECUTABLE_WINDOW`로 돌려보낸다. `target_session`이 없는 기존 행은 지금과 같다. 어떤 경우에도 지금 `ALLOW`가 아닌 것을 `ALLOW`로 바꾸지 않는다.
4. **가격 기준을 같은 세션에서 잡는다**: `source_asof.price_reference = {session, venue, price, price_basis(last|best_bid|best_ask|expected), observed_at}`. 재확인 레버와 카드 문구는 이 기준가와 현재 같은 세션의 기준가를 나란히 보인다. `target_session`이 NXT/KRX 애프터인데 기준가가 정규장 값이면 카드에 `price_reference_session_mismatch` 경고를 단다(차단이 아니라 표시 — 임계값은 정책이라 §7 Q4).
5. **일봉 `high`는 체결 가능 증거가 아니다**: `high`·정규장 종가는 기준가 후보에서 제외한다(`price_basis`가 닫힌 어휘이므로 `day_high`는 값으로 못 들어온다). 자동승인 가격 폴백(`app/services/order_proposals/auto_approve_price_fallback.py:99`~`:125`)은 이미 NXT 세션에서 5분 이내 NXT 호가만 인정하므로 변경하지 않는다. 이 폴백은 `target_session`과 무관하게 현재 세션의 값만 받는다는 점만 테스트로 고정한다.
6. **캘린더 불일치(§2.3)**: 두 갈래다. (i) 수능일처럼 거래소가 정규장 시각을 바꾸는 날은 저장소 안 **특별 세션 표**(날짜 → 개장·마감, 운영자가 KRX 공지로 매년 PR로 갱신)를 `session_calendar`에 두고, 표가 있는 날은 XKRX 값을 표 값으로 대체한다. (ii) 토스 캘린더와 어긋나면 계속 `CALENDAR_UNKNOWN`이다(막는 쪽 유지). 표를 넣으면 15:30–16:30 같은 구간이 풀리므로 **fail-closed를 일부 푸는 변경**이다. 하드룰 4의 "fail-closed→fail-open 전환 금지"에 닿는지 운영자 확인이 필요하다(§7 Q5). 승인되지 않으면 (i)는 하지 않고 "수능일은 토스·XKRX가 둘 다 허용하는 구간만 통과"를 유지한다.

### 3.3 (c) US: 개장 전 제안이 겨냥하는 세션을 지닌다

- 같은 `target_session`을 쓴다. 08:39 ET에 만든 제안은 `us_regular`/`session_date=그날`로 찍히고, `valid_until`은 그 정규장 마감(US 16:00 ET, `regular_session_bounds`, `session_calendar.py:82`)이다. KST 자정이 아니다.
- 이 시각은 판정 코드가 이미 쓰는 두 소스를 그대로 쓴다: 토스 정규장 창(`approval_window.py:364`~`:403`)과 XNYS 달력(`approval_window.py:782`~`:822`). DST는 `America/New_York`이 처리하므로 EST/EDT 어느 쪽이든 변하지 않는다.
- 개장 전(DEFER)은 §3.1의 레버가 09:35 ET에 다시 확인한다. 그 사이 `valid_until`이 다음 정규장 개장 전이면 기존 `NO_EXECUTABLE_WINDOW`가 막는다(`approval_window.py:1151`~`:1159`).
- Toss US의 pre/post는 기존 정책 키(`order_proposals.approval_window.toss_live_us_sessions`, 기본 `[regular]`)가 정한다. 이 설계는 그 키를 켜지도 읽는 방식을 바꾸지도 않는다.

### 3.4 바꾸지 않는 것

nonce 단일 소비, 승인 해시, 손실매도 가드·쿨다운, `exit_intent` 면제(`approval_window.py:218`~`:263`), 주문 한도·자동승인 캡, W5 durable inbox 게이트, 승인 창 정책 stamp, 브로커 전송 경계. 새 코드는 카드 발송과 기록·표시·거부만 더한다.

## 4. 검증 계획(구현 PR이 따라야 할 것)

| 주장 | 이전 head에서 실패해야 하는 테스트 |
|---|---|
| 자동승인 + 장외 생성이 `failed`/`DEFER_*`를 남긴다 | `test_dispatch.py`에 `ORDER_PROPOSALS_AUTO_APPROVE=True`용 복제본(현재 수동용 `:365`만 있다) |
| 재확인 레버가 DEFER 제안에 수동 카드만 보내고 브로커 호출 0 | 브로커 함수를 트랩으로 둔 레버 테스트 |
| 통과 못 한 제안은 가격을 바꾸지 않고 사유만 낸다 | `redispatch_price_changed`·`..._crossed_market` 그대로 |
| 기본 `valid_until`이 세션 끝이다 | KR 16:08 NXT → 20:00, US 08:39 ET → 16:00 ET, 방어 청산 하한 유지 |
| `target_session`이 없는 행은 평가가 바이트 동일 | 기존 `test_approval_window*.py` 전부 |
| 호출자가 준 `target_session`은 서버가 덮는다 | `creator_agent_id` 사례 테스트와 같은 모양 |
| 특별 세션 표(승인 시) | 11-19 09:30·15:45·16:35 세 시각 |

DB 왕복 테스트는 CI에서만 돈다.

## 5. 운영 점검 목록(`[실측 필요]` 모음)

운영자가 읽기 전용으로 확인할 항목이며, 이 PR은 어느 것도 실행하지 않았다.

1. 1623·1624·1625·1626: `lifecycle_state`, `approval_dispatch_state`/`failure_code`, `source_asof->'approval_dispatch_alert'`, 루프 `void_reason`·`updated_at`, `valid_until`.
2. 1629·1630: `valid_until`, `lifecycle_state`, `created_at`.
3. 운영 env: `ORDER_PROPOSALS_AUTO_APPROVE`(와 `_MODE`), `ORDER_PROPOSAL_NIGHT_SWEEP_ENABLED`, `ORDER_PROPOSAL_NIGHT_SWEEP_SCHEDULE_ENABLED`, `ORDER_PROPOSAL_EXPIRE_SWEEP_ENABLED`.
4. Dagu에 `at-batch2-order-proposal-night-sweep`가 등록돼 있는지와 최근 실행 결과(`skipped` 여부).
5. 11-19 에스엠 제안의 차단 코드·`evidence.detail`과 그날 토스 `market_calendar_kr` 응답.

## 6. 티켓 분해

한 PR이 하나의 티켓이다. 등급·티어는 작업 제목 표기(grade, Tn)를 따른다. **T3 = 주문 경로(승인 창·디스패치·재검증 코드)를 건드림 → 운영자 배포 승인 필요.**

| # | 티켓 | 파일 | 등급/티어 | 선행 |
|---|---|---|---|---|
| 1 | `dispatch_proposal` 창 차단을 원장에 기록(수동 경로와 동일) | `dispatch.py`, `test_dispatch.py` | A / T3 | — |
| 2 | 세션 재확인 CLI + 게이트 + 만료 선행 + Dagu 제안 사본 | `scripts/order_proposal_session_recheck.py`, 앱 함수 1개, `deploy/dagu/at-batch3/`, 계약 문서 | B / T2(카드 발송만, 브로커 비접촉을 트랩 테스트로 증명) | 1 |
| 3 | `target_session` 기록 + 세션 끝 기본 `valid_until` + 창 평가의 거부 추가(KR·US) | `service.py`, `approval_window.py`, `order_proposal_tools.py` | A / T3 | 1 |
| 4 | 가격 기준(`price_reference`) 기록·카드 표시·세션 불일치 경고 | `order_proposal_tools.py`, 카드 렌더러, `redispatch.py` | B / T3(재검증 코드 접촉) | 3 |
| 5 | KRX 특별 세션 표(수능일) | `session_calendar.py`, 테스트 | B / T3(`_resolve_kr_session` 입력) — 운영자 확인 Q5 후에만 | 운영 |
| 6 | 운영 활성화(코드 아님): DAG 등록·플래그·야간 스윕 점검 | 운영자 호스트 | — / 운영자 | 2 |

- 1·3·5는 주문 경로를 건드리므로 T3이고 운영자 배포 승인 없이 배포하지 않는다. 2는 코드상 카드 발송과 기록만이지만, 레버가 `send_proposal_for_approval`을 부르므로 PR 본문에 브로커 비접촉 테스트 출력을 싣는다.
- 순서: 1 → (2, 3 병행 가능) → 4. 5는 Q5 답이 오면 별도.
- 이 문서의 후속 hk 작업은 각 티켓별로 만든다. 이 PR은 문서만이다.

## 7. 운영자 결정이 필요한 질문

| Q | 질문 | 기본값 |
|---|---|---|
| Q1 | 재확인 시각: KR 09:05·16:05 KST, US 09:35 ET로 충분한가, 더 이르게(예: KR 08:55) | 개장 5분 뒤 |
| Q2 | 자동승인 제안이 개장 때 자동 제출까지 이어지게 할까(주문 경로, T3) 아니면 수동 카드만 | 수동 카드만 |
| Q3 | 재확인에서 가격이 바뀐 제안을 자동으로 새 가격으로 다시 제안할까 | 하지 않음(제안자 몫) |
| Q4 | 기준가 세션 불일치를 경고로만 둘까, 임계값을 정해 막을까 | 경고만 |
| Q5 | 수능일 등 특별 세션 표를 넣어 15:30–16:30 같은 구간을 허용해도 되는가(fail-closed 일부 완화) | 넣지 않음 |
| Q6 | 야간 스윕 DAG 등록·플래그 두 개를 켜서 기본 만료 청소를 돌릴까 | 켠다 |

## 8. 건드리지 않은 것

코드, 테스트, 마이그레이션, DB, 브로커, 스케줄러, env, 정책 YAML, 프롬프트. `.venv`는 로컬 확인(`exchange_calendars`)용으로 이 작업트리에서만 만들었고 커밋하지 않는다.
