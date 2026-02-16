# Crypto 실시간 RSI 공통화 설계 (배치 Ticker + 2초 메모리 TTL)

작성일: 2026-02-16  
상태: 승인됨 (brainstorming 결과 고정)

## 1. 배경 및 목표

현재 crypto RSI 계산은 호출 경로마다 OHLCV 조회와 지표 계산이 분산되어 있어, 동시 요청 시 Upbit API 호출이 fan-out 되는 문제가 있다. 최근 도입한 Upbit OHLCV 확정봉 캐시로 일봉 조회 비용은 줄었지만, "오늘 시점" RSI를 위해 추가 조회가 반복되면서 호출량이 다시 증가할 수 있다.

핵심 목표:

- crypto RSI 계산 경로를 공통화해 호출 중복을 줄인다.
- RSI는 **확정 일봉 + 현재가(Ticker)** 기반의 실시간 값으로 제공한다.
- 필드 계약을 `rsi` 단일 필드로 통일한다 (`rsi_14` 제거).
- Upbit API 제한(초당 10회)은 API 호출 지점에서만 제어한다.

## 2. 확정된 요구사항

- 적용 범위는 crypto RSI를 사용하는 전 경로다.
  - `screen_stocks` crypto 경로
  - `recommend_stocks` crypto 경로
  - `get_indicators`/portfolio/quotes/DCA에서 crypto RSI를 사용하는 경로
- Ticker는 배치 조회를 사용한다.
- Ticker 캐시는 **전역 메모리 TTL 2초**를 사용한다.
- 동일 시점 동시 요청은 in-flight dedupe로 1회 호출만 수행한다.
- `order_execution` 등 체결 민감 경로는 Ticker 캐시를 사용하지 않는다.
- RSI 계산용 OHLCV 목표 조회량은 200개로 상향한다.
- RSI 계산 가능 최소 기준은 `close` 유효값 15개 이상이다.
  - 15개 미만이면 `rsi=None`
  - 상장 200일 미만 종목도 15개 이상이면 계산한다.
- task 수준 semaphore는 제거하고, API 호출 제한은 Upbit rate limiter만 사용한다.

## 3. 접근 대안 및 선택

### 대안 A (선택): 실시간 RSI 엔진 공통화 + Ticker 단기 메모리 캐시

- 장점: 호출 경로 일관성, 호출량 절감 효과 최대, 정책 중앙화
- 단점: 초기 리팩터링 범위가 큼

### 대안 B: 경로별 부분 최적화

- 장점: 부분 적용이 빠름
- 단점: 중복 코드 증가, 정책 불일치 가능

### 대안 C: Redis 기반 초단기 Ticker 캐시

- 장점: 멀티 인스턴스 공유 가능
- 단점: 2초 TTL 대비 왕복/직렬화 오버헤드, 복잡도 증가

결론: 현재 요구사항과 운영 특성(초단기 TTL, 워커 환경) 기준으로 대안 A가 가장 실용적이다.

## 4. 아키텍처 변경

## 4.1 공통 책임 분리

- `app/services/upbit.py`
  - `fetch_multiple_current_prices_cached(..., ttl_seconds=2, use_cache=True)` 추가
  - 심볼 정규화 + 부분 hit + in-flight dedupe + TTL 만료 처리
  - 기존 `fetch_multiple_current_prices`는 raw 호출 엔진으로 유지
- `app/mcp_server/tooling/market_data_indicators.py`
  - crypto 실시간 RSI 공통 계산 함수 추가
  - 내부에서 OHLCV(200) + batch ticker 결합 후 RSI 산출
- 상위 호출부는 개별 RSI 계산 로직 대신 공통 함수 호출로 통일

## 4.2 필드 계약 정리

- `rsi_14` 필드는 제거한다.
- 내부 계산/정렬/필터/reason/응답은 `rsi`만 사용한다.
- 하위호환 레이어는 두지 않는다.

## 5. 데이터 플로우

1. 입력 symbol 목록을 정규화하고 중복 제거한다.
2. 각 symbol에 대해 `fetch_ohlcv(days=200, period="day")`를 호출한다.
   - Upbit 서비스 경계의 확정봉 캐시를 그대로 활용한다.
3. 전체 symbol을 대상으로 `fetch_multiple_current_prices_cached`를 1회 호출한다.
4. symbol별로 OHLCV close 시퀀스 마지막 값을 ticker 현재가로 교체한다.
   - ticker 값이 없으면 OHLCV 마지막 close를 그대로 사용한다.
5. RSI(14)를 계산해 `rsi` 필드에 기록한다.

## 6. 오류/성능 정책

- `close` 유효값이 15개 미만이면 `rsi=None`.
- ticker 조회 실패 시 OHLCV close 기반 계산으로 폴백한다.
- OHLCV 조회 실패/결측이면 해당 symbol만 `rsi=None` 처리한다.
- task semaphore는 제거한다.
- 호출량 제어는 Upbit API 호출 지점의 rate limiter만 사용한다.
- 관측 로그:
  - ticker cache `hit/miss/partial_hit/inflight_join/bypass`
  - batch 크기, 실제 Upbit 호출 건수
  - RSI 계산 성공/실패/None 건수

## 7. 영향 파일(예정)

- `app/services/upbit.py`
- `app/mcp_server/tooling/market_data_indicators.py`
- `app/mcp_server/tooling/analysis_screen_core.py`
- `app/mcp_server/tooling/analysis_recommend.py`
- `app/mcp_server/tooling/portfolio_holdings.py`
- `app/mcp_server/tooling/market_data_quotes.py`
- `app/mcp_server/tooling/portfolio_dca_core.py` (간접)
- 관련 테스트 파일들 (`tests/test_mcp_screen_stocks.py`, `tests/test_mcp_recommend.py`, `tests/test_mcp_server_tools.py` 등)

## 8. 테스트 전략

## 8.1 단위 테스트

- Ticker 캐시:
  - TTL hit/miss
  - partial hit
  - in-flight dedupe
  - `use_cache=False` bypass
- 실시간 RSI:
  - ticker 반영 시 RSI 변경
  - ticker 미존재 폴백
  - close 15개 미만 `None`
  - close 15개 이상 200개 미만 정상 계산

## 8.2 통합 테스트

- crypto screen/recommend에서 `rsi` 단일 필드 사용 확인
- `rsi_14` 참조 제거 회귀 검증
- order execution 경로 캐시 bypass 검증
- 동일 요청 반복 시 Upbit ticker 호출 감소 검증

## 9. 롤아웃/비목표

- 기본 TTL은 2초, 필요 시 설정화는 후속 작업으로 분리한다.
- 비목표:
  - Redis 기반 티커 캐시
  - 장기 시계열 저장 구조 변경
  - 체결/주문 경로 동작 변경(캐시 bypass 유지)
