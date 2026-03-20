# CoinGecko `coins/list` Redis 공유 캐시 설계 (`get_crypto_profile` 경로)

작성일: 2026-02-17  
상태: 승인됨 (brainstorming 결과 고정)

## 1. 배경 및 문제

- Sentry trace `a54c5b6dd3ba41adba1b3b4f697610e8`(2026-02-17 08:59:00 UTC)에서 `tools/call get_crypto_profile` 총 처리 시간은 약 5.1초였다.
- 동일 trace의 `http.client` span 중 `GET https://api.coingecko.com/api/v3/coins/list`가 약 4.66초로 병목이었다.
- 반면 같은 trace의 `GET https://api.coingecko.com/api/v3/coins/raydium`는 약 0.36초로 상대적으로 짧았다.
- 현재 `coins/list`는 프로세스 메모리 캐시만 사용하므로, 멀티 프로세스 환경/재시작 시 콜드 스타트가 반복되어 동일 호출이 재발한다.

목표:

- `get_crypto_profile` 경로의 `coins/list` 호출 빈도를 유의미하게 줄인다.
- `coins/{id}`(상세 프로필) 동작은 변경하지 않는다.
- 외부 계약(도구 입력/출력)과 오류 표면은 유지한다.

## 2. 확정 요구사항

- 저장소 계층: `Redis + 프로세스 메모리` 2단 캐시
- 캐시 대상: CoinGecko `coins/list`만
- TTL: 24시간(86400초)
- TTL 만료 후 재조회 실패 시 stale fallback 미사용 (만료 캐시 재사용 금지)
- 성공 판단: 절대 latency 목표 없음, `coins/list` 호출 횟수 감소 중심

## 3. 접근 대안 및 선택

### 대안 A: 메모리 TTL만 24시간으로 확장

- 장점: 구현 최소
- 단점: 프로세스 간 공유 불가, 재시작 콜드 스타트 반복

### 대안 B (선택): `coins/list` Redis 공유 캐시 + 메모리 hot cache

- 장점: 프로세스 간 캐시 공유, 기존 코드 영향 범위 최소
- 단점: Redis read/write 및 payload 검증 코드 추가 필요

### 대안 C: 파일 스냅샷 기반 심볼 맵

- 장점: API 호출 절감 가능
- 단점: 운영 복잡도 증가, 현재 요구 대비 과도함

결론: 요구사항/리스크 기준에서 대안 B를 채택한다.

## 4. 아키텍처

## 4.1 경계 및 책임

- 대상 모듈: `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/fundamentals_sources_coingecko.py`
- 수정 대상 함수: `_get_coingecko_symbol_to_ids()`
- 유지되는 사항:
  - `_resolve_coingecko_coin_id()`의 override/후보 선택 정책
  - `_fetch_coingecko_coin_profile()`의 기존 메모리 TTL 정책
  - 도구 반환 계약 및 오류 응답 포맷

## 4.2 캐시 계층/키

- L1 메모리: `_COINGECKO_LIST_CACHE`
- L2 Redis: 단일 키 `coingecko:coins:list:v1`
- Redis 값: `dict[str, list[str]]`를 JSON 직렬화해 저장
- Redis TTL: `EX 86400`
- 메모리 TTL: Redis와 동일한 만료 시각 기준으로 갱신

## 4.3 동시성

- 기존 `_COINGECKO_LIST_LOCK` 유지
- lock 내부에서 메모리와 Redis를 재검사하여 single-flight 보장
- lock 경합 시 기존 await 흐름을 유지해 중복 원격 호출 최소화

## 5. 데이터 흐름

`_get_coingecko_symbol_to_ids()`의 확정 플로우:

1. L1 메모리 TTL 검증
2. miss 시 lock 획득
3. lock 내부 L1 재검증
4. miss 시 Redis `GET coingecko:coins:list:v1`
5. Redis hit + payload 유효 시 L1 갱신 후 반환
6. Redis miss/무효 payload면 CoinGecko `coins/list` 원격 호출
7. 성공 시 symbol map 생성 후 Redis `SET ... EX 86400`, L1 갱신 후 반환
8. 원격 호출 실패 시 예외 전파 (stale fallback 없음)

## 6. 오류 처리 정책

- Redis 연결/명령 오류:
  - 경고 로그 후 원격 호출로 기능 열화
  - 전체 요청 실패로 승격하지 않음
- Redis payload 파손/스키마 불일치:
  - 캐시 miss로 처리하고 원격 재조회
- CoinGecko 원격 호출 오류:
  - 기존처럼 예외 전파
  - 상위 `get_crypto_profile` 오류 응답 경로 유지

## 7. 테스트 전략

대상 파일: `/Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py`

추가/보강 케이스:

1. 메모리 hit 시 Redis/원격 호출이 발생하지 않는지 검증
2. Redis hit 시 원격 `coins/list` 호출이 발생하지 않는지 검증
3. Redis miss 시 원격 호출 후 Redis set이 수행되는지 검증
4. Redis payload 손상 시 원격 재조회로 복구되는지 검증
5. Redis 오류 시 원격 재조회로 동작이 유지되는지 검증
6. 동시 호출 시 `coins/list` 원격 호출 횟수가 1회로 수렴하는지 검증

## 8. 관측 및 성공 기준

Sentry에서 아래 span 빈도를 전/후 24시간 비교한다.

- `span.description:"GET https://api.coingecko.com/api/v3/coins/list"`
- 범위: `transaction:"tools/call get_crypto_profile"`

성공 기준:

- 절대 p95 목표 대신 `coins/list` outbound 호출 빈도가 유의미하게 감소

권장 로그 이벤트:

- `coingecko_list_cache_memory_hit`
- `coingecko_list_cache_redis_hit`
- `coingecko_list_cache_remote_fetch`
- `coingecko_list_cache_redis_error`

## 9. 비목표

- `coins/{id}` 상세 프로필 캐시 TTL/정책 변경
- `screen_stocks`의 `coins/markets` 캐시 구조 통합
- MCP README의 사용자 계약 변경
- 캐시 스키마 다중 버전 마이그레이션
