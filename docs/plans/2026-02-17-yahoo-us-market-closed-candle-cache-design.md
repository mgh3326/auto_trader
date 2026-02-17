# Yahoo 서비스 경계 확정봉 Redis 캐시 설계 (NYSE 캘린더 기반)

작성일: 2026-02-17  
상태: 승인됨 (brainstorming 결과 고정)

## 1. 배경 및 문제

- 트레이스 `a87addcf67ab4622b70f85651fe2756c`에서 `http.client` span이 과다하게 발생해 yfinance 요청이 집중되고 있다.
- 현재 `/Users/robin/PycharmProjects/auto_trader/app/services/yahoo.py`의 `fetch_ohlcv()`는 매 호출마다 `yf.download()`를 수행한다.
- Upbit는 서비스 경계 확정봉 캐시가 이미 적용되어 있으나 Yahoo는 동일 패턴이 없어 반복 조회 비용이 크다.

목표:

- Yahoo OHLCV 조회에 `day/week/month` 확정봉 Redis 캐시를 적용한다.
- 확정 기준은 고정 UTC/KST 경계가 아니라 미국장(ET) 실제 거래 세션 마감 기준으로 처리한다.
- 기존 상위 호출부 변경 없이 `fetch_ohlcv()` 단일 진입점에서 투명 캐시를 제공한다.

## 2. 확정 요구사항

- 적용 범위: Yahoo `fetch_ohlcv(period in {"day","week","month"})`
- 캘린더: `exchange_calendars`의 `XNYS`
- day/week/month 모두 "진행 중 버킷 제외, 마감된 버킷만 반환"
- 장애 시 캐시는 기능 열화(fallback)로 동작하고 raw 경로를 보장
- 반환 계약(`date/open/high/low/close/volume` DataFrame)은 유지

## 3. 접근 대안 및 선택

### 대안 A (선택): Yahoo 서비스 경계 투명 캐시

- 내용: `app/services/yahoo.py` 내부에서 캐시를 우선 적용하고, 실패 시 raw fallback
- 장점: 호출부 수정 최소, Upbit 패턴과 일관, 빠른 적용
- 단점: Yahoo 서비스 책임 증가

### 대안 B: Upbit/Yahoo 공통 캐시 엔진 통합

- 장점: 중복 제거
- 단점: 리팩터링 범위가 커져 이번 목표 대비 리스크 증가

### 대안 C: MCP 도구 계층 캐시

- 장점: 국소적 변경 가능
- 단점: 호출 경로별 분기 확산, 누락 가능성 증가

결론: 현재 요구사항과 리스크 기준에서 대안 A를 선택한다.

## 4. 아키텍처

## 4.1 경계 및 책임

- `/Users/robin/PycharmProjects/auto_trader/app/services/yahoo.py`
  - 공개 진입점: `fetch_ohlcv()`
  - 내부에서 캐시 경로 선택
  - raw 호출은 `_fetch_ohlcv_raw()`로 분리
- 신규 `/Users/robin/PycharmProjects/auto_trader/app/services/yahoo_ohlcv_cache.py`
  - NYSE 캘린더 기반 확정 버킷 계산
  - Redis read/backfill/lock/fallback
  - `raw_fetcher` 콜백 호출 (순환 의존 방지)

## 4.2 Redis 키 구조

- `yahoo:ohlcv:{period}:v1:{ticker}:dates`
- `yahoo:ohlcv:{period}:v1:{ticker}:rows`
- `yahoo:ohlcv:{period}:v1:{ticker}:meta`
- `yahoo:ohlcv:{period}:v1:{ticker}:lock`

`meta` 필드:

- `last_closed_bucket`
- `oldest_date`
- `oldest_confirmed`
- `last_sync_ts`

## 5. 데이터/시간 정책 (NYSE 캘린더)

## 5.1 공통 원칙

- `now`는 timezone-aware UTC로 정규화
- 캘린더는 `XNYS` 세션 close 시각을 기준으로 확정 여부 판단
- 고정 시각 경계(UTC 일자/ KST 09:00)를 확정 기준으로 사용하지 않음

## 5.2 period별 확정 버킷

- Day:
  - `close_ts <= now`인 마지막 거래일
- Week:
  - 세션을 ISO week 단위로 그룹화
  - 각 week의 `max(close_ts)`를 계산
  - `max(close_ts) <= now`인 마지막 week
- Month:
  - 세션을 `(year, month)`로 그룹화
  - 각 month의 `max(close_ts)`를 계산
  - `max(close_ts) <= now`인 마지막 month

이 정책으로 DST, 휴장일, 조기폐장을 자연스럽게 반영한다.

## 5.3 적재/반환 필터

- raw fetch 결과 중 `date <= target_closed_bucket_date`만 저장/반환
- 진행 중 버킷 데이터는 캐시에 적재하지 않는다.

## 6. 캐시 플로우

1. `yahoo.fetch_ohlcv()`에서 period가 day/week/month이고 캐시 활성 시 캐시 모듈 호출
2. 캐시 hit면 즉시 반환
3. miss면 lock 획득 후 backfill 수행
4. backfill은 2단계:
   - Stage A: latest를 target_closed_bucket까지 forward fill
   - Stage B: requested_count 부족분만 과거 방향으로 backfill
5. lock 해제 후 재조회 반환
6. 캐시 실패 시 `None` 반환, 호출자가 raw fallback 수행

## 7. 에러/락/폴백 정책

- 캐시 모듈 예외는 서비스 장애로 승격하지 않고 경고 로그 후 fallback
- lock 획득 실패 시 짧은 재시도
- 재시도 후:
  - 캐시 충분하면 캐시 반환
  - 여전히 부족하면 `None` 반환해 raw fallback 유도

## 8. 설정

- `/Users/robin/PycharmProjects/auto_trader/app/core/config.py`에 Yahoo 캐시 설정 추가:
  - `yahoo_ohlcv_cache_enabled: bool = True`
  - `yahoo_ohlcv_cache_max_days: int = 400`
  - `yahoo_ohlcv_cache_lock_ttl_seconds: int = 10`

## 9. 테스트 전략

## 9.1 캐시 모듈 테스트 (신규)

- 파일: `/Users/robin/PycharmProjects/auto_trader/tests/test_yahoo_ohlcv_cache.py`
- 케이스:
  - day/week/month 확정 버킷 계산
  - DST 경계, 휴장일, 조기폐장 시나리오
  - cache hit / partial hit / forward fill / backward fill
  - lock 경합 및 fallback
  - oldest_confirmed 동작

## 9.2 서비스 경계 테스트 (보강)

- 파일: `/Users/robin/PycharmProjects/auto_trader/tests/test_services.py`
- 케이스:
  - `yahoo.fetch_ohlcv()` 캐시 경로 우선 적용
  - 캐시 `None` 시 raw fallback
  - 반환 스키마/row count 계약 유지

## 9.3 상위 회귀 검증

- `market_data_indicators`, `market_data_quotes`, `analysis` 경로에서 계약 불변 확인
- 관측 검증: 동일 요청 반복 시 yfinance outbound 호출수 감소 확인

## 10. 관측

- 로그 키워드:
  - `yahoo_ohlcv_cache hit/miss/forward_fill/backfill/trimmed/fallback`
- Sentry 추적 지표:
  - Yahoo 관련 `http.client` span 개수/총 시간 감소
  - 캐시 fallback 빈도

## 11. 비목표

- `yf.Ticker(...).info`, `fast_info`, `screen` 캐시
- Upbit/Yahoo 캐시 공통 엔진 리팩터링
- 분봉/실시간 틱 캐시
