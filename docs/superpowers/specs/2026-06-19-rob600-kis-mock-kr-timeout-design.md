# ROB-600 — kis_mock KR 국내 주문/잔고 간헐적 타임아웃이 빈 에러("")로 실패

- **Linear**: [ROB-600](https://linear.app/mgh3326/issue/ROB-600) · Bug / High
- **날짜**: 2026-06-19
- **브랜치**: `rob-600`
- **범위 결정**: 옵션 A — 진단가능(a) + 정직한 잔고(c) + read 타임아웃(b). **주문 전송 재시도는 추가하지 않음**.

---

## 1. 문제 (Symptom)

`account_mode='kis_mock'`의 **KR 국내** 주문/잔고 경로가 간헐적으로 **빈 에러(`""`) / ReadTimeout**으로 실패한다. 에러 메시지가 비어 있어 운영자가 원인 진단 불가.

2026-06-19 장중(10:23~10:35 KST) 운영 MCP 세션 재현:

- `get_available_capital(kis_mock)` / `get_cash_balance(kis_mock)`: `errors[]={source:'kis', market:'kr', error:''}` (빈 문자열), `accounts[]`에 `kis_domestic` 라인 누락, `summary` total에 KIS 모의현금 미반영 → **현금 0으로 오인**. 3회 연속.
- `kis_mock_place_order(005930 sell 370000 x2, dry_run=False)`: `{success:false, error:"", source:"kis"}`. 2회 연속.
- 직전 buy `dry_run=True`: 성공하나 `warning: "KIS mock balance precheck unavailable: ReadTimeout"` ← **여기만 사유가 보임**.

약 1시간 전 프로브에선 정상(orderable 3,102,026 반환) → 상시장애 아닌 **간헐적 타임아웃**.

---

## 2. 근본 원인 (코드로 확정)

### 2.1 빈 에러 문자열
`str(httpx.ReadTimeout())`은 **빈 문자열**이다 (실증: `str(httpx.ReadTimeout('')) == ''`). 타임아웃 예외를 잡는 자리들이 `or <classname>` 폴백 없이 **bare `str(exc)`**를 쓴다:

- `app/mcp_server/tooling/order_execution.py:1138` — `_order_error(str(exc))` (주문 sell 빈에러의 진원지; sell은 buy precheck를 안 타고 바로 outer except로 떨어짐)
- `app/mcp_server/tooling/order_execution.py:1128` — `error=str(exc)` (히스토리 기록)
- `app/mcp_server/tooling/portfolio_cash.py:252` (kis kr) / `:303` (kis us) — `errors.append({..., "error": str(exc)})`
- 별도 진원지: `app/services/brokers/kis/account.py:584` — `RuntimeError(f"{msg_cd} {msg1}")`에서 `msg1`이 빈 값이면 비-타임아웃 실패도 hollow

**정답은 이미 코드에 존재**: precheck `order_validation.py:1067`와 preview `order_execution.py:1060`는 `str(exc) or exc.__class__.__name__`를 써서 "ReadTimeout"이 보인다. 이 관용구는 코드베이스 ~10곳(`kis_live_ledger`, `toss_live_ledger`, `kis_mock_ledger`, `live_order_ledger` …)에 copy-paste되어 있으나 **공용 헬퍼는 없다**.

### 2.2 간헐 타임아웃의 진짜 레버 = read 타임아웃 5s
- 잔고 read `account.py:565` `inquire_domestic_cash_balance`는 `timeout=5`, 주문은 `timeout=10`. 모의 호스트 `openapivts.koreainvestment.com:29443`(`config.py:185`)가 느려 5s 경계에서 간헐 ReadTimeout.
- **선례**: `invest_home_readers.py:995` ROB-270 주석 — *"mock VTS is slow near the 5s boundary; 10s 단일 시도 + ReadTimeout 재시도 끔"*. 같은 처방을 MCP 잔고/자본 도구에는 아직 안 깔았을 뿐.

### 2.3 잔고가 0으로 오인되는 메커니즘
`portfolio_cash.py:235-246` — `kis_domestic` account dict 생성과 `total_krw += dncl_amt`가 **모두 try 블록 안**. 예외 시 둘 다 skip → `accounts[]`에서 라인 누락 + total 미반영. `summary`(`:305-312`)가 `accounts[]`를 합산하므로 KIS 현금이 **조용히 0**. `get_available_capital_impl`도 같은 `accounts[]`를 받아 `total_orderable_krw`를 합산(`:372-378`)하므로 동일하게 누락.

---

## 3. 제약 / 비-목표

- **주문 전송(place/cancel/modify)에는 타임아웃 재시도를 추가하지 않는다.** 타임아웃은 실패를 증명하지 않음(이미 접수됐을 수 있음) → 재시도 = double-submit 위험. idempotency key 없이는 금지.
- ⚠️ 본 브랜치에는 **ROB-585(주문 no-timeout-retry, EGW00215 throttle)가 미머지**다 (`grep EGW00215 app/` 0건, `3c873dca`는 HEAD 조상 아님). 따라서 현재 주문 전송은 default `retry_request_errors=True`로 동작한다. 이 잠재 double-submit 하드닝은 **ROB-585 스코프에 위임**하고 ROB-600은 건드리지 않는다.
- live/US read 타임아웃 튜닝, 기존 ~10곳 헬퍼 일괄 마이그레이션은 **스코프 밖**.

---

## 4. 설계

### 4.1 공용 헬퍼 — `app/core/exceptions.py` (신규)

```python
def describe_exception(exc: BaseException) -> str:
    """비어있지 않은 구체 사유를 반환한다.

    httpx 타임아웃 예외(ReadTimeout/ConnectTimeout/PoolTimeout 등)는 ``str()``이
    빈 문자열이라, 그대로 노출하면 진단 불가능한 ``error: ""``가 된다. 메시지가
    비면 클래스명으로 폴백해 'ReadTimeout' 같은 구체 사유를 표면화한다 (ROB-600).

    코드베이스에 흩어진 ``str(exc) or exc.__class__.__name__`` 관용구를 단일화한다.
    """
    return str(exc).strip() or type(exc).__name__
```

`app/core/`는 `app/services/`(base.py)와 `app/mcp_server/`(order/cash) 양쪽이 import 가능한 공유 레이어. base.py는 이미 `app/core`(settings)를 import하므로 순환참조 없음.

### 4.2 결함 (a) — 빈에러 → 구체사유 (behavior-preserving, 문자열만)

| 자리 | 변경 |
|---|---|
| `order_execution.py:1138` | `return _order_error(describe_exception(exc))` |
| `order_execution.py:1128` | `error=describe_exception(exc)` |
| `portfolio_cash.py:252` (kis kr) | `"error": describe_exception(exc)` |
| `portfolio_cash.py:303` (kis us) | `"error": describe_exception(exc)` |
| `kis/base.py:544` (빈 로그) | 로그 포맷 인자를 `describe_exception(e)`로 |
| `kis/base.py:557` (`RateLimitExceededError`) | `f"... {describe_exception(last_error)}"` |

모두 문자열만 바꾸므로 제어흐름·반환계약 불변. (`account.py:584`의 hollow `msg1`은 (a)의 호출부 폴백으로 이미 비-빈 처리되므로 별도 변경 불필요 — 단 `msg_cd`가 메시지에 포함되어 그대로 살아남는다.)

### 4.3 결함 (b) — read 타임아웃 (mock-aware, 잔고 read만)

`account.py:565` `inquire_domestic_cash_balance`:

```python
timeout=10 if is_mock else 5,
```

- retry는 기존 default `retry_request_errors=True` 유지 → 이슈 제안 #2의 "자동 1~2회 재시도" 충족(429/RequestError 재시도 경로는 `base.py:537-554`에 이미 존재).
- 이 함수는 잔고집계(`portfolio_cash.py:209`)와 **mock buy precheck read**(`order_validation.py:513`) 양쪽이 호출하므로 한 변경으로 둘 다 해결.
- live/US read(`inquire_integrated_margin`, `inquire_overseas_margin`, `fetch_my_stocks`)는 무변경.

### 4.4 결함 (c) — "조회실패(미반영)" 명시 (consumer-safe)

`portfolio_cash.get_cash_balance_impl`에서 source별 except가 잡힐 때마다 `unavailable_sources`를 누적해 `summary`에 노출하고, `get_available_capital_impl` summary로도 전파한다.

```jsonc
"summary": {
  "total_krw": 0.0, "total_usd": 0.0,
  "unavailable_sources": [
    {"account": "kis_domestic", "market": "kr", "reason": "ReadTimeout"}
  ]
}
```

- **`accounts[]`엔 placeholder row를 넣지 않는다.** 이유: `order_validation.py:498` `_live_kis_orderable`은 `accounts[]`에서 `kis_domestic` row를 찾고 **부재 시 `raise RuntimeError`**(loud)한다. placeholder row(`orderable: None`)를 넣으면 `None or 0.0 == 0.0`을 반환해 **live buy precheck가 실패를 silent 0으로 오인**하는 회귀가 생긴다. row를 넣지 않으면 raise-on-missing이 보존된다. `total_orderable_krw` 합산(`:375` `or 0.0`)도 영향 없음.
- `errors[]`의 사유는 (a)로 이미 non-empty. `unavailable_sources`는 그것을 first-class·machine-readable로 승격 → 호출 에이전트가 "현금 0"과 "조회 실패"를 구분.
- 적용 범위: `get_cash_balance_impl`의 toss/upbit/kis_kr/kis_us 모든 caught source 실패를 `unavailable_sources`에 반영(정직성, 추가 비용 미미). `summary`는 기존 키에 `unavailable_sources`만 additive 추가.

---

## 5. 테스트 (TDD)

| 대상 | 단언 |
|---|---|
| `describe_exception` 단위 | `ReadTimeout('')` → `"ReadTimeout"`; `RuntimeError("EGW00201 x")` → `"EGW00201 x"` |
| `_place_order_impl` | 실행부가 `httpx.ReadTimeout('')` raise → `result["error"] == "ReadTimeout"` (not `""`) |
| `get_cash_balance_impl` (mock) | kis read가 `ReadTimeout` → `errors[].error == "ReadTimeout"`, `summary.unavailable_sources`에 `kis_domestic`, `total_krw`에 kis 미포함, **`accounts[]`에 `kis_domestic` row 없음** |
| `get_available_capital_impl` | `summary.unavailable_sources` 전파, `total_orderable_krw` 불변 |
| 회귀가드 | `_live_kis_orderable`은 kis row 부재 시 여전히 `raise`(placeholder 없음 증명) |
| 타임아웃 | `inquire_domestic_cash_balance(is_mock=True)` → request 레이어에 `timeout=10`; `is_mock=False` → `timeout=5` |
| `base.py` | `last_error=ReadTimeout('')`일 때 `RateLimitExceededError` 메시지 non-empty |

테스트는 DB/실네트워크 없이 모킹(`_request_with_rate_limit` / kis client 메서드 / httpx 예외 주입)으로 구성.

---

## 6. 영향 / 호환성

- **DB 마이그레이션 0** (스키마/모델 변경 없음).
- MCP 계약은 **additive**: `summary.unavailable_sources`는 신규 optional 필드, `error` 문자열이 빈→비-빈으로만 바뀜 → 하위호환.
- 런타임 LLM 경계·브로커 mutation 경로 무관(읽기/에러표면화/타임아웃 상수만).
- 운영자 영향: 배포 후 잔고/주문 도구가 실패 사유를 구체적으로 보고, `unavailable_sources`로 "현금 0 오인" 차단. MCP 재시작 외 별도 operator 조치 없음.

---

## 7. 변경 파일 요약

- `app/core/exceptions.py` (신규) — `describe_exception`
- `app/mcp_server/tooling/order_execution.py` — `:1128`, `:1138`
- `app/mcp_server/tooling/portfolio_cash.py` — `:252`, `:303`, `unavailable_sources` (get_cash_balance_impl + get_available_capital_impl summary)
- `app/services/brokers/kis/account.py` — `:565` timeout mock-aware
- `app/services/brokers/kis/base.py` — `:544`, `:557` 로그/예외 문구 보강
- 테스트 — 위 7개 케이스
