# Upbit 서비스 경계 캐시 설계 (Day/Week/Month 확정봉 공통)

작성일: 2026-02-16  
상태: 승인됨 (brainstorming 결과 고정)

## 1. 배경 및 목표

현재 Upbit OHLCV 캐시는 MCP 도구 계층(`market_data_indicators`)에 결합되어 있어, 캐시 적용 지점이 늘어날수록 호출부 분기가 확산될 수 있다.  
목표는 캐시 책임을 Upbit 서비스 경계(`app/services/upbit.py`)로 이동해, 호출부 분기 증가 없이 일관된 확정봉 정책을 강제하는 것이다.

핵심 목표:

- `fetch_ohlcv()` 하나의 진입점에서 캐시/백필/폴백을 처리
- `day/week/month` 모두 "미확정 진행봉 제외" 정책 통일
- 기존 호출부 변경 최소화(투명 캐시)

## 2. 확정된 요구사항

- 캐시는 Upbit API 호출에 가장 가까운 계층에 둔다.
- 적용 범위는 `fetch_ohlcv(period in {"day","week","month"})` 전체다.
- 확정봉 판정 기준은 UTC 고정이 아니라 **KST 09:00 경계** 기반이다.
- 공개 API는 `fetch_ohlcv()`를 유지하고 내부에 투명 캐시를 적용한다.
- 하위호환 키 마이그레이션은 불필요(아직 미배포).

## 3. 접근 대안 및 선택

### 대안 A (선택): `fetch_ohlcv()` 내부 투명 캐시

- 장점: 호출부 수정 최소, 정책 일원화, 빠른 적용
- 단점: `upbit.py` 책임 증가

### 대안 B: `fetch_ohlcv_cached()` 별도 함수 추가

- 장점: 기존 함수 단순성 유지
- 단점: 호출부 점진 전환 필요, 분기 확산 지속 가능

### 대안 C: `_request_json` 레벨 raw 캐시

- 장점: 네트워크 절감
- 단점: 확정봉 정책 반영 어려움, 키/정합성 관리 복잡

결론: 현재 목표 대비 대안 A가 가장 실용적이다.

## 4. 아키텍처 변경

## 4.1 경계 및 책임

- `app/services/upbit.py`
  - 공개 진입점: `fetch_ohlcv()`
  - 내부에서 캐시 경로/원본 경로 선택
  - 원본 Upbit 호출은 `_fetch_ohlcv_raw()`로 분리
- `app/services/upbit_ohlcv_cache.py`
  - `period`를 인자로 받아 공통 로직 처리
  - 원본 데이터 획득은 콜백(예: `_fetch_ohlcv_raw`) 주입 방식으로 호출
  - `upbit.fetch_ohlcv()` 재호출 금지(순환 의존 방지)

## 4.2 캐시 키 구조

하위호환 고려 없이 period 분리 키 사용:

- `upbit:ohlcv:{period}:v1:{market}:dates`
- `upbit:ohlcv:{period}:v1:{market}:rows`
- `upbit:ohlcv:{period}:v1:{market}:meta`
- `upbit:ohlcv:{period}:v1:{market}:lock`

`meta` 필드:

- `last_closed_bucket`
- `oldest_bucket`
- `oldest_confirmed`
- `last_sync_ts`

## 5. 데이터/시간 정책

## 5.1 KST 09:00 앵커 기반 확정봉

공통 함수: `get_last_closed_bucket_kst(period, now)`

- Day:
  - KST 09:00 이전: `D-2`
  - KST 09:00 이후: `D-1`
- Week:
  - 기준점: 월요일 09:00 KST
  - 기준점 이전: `W-2`
  - 기준점 이후: `W-1`
- Month:
  - 기준점: 매월 1일 09:00 KST
  - 기준점 이전: `M-2`
  - 기준점 이후: `M-1`

주의: 계산 시 UTC를 사용할 수는 있으나, 경계 판정은 반드시 KST 기준으로 변환해 처리한다.

## 5.2 `fetch_ohlcv()` 동작

1. 입력 검증(`days <= 200`, `period` 유효성)
2. `period in {"day","week","month"}` + 캐시 활성:
   - 캐시 서비스 호출(`get_closed_candles(period, market, count, raw_fetcher)`)
3. 캐시 결과가 `None`이면 raw fallback
4. raw 결과도 마지막에 `<= last_closed_bucket` 필터 적용
5. 오름차순 정렬/중복 제거 후 반환

## 5.3 백필 로직

- Stage A (forward fill): latest cached bucket가 `last_closed_bucket`에 도달할 때까지 최신 누락분 보강
- Stage B (backward fill): 요청 count 부족분만 과거 방향 보강
- `oldest_confirmed`는 Stage B 중단 조건에만 사용
- 미확정 진행봉은 저장/반환에서 제외

## 6. 장애/락 처리

- 락 획득 실패 시 짧은 재시도(기존 정책 유지)
- 재시도 후:
  - 캐시 충분하면 캐시 반환
  - 캐시 부족하면 `None` 반환해 호출자 raw fallback 유도
- 캐시 관련 예외는 기능 열화(fallback)로 처리하고 전체 실패로 전파하지 않는다.

## 7. 테스트 전략

## 7.1 캐시 단위 테스트 (`tests/test_upbit_ohlcv_cache.py`)

- day/week/month 진행봉 제외 검증
- count 충족 상태에서도 latest closed 누락 시 forward fill 검증
- `oldest_confirmed=true` 상태에서 latest 갱신 유지 검증
- 락 경합 시 충분 캐시 반환 / 부족 캐시 `None` 반환

## 7.2 서비스 경계 테스트 (신규 또는 `tests/test_services.py`)

- `upbit.fetch_ohlcv()`의 캐시 경로 진입 검증
- 캐시 `None` 시 raw fallback + closed filter 검증
- period별 키 분리 검증

## 7.3 상위 레이어 회귀

- MCP 도구 테스트는 "캐시 모듈 호출 여부"보다 최종 데이터 계약(확정봉 반환) 중심으로 검증

## 8. 롤아웃 및 관측

- 플래그: `upbit_ohlcv_cache_enabled` 재사용
- 사전 검증:
  - 동일 요청 2회 시 Upbit API 호출 감소
  - KST 09:00 경계 전/후 결과 전환 검증
- 관측 로그:
  - cache hit/miss
  - forward/backward fill rows
  - fallback count
  - period별 hit ratio

## 9. 비목표 (이번 범위 제외)

- DB+Redis 이중화
- 분봉/티커 캐시
- 장기 운영 지표 대시보드 구축
- 키 스키마 하위호환 마이그레이션

