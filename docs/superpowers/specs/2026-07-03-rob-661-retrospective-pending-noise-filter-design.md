# ROB-661 — `trade_retrospective_pending` 노이즈 필터 (제안 1)

## 배경 / 문제

`trade_retrospective_pending` (ROB-647) 는 3개 live ledger
(`kis_live_order_ledger`, `live_order_ledger`, `toss_live_order_ledger`) 에서
lifecycle-terminal 상태이면서 회고(retrospective)가 아직 없는 주문을 due-list 로
반환한다. 2026-07-03 첫 실전 스캔에서 신뢰도 문제가 드러났다:

- **노이즈**: DAY 만료(매일 15~20건, 그물 재배치 운영 특성) + 전략적 취소(죽은 주문
  정리 목적) 가 전부 `cancelled` terminal 로 잡혀 due 목록을 스팸화. 진짜 회고
  대상(체결·거부)이 묻힌다.
- **누락**: 7/2 네오위즈/대웅 매도 **체결**이 목록에 없었음. 원인은 ROB-631
  (Toss KR fill booking `'equity' InstrumentType` ValueError) 로 ledger 가
  accepted-only 에 머물렀던 것. ROB-631 은 이미 main (`e4ada0f0`) 에 머지되어
  신규 fill 은 정상 booking 되므로 **이 스펙 범위 밖**.

핵심 사실: `expired` 는 별도 ledger status 가 아니다. KIS 는 booking 시점에
`expired → cancelled` 로 collapse 한다 (`kis_live_ledger.py:49`). 따라서 ledger
status 레벨에서 DAY-만료와 전략적 취소는 **모두 `cancelled` 한 버킷**이다.

## 범위

**제안 1 (트리거 필터) 만.** 제안 2(일자별 그물 요약 묶음), 제안 3(broker-evidence
fill 탐지) 는 명시적으로 defer. 마이그레이션 0 — 읽기 전용 도구 변경만.

## 설계

### 기본/opt-in terminal 집합

각 ledger 의 terminal 상수를 두 그룹으로 분리한다:

| ledger | DEFAULT (항상) | CANCEL-family (opt-in) |
|--------|----------------|------------------------|
| KIS live | `filled, rejected, anomaly` | `cancelled` |
| Generic live | `filled, rejected, anomaly` | `cancelled` |
| Toss live | `filled, rejected, anomaly` | `cancelled, cancel_rejected, replace_rejected` |

- `anomaly` 는 **기본 유지** — booking/reconcile 실패 신호라 회고 가치가 있고,
  ROB-631 이전엔 진짜 fill 이 anomaly 로 새는 경우도 있었다.
- CANCEL-family (`cancelled` = DAY 만료 + 전략적 취소 포함) 는 기본 제외.

### 파라미터

`build_retrospective_pending(..., include_cancelled: bool = False)` 및 MCP
`trade_retrospective_pending(..., include_cancelled: bool = False)` 추가.

- `include_cancelled=False` (기본): DEFAULT 집합만 `pending` 에 노출.
- `include_cancelled=True`: DEFAULT ∪ CANCEL-family (= 현행 전체 terminal 동작 =
  깔끔한 backcompat).

### 스캔 & 카운트

- 스캔은 각 ledger 에서 `DEFAULT ∪ CANCEL` 전체를 1회 fetch (현행 per-ledger
  `_PENDING_LEDGER_FETCH_CAP = 1000` 유지). cancel 물량 ~20/일 × 14일 = 280 건으로
  cap 1000 을 넘지 않는다. `trade_date desc` 정렬이라 최근 fill/reject 가 cap 에
  밀리지 않는다.
- coverage 차감(기존 `_is_covered`) 후, `include_cancelled=False` 이면 status 가
  CANCEL-family 인 entry 를 `pending` 에서 빼고 **제외 건수를 센다**.

### 응답 (투명성)

기존 키(`kst_date_from/to`, `account_mode`, `terminal_scanned`, `total_pending`,
`returned`, `pending`) 는 그대로 유지하고 추가:

- `include_cancelled`: bool echo
- `excluded_by_filter`: `{"cancelled": N}` — coverage 차감된 실제 숨겨진 cancel
  건수 (silent drop 방지, no-silent-caps 원칙). `include_cancelled=True` 면
  `{"cancelled": 0}`.

`total_pending` / `returned` / `pending` 는 필터 **적용 후** 값이다.

### MCP 도구 description

기본=filled/rejected/anomaly, cancelled(DAY 만료·전략적 취소 포함)은
`include_cancelled=true` 로만 노출된다는 점을 description 에 명시.

## 테스트 (TDD)

1. 기본 스캔이 cancel-family entry 를 `pending` 에서 제외하고 `excluded_by_filter`
   에 카운트.
2. `include_cancelled=True` 시 cancel-family 포함 (현행 전체 terminal 동작).
3. `anomaly` 는 기본에서 유지됨.
4. Toss `cancel_rejected` / `replace_rejected` 가 CANCEL-family 로 분류 (기본 제외,
   opt-in 시 포함).
5. `filled` / `rejected` 는 두 모드 모두 포함.
6. MCP 래퍼가 `include_cancelled` 를 서비스로 pass-through.

## 범위 밖 (defer)

- 제안 2: 일자별 그물 요약 묶음 (하루 20건 개별 대신 요약 1건).
- 제안 3: broker evidence / positions delta 기반 fill 탐지 (ROB-631 머지로 신규
  fill 은 이미 booking).
