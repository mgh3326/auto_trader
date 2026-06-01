# ROB-403 — watch_condition zone/다중조건 + max_action 스키마

- **이슈**: ROB-403 (G2) — `watch_condition` zone(between)·다중임계 + `max_action(qty/limit_price)` 스키마
- **부모 에픽**: ROB-401 (모의 자율매매 루프), 오케스트레이션 트래커 ROB-410 Wave 1
- **작성일**: 2026-06-01
- **상태**: 설계 승인됨 → 구현 계획 수립 단계

## 1. 배경 / 문제

현재 watch는 단일 임계치만 표현한다(`metric` + `operator(above|below)` + `threshold`). 자율 루프의 매수존/매도존("가격이 A~B 구간에 들어오면") 이나 다중메트릭 룰("가격이 존 안 + RSI<35")을 담을 수 없다. 또한 `max_action` JSONB 컬럼은 이미 존재하나 free-form이고 스키마/검증이 없어 ROB-402 자동집행이 주문 파라미터로 신뢰하고 쓸 수 없다.

### 현 상태 (코드 매핑)

- **모델** `app/models/investment_reports.py`:
  - `InvestmentWatchAlert` (439–555): `metric`, `operator`, `threshold`, `threshold_key`, `action_mode`, `max_action`(JSONB dict, default `{}` — 존재), `trigger_checklist`(JSONB list), `valid_until`, `status`. CHECK: `operator IN ('above','below')`. **account_mode 컬럼 없음** (계좌 컨텍스트는 리포트-레벨 `account_scope`).
  - `InvestmentReportItem`: `watch_condition`(JSONB nullable), `max_action`(JSONB dict). CHECK `ck_investment_report_items_watch_has_condition`.
- **스키마** `app/schemas/investment_reports.py`:
  - `WatchConditionPayload` (72–91): `metric/operator/threshold/threshold_key/target_kind/action_mode` — 단일 임계.
- **스캐너**:
  - `app/jobs/watch_market_data.py::is_triggered` (201–208): 단일 threshold above/below 이진 비교.
  - `get_current_value` (170–198): target_kind×metric 디스패치(asset/index/fx × price/rsi/trade_value).
  - `app/jobs/investment_watch_scanner.py` (153–155): `is_triggered(current, alert.operator, alert.threshold)`.
- **활성화** `watch_activation.py` (79–96): item.watch_condition dict에서 metric/operator/threshold 추출 → alert 컬럼에 기록.
- **max_action 소비처**: `mock_preview/bridge.py`가 `notional_usd`/`notional_cap_usd`만 읽음.
- **마이그레이션 패턴**: additive 컬럼 + CHECK drop(hashed+canonical)+recreate. 최근: `20260520_rob274_p1`.

## 2. 목표 / 비목표

**목표**
- `watch_condition`이 zone(between)·다중메트릭 AND를 표현.
- 스캐너가 zone/다중조건을 평가.
- `max_action`을 구조화(`side`, `quantity|notional`, `limit_price`, `account_mode`)하고 검증 → ROB-402가 주문 파라미터로 사용.
- 기존 단일임계 watch / 기존 max_action 키(`notional_usd`) 무손상(back-compat).

**비목표 (범위 제외)**
- ROB-393 (review-op watch activate 불가) — 별도 이슈.
- ROB-402 자동집행 배선 + (auto_execute_mock+live) 차단 가드 — 별도 이슈(403은 순수 스키마/평가).
- `combine="or"` — 필드는 예약하되 미구현(YAGNI).
- `valid_until` 자동만료 — ROB-406에서 미룸.

## 3. 설계: 조건 표현 = JSONB conditions 배열

### 3.1 watch_condition v2 (Pydantic + JSONB)

```jsonc
{
  "conditions": [
    {"metric": "price", "op": "below",   "threshold": "55000"},
    {"metric": "rsi",   "op": "below",   "threshold": "35"}
  ],
  "combine": "and",
  "target_kind": "asset",
  "action_mode": "notify_only"
}
```

- **조건 단위** (`WatchConditionClause`):
  - above/below: `{metric, op, threshold}`
  - between(zone): `{metric, op: "between", low, high}` (`low <= high` 검증)
- `op ∈ {above, below, between}`, `metric ∈ {price, rsi, trade_value}`.
- `combine ∈ {and}` (현재 and만; or는 reject).
- `conditions`는 최소 1개.
- **back-compat 정규화**: 입력이 구형 flat `{metric, operator, threshold, threshold_key?, target_kind?, action_mode?}` 이면 ingest 단계에서 `conditions=[{metric, op:operator, threshold}]` 단일조건으로 정규화. 신규/구형 페이로드 모두 동일 내부 표현으로 수렴.

### 3.2 Alert 영속 (investment_watch_alerts, additive)

신규 컬럼:
- `conditions` JSONB, default `[]` — 평가 canonical (정규화된 조건 배열).
- `combine` Text, default `'and'`, CHECK `combine IN ('and')`.
- `threshold_high` Numeric(20,8) nullable — primary 조건이 between일 때 high.

flat `metric/operator/threshold/threshold_key`는 **primary(첫) 조건의 요약**으로 계속 채운다(대시보드/dedup back-compat):
- primary가 above/below: `operator`=op, `threshold`=threshold, `threshold_high`=NULL.
- primary가 between: `operator`='between', `threshold`=low, `threshold_high`=high.
- 따라서 `operator` CHECK를 `IN ('above','below','between')`로 확장.
- `threshold_key`는 신규 alert에서 전체 conditions의 결정적 문자열로 파생(같은날 재발화 dedup 유지).

### 3.3 스캐너 평가

`app/jobs/watch_market_data.py`:
- 신규 `evaluate_clause(current: float | None, clause: dict) -> bool`:
  - `current is None` → False.
  - above: `current > threshold`; below: `current < threshold`; between: `low <= current <= high`.
- 신규 `evaluate_conditions(clauses, combine, fetch_value) -> bool`:
  - 각 clause의 `metric`으로 `fetch_value(metric)` 호출(현 `get_current_value`가 target_kind×metric 디스패치; clause는 alert의 target_kind/symbol/market 공유, metric만 다름).
  - `combine='and'` → 모든 clause 참이어야 True.

`app/jobs/investment_watch_scanner.py`:
- alert에 `conditions`가 있으면 `evaluate_conditions` 사용; 비어있으면(legacy) 기존 `is_triggered(current, operator, threshold)` fallback.

### 3.4 max_action 스키마 (MaxActionPayload — 기존 JSONB 컬럼, 마이그레이션 불요)

```jsonc
{ "side": "buy", "quantity": "10", "notional": null,
  "limit_price": "55000", "account_mode": "kis_mock" }
```

- `side: Literal["buy","sell"]` — 필수.
- `quantity: Decimal | None`, `notional: Decimal | None` — **정확히 하나** 필수(XOR).
- `limit_price: Decimal | None` — None 허용(ROB-402가 주문타입 결정).
- `account_mode: AccountMode` — ROB-100 `app.schemas.execution_contracts.AccountMode`(`kis_live|kis_mock|alpaca_paper|db_simulated`) 재사용, **비제한**.
- Pydantic `model_config = ConfigDict(extra="allow")` → 기존 `notional_usd`/`notional_cap_usd` 등 키 무손상.
- 검증 시점: `item_kind="watch"` + `operation ∈ {create, modify}` + `max_action` 비어있지 않을 때. 비어있으면(`{}`) 검증 skip(선택).
- **live 자동집행 차단은 ROB-402가 (action_mode=auto_execute_mock + account_mode=live)→reject로 전담.** 403은 account_mode를 타입만 강제.

## 4. 컴포넌트 / 인터페이스

| 단위 | 위치 | 책임 |
|---|---|---|
| `WatchConditionClause` | `app/schemas/investment_reports.py` | 단일 조건 절(above/below/between) Pydantic 모델 + between low<=high 검증 |
| `WatchConditionPayload` v2 | `app/schemas/investment_reports.py` | `conditions[]`+`combine`+target_kind+action_mode. 구형 flat 입력 정규화(validator) |
| `MaxActionPayload` | `app/schemas/investment_reports.py` | side/quantity|notional(XOR)/limit_price/account_mode, extra=allow |
| Alert 컬럼 | `app/models/investment_reports.py` | `conditions`/`combine`/`threshold_high` 추가 + operator CHECK 확장 |
| Event 컬럼 | `app/models/investment_reports.py` | `InvestmentWatchEvent`: operator CHECK 확장(between) + `threshold_high` 추가 |
| Hermes payload | `app/services/hermes_client.py` | `ReviewTriggerPayload.operator`→between + `threshold_high` 필드 |
| 스캐너 emission | `app/jobs/investment_watch_scanner.py` | `insert_event`/payload에 threshold_high 전달 |
| 활성화 매핑 | `app/services/.../watch_activation.py` | watch_condition→alert: conditions/combine/threshold_high + flat primary 요약 + threshold_key 파생 |
| 조건 평가 | `app/jobs/watch_market_data.py` | `evaluate_clause` + `evaluate_conditions` |
| 스캐너 분기 | `app/jobs/investment_watch_scanner.py` | conditions 있으면 evaluate_conditions, 없으면 flat fallback |
| 마이그레이션 | `alembic/versions/<rev>_rob403_*.py` | 3 컬럼 + operator/combine CHECK |
| conftest drift | `tests/conftest.py` | 영속 테스트 DB operator CHECK + 신규 컬럼 패치 |

## 5. 데이터 모델 변경

`investment_watch_alerts` (additive):
- `conditions` JSONB NOT NULL default `'[]'::jsonb`
- `combine` TEXT NOT NULL default `'and'` + CHECK `combine IN ('and')`
- `threshold_high` NUMERIC(20,8) NULL
- `operator` CHECK 재작성: `operator IN ('above','below','between')`

`investment_report_items.watch_condition` (JSONB)는 schemaless라 컬럼 변경 없음 — v2 페이로드를 그대로 저장. `max_action` 컬럼도 기존 그대로(스키마는 Pydantic 레이어).

**between operator 전파 체인 (계획 단계 발견 — 필수)**: `operator='between'`은 alert→event→Hermes payload로 흐른다. 두 곳이 above/below로 제약돼 있어 추가 widening이 필요하다:
- `investment_watch_events.operator` CHECK(`ck_investment_watch_events_operator`)를 `IN ('above','below','between')`로 확장 + `threshold_high` Numeric(20,8) nullable 컬럼 추가(상위 bound가 이벤트/알림에서 유실되지 않도록). 마이그레이션 + conftest×2.
- `app/services/hermes_client.py::ReviewTriggerPayload.operator`를 `WatchClauseOpLiteral`(above|below|between)로 확장 + `threshold_high: Decimal | None = None` 필드 추가.
- 스캐너 emission(`_upsert_event` → `insert_event` → `ReviewTriggerPayload`)이 alert.threshold_high를 event/payload로 전달.
- **다중메트릭 AND의 알림 fidelity**: event/payload flat 요약은 primary 절만 담는다(트리거 정확성과 무관). 전체 conditions를 알림에 싣는 것은 후속(Hermes-side).

## 6. 안전 경계

- 순수 스키마/평가 작업. broker/order/watch-activation **mutation 추가 없음**(활성화 매핑은 기존 쓰기 경로 확장).
- live 자동집행 가드는 ROB-402 책임(403은 account_mode 타입만 강제).
- 마이그레이션 포함, operator `alembic upgrade head` 별도 실행.
- back-compat: 구형 flat watch_condition + 기존 max_action 키 무손상.

## 7. 테스트

1. 구형 flat watch_condition → 단일조건 배열 정규화(`conditions` len 1).
2. between clause: `low<=cur<=high`이면 trigger, 벗어나면 미trigger; `low>high`는 schema reject.
3. 다중메트릭 AND: price-between + rsi-below 둘 다 충족 시만 trigger; 하나만이면 미trigger.
4. legacy alert(conditions 빈 배열, flat만) → 기존 flat fallback로 trigger.
5. `evaluate_clause` 단위: above/below/between + None→False.
6. `MaxActionPayload`: side 필수; quantity·notional XOR(둘 다/둘 다 없음→reject); account_mode typed; extra 키(`notional_usd`) 보존.
7. 활성화 매핑: v2 payload→alert.conditions/combine/threshold_high + flat primary 요약 + operator='between' 허용.
8. 마이그레이션/모델: `operator='between'`·`combine='and'` 허용, 기존 값 유지; `conditions`/`threshold_high` 존재.
9. 회귀: 기존 스캐너 트리거 테스트 + activation corrupt-state 테스트 무손상.

## 8. 미해결 / 후속

- ROB-402가 max_action을 실제 주문 파라미터로 사용 + (auto_execute_mock+live) reject.
- ROB-393 review-op activate 계약(별도).
- `combine="or"`, 추가 metric, `valid_until` — 후속.
