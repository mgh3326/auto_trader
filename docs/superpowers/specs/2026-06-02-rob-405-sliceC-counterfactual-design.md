# ROB-405 Slice C — counterfactual (룰 효과 정량화)

- **이슈**: ROB-405 (G4) 회고 배선 — **Slice C (counterfactual)**
- **부모 에픽**: ROB-401, 트래커 ROB-410 Wave 3
- **선행**: Slice A(#1086 mock roundtrip→journal), Slice B(#1089 verdict) — merged
- **작성일**: 2026-06-02
- **상태**: 설계 승인됨 → 구현 계획 수립 단계

## 1. 배경 / 현 상태

회고에서 "룰 효과"를 정량화하려면 watch-구동 roundtrip마다 **trigger_price(룰 레벨) vs actual_fill(실제 체결) vs no_action_price(무행동 시 가격)** 를 비교해야 한다. A/B로 데이터는 갖춰졌다:
- `trade_journals`(account_type='mock', correlation_id, entry_price, status='closed') — Slice A.
- `KISMockOrderLedger`(correlation_id, price) — roundtrip.
- `InvestmentWatchEvent`(correlation_id, threshold, current_value, symbol, market) — 트리거 스냅샷.
- `app/jobs/watch_market_data.py::get_price(symbol, market) -> float | None` — 라이브 시세(scanner가 사용).

counterfactual 개념/테이블은 없음(net-new). (`trading_decision.TradingDecisionCounterfactual`은 별개 — 위원회 제안용.)

## 2. 목표 / 비목표
**목표**: closed mock roundtrip(correlation_id)별 trigger/fill/no_action 3가격 + deltas 기록. 무행동 가격은 **sync 시점 라이브 시세**(injectable). default-off, 멱등, mock·룰구동 전용.
**비목표**: D 사이클 read API(집계) / E follow_up. forward 다중-horizon no_action(현재 1회 스냅샷). operator flip.

## 3. 설계

### 3.1 데이터 모델 (신규 테이블 + 마이그레이션)
`review.trade_journal_counterfactuals`:
- `id` BigInteger PK
- `journal_id` BigInteger FK→`review.trade_journals.id`(ondelete CASCADE) NOT NULL
- `correlation_id` Text NOT NULL — UNIQUE(멱등; roundtrip당 1개)
- `symbol` Text NOT NULL, `market` Text NOT NULL
- `trigger_price` Numeric(20,8) NOT NULL — InvestmentWatchEvent.threshold(룰 레벨)
- `triggered_value` Numeric(20,8) nullable — event.current_value(트리거 시점 실제가)
- `actual_fill_price` Numeric(20,8) nullable — journal.entry_price
- `no_action_price` Numeric(20,8) nullable — sync 시점 라이브 시세
- `no_action_as_of` TIMESTAMP(tz) nullable
- `fill_vs_trigger_pct` Numeric(10,4) nullable — (fill-trigger)/trigger*100
- `no_action_vs_fill_pct` Numeric(10,4) nullable — (no_action-fill)/fill*100
- `created_at` TIMESTAMP server_default now()
- Index(correlation_id unique), Index(journal_id).
- 신규 테이블 → `Base.metadata.create_all`이 테스트 DB 생성(conftest drift 패치 불요). alembic migration 추가.

### 3.2 Sync 서비스 (`journal_counterfactual_service.py`)
`sync_journal_counterfactuals(db, *, force=False, price_fn=<get_price>) -> dict`:
- gate(`force or JOURNAL_COUNTERFACTUAL_ENABLED`) → 미통과 `{"status":"disabled","created":0}`.
- `status='closed'` + `account_type='mock'` + `correlation_id IS NOT NULL` + counterfactual 미존재 journal 스캔.
- 각 journal: correlation_id로 `InvestmentWatchEvent` 1건 조회 → **없으면 skip**(룰-구동 아님). 있으면 trigger_price=event.threshold, triggered_value=event.current_value, symbol=event.symbol, market=event.market.
- `no_action_price = await price_fn(symbol, market)` (기본 `get_price`; injectable). None이면 no_action_price/deltas null(**fail-open** — trigger/fill은 기록), as_of는 fetch 시각.
- deltas: `fill_vs_trigger_pct`(trigger>0), `no_action_vs_fill_pct`(fill>0 & no_action not None). Decimal.
- insert (unique correlation_id로 멱등; 이미 있으면 ON CONFLICT DO NOTHING/사전조회 skip). 반환 `{status, created}`.
- price_fn 예외는 잡아 no_action null로 처리(한 종목 실패가 전체 안 깸).

### 3.3 발화 (default-off)
- `JOURNAL_COUNTERFACTUAL_ENABLED: bool = False`. paused taskiq `journal_counterfactual.sync` + operator CLI(force run).

## 4. 컴포넌트
| 단위 | 위치 | 책임 |
|---|---|---|
| 모델/마이그레이션 | `app/models/review.py` + alembic | `trade_journal_counterfactuals` |
| 서비스 | `app/services/trade_journal/journal_counterfactual_service.py` | `sync_journal_counterfactuals` |
| config | `app/core/config.py` | `JOURNAL_COUNTERFACTUAL_ENABLED=False` |
| task/CLI | `app/tasks/journal_counterfactual_tasks.py` + `scripts/sync_journal_counterfactuals.py` | paused + operator |

## 5. 안전 경계
- mock·룰구동(watch event 존재) roundtrip 전용. DB write + 시세 read만(broker/order mutation 없음). default-off inert. 멱등(unique correlation_id). price_fn 실패 fail-open(null). Slice A/B 무변경.

## 6. 테스트
1. 테이블 insert + unique(correlation_id) + FK CASCADE.
2. sync: closed mock journal + 대응 watch event + price_fn stub → counterfactual row(trigger/triggered_value/fill/no_action + deltas 정확).
3. watch event 없는 journal → skip(룰-구동 아님).
4. 멱등: 재실행 중복 없음.
5. price_fn None/예외 → no_action_price·no_action_vs_fill_pct null, trigger/fill/fill_vs_trigger 기록(fail-open).
6. flag off → `disabled`, row 0.
7. non-closed / non-mock journal 무시.

## 7. 미해결 / 후속
- D 사이클 read API(armed/triggered/filled/PnL/hit-miss + verdict + counterfactual 집계) / E follow_up_report_item_id.
- forward 다중-horizon no_action(현재 sync 1회 스냅샷). operator flag flip + smoke.
- 구현 시 origin/main(A/B merged) 기준.
