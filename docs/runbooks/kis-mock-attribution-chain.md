# kis_mock 귀속 사슬 (signal → submit → ledger → reconcile)

`kis_mock` 주문 하나를 **단일 `correlation_id`** 로 신호 발생 시점부터
reconcile 까지 추적하기 위한 계약. 성공척도 「주문 귀속 100% · lifecycle
evidence 100%」를 **측정 가능**하게 만드는 것이 목적이다.

## 1. 왜 필요했나 — 사슬이 끊긴 지점

수리 전 상태:

| 구간 | 상태 | 근거 |
|---|---|---|
| signal → submit | **기록 없음** | 신호가 주문이 됐든 안 됐든 durable record 자체가 없음 |
| submit 시점 귀속 | **사후 부여** | `correlation_id` 를 브로커 응답 처리부(`_record_kis_mock_order`)에서 mint — 즉 **POST 이후** |
| `strategy` | **nullable + 무검증** | `_place_order_impl` 주석에 "mock 은 thesis/strategy 선택" 이라고 명시돼 있었음 |
| reconcile | **correlation_id 미전달** | `OrderLifecycleEvent.correlation_id` 필드는 ROB-100 부터 있었으나 reconciler 가 채우지 않음 |

핵심은 **순서**다. 사후 mint 는 "주문은 나갔는데 원장 write 가 실패/프로세스
사망" 시 **브로커에만 존재하고 귀속은 어디에도 없는 주문**을 만든다.
ROB-1093(`execution_asset_class` 전부 NULL → 사후 트랙 분리 불가)과 같은 모양:
nullable 컬럼은 「채워질 수도 있다」가 아니라 **「안 채워져도 통과한다」**.

## 2. 수리 후 구조

```
resolve_attribution()        ← 순수. strategy 없으면 MissingAttribution
        │  (네트워크·DB 접촉 0)
        ▼
record_signal()              ← review.kis_mock_signal_ledger COMMIT
        │                       실패하면 여기서 중단 (fail-closed)
        ▼
_execute_order()             ← 브로커 POST. 여기 도달했다는 것 자체가
        │                      "귀속이 이미 durable 하다" 는 증거
        ▼
kis_mock_order_ledger        ← 같은 correlation_id + strategy 상속
        ▼
reconcile event              ← OrderLifecycleEvent.correlation_id +
                               last_reconcile_detail.correlation_id
```

- 게이트 위치: `app/mcp_server/tooling/order_execution.py::_execute_and_record`
  **함수 최상단**. 보유 baseline 조회(브로커 read)보다도 앞 — 귀속 없는 주문은
  브로커 I/O 를 **한 번도** 하지 않는다.
- 서비스: `app/services/kis_mock_attribution.py`
- 조회: `app/services/kis_mock_attribution_chain.py::load_attribution_chain`

## 3. 강제 수준

| 대상 | 수준 | 이유 |
|---|---|---|
| `review.kis_mock_signal_ledger.{correlation_id,strategy,signal_source}` | **DB NOT NULL + 공백거부 CHECK** | 신규 테이블 → legacy row 없음 → 제약 추가가 마이그레이션을 깨뜨리지 않음 |
| 주문 전송 | **APP FAIL-CLOSED** | 신호 row 를 못 쓰면 전송 안 함 |
| `review.kis_mock_order_ledger.{correlation_id,strategy}` | nullable 유지 | 과거 행에 NULL 존재(ROB-321/730 이전) → NOT NULL 은 마이그레이션 파괴 |

즉 **DB 제약은 pre-submit 레코드에 걸려 있다.** 주문은 그 레코드가 성공해야만
나가므로, 결과적으로 "전송된 주문 = DB 가 NOT NULL 을 검증한 귀속을 가진 주문"
이다. 주문 원장 쪽 과거 NULL 백필은 별건(§6).

## 4. 호출자 계약 (파괴적 변경)

`place_order(..., is_mock=True)` / `account_mode="kis_mock"` 는 이제
**`strategy` 를 요구**한다. 없으면:

```json
{"success": false, "error_code": "attribution_required",
 "missing_attribution": ["strategy"]}
```

레인 자동 판정(명시 strategy 불필요):

| 컨텍스트 | strategy | signal_source |
|---|---|---|
| `mirror_cohort="mock_counterfactual"` | `mock_counterfactual_mirror` | `mirror` |
| 그 외 · strategy 없음 | — | **차단** |

레포 내 기존 호출자는 전부 배선 완료: scalping(`strategy_id`), mirror
(`mirror_counterfactual`), watch auto-execute(`watch_auto_execute_mock`).
`thesis` 는 여전히 불필요(mock 은 TradeJournal 을 만들지 않음).

## 5. 사슬 조회

```python
chain = await load_attribution_chain(db, correlation_id=cid)
chain.unbroken          # gaps 가 비었는가
chain.gaps              # signal_missing / order_missing /
                        # order_unattributed / reconcile_missing
chain.strategy
```

- 주문으로 이어지지 **않은** 신호(`decision="no_order"`)는 gap 이 아니다 —
  분모를 유지하기 위한 증거다.
- `previewed/failed/anomaly` 상태 행은 reconcile 을 기대하지 않는다.

## 6. 남은 한계

1. **주문 원장 과거 NULL 미측정·미백필.** 운영 DB 접속이 금지된 작업 범위여서
   `kis_mock_order_ledger` 의 기존 NULL 행 수를 실측하지 않았다. 백필/NOT NULL
   승격은 별도 작업.
2. **scalping 왕복은 signal row 1개.** entry/exit 두 leg 가 하나의
   `correlation_id` 를 공유하므로 `uq_..._correlation_id` 에 걸려 entry 것만
   남는다. 귀속 자체는 온전하나 leg 별 신호 기록이 필요하면 uniqueness 를
   `(correlation_id, leg)` 로 확장해야 한다.
3. **전송 중 예외 시 `outcome_state='recorded'` 유지.** 실패로 단정하지 않는
   것이 의도 — 접수 여부 불명이므로 reconcile 이 판정한다.
4. **신호 row 는 있고 주문이 없는 창(window)이 존재.** 신호 COMMIT 과 POST
   사이에서 프로세스가 죽으면 `order_missing` 으로 **탐지된다**. 완전 제거가
   아니라 관측 가능화다.
5. **signal 기록은 `_execute_and_record` 경유 주문만.** scalping 어댑터가 직접
   쓰는 synthetic evidence row(`-entry`/`-exit`/`-anomaly`)는 signal ledger 를
   거치지 않지만, 동일 `correlation_id` 를 달고 있어 같은 사슬로 조회된다.

## 7. 마이그레이션

`20260803_kis_mock_signal` — `review.kis_mock_signal_ledger` **생성만**.
기존 테이블/컬럼/제약/인덱스 변경 0.

scratch DB 실측(2026-08-03, 로컬 postgres 17.9):

```
baseline: ORM 139 테이블 · 스키마 오브젝트 3,657개 (신규 테이블 제외 스냅샷)
upgrade head    → 신규 테이블 생성  · 나머지 스냅샷 IDENTICAL
downgrade -1    → 신규 테이블 제거  · 나머지 스냅샷 IDENTICAL
upgrade head    → 신규 테이블 재생성 · 나머지 스냅샷 IDENTICAL
NULL strategy / 공백 strategy / NULL correlation_id / 공백 signal_source
  → 4건 전부 REJECTED, 정상 행은 INSERT (positive control)
```

⚠️ 로컬 postgres 에 timescaledb 가 없어 **전체 체인 replay 는 불가**했다.
baseline 은 `Base.metadata.create_all`(신규 테이블 제외)로 구성했다.
CI 쪽 정적 증명은 `tests/test_kis_mock_signal_migration_additive.py`.

## 8. 검증

```bash
uv run pytest tests/services/test_kis_mock_attribution_chain.py \
              tests/test_kis_mock_signal_migration_additive.py -q
```

`test_positive_control_attributed_order_does_reach_the_broker` 는 필수다 —
이것이 없으면 "모든 주문이 막히는 설정 오류"와 "fail-closed 정상 동작"이
구분되지 않는다.
