# Upbit get_cash_balance orderable 필드 설계

## 1. 배경
- 현재 MCP `get_cash_balance` 응답에서 Upbit 계좌는 `balance`만 반환한다.
- KIS 계좌는 `balance`와 `orderable`을 함께 반환하므로, 계좌 간 의미가 일관되지 않는다.
- 사용자 요구사항은 Upbit도 KIS와 유사하게 `balance`(총액)와 `orderable`(실주문가능)을 동시에 제공하는 것이다.

## 2. 합의된 요구사항
- Upbit 응답 의미를 다음으로 고정한다.
  - `balance`: 총 KRW 잔액 (`가용 + locked`)
  - `orderable`: 실주문가능 KRW (`가용`)
- `locked`는 응답 필드로 노출하지 않는다.
- 기존 호출부 혼란을 줄이기 위해 의미가 모호한 함수명 사용을 단계적으로 정리한다.

## 3. 대안 검토
### 대안 A: `portfolio_cash`에서 직접 계산
- `get_cash_balance_impl()`에서 `fetch_my_coins()`를 직접 호출해 Upbit 금액 계산.
- 장점: 변경 범위 최소.
- 단점: Upbit 도메인 로직이 MCP 계층으로 새어 나와 중복/결합도 증가.

### 대안 B: Upbit 서비스에 요약 함수 추가 (채택)
- `app/services/upbit.py`에 KRW 요약 함수 추가.
- `portfolio_cash`는 요약 함수 결과만 소비.
- 장점: 계산 규칙이 서비스 경계에 모이고 재사용/테스트 용이.
- 단점: 함수 추가 및 테스트 보강 필요.

### 대안 C: 기존 `fetch_krw_balance()` 의미 변경
- 기존 함수를 총액 반환으로 바꾸고 모든 호출부 재정렬.
- 장점: 함수 수 증가 없음.
- 단점: 기존 의미(주문 가능 잔고) 의존 코드 회귀 위험이 큼.

## 4. 최종 설계
### 4.1 서비스 계층 (`app/services/upbit.py`)
- 신규 함수:
  - `fetch_krw_cash_summary() -> dict[str, float]`
  - 반환 예: `{"balance": 700000.0, "orderable": 500000.0}`
- 명시 함수:
  - `fetch_krw_orderable_balance() -> float` (기존 `fetch_krw_balance` 의미를 명시화)
- 호환 함수:
  - `fetch_krw_balance()`는 즉시 제거하지 않고 `fetch_krw_orderable_balance()`를 위임 호출하는 호환 래퍼로 유지.
  - docstring에 신규 코드에서는 `fetch_krw_orderable_balance()` 또는 `fetch_krw_cash_summary()`를 사용하도록 명시.

### 4.2 MCP cash 응답 계층 (`app/mcp_server/tooling/portfolio_cash.py`)
- Upbit 분기에서 `fetch_krw_balance()` 대신 `fetch_krw_cash_summary()` 호출.
- Upbit 계좌 응답 예:
  - `balance`: 총 KRW
  - `orderable`: 실주문가능 KRW
  - `formatted`: 총 KRW 기준 문자열
- `locked`는 응답에 포함하지 않음.

### 4.3 계산 규칙
1. `fetch_my_coins()`에서 KRW row 조회
2. `orderable = float(row["balance"] or 0)`
3. `locked = float(row["locked"] or 0)`
4. `balance = orderable + locked`
5. KRW row 없음: `balance=0.0`, `orderable=0.0`

## 5. 에러 처리
- Upbit API 예외 발생 시 기존 `get_cash_balance_impl` 동작 유지:
  - 전체 조회(`account=None`)에서는 `errors`에 누적하고 나머지 계좌 계속 반환
  - strict fail-close 동작은 KIS 전용 현재 정책을 유지 (Upbit는 이번 범위에서 변경하지 않음)

## 6. 테스트 설계
### 6.1 수정 테스트
- `tests/test_mcp_server_tools.py`
  - `test_get_cash_balance_all_accounts`
  - `test_get_cash_balance_with_account_filter`
  - `test_get_cash_balance_partial_failure`
- Upbit mocking을 `fetch_krw_balance`에서 `fetch_krw_cash_summary`로 전환.

### 6.2 신규 테스트
- `tests/test_mcp_server_tools.py`에 케이스 추가:
  - Upbit KRW row가 `balance=500000`, `locked=200000`일 때
  - 결과 `balance=700000`, `orderable=500000` 검증.

### 6.3 서비스 단위 테스트(선택)
- `app/services/upbit.py`의 요약 계산 함수를 독립 검증하는 테스트 추가 검토.

## 7. 문서 반영
- `app/mcp_server/README.md`의 `get_cash_balance` 계약 설명에 Upbit `orderable` 필드 추가.

## 8. 범위 제외
- Upbit strict 모드 에러 정책 변경
- `locked` 필드 외부 노출
- 주문 실행/검증 로직의 의미 변경
