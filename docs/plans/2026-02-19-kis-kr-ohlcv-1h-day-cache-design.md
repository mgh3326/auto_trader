# KIS KR OHLCV 1H + Day Redis Cache Design

작성일: 2026-02-19  
상태: 승인됨 (brainstorming 결과 고정)

## 1. 배경

- MCP `get_ohlcv`는 현재 KR 주식에서 `day/week/month`만 지원하고 `1h`는 거부한다.
- KIS KR 분봉 API는 호출당 반환량 제한이 있어(문서상 `inquire-time-itemchartprice` 30건, `inquire-time-dailychartprice` 120건) 장시간 구간을 매번 원격 호출로만 제공하기 어렵다.
- 사용자 요구:
  - KR `1h` 지원
  - `1h` 미완성봉 제거하지 않음 (Upbit/Yahoo와 동일 정책)
  - Redis 캐시 포함
  - KR 일봉(`day`)도 이번에 함께 캐시 적용

## 2. 요구사항 확정

- `get_ohlcv(symbol, market="kr", period="1h")`를 지원한다.
- KR `1h`는 미완성봉을 포함한다.
- KR `day`는 Redis 캐시를 적용한다.
- KR `week/month`는 이번 범위에서 기존 동작 유지(비캐시).
- `end_date`가 지정된 요청은 캐시를 우회한다.
- KIS 모의환경에서 `1h` 분봉 API 미지원일 때는 명시적 에러를 반환한다.
- 응답 계약(`symbol`, `source`, `period`, `rows` 등)은 기존 `get_ohlcv` 형식을 유지한다.

## 3. 대안 검토 및 선택

### 대안 A: KR `1h`만 추가 (비캐시)
- 장점: 구현 단순.
- 단점: 호출량/지연/레이트리밋 리스크 큼.

### 대안 B: KR `1h` 캐시 + KR `day` 캐시만 추가 (채택)
- 장점: 이번 요구를 충족하면서 변경 범위를 통제 가능.
- 단점: `week/month` 캐시는 후속 과제로 남음.

### 대안 C: KR `1h/day/week/month` 캐시 일괄 도입
- 장점: 장기적으로 일관성 높음.
- 단점: 변경량과 테스트 범위가 과도하게 커짐.

## 4. 아키텍처

## 4.1 MCP 계층 (`market_data_quotes`)

- KR `1h` 금지 guard를 제거하고 허용한다.
- KR 분기:
  - `period == "1h"`: KR 분봉 집계 경로 사용
  - `period == "day"`: KR 일봉 캐시 경로 사용
  - `period in {"week","month"}`: 기존 `inquire_daily_itemchartprice` 유지

## 4.2 KIS 서비스 계층 (`app/services/kis.py`)

- 신규 메서드 추가:
  - `inquire_time_dailychartprice(...)`:
    - 엔드포인트: `/uapi/domestic-stock/v1/quotations/inquire-time-dailychartprice`
    - TR: `FHKST03010230`
    - 반환 분봉(`output2`)을 DataFrame으로 정규화
  - 내부 집계 헬퍼:
    - 분봉 DataFrame -> `1h` OHLCV로 집계 (`datetime.floor("60min")`)
    - 마지막(진행 중) 시간버킷도 포함

## 4.3 Redis 캐시 계층 (`app/services/kis_ohlcv_cache.py`, 신규)

- 지원 period: `day`, `1h`
- 키:
  - `kis:ohlcv:{period}:v1:{symbol}:dates`
  - `kis:ohlcv:{period}:v1:{symbol}:rows`
  - `kis:ohlcv:{period}:v1:{symbol}:meta`
  - `kis:ohlcv:{period}:v1:{symbol}:lock`
- 동작:
  - hit + 충분: 캐시 반환
  - 부족: 원격 fetch 후 upsert
  - 최신 버킷 overwrite 허용(특히 `1h` partial candle 갱신 목적)
  - Redis 장애/락 실패: raw fallback

## 4.4 `end_date` 정책

- `end_date is None`: 캐시 경로 사용
- `end_date` 지정: 캐시 우회 (deterministic 조회 + 캐시 오염 방지)

## 5. 설정

`app/core/config.py`에 KIS 캐시 설정 추가:

- `kis_ohlcv_cache_enabled: bool = True`
- `kis_ohlcv_cache_max_days: int` (`day` retention)
- `kis_ohlcv_cache_max_hours: int` (`1h` retention)
- `kis_ohlcv_cache_lock_ttl_seconds: int`

## 6. 에러 처리

- KIS `1h` 모의투자 미지원 응답코드/메시지는 `RuntimeError`로 정규화하고,
  MCP에서 기존 에러 payload 형식(`source="kis"`)으로 반환.
- KIS 응답 필드 누락/파싱 실패 시 해당 요청 실패 처리.
- Redis 예외는 캐시 기능만 포기하고 조회 기능은 유지.

## 7. 테스트 전략

- `tests/test_mcp_server_tools.py`
  - KR `1h` 성공 라우팅 테스트 추가
  - KR `1h` 기존 reject 테스트 제거/대체
  - KR `day` 캐시 경유 테스트 추가
  - `end_date` 지정 시 캐시 우회 테스트 추가
  - KR `1h` mock 미지원 에러 payload 테스트 추가
- 신규 `tests/test_kis_ohlcv_cache.py`
  - hit/miss/lock/retention/fallback 검증
- (필요 시) `tests/test_services.py` 또는 전용 테스트 파일에
  - 분봉 -> 1시간봉 집계 검증

## 8. 문서 동기화

- `app/mcp_server/README.md` 업데이트:
  - `1h` 지원 범위를 `US equity + crypto`에서 `KR/US equity + crypto`로 갱신
  - KR `1h`는 미완성봉 포함 정책 명시

## 9. 비목표

- KR `week/month` 캐시 도입
- KR `1m/3m/5m/...` 공개 API 확장
- WebSocket 기반 실시간 봉 스트리밍
