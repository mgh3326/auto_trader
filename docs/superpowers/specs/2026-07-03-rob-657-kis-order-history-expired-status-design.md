# ROB-657 — `kis_live_get_order_history`: 소멸된 주문을 `pending`으로 오표시하는 결함 수정

- **Linear**: ROB-657 (High)
- **Related**: ROB-631 (Toss DAY 만료→REJECTED), ROB-476/487 (`live_order_expiry` 분류기)
- **Date**: 2026-07-03
- **Scope decision**: 제안 1+2만 (status 도출 + `is_live`). 제안 3(NXT carry / `expected_expiry`)은 후속.
- **Migration**: 없음 (응답 shape additive)

## 배경 / 증거

2026-07-02 실거래: 기아(000270) 8주 @129,600 매수(odno 0013894000)가 15:30 정규장
마감 시점에 실제로 소멸(운영자 KIS 앱: 미체결 없음 + 주문가능 전액 복원). 그러나
`kis_live_get_order_history(symbol="000270", status="all")`는 16:09·17:08 두 차례 모두
`status="pending"`으로 반환 → Claude가 "주문 살아있음"으로 두 번 오판.

모순 페이로드:
```json
{"order_id":"0013894000","status":"pending","ordered_qty":8,"filled_qty":0,"remaining_qty":0}
```
`remaining_qty=0` + `filled_qty=0` = 정정취소가능수량 0 = **죽은 주문**(KIS 원장 기준)인데
`status`가 `pending`으로 분류되고 `summary.pending=1`.

## 근본 원인

`app/mcp_server/tooling/orders_modify_cancel.py::_map_kis_status(filled, remaining, status_name)`:

```python
def _map_kis_status(filled: int, remaining: int, status_name: str | None) -> str:
    normalized_name = str(status_name or "").strip()
    if normalized_name in ("접수", "주문접수"):
        return "pending"
    if normalized_name == "주문취소":
        return "cancelled"
    if normalized_name == "체결":
        if filled > 0 and remaining > 0:
            return "partial"
        return "filled"
    if normalized_name == "미체결":
        return "pending"
    if filled > 0 and remaining <= 0:
        return "filled"
    if filled > 0 and remaining > 0:
        return "partial"
    return "pending"          # ← 기아 주문(filled=0, remaining=0)이 여기로 떨어짐
```

- 죽은 주문(`filled==0 && remaining==0`)에 대한 규칙이 없음 → 기본값 `pending`.
- `inquire_korea_orders`(TTTC8036R, 정정취소가능주문조회)는 `prcs_stat_name`을 실제로
  싣지 않는 것으로 실측됨(ROB-487 note) → name 분기가 안 걸리고 기본값으로 낙하.

코드베이스는 이미 올바른 semantic을 다른 곳에 갖고 있음:
- `app/services/brokers/kis/domestic_orders.py:560`: `rmn_qty: 잔여수량 (> 0 이면 주문 생존)`
- `app/services/brokers/kis/live_order_expiry.py`: EOD day-order 소멸을 `expired`/`cancelled`로 분류.

## 변경 사항

### 변경 1 — `_map_kis_status` 종료(death) 규칙 추가

시그니처에 `ordered`를 추가하고, **명시적 취소 증거가 우선**하도록 순서를 잡아 death 규칙을 삽입:

```python
def _map_kis_status(ordered: int, filled: int, remaining: int, status_name: str | None) -> str:
    normalized_name = str(status_name or "").strip()

    # 명시적 취소 증거는 언제나 우선.
    if normalized_name == "주문취소":
        return "cancelled"
    # ROB-657: 체결 0 + 잔여 0 = 정정취소가능수량 0 = 소멸된 주문(만료/거부).
    # KIS 원장에서 "주문 생존 = rmn_qty > 0"이므로 stale한 '접수' 상태명보다 우선.
    if ordered > 0 and filled == 0 and remaining <= 0:
        return "expired"
    if normalized_name in ("접수", "주문접수"):
        return "pending"
    if normalized_name == "체결":
        if filled > 0 and remaining > 0:
            return "partial"
        return "filled"
    if normalized_name == "미체결":
        return "pending"
    if filled > 0 and remaining <= 0:
        return "filled"
    if filled > 0 and remaining > 0:
        return "partial"
    return "pending"
```

동작표:

| ordered | filled | remaining | name     | before   | after     |
|---------|--------|-----------|----------|----------|-----------|
| 8       | 0      | 0         | `""`/None| pending  | **expired** |
| 8       | 0      | 0         | 접수     | pending  | **expired** |
| 8       | 0      | 0         | 미체결   | pending  | **expired** |
| 8       | 0      | 0         | 주문취소 | cancelled| cancelled |
| 10      | 0      | 10        | 접수/None| pending  | pending   |
| 5       | 5      | 5         | 체결/None| partial  | partial   |
| 10      | 10     | 0         | 체결/None| filled   | filled    |
| 0       | 0      | 0         | None     | pending  | pending (degenerate, 소멸시킬 주문 없음) |

호출부(같은 파일):
- `_normalize_kis_domestic_order` — `_map_kis_status(ordered, filled, remaining, prcs_stat_name)`
- `_normalize_kis_overseas_order` — `_map_kis_status(ordered, filled, remaining, prcs_stat_name)`
  (overseas는 `remaining = ordered - filled`이라 death 경로에 원래 못 들어가지만 공유 헬퍼로 무해히 커버.)

### 변경 2 — `is_live: bool` 명시 필드

`_normalize_kis_domestic_order` / `_normalize_kis_overseas_order` 반환 dict에 추가:

```python
"is_live": status in ("pending", "partial"),
```

LLM이 `remaining_qty`/`filled_qty` 모순을 추론하지 않고 단일 boolean으로 생존 여부를 읽게 함.
`expired`/`cancelled`/`filled` → `is_live=false`.

### 변경 3 — `summary`에 `expired` 반영

`orders_history.py::_calculate_order_summary`에 카운트 추가:

```python
expired = sum(1 for o in orders if o.get("status") == "expired")
return {..., "cancelled": cancelled, "expired": expired}
```

파생 효과(기존 `_filter_and_sort_orders` 그대로):
- `status="pending"` 쿼리 → death 주문은 이제 `expired`이므로 **제외**(pending/partial만 통과).
- `status="all"` 쿼리 → `expired` + `is_live=false`로 노출, `summary.expired=1`, `summary.pending=0`.
- 쿼리 파라미터 `Literal`은 `["all","pending","filled","cancelled"]` 유지 — `expired`는 `all`에서 노출되므로 새 쿼리 enum 불필요.

## 범위 밖 (후속)

- **제안 3** — 정규장 마감 시 KRX→NXT carry 여부를 실측해 `expected_expiry`를 15:30/20:00로
  정확화. `get_order_history`는 `expected_expiry`를 방출하지 않으며, 이 필드는
  `app/services/action_report/snapshot_backed/collectors/pending_orders.py` 및
  `app/mcp_server/tooling/kis_live_ledger.py`에 존재. 라이브 실측이 필요하므로 별도 이슈로 분리.

## 테스트

`tests/test_kis_domestic_order_normalization.py` (신규/수정):
- `_map_kis_status` parametrized: 시그니처 `ordered` 추가에 맞춰 모든 케이스 갱신.
  - `(8,0,0,None)` / `(8,0,0,"")` / `(8,0,0,"접수")` / `(8,0,0,"미체결")` → `expired`
  - `(8,0,0,"주문취소")` → `cancelled`
  - `(0,0,0,None)` → `pending` (degenerate)
  - 기존 live/partial/filled 케이스 → 불변
- `_normalize_kis_domestic_order`:
  - 기아 shape(`ord_qty=8, tot_ccld_qty=0, rmn_qty=0`) → `status="expired"`, `is_live=False`, `remaining_qty=0`
  - live pending(`rmn_qty=10`) → `is_live=True`
- `_normalize_kis_overseas_order`: `is_live` 필드 존재 + 값 정합.
- `_calculate_order_summary`: `expired` 카운트 포함.
- `get_order_history_impl` 통합(브로커 모킹, KR):
  - `status="all"` → 죽은 주문이 `expired`/`is_live=False`, `summary.expired=1`, `summary.pending=0`
  - `status="pending"` → 죽은 주문 제외(빈 orders 또는 미포함)

## 문서

- `_map_kis_status` docstring에 death 규칙(ROB-657) 근거 주석.
- `app/mcp_server/README.md`의 order-history 도구 설명이 status 값을 열거하면 `expired`/`is_live` 반영.
```

## 검증

- `make format && make lint` (ruff + ty)
- `uv run pytest tests/test_kis_domestic_order_normalization.py tests/test_orders_history_kis_mock.py -v`
- 관련 회귀: `uv run pytest tests/test_mcp_kis_order_variants.py -v`
