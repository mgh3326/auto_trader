# ROB-406 — kis_mock 주문 취소·정정 복구 (C 하이브리드)

- **이슈**: ROB-406 (G5/버그) — kis_mock 주문 취소·정정 불가 (TTTC8036R 미지원)
- **부모 에픽**: ROB-401 (모의 자율매매 루프), 오케스트레이션 트래커 ROB-410 Wave 1
- **작성일**: 2026-06-01
- **상태**: 설계 승인됨 → 구현 계획 수립 단계

## 1. 문제

`kis_mock_cancel_order` / `kis_mock_modify_order` 호출 시:

```
error: kis_mock: domestic pending-orders inquiry (TTTC8036R) is not available in mock mode
mock_unsupported: true
```

자율 루프의 핵심인 "시간당 룰 갱신"(걸어둔 resting 존 지정가를 거두거나 가격 조정)이 전면 차단된다. 시장이 반대로 가도 armed 주문이 그대로 남는다.

### 근본 원인

`app/mcp_server/tooling/orders_modify_cancel.py`:

- `_cancel_kis_domestic` (446–557): symbol이 없으면 `inquire_korea_orders`(456), 그리고 symbol이 있어도 orgno/side/price를 얻으려 **무조건** `inquire_korea_orders`(495)를 호출.
- `_modify_kis_domestic` (877–): 대상 주문 탐색 + orgno/원가격/원수량 추출을 위해 `inquire_korea_orders`(891) 호출.

`app/services/brokers/kis/domestic_orders.py`의 `inquire_korea_orders`는 `is_mock=True`이면 즉시 `RuntimeError("... TTTC8036R ... not available in mock mode")`를 던진다(약 111–115).

그러나 실제 취소/정정 TR(`VTTC0013U`)에 필요한 값은 `krx_fwdg_ord_orgno` + `order_no` + `symbol` + side/qty/price뿐이고, 이는 **이미 주문 시점에 `review.kis_mock_order_ledger`에 저장**된다 (`kis_mock_ledger.py` 280·358행에서 `krx_fwdg_ord_orgno`, `order_no` 기록). 즉 pending-orders 조회는 불필요한 의존이다.

## 2. 목표 / 비목표

**목표**
- kis_mock 취소/정정이 TTTC8036R pending-orders 조회에 의존하지 않고 동작.
- mock에서 broker TR이 거부될 경우 fail-closed soft-cancel fallback (거짓 성공 절대 금지).
- live 경로는 무변경.

**비목표 (범위 제외)**
- `valid_until` 자동만료 / GTC→IOC/Day 정책 → 별도 후속 이슈. (cancel이 동작하면 자율루프 룰 갱신은 명시적 cancel+place로 충분.)
- modify의 cancel+replace 합성 → 후속.
- live(`kis_live`)·해외(`equity_us`)·Upbit·Kiwoom 경로 변경.

## 3. 접근: C 하이브리드

취소 데이터 흐름 (mock):

1. **Ledger resolver** — `order_no`로 `KISMockOrderLedger`에서 `symbol` + `krx_fwdg_ord_orgno` (+ side/quantity/price)를 조회. `_cancel_kis_domestic`은 `is_mock`이면 `inquire_korea_orders` 대신 이 resolver를 사용한다. live는 기존 inquire 경로 무변경.
2. `cancel_korea_order(..., krx_fwdg_ord_orgno=<ledger>, is_mock=True)` → `VTTC0013U` 호출.
3. **success** → ledger row `lifecycle_state='cancelled'`, broker-confirmed 증거를 `last_reconcile_detail`(또는 동등 필드)에 기록.
4. **명시적 "mock 미지원" 신호** (broker가 VTTC0013U를 미지원으로 거부) → **soft-cancel fallback**:
   - ledger row `lifecycle_state='cancelled'` 표시 +
   - 응답에 `mock_unsupported=true` + `broker_cancel_confirmed=false` +
   - 경고: "브로커 resting 주문이 아직 살아있을 수 있음(soft-cancel). 체결 시 reconcile에서 정정됨."
   - **clean cancel로 위장 금지.**
5. **그 외 broker 에러** (이미 체결/무효 주문 등) → 에러를 정직하게 표면화, soft-cancel 안 함, lifecycle 무변경.

fallback 트리거는 **보수적**으로: 알려진 unsupported 마커(특정 msg_cd / "not available" / "mock" 류 신호)만 soft-cancel로 분기하고, 모호하거나 일반적인 broker 에러는 5번처럼 실패로 surface한다. VTTC0013U의 mock 동작은 **현재 미검증**(creds 부재)이므로 operator smoke로 검증한다(코드베이스 관례: live/mock smoke operator-gated).

정정 데이터 흐름 (mock):

- 동일 resolver로 orgno/원가격/원수량/side 확보 → `modify_korea_order(..., is_mock=True)` → `VTTC0013U` (`RVSE_CNCL_DVSN_CD='01'`).
- **success** → ledger row의 price/quantity 갱신(또는 신규 order_no 반영), 증거 기록.
- **미지원 시 fail-closed** — 정직한 에러("kis_mock modify 미지원 — cancel 후 재주문 사용") 반환. **soft-modify 안 함** (검증 못 한 정정을 위장 불가). cancel+replace 합성은 후속.

## 4. 컴포넌트 / 인터페이스

| 단위 | 위치 | 책임 |
|---|---|---|
| Lifecycle vocabulary | `app/schemas/execution_contracts.py` (ROB-100) | `OrderLifecycleState` Literal + `ORDER_LIFECYCLE_STATES` + `TERMINAL_LIFECYCLE_STATES`에 `'cancelled'` 추가. `apply_lifecycle_transition`이 `ORDER_LIFECYCLE_STATES`로 검증하므로 선행 필수. additive. |
| Ledger order resolver | `app/services/kis_mock_lifecycle_service.py` (`KISMockLifecycleService`) | `order_no`로 취소/정정에 필요한 필드(ledger id, symbol, krx_fwdg_ord_orgno, side, quantity, price, lifecycle_state)를 담은 조회 메서드 제공. 없으면 `None`. |
| Cancelled 전이 | `KISMockLifecycleService` | 기존 `apply_lifecycle_transition(ledger_id, next_state="cancelled", reason_code, detail, dry_run)` 재사용 — `broker_confirmed`는 `detail`에 기록. 단일 write chokepoint 유지(새 mutation 메서드 불필요). |
| Cancel MCP 분기 | `orders_modify_cancel.py::_cancel_kis_domestic` | `is_mock`이면 resolver 사용 → cancel TR → 결과 분류(success / unsupported→soft-cancel / error). live 경로 무변경. |
| Modify MCP 분기 | `orders_modify_cancel.py::_modify_kis_domestic` | `is_mock`이면 resolver 사용 → modify TR → success/ledger 갱신 또는 미지원→fail-closed. live 무변경. |
| 마이그레이션 | `alembic/versions/<rev>_*.py` | `kis_mock_ledger_lifecycle_state_allowed` CHECK에 `'cancelled'` 추가 (drop+recreate). |
| 모델 | `app/models/review.py::KISMockOrderLedger` | CHECK 제약 정의에 `'cancelled'` 반영(코드/DB 동기). |

## 5. 데이터 모델 변경

두 곳을 동기화해야 한다(코드 contract + DB CHECK):

**1) 공유 lifecycle vocabulary** — `app/schemas/execution_contracts.py` (ROB-100):
- `OrderLifecycleState` Literal에 `"cancelled"` 추가
- `ORDER_LIFECYCLE_STATES` frozenset에 `"cancelled"` 추가 (이게 없으면 `apply_lifecycle_transition`이 `ValueError`로 거부)
- `TERMINAL_LIFECYCLE_STATES`에 `"cancelled"` 추가 — 취소는 operator 행동 없이는 변하지 않는 최종 결과. 전이 시 `reconciled_at` 스탬프가 찍힘.

**2) `KISMockOrderLedger.lifecycle_state` CHECK 허용값** (모델 + alembic):

```
'planned','previewed','submitted','accepted','pending','fill',
'reconciled','stale','failed','anomaly','cancelled'   ← 추가
```

- 둘 다 additive only. 기존 행 무영향. operator가 `alembic upgrade head` 별도 실행(cutover 관례).
- `'cancelled'`는 success 취소와 soft-cancel fallback 둘 다의 터미널 상태. `broker_confirmed` 여부는 `last_reconcile_detail`(JSONB)로 구분(상태값을 둘로 쪼개지 않음).
- `OPEN_LIFECYCLE_STATES`(`{accepted,pending,fill}`)에는 추가하지 않음 → 취소된 주문은 shadow pending/exposure에서 자동 제외(자율루프가 회수된 주문을 더는 resting으로 안 봄).
- 공유 contract이지만 추가는 순수 additive이고 다른 소비자(예: binance demo는 자체 state machine)에 회귀 없음 — terminal/in-flight 집합 멤버십만 늘어남.

## 6. 안전 경계

- **mock-only**: MCP variant wrapper(`orders_kis_variants.py`)가 `is_mock=True`를 핀하므로 live 도달 불가. live 경로(`inquire_korea_orders` 사용)는 코드 변경 없음.
- **soft-cancel는 broker 취소를 절대 주장하지 않음**: `broker_cancel_confirmed=false` + 경고 필수.
- **fail-closed**: 모호한 broker 에러는 success로 위장하지 않고 surface.
- **secret 출력 없음**, production env/secret 변경 없음.
- **broker mutation은 mock 계정 한정**.

## 7. 테스트

1. resolver가 seed된 `KISMockOrderLedger` 행에서 symbol/orgno/side/qty/price를 정확히 반환.
2. mock cancel 경로가 `inquire_korea_orders`를 **호출하지 않음** (회귀 가드 — 호출 시 실패하도록 mock).
3. VTTC0013U success → 응답 success + ledger `lifecycle_state='cancelled'` + `broker_cancel_confirmed=true`.
4. unsupported 신호 응답 → soft-cancel: `cancelled` + `mock_unsupported=true` + `broker_cancel_confirmed=false` + 경고. broker 취소 주장 안 함.
5. 기타 broker 에러 → 실패 surface, soft-cancel 안 함, lifecycle 무변경.
6. modify success → ledger price/qty 갱신. modify 미지원 → fail-closed 정직 에러(soft-modify 안 함).
7. **live 경로 회귀**: `is_mock=False` cancel/modify가 여전히 `inquire_korea_orders` 사용(무변경).
8. 모델/마이그레이션: CHECK가 `'cancelled'`를 허용, 기존 값도 여전히 허용. `'cancelled'` ∈ `ORDER_LIFECYCLE_STATES` ∩ `TERMINAL_LIFECYCLE_STATES`, `apply_lifecycle_transition(next_state="cancelled")`가 `ValueError` 없이 적용됨.

## 8. 미해결 / 후속

- VTTC0013U의 KIS mock 실제 동작은 operator smoke로만 확정 가능(creds 부재). smoke 전까지 soft-cancel fallback이 안전망. smoke 결과를 ROB-406/ROB-410에 증거로 남긴다.
- unsupported 신호의 정확한 msg_cd/문구는 smoke에서 확인 후 fallback 트리거 패턴을 좁힐 수 있음(초기엔 보수적 패턴 + 로깅).
- `valid_until` 자동만료, modify의 cancel+replace 합성은 별도 이슈.
