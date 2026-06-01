# ROB-402 — watch `auto_execute_mock` → kis_mock 자동집행 (live 영구차단)

- **이슈**: ROB-402 (G1) — watch `auto_execute_mock` action_mode + 트리거 → kis_mock 자동집행 배선
- **부모 에픽**: ROB-401 (모의 자율매매 루프), 오케스트레이션 트래커 ROB-410 Wave 2
- **작성일**: 2026-06-01
- **상태**: 설계 승인됨 → 구현 계획 수립 단계

## 0. 의존 / 머지 순서 (중요)

ROB-402는 **403의 `MaxActionPayload`(PR #1075)** 에 코드 의존한다(주문 파라미터 소스). 구현은 **#1075 머지 후 origin/main에 rebase**하여 진행한다. #1072(406 cancel)·#1077(404 reconcile)은 운영상 연계이나 코드 차단은 아니다(루프 완성도 차원). 이 worktree는 origin/main 기준 — 구현 시점에 `MaxActionPayload` 부재면 import 실패하므로 rebase 선행.

## 1. 배경 / 현 상태

watch는 현재 리뷰 전용(Hermes 알림)으로 주문하지 않는다. interim 루프는 운영자가 수동으로 mock 지정가를 걸어 동작 중. ROB-402는 트리거 시 mock 계좌에 **자동 주문**을 배선한다. **live 자동집행은 영구 차단(코드 가드 + 테스트).**

### 코드 매핑

- **action_mode**: `app/schemas/investment_reports.py:59` `WatchActionModeLiteral = Literal["notify_only","preview_only","approval_required"]`. CHECK 2곳: `InvestmentWatchAlert`(models:470 `ck_investment_watch_alerts_action_mode`), `InvestmentWatchEvent`(models:608 `ck_investment_watch_events_action_mode`).
- **스캐너**: `app/jobs/investment_watch_scanner.py` — `_OUTCOME_BY_ACTION_MODE`(56–65) action_mode→outcome 매핑, `_upsert_event`(230) outcome 계산, correlation_id=uuid4().hex(231, 트리거당 신규/같은날 재발화는 idempotency_key로 재사용), event row 생성(insert_event).
- **watch_order_intent_ledger**: 마이그레이션 `daf4130b13ce`로 **테이블만 존재, ORM 없음**. NOT NULL: correlation_id(unique), idempotency_key, market, target_kind, symbol, condition_type, threshold, threshold_key, action, side, account_mode, execution_source, lifecycle_state, preview_line, kst_date. CHECK: `account_mode='kis_mock'`(DB레벨 live차단), `execution_source='watch'`, `lifecycle_state∈{previewed,failed}`, `side∈{buy,sell}`. `execution_allowed` default false, `approval_required` default true, `blocking_reasons` default []. partial-unique idempotency_key WHERE lifecycle='previewed'.
- **kis_mock_place_order**: `app/mcp_server/tooling/orders_kis_variants.py` → `order_execution._place_order_impl(symbol, side, order_type, quantity, price, dry_run, ..., is_mock=True)`. mock이면 `_record_kis_mock_order`→`_save_kis_mock_order_ledger`(이미 `correlation_id` 파라미터 보유). **`_place_order_impl`/`_record_kis_mock_order`는 correlation_id 미전달** → ROB-402가 스레딩.
- **MaxActionPayload**(403, #1075): `side: buy|sell`, `quantity: Decimal|None`, `notional: Decimal|None`, `limit_price: Decimal|None`, `account_mode: AccountMode`, `extra="allow"`, quantity·notional XOR.
- **live 차단 전례**: `AccountMode` literal(execution_contracts.py), DB CHECK `account_mode='kis_mock'`(intent ledger·mock ledger), kis_live advisory-only CHECK(investment_reports).

## 2. 목표 / 비목표

**목표**
- `auto_execute_mock` action_mode 추가.
- 트리거 + mock 계좌 + 게이트 통과 시 intent 기록 → `kis_mock_place_order` 호출, correlation_id로 watch→order 링크.
- **live 자동집행 영구 차단**(코드 가드 + DB CHECK + 테스트).
- 전부 **default-off inert**(global flag OFF면 실주문 0).

**비목표**
- kiwoom_mock 자동집행(ROB-399 후속) — 가드가 unsupported로 차단.
- notional-sizing / market order — v1은 quantity + limit_price(resting 지정가)만. 그 외 blocking_reason.
- global flag flip + operator live-mock smoke, scanner taskiq schedule 등록.

## 3. 안전 모델 (다층, 기본 inert)

실제 주문은 **아래 전부** 충족 시에만:
1. `WATCH_AUTO_EXECUTE_MOCK_ENABLED`(env, **default False**) — 머지 PR inert.
2. alert `action_mode == "auto_execute_mock"`.
3. `max_action.account_mode == "kis_mock"`. live(`kis_live`/`upbit_live`/…) → **hard reject**(`AutoExecuteLiveBlocked`). 그 외 비-kis_mock(kiwoom_mock 등) → `AutoExecuteUnsupported`.
4. `max_action`에 `side` + `quantity` + `limit_price` 존재.

미충족 → intent row `lifecycle_state='failed'` + `blocking_reasons=[...]` + `blocked_by`(audit), **주문 없음**.

## 4. 컴포넌트 / 인터페이스

| 단위 | 위치 | 책임 |
|---|---|---|
| action_mode 확장 | `app/schemas/investment_reports.py` + `app/models/investment_reports.py` | literal + CHECK×2에 `auto_execute_mock` |
| 마이그레이션 | `alembic/versions/<rev>_rob402_*.py` | action_mode CHECK 2곳 widening |
| conftest×2 | `tests/conftest.py` + `tests/_investment_reports_helpers.py` | action_mode CHECK drift 패치(alert+event) |
| Intent ledger ORM | `app/models/review.py` `WatchOrderIntentLedger` | 마이그레이션 `daf4130b13ce` 미러(create_all 테스트용) |
| Live-block 가드 | `app/services/investment_reports/auto_execute_guard.py` | `assert_auto_execute_account_allowed` + `AutoExecuteLiveBlocked`/`AutoExecuteUnsupported` |
| Auto-execute 서비스 | `app/services/investment_reports/watch_auto_execute.py` | `maybe_auto_execute(db, alert, event)` — 게이트/가드/검증/intent 기록/place_order/correlation_id |
| correlation_id 스레딩 | `order_execution._place_order_impl` + `kis_mock_ledger._record_kis_mock_order` | `correlation_id` 인자 추가 → save(이미 보유) |
| 스캐너 훅 | `app/jobs/investment_watch_scanner.py` | outcome dict + event 생성 후 `maybe_auto_execute` 호출 |
| Config | `app/core/config.py` | `WATCH_AUTO_EXECUTE_MOCK_ENABLED: bool = False` |

## 5. 데이터 흐름

```
스캐너 트리거(action_mode=auto_execute_mock)
  → event row 생성(correlation_id, kst_date)
  → maybe_auto_execute(db, alert, event):
      guard: assert_auto_execute_account_allowed(action_mode, max_action.account_mode)
             # live → AutoExecuteLiveBlocked, 비-kis_mock → AutoExecuteUnsupported
      preconditions: global flag ON? max_action(side+quantity+limit_price) 완비?
      intent row INSERT ON CONFLICT(correlation_id) DO NOTHING:
        - 통과: lifecycle='previewed', execution_allowed=true, blocking_reasons=[]
        - 미충족: lifecycle='failed', execution_allowed=false, blocking_reasons=[...]
      conflict(이미 존재) → skip(멱등)
      통과+inserted → _place_order_impl(symbol, side, quantity, price=limit_price,
                        order_type="limit", dry_run=False, is_mock=True,
                        correlation_id=event.correlation_id)
        → KISMockOrderLedger 기록(correlation_id 링크)
```

intent ledger = 결정/차단 audit(previewed|failed). 실제 주문 = `KISMockOrderLedger`(correlation_id 링크). intent 마이그레이션 추가 없음.

## 6. Live-block 가드 (필수)

```python
_LIVE_ACCOUNT_MODES = frozenset({"kis_live", "upbit_live"})
_AUTO_EXECUTE_ALLOWED = frozenset({"kis_mock"})  # kiwoom_mock = follow-up

def assert_auto_execute_account_allowed(action_mode: str, account_mode: str) -> None:
    if action_mode != "auto_execute_mock":
        return
    if account_mode in _LIVE_ACCOUNT_MODES:
        raise AutoExecuteLiveBlocked(account_mode)
    if account_mode not in _AUTO_EXECUTE_ALLOWED:
        raise AutoExecuteUnsupported(account_mode)
```

defense-in-depth: intent ledger DB CHECK `account_mode='kis_mock'`. **live reject 테스트 필수.**

## 7. 멱등성

- 스캐너가 트리거당 correlation_id 생성(같은날 재발화는 event idempotency_key로 동일 correlation_id 재사용).
- intent ledger `correlation_id` UNIQUE → `INSERT ... ON CONFLICT DO NOTHING`로 동일 트리거 재집행 차단(inserted=False면 place_order skip).

## 8. 안전 경계

- live 자동집행 **영구 차단**(가드 + DB CHECK + 테스트). kis_live/upbit_live 도달 불가.
- **default-off inert**: global flag OFF면 실주문 0(intent failed 기록만).
- broker mutation은 kis_mock 한정. 스케줄러 auto-start 없음(이 PR은 flag로 inert; scanner taskiq schedule은 별개).
- production env/secret 변경 없음.

## 9. 테스트

1. **live 차단**: `assert_auto_execute_account_allowed("auto_execute_mock","kis_live")` → `AutoExecuteLiveBlocked`. 서비스 경로로 live alert → 주문 미호출 + intent failed.
2. kiwoom_mock → `AutoExecuteUnsupported`, 주문 미호출.
3. **global flag OFF**(default) → intent `failed` + `blocking_reasons=['auto_execute_globally_disabled']`, place_order 미호출.
4. **happy path**(flag ON + kis_mock + max_action 완비) → place_order 호출(dry_run=False, is_mock=True, correlation_id 전달) + intent `previewed`.
5. **멱등**: 동일 correlation_id 2회 → intent 1개, place_order 1회.
6. max_action 없음 / quantity 없음 / limit_price 없음 / notional-only → intent failed + 해당 blocking_reason, 주문 미호출.
7. action_mode CHECK: alert·event가 `auto_execute_mock` 허용.
8. correlation_id 스레딩: `_place_order_impl(..., correlation_id="x", is_mock=True)` → KISMockOrderLedger.correlation_id == "x".
9. 스캐너 훅: auto_execute_mock alert 트리거 → `maybe_auto_execute` 호출(서비스 mock으로 검증), 예외는 잡혀 스캔 루프 안 죽음.
10. 회귀: 기존 notify_only/preview_only/approval_required outcome 무변경.

## 10. 미해결 / 후속

- global flag flip + operator live-mock smoke(실 트리거→주문→reconcile 라운드트립, creds 필요).
- kiwoom_mock 자동집행(ROB-399 조회 결함 해소 후).
- notional-sizing(lot/min-notional) + market order.
- ROB-405 회고 배선이 correlation_id 링크된 watch→order→fill 체인 위에 구축.
