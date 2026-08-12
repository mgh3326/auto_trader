# order_proposal_void 권한 계약 (ROB-1238)

`order_proposal_void` 는 **전 lane 에서 blocked** 였다. 그래서 세션이 죽은 제안을 하나도
정리하지 못했고, 유령 proposed/resting 제안이 **07-27 8건 → 08-10 27건** 으로 누적됐다.
그중 BSX 제안 `dd7a68d7` 은 loss guard 위반가(평단×1.01 = 48.48 인데 48.40)인데도
`proposed` 로 3주를 생존했고 **텔레그램 승인 버튼이 살아 있었다**.

이 문서는 그 수리의 계약이다. 🔴 **수리는 "void 전면 개방"이 아니다.**

## 1. 왜 전 lane 이 막혔나 (근본 원인)

`order_proposal_void` ∈ `PROPOSAL_LIFECYCLE_TOOLS` → `MUTATION_TOOLS` 에 union 된다.
그런데 `build_route_plan` 의 `allowed_candidates` 어디에도 들어 있지 않았다:

```
blocked = (MUTATION_TOOLS & registered_tools) - allowed
```

→ 모든 lane 의 `blocked_actions` 에 등장. 수렴 경로가 아예 없었다.

## 2. 무엇을 열었나 — 표면 vs 권한

두 층을 분리해서 읽어야 한다.

| 층 | 파일 | 역할 |
|---|---|---|
| **표면** | `route_request_lanes.LANE_PROPOSAL_LIFECYCLE_ALLOWED` | buy/sell(proposal-led) lane 에서만 도구 노출. discovery/bootstrap 은 여전히 blocked |
| **권한** | `order_proposals/void_authorization.py` + `service.void_proposal` | 🔴 **무엇을 void 할 수 있는지의 fail-closed 판정** |

🔴 **표면을 넓혀도 권한은 넓어지지 않는다.** `LANE_PROPOSAL_LIFECYCLE_ALLOWED` 를 전 lane 으로
바꿔도 남의 lane 제안은 여전히 못 건드린다. 권한은 서비스 레이어가 단독으로 판정한다.

## 3. 권한 3종 (이외 전부 거부)

`authorize_void` 는 순수 함수(stdlib only, DB/네트워크/시계 없음)다. 우선순위 순:

| authority | 조건 | 종착 상태 |
|---|---|---|
| `self_created` | 요청자 == 제안 생성자 | `voided` |
| `server_loss_guard_invalid` | 서버 자신의 revalidation 이 loss-sell guard 위반을 관측·기록 | `voided` |
| `server_expired` | `now >= valid_until` (`valid_until_block` 과 동일 규칙) | 🔴 `expired` |

`server_expired` 가 `voided` 가 아니라 **`expired`** 로 가는 이유: 서버가 증명한 것은 만료다.
`voided` 로 기록하면 죽은 이유를 오보한다.

**그 외 = `void_not_authorized`.** 남의 lane 의 **살아 있는** 제안은 불가침이다.

### 소유권은 위조 불가

- 생성자 id 는 `source_asof["creator_agent_id"]` 에 **서버가** 기록한다. 출처는
  `CallerIdentityMiddleware` 가 푼 MCP caller identity 이며, **도구 인자가 아니다**.
- `create_proposal` 은 호출자가 넘긴 `source_asof` 의 `creator_agent_id` 를 **먼저 제거**한 뒤
  서버 값을 넣는다 → pre-seed 위조 차단.
- 🔴 `proposer` 텍스트 필드는 소유권 근거가 **아니다** (자유 입력).
- 요청자 id 가 `None` 이면 self 권한 없음. 생성자 id 가 `None`(유령 27건 등 legacy 행)이어도
  `None == None` 로 소유권이 성립하지 않는다.

### loss guard 판정은 재구현이 아니다

임계값(평단×1.01)은 `order_validation.evaluate_limit_sell_price_guard` /
`evaluate_market_sell_loss_guard` 단일 소스에 남아 있다. ROB-1238 은 **그 결과를 기록만** 한다:
revalidation 이 preview 실패를 loss-guard 로 분류하면
`service.record_loss_guard_verdict` 가 `source_asof["loss_guard_verdict"]` 에
`{violated: true, source: "server_revalidation", ...}` 를 남긴다.

🔴 `extract_loss_guard_violation` 은 `violated is True` **그리고** `source ==
"server_revalidation"` 일 때만 인정한다. 호출자가 주입한 payload·`"yes"` 같은 truthy 값은
전부 무시(fail-closed).

🔴 잔고부족·opposite-pending·stop-loss cooldown 은 guard block 이지만 **loss guard 가 아니다**
(`_LOSS_GUARD_ERROR_MARKERS` 는 `_GUARD_ERROR_MARKERS` 보다 좁다). 계좌 상태이지 제안 자체의
무효 증거가 아니므로 void 권한을 주지 않는다.

## 4. lazy 설계 — 🔴 스케줄러 0

수렴은 **조회·dispatch 시점 판정**으로만 이뤄진다. cron·launchd·TaskIQ·Prefect·APScheduler
**등록이 없다**.

- 만료 제안은 누군가 `order_proposal_void` 를 호출하는 순간 `server_expired` 권한으로 `expired` 전이.
- dispatch 시점에는 `assert_dispatch_still_authorized` 가 판정.
- 기존 `app/tasks/order_proposal_expiry_tasks.py`(ROB-897)는 **그대로 scheduleless** — 이 PR 은
  건드리지 않았다.

`tests/services/order_proposals/test_void_authorization.py::test_no_scheduler_registration_in_lifecycle_modules`
가 4개 lifecycle 모듈의 import 를 AST 로 스캔해 이를 고정한다.

## 5. dispatch 직전 원자적 재검증

`revalidate_and_submit` 은 group 을 **한 번** 읽고, 그 스냅샷으로 rung 별 preview·eligibility·
buying-power·calendar I/O 를 전부 await 한다. 그 사이에 다른 세션의 `order_proposal_void`
(별도 session·별도 트랜잭션)가 끼어들 수 있고, 기존 코드는 그걸 **보지 못한 채** 전송했다.

`_pre_submit_lifecycle_gate` 를 마지막 window gate 와 submit 사이에 넣었다:

```
… preview → eligibility → buying power → window gate
  → [assert_dispatch_still_authorized: SELECT … FOR UPDATE]   ← 여기
  → approved → submitting → 브로커 전송
```

🔴 **원자성 근거**: `assert_dispatch_still_authorized` 는 행을 `FOR UPDATE` 로 다시 읽고,
그 락은 **트랜잭션 끝까지**(= submit 을 넘어서) 유지된다. 판정과 전송 사이에 다른 writer 가
끼어들 수 없다. "최근에 확인했다"가 아니라 "확인 이후 바뀔 수 없다"이다.

재검증 항목 4종 (하나라도 걸리면 fail-closed):

1. `proposal_approval_block_reason` — voided/expired/superseded/terminal
2. `no_resubmit`
3. `valid_until` — 🔴 **새 규칙을 만들지 않고** 공용 `valid_until_block` 재사용
4. 소유권 = 승인 토큰 동일성 (`approval_nonce`)
5. 서버 확정 loss-guard 위반

🔴 **거부 시 승인 토큰 무효화**: `approval_nonce=None`, `approval_nonce_used_at=now`.
그래서 낡은 텔레그램 카드를 다시 눌러도 재시도가 되지 않는다. `void_proposal` 도 동일하게
nonce 를 지운다 → 제안이 죽으면 승인 버튼도 같이 죽는다.

## 6. 운영 절차

### 유령 제안 정리

```
order_proposal_list(lifecycle_state="proposed")     # read-only, 후보 확인
order_proposal_void(proposal_id=..., reason="phantom cleanup")
```

- 만료분은 `expired` 로 수렴하고 `void_reason` 에 `[authority=server_expired]` 가 남는다.
- 살아 있는 남의 lane 제안은 `success=false, error="void_not_authorized"` +
  `authorization.detail`(고려된 증거)로 거부된다. 🔴 **우회 금지** — 소유 세션에 릴레이하라.

### BSX 류(loss guard 위반) 정리

승인 탭이 한 번이라도 revalidation 을 돌렸다면 verdict 가 기록되어 즉시 void 가능하다.
아직 아니라면 `valid_until` 경과를 기다리거나 소유 세션이 직접 void 한다.
🔴 dispatch 는 verdict 기록 시점부터 이미 fail-closed 이므로 **오탭해도 주문은 안 나간다**.

## 7. 불변

- 🔴 브로커/계좌 mutation **0** — 이 경로는 로컬 DB 상태 전이만 한다.
  `void_proposal` 의 `unverified` 분기는 기존 broker-absence **read** 증거 규약을 그대로 유지한다.
- 🔴 스케줄러 등록 **0**.
- `scripts/b0x/**` 무접촉. B0-X 관측 수치 불변.
- migration **0** — `source_asof`(기존 JSONB) 만 사용, 신규 컬럼 없음.

## 8. 감수한 구멍 (정직하게)

- 🔴 `valid_until` 이 `None` 인 행은 `server_expired` 로 수렴하지 않는다. 마감이 없다는 사실은
  마감이 지났다는 증거가 아니다. 그런 행은 소유 세션만 정리할 수 있다.
- 🔴 legacy 유령 행에는 `creator_agent_id` 가 없다. 만료됐다면 누구나 정리할 수 있지만,
  만료도 안 됐고 loss-guard 위반도 아니면 **아무도 못 지운다**(설계상 의도 — 살아 있을지도
  모르는 남의 제안을 함부로 죽이지 않는다).
- 🔴 `_pre_submit_lifecycle_gate` 는 place 경로에 배선돼 있다. cancel/replace 경로는 브로커
  target 대조(`acquire_target_mutation_lock` + target snapshot)로 방어되지만 동일 gate 는
  타지 않는다 — 후속 작업.
- 🔴 caller identity 는 HTTP 헤더(`x-paperclip-agent-id`) 또는 env fallback 이다. 같은
  fallback 을 공유하는 두 세션은 서로를 소유자로 본다. 세션 분리가 필요하면 헤더를 세션별로
  줘야 한다.
