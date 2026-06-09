# ROB-400 — kis_mock reconciler fill 이중계상 + lifecycle/status 정합 (설계)

- **Linear**: ROB-400 (High, Backlog) — `[모의/버그] kis_mock reconciler가 동일종목 다중주문 시 보유 델타를 중복 귀속(fill 이중계상) + 주문이력 lifecycle 모순`
- **Branch**: `rob-400`
- **관련**: ROB-395(live 선반영), ROB-404(체결 이벤트 correlation 소스), ROB-401(자율매매 에픽), ROB-406(취소·정정 미지원)
- **마이그레이션**: 0 (새 lifecycle 상태/컬럼 없음)

## 1. 문제

2026-06-01 모의 자율매매 데모: 0148J0 매수 2건(ledger 23 @15,500·10주, ledger 24 @15,900·10주) 후
`kis_mock_reconciliation_run(dry_run=false, confirm=true)`.

**증상 1 — fill 이중계상.** reconcile 결과 ledger 23·24 둘 다 `next_state=fill`(각 `fill_detected`,
`observed_delta=+10`). 그러나 실제 보유 증가는 **0148J0 총 +10주**. 하나의 +10 델타가 두 주문에 각각
귀속되어 체결이 2배 계상됨.

**증상 2 — lifecycle/status 모순.** `kis_mock_get_order_history(symbol=0148J0)`가 두 건 모두
`status="pending"`, `filled_qty=0`, `remaining_qty=10` 인데 `lifecycle_state="fill"`. 같은 주문이
"체결됨(lifecycle)" + "미체결 0주(status)"로 동시 표기됨.

## 2. 루트코즈 (코드 검증 완료)

`app/services/kis_mock_holdings_reconciler.py::classify_orders` (line 111~) 가 주문을 **독립적으로** 평가:

```python
for order in orders:
    snapshot = holdings.get(order.symbol)          # 종목당 단일 스냅샷
    delta = snapshot.quantity - order.holdings_baseline_qty
    decision = classify_fill_by_delta(...)          # 주문마다 같은 델타 재적용
```

데모 추적: ledger 23·24 모두 `baseline=0`(주문 시점 0보유, 24 주문 시점에도 23 미체결), `snapshot=10`
→ 각각 `delta=+10 ≥ ordered 10` → 둘 다 `filled`. 종목 레벨에서 "이미 소비된 델타"를 차감하는 중재가 없음.

추가로 `fill` 재검증 경로(line 149~177)도 같은 단일 스냅샷을 쓰므로, 다음 reconcile에서 두 fill 모두
`delta >= expected`를 만족 → 둘 다 `reconciled`로 굳어져 이중계상이 영구 확정됨.

증상 2는 구조적: `status`/`filled_qty`/`remaining_qty`는 broker(또는 shadow) 소스에서, `lifecycle_state`는
ledger에서 오며 둘이 정합되지 않음. `app/mcp_server/tooling/kis_mock_ledger.py::_shadow_row_to_order` 가
shadow pending 행에 `status="pending"`, `filled_qty=0.0`, `remaining_qty=전량` 을 **하드코딩**하면서
ledger의 `lifecycle_state`(reconciler가 `fill`로 올린 값)를 그대로 첨부.

## 3. 결정 사항

- **배분 우선순위**: 매수 = 가격 DESC(고가 우선), 매도 = 가격 ASC(저가 우선) — 시장에서 더 공격적인 호가가
  먼저 체결될 가능성 반영. 동가는 `(trade_date, id)` ASC(오래된 주문) tiebreaker. `price` 컬럼은 NOT NULL.
- **잔여/모순 표기**: 예산을 못 받은 주문은 **pending 유지**. 새 lifecycle 상태(`unknown`) 추가하지 않음
  (DB CHECK 제약 + 마이그레이션 회피). 물리적으로 불가능한 경우만 기존 `anomaly`("operator review 필요").
- **수량 비례 분배는 채택하지 않음**: 모의에는 주문단위 체결 증거가 없어 임의 partial을 만들면 평단·PnL을 더 왜곡.

## 4. 설계

### 4.1 핵심 전환 — 종목별 델타 예산(budget) 배분

`classify_orders`를 주문 독립 평가에서 **종목별 예산 배분**으로 재작성. 종목당 단일 보유 델타는 한 번만 소진.

`classify_fill_by_delta` 순수 커널(ROB-341 공유, line 76~96)은 **보존**한다. budget을 인자로 받는 얇은
배분 호출부만 상위에 추가한다.

### 4.2 배분 알고리즘 (종목 + side 단위)

1. reconcilable 주문(`accepted`/`pending`/`fill`)을 **종목 + side(buy/sell)** 로 그룹핑.
   baseline_missing / holdings_snapshot_missing 은 기존대로 그룹 진입 전 `anomaly` 처리.
2. **기준 baseline** = 해당 그룹 reconcilable 주문들의 `holdings_baseline_qty` **최소값**.
   이 경쟁 배치 직전의 포지션을 뜻한다. 종료(terminal: reconciled/failed/stale) 주문은 그룹에서 빠지며,
   그 체결 효과는 후속 주문의 baseline에 이미 반영되어 있어 정합이 유지된다.
3. **directional budget** = 매수 `snapshot.qty − 기준baseline`, 매도 `기준baseline − snapshot.qty`.
   0 이하이면 0.
4. 우선순위 정렬: 매수 = `price` DESC, 매도 = `price` ASC, 동가는 `(trade_date, id)` ASC.
   **단, 이미 `fill` 상태인 주문이 예산을 최우선으로 소진**한다(과거 귀속분을 먼저 차감 →
   fill→reconciled 경로의 이중계상 차단).
5. 우선순위대로 greedy 소진: `fill_q = min(잔여예산, ordered_qty)`.
   - `fill_q ≥ ordered_qty` → `filled` (accepted/pending → `fill`; 기존 `fill` → `reconciled`)
   - `0 < fill_q < ordered_qty` → `partial` (→ `fill`, `partial_fill_detected`)
   - `fill_q == 0` → none → 기존 `_pending_or_stale(order, now, thresholds)` 재사용
   - 소진할 때마다 잔여예산 차감.
6. 예산을 못 받은 주문 → **pending 유지**. 모든 주문 충족 후 남은 양의 예산(주문 합보다 보유가 더 큼 =
   외부/수동 보유 추정) → 귀속하지 않음, anomaly 아님.
7. **물리적 모순만 anomaly**: 이미 `fill`인데 예산이 그 귀속을 받치지 못함(보유가 fill 요구량 밑) →
   `holdings_mismatch` anomaly. 기존 fill-revalidation의 mismatch 의미를 budget 기준으로 재정의.

각 주문에 대해 기존과 동일한 `LifecycleTransitionProposal`(`observed_holdings_qty`, `observed_delta`)을
반환하되, **`attributed_fill_qty`(이 주문에 귀속된 수량)와 배분 컨텍스트(기준baseline, 우선순위)** 를 함께
싣는다.

### 4.3 Fix #3 — lifecycle ↔ status 정합

- reconciler가 `last_reconcile_detail`(기존 JSONB)에 **주문별 `attributed_fill_qty`** 를 기록한다
  (현재는 aggregate `observed_delta`만). 컬럼 추가 없음.
- `_shadow_row_to_order` (및 history 정규화 경로)가 하드코딩 `filled_qty=0`/`status="pending"` 대신
  **`lifecycle_state` + `attributed_fill_qty`에서 파생**한다:
  - `fill` + 귀속 q: `status = "filled"`(q ≥ ordered) | `"partial"`, `filled_qty = q`,
    `remaining_qty = ordered − q`.
  - `accepted`/`pending`(귀속 없음): 기존대로 pending, filled 0.
  → "체결됨 + 미체결 0주" 모순 제거. shadow warning 텍스트(미지원 엔드포인트 한계 고지)는 유지.

## 5. 영향 범위 (마이그레이션 0)

| 파일 | 변경 |
|------|------|
| `app/services/kis_mock_holdings_reconciler.py` | `classify_orders` 재작성(그룹핑/예산 배분). `classify_fill_by_delta` 커널 보존. `attributed_fill_qty` 산출. |
| `app/jobs/kis_mock_reconciliation_job.py` | 그룹 입력 전달 + `last_reconcile_detail`에 `attributed_fill_qty` 기록. |
| `app/mcp_server/tooling/kis_mock_ledger.py` | `_shadow_row_to_order` 파생 로직(status/filled_qty/remaining_qty). |

- 새 lifecycle 상태/enum/CHECK 변경 없음. ledger 컬럼 추가 없음.
- read-only 경로 외 브로커/주문 mutation 없음. dry_run 기본, apply는 `dry_run=False`+`confirm=True` 유지.

## 6. 안전 / 에러 처리

- 데이터 부족(baseline/snapshot missing) → 기존 `anomaly` 유지(fill 단정 금지 원칙 강화).
- dry_run 기본값·confirm 게이트·fail-closed(config missing) 모두 불변.
- 한계 명시: holdings-delta 만으로 주문단위 귀속은 원리적 추정. 진짜 주문단위 correlation 은 ROB-404
  (Redis `execution:{market}` 체결 이벤트)에 의존 — 본 설계는 그 전까지의 결정적 best-effort 배분이며,
  주석/런북에 이 한계를 남긴다.

## 7. 테스트 (TDD, 순수 함수 중심)

1. **데모 재현**: 동일종목 매수 2건(15,500/15,900), 보유 +10 → ledger24(고가) `filled`, ledger23 `pending`.
2. **동가 tiebreaker**: 동가 2주문, 보유 +10 → 오래된 주문(trade_date,id) `filled`, 나머지 `pending`.
3. **부분 배분**: 보유 +6, 두 주문 각 10 → 우선순위 1건 `partial`(6), 2건째 `pending`.
4. **이미 fill 2건 + 보유 1건분만 받침**: 1건 `reconciled`, 1건 `anomaly`(`holdings_mismatch`).
5. **외부 보유 초과**: 보유 +15, 주문 합 10 → 10 귀속, 잔여 5 무시(anomaly 아님).
6. **매도 대칭**: 저가 우선 배분.
7. **Fix #3 정합**: lifecycle=`fill` + attributed 6, ordered 10 → history `status=partial`,
   `filled_qty=6`, `remaining_qty=4`.
8. baseline_missing / snapshot_missing → `anomaly` (회귀 방지).

## 8. 비범위 (Out of scope)

- 주문단위 체결조회(ccld) 보강 / Redis 체결 이벤트 correlation → ROB-404.
- live 경로(ROB-395). 취소·정정 미지원(ROB-406).
- 새 lifecycle 상태 도입.
