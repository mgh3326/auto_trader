# ROB-473 — report_item ↔ order_id 링크 (감사 추적) (Design Spec)

- **Issue:** [ROB-473](https://linear.app/mgh3326/issue/ROB-473) (Feature, ROB-459 P4에서 분리)
- **Date:** 2026-06-09
- **Status:** Approved design → writing-plans
- **Scope:** Slice 1 (쓰기측: 링크 컬럼 + send-time threading). **Slice 2(읽기 join-back) deferred** — 별도 후속.
- **방향/범위 (확정):** Reverse(ledger에 `report_item_uuid`) · **라이브만**(kis_live + live_order).

---

## 1. 배경 (코드-grounded, 현재 main `ad764592`)

리포트 item과 실제 체결 라이브 주문이 **구조적으로 미링크**다(이슈 use case: 하나금융 odno `0004332700`, LGD 라이브 주문이 리포트 item과 추적 연결 안 됨).

### grounded 사실
- **report item 쪽**: `InvestmentReportItem.item_uuid`(PG_UUID, unique)는 `investment_report_get`/`decide_item`로 노출 → 외부 주문 도구가 참조 가능. 모델 docstring(`app/models/investment_reports.py:206-207`)은 **"execution은 report item에 절대 안 산다(journal/ledger에만)"** 원칙 명시 → forward(item에 order 저장) 방향은 원칙 위반. **Reverse 채택.**
- **ledger 쪽**: 어떤 ledger도 `report_item_uuid` 없음. 라이브 ledger(`KISLiveOrderLedger` ROB-395, `LiveOrderLedger` ROB-407)는 **`correlation_id`조차 없음**.
- **order 도구**: `_place_order_impl`(`app/mcp_server/tooling/order_execution.py:896`)은 이미 `correlation_id` 등 다수 send-time param 보유하나 `_execute_and_record`가 **kis_mock에만** 전달, kis_live/live_order record 함수엔 미전달. MCP 표면은 변형 도구(`kis_live_place_order` 등, `orders_kis_variants.py:_place_order_variant`→`_place_order_impl`).
- **✅ send-time 컬럼은 reconcile까지 durable**(확인): reconcile(`_update_ledger_outcome`/`_update_live_ledger_outcome`)은 `filled_qty/avg_fill_price/trade_id/journal_id/reconciled_at`만 갱신, **send-time intent 컬럼(reason/thesis/strategy/...)은 불변**. 라이브 ledger는 order_no 1:1 row이므로 send에 박은 `report_item_uuid`는 불변 보존.
- **precedent**: `AlpacaPaperOrderLedger.candidate_uuid`/`briefing_artifact_run_uuid`(`app/models/review.py`) = "ledger row가 소스 승인 아티팩트 uuid를 send-time에 박고 **FK 없이** 불변·indexed, `list_by_*` 쿼리". 정확히 이 패턴.
- **명명/타입 선례**: `InvestmentReportNewsCitation.report_item_uuid = PG_UUID(as_uuid=True) | None`(`app/models/investment_reports.py:821`) → 동일 명명/타입 미러.

---

## 2. Goals / Non-goals

**Goals**
- 라이브 주문(kis_live KR, live_order US/crypto)이 어느 report item에서 비롯됐는지 **감사 추적** 가능하게 — `report_item_uuid`를 send-time에 ledger row에 기록, reconcile까지 durable.
- `report_item_uuid`로 주문을 역조회(`list_*_by_report_item_uuid`).

**Non-goals**
- **forward 방향**(report item에 order 저장) — 설계 원칙 위반.
- **읽기 join-back**(`investment_report_get`에 linked order 노출) = Slice 2, deferred.
- mock(kis_mock) / alpaca / binance 링크 — 이번 범위 밖(라이브만).
- broker/order mutation 변경. FK·cross-schema cascade. report_item_uuid 존재 검증(record-as-provided, fail-open).
- reconcile 로직 변경(불변 유지가 핵심).

---

## 3. 설계 (Slice 1)

### 3-1. 스키마 (1 alembic migration, additive)
`app/models/review.py`:
- `KISLiveOrderLedger`: `report_item_uuid: Mapped[uuid.UUID | None] = mapped_column(PG_UUID(as_uuid=True), nullable=True)` + index `ix_kis_live_ledger_report_item_uuid`.
- `LiveOrderLedger`: 동일 컬럼 + index `ix_live_ledger_report_item_uuid`.
- **FK 없음**(AlpacaPaper `candidate_uuid` 패턴 — 의도적 decoupling). nullable → 기존 row backward-compat.
- migration: 두 컬럼 + 두 index 추가(upgrade/downgrade 대칭). operator가 `alembic upgrade head`로 적용(타 ledger migration 동형 게이트).

### 3-2. Threading (send-time)
- `_place_order_impl`(order_execution.py:896): `report_item_uuid: str | None = None` 추가. 빈/None이면 무시, 값이면 UUID 파싱(parse 실패 시 fail-open: 무시 또는 구조화 경고 — 주문 차단 금지).
- `_execute_and_record`: `report_item_uuid`(파싱된 UUID|None) 수신 → `_record_kis_live_order`(kis_live 경로)와 `_record_live_order`(US/crypto 경로) 호출에 전달(correlation_id 옆에 동형 추가).
- `_record_kis_live_order`/`_save_kis_live_order_ledger`(`kis_live_ledger.py`): `report_item_uuid` param 추가 → SEND insert values에 컬럼 기록.
- `_record_live_order`/`_save_live_order_ledger`(`live_order_ledger.py`): 동일.
- 변형 도구 `_place_order_variant`(`orders_kis_variants.py`) + 라이브 변형 등록(`kis_live_place_order`, US/crypto live): `report_item_uuid` param 노출 + `_place_order_impl`로 전달. 도구 description에 "report item에서 비롯된 라이브 주문이면 `investment_report_get`의 `item_uuid`를 넘겨 감사 링크" 기재.
- **reconcile 무변경**: `_update_ledger_outcome`/`_update_live_ledger_outcome`는 그대로(fill 컬럼만). docstring에 **send-time intent/provenance 컬럼 불변** 계약 명시(report_item_uuid 포함).

### 3-3. 쿼리 (역조회)
- ledger 서비스/리포지토리에 `list_live_orders_by_report_item_uuid(report_item_uuid)` (index-backed) 추가 — AlpacaPaper `list_by_candidate_uuid` 패턴 미러.
- 라이브 ledger row 직렬화(있는 경우)에 `report_item_uuid` 포함.

### 3-4. 에러 처리 / 안전
- **record-as-provided**: report_item_uuid 존재 여부 미검증(감사 provenance). FK 없음 → report item 삭제와 ledger 독립.
- **fail-open**: report_item_uuid 파싱 실패가 주문을 차단하지 않음.
- mock/alpaca/binance 경로 무영향. dry_run은 ledger 미기록(기존 동작).

---

## 4. 테스트
- **스키마/migration**: upgrade 후 두 ledger에 `report_item_uuid` 컬럼+index 존재, nullable; downgrade 대칭.
- **kis_live 기록**: `_save_kis_live_order_ledger(..., report_item_uuid=X)` → row.report_item_uuid == X; omit → NULL.
- **live_order 기록**: `_save_live_order_ledger(..., report_item_uuid=X)` → 동일(US/crypto).
- **threading**: `_execute_and_record`가 report_item_uuid를 kis_live/live record로 전달(실 broker 미호출, record/save 레벨 단위테스트).
- **reconcile 불변**: reconcile 후 report_item_uuid 미변경.
- **역조회**: `list_live_orders_by_report_item_uuid(X)` → 해당 주문 반환.
- **fail-open**: 잘못된 report_item_uuid 문자열 → 주문 경로 미차단.

---

## 5. 제약 / 롤아웃
- additive nullable 컬럼 + index, backward-compat. broker mutation 0.
- **migration 1개** — operator-gated(`alembic upgrade head`). PR에 포함되나 operator가 별도 적용(타 ledger 동형).
- mock/alpaca/binance/reconcile 로직 무변경.

## 6. Deferred (Slice 2 / 후속)
- `investment_report_get` 번들에 item별 linked 라이브 order 조인 노출(크로스-스키마 read-join). 감사 UX.
- mock/alpaca/binance로 링크 확대(필요 시).

## 7. 핵심 코드 앵커
| 무엇 | 위치 |
|---|---|
| report item identity(노출) | `app/models/investment_reports.py` `item_uuid`; `investment_report_get`/`decide_item` |
| "execution은 report item에 안 산다" 원칙 | `app/models/investment_reports.py:206-207` |
| 라이브 ledger(컬럼 추가 대상) | `app/models/review.py` `KISLiveOrderLedger`(266-338), `LiveOrderLedger`(341-426) |
| report_item_uuid 명명/타입 선례 | `app/models/investment_reports.py:821` (PG_UUID(as_uuid=True), nullable) |
| precedent(send-time uuid, FK없음, 불변) | `AlpacaPaperOrderLedger.candidate_uuid`/`briefing_artifact_run_uuid` (review.py) |
| order 도구 진입 | `order_execution.py:896` `_place_order_impl`; `orders_kis_variants.py` `_place_order_variant` |
| send/reconcile 라이프사이클(durable) | `kis_live_ledger.py`(_save/_update), `live_order_ledger.py`(_save/_update) |
