# Issue #232 KR get_ohlcv Intraday Design

작성일: 2026-03-09  
상태: 제안안 (analyze-mode synthesized)

## 1. 배경

- 현재 MCP `get_ohlcv`는 `app/mcp_server/tooling/market_data_quotes.py`에서 KR `day/week/month/1h`, US `day/week/month/1h`, crypto `day/week/month/1h/4h`만 지원한다.
- KR `1h`는 `app/services/kr_hourly_candles_read_service.py`의 DB-first 경로를 사용하지만, period validation과 fetch routing은 `app/services/market_data/service.py`와 MCP 계층에 중복되어 있다.
- PR #230에서 도입된 shared OHLCV constants와 `include_indicators` 계약은 현재 브랜치에 없다.
- Issue #232는 KR `1m/5m/15m/30m/1h`를 같은 `get_ohlcv` 계약 안에서 지원하고, 데이터 소스를 `kr_candles_1m` raw + `kr_candles_5m/15m/30m/1h` continuous aggregate + 최근 30분 KIS overlay 조합으로 고정한다.

## 2. 요구사항 확정

- `get_ohlcv(symbol, count=100, period="day", end_date=None, market=None, include_indicators=False)` 시그니처를 유지한다.
- KR 허용 period를 `day|week|month|1m|5m|15m|30m|1h`로 확장한다.
- US는 기존 `day|week|month|1h`, crypto는 기존 minute/`4h` 지원을 유지한다.
- `include_indicators=True`면 각 row에 `rsi_14`, `ema_20`, `bb_upper`, `bb_mid`, `bb_lower`, `vwap`를 추가하고, top-level payload에 `indicators_included`를 추가한다.
- KR intraday row는 `datetime/date/time/open/high/low/close/volume/value`에 `session`, `venues`를 유지한다.
- crypto minute 전용 `timestamp`/`trade_amount` 공개 shape는 그대로 유지한다.
- KR `day` Redis cache 동작은 유지하고, KR intraday(`1m/5m/15m/30m/1h`)는 모두 cache bypass로 고정한다.
- 스케줄러는 `app/tasks/kr_candles_tasks.py`의 `*/10 * * * 1-5`를 유지한다.

## 3. 대안 검토 및 선택

### 대안 A: MCP 계층에 KR minute period만 직접 추가

- 장점: 표면상 변경 파일 수가 적다.
- 단점: `app/services/market_data/service.py`와 MCP가 더 크게 드리프트하고, KR `1h`와 새 minute periods가 서로 다른 읽기 경로를 갖게 된다.

### 대안 B: raw `kr_candles_1m`만 읽고 5m/15m/30m/1h를 모두 on-read 집계

- 장점: 새 CAGG migration이 필요 없다.
- 단점: 이슈에서 고정한 데이터 전략과 다르고, 조회 cost와 테스트 surface가 커진다.

### 대안 C: shared contract 복구 + interval-aware KR intraday reader + 새 CAGG 추가 (채택)

- 장점: 이슈 요구사항과 현재 KR `1h` 구조를 가장 잘 잇는다.
- 장점: validation, cache policy, market guard를 한 곳에서 맞출 수 있다.
- 단점: migration/sql/test 업데이트 범위가 넓다.

## 4. 아키텍처

### 4.1 계약 소유권

- shared period matrix와 공통 에러 메시지는 신규 `app/services/market_data/constants.py`가 소유한다.
- `app/mcp_server/tooling/market_data_quotes.py`와 `app/services/market_data/service.py`는 이 constants를 같이 사용해 동일한 period validation과 market guard를 적용한다.
- 내부 service 계약 `app/services/market_data/contracts.py`의 `Candle` dataclass는 유지한다. 즉, MCP 공개 payload에만 `session`, `venues`, indicator fields를 붙이고, service layer는 core candle fields만 유지한다.
- `include_indicators`는 MCP `get_ohlcv` 공개 계약에서만 복구한다. service-layer `get_ohlcv`는 list[`Candle`] 반환을 유지한다.

### 4.2 Shared period matrix

- `OHLCV_ALLOWED_PERIODS`: `("day", "week", "month", "1m", "5m", "15m", "30m", "4h", "1h")`
- `KR_OHLCV_PERIODS`: `{"day", "week", "month", "1m", "5m", "15m", "30m", "1h"}`
- `US_OHLCV_PERIODS`: `{"day", "week", "month", "1h"}`
- `CRYPTO_OHLCV_PERIODS`: `{"day", "week", "month", "1m", "5m", "15m", "30m", "1h", "4h"}`
- `CRYPTO_ONLY_OHLCV_PERIODS`: `{"1m", "5m", "15m", "30m", "4h"}`
- KR intraday cache policy는 allowlist가 아니라 caller policy로 관리한다. 즉 `kis_ohlcv_cache` 지원 period를 넓히지 않고 caller가 intraday를 cache bypass한다.

### 4.3 KR intraday read service 일반화

- `app/services/kr_hourly_candles_read_service.py`에 `read_kr_intraday_candles(period, symbol, count, end_date=None, now_kst=None)`를 추가한다.
- 기존 `read_kr_hourly_candles_1h()`는 `return await read_kr_intraday_candles(period="1h", ...)` thin wrapper로 남긴다.
- 내부에 period config map을 둔다.
  - `1m` -> raw `public.kr_candles_1m`
  - `5m` -> `public.kr_candles_5m`
  - `15m` -> `public.kr_candles_15m`
  - `30m` -> `public.kr_candles_30m`
  - `1h` -> `public.kr_candles_1h`
- 공통 흐름:
  1. universe row 조회로 `nxt_eligible`와 active 상태 확인
  2. `end_date`를 date cursor로 해석하고 intraday에서는 time component 무시
  3. history는 해당 raw/cagg source에서 closed bucket 위주로 조회
  4. same-day recent window는 DB minute rows + KIS minute rows를 venue-aware merge
  5. merged minute rows를 요청 period로 재집계해 recent closed/partial bucket을 덮어쓴다
  6. DB count가 부족한 same-day 범위만 KIS pagination fallback으로 보강한다

### 4.4 Overlay 및 venue merge 규칙

- overlay window는 최근 30분으로 고정한다. 이유는 sync cron이 10분 단위라 현재 bucket만 보정하면 recently closed 5m/15m bucket 공백이 남기 때문이다.
- KIS minute API는 same-day 보강 용도로만 사용한다. multi-day intraday history의 정답 소스는 TimescaleDB다.
- 동일 minute에 `KRX`와 `NTX`가 함께 있으면:
  - price fields (`open/high/low/close`)는 `KRX` 우선
  - `volume`, `value`는 합산
  - `venues`는 정렬된 unique list 유지
- `end_date`가 오늘 이전이면 live overlay를 하지 않고 closed bucket만 반환한다.
- partial bucket 포함 정책은 현재 KR `1h`와 동일하게 유지하되, `1m/5m/15m/30m`에서도 최근 merged minute rows를 재집계한 마지막 bucket을 포함한다.

### 4.5 CAGG / SQL / retention

- `public.kr_candles_1m` hypertable은 재생성하지 않는다.
- 새 continuous aggregate를 추가한다.
  - `public.kr_candles_5m`
  - `public.kr_candles_15m`
  - `public.kr_candles_30m`
- 세 view 모두:
  - `timescaledb.continuous`
  - `timescaledb.materialized_only = false`
  - `venues` 집계 포함
  - refresh policy schedule `5 minutes`
  - `end_offset`은 bucket 크기와 동일 (`5m`, `15m`, `30m`)
  - retention `90 days`
- 변경 파일:
  - 신규 Alembic revision
  - `scripts/sql/kr_candles_timescale.sql`
  - retention migration 후속 반영

### 4.6 MCP / service wiring

#### MCP `app/mcp_server/tooling/market_data_quotes.py`

- PR #230 shared constants와 `include_indicators` 계약을 복구한다.
- KR 분기:
  - `period == "day"`: 기존 `kis_ohlcv_cache` 경유 day path 유지
  - `period in {"1m", "5m", "15m", "30m", "1h"}`: `read_kr_intraday_candles()` 경유, cache bypass
  - `period in {"week", "month"}`: 기존 `inquire_daily_itemchartprice` 유지
- indicator fields는 base public row shape를 만든 뒤 명시적으로 append한다. DataFrame column 전체가 자동 공개되지 않게 한다.

#### service `app/services/market_data/service.py`

- 같은 period matrix를 사용해 validation과 market guard를 맞춘다.
- KR intraday period는 MCP와 동일한 reader를 재사용하되, return type은 list[`Candle`] 유지한다.
- `app/jobs/watch_scanner.py`는 현재 `period="day"`만 사용하므로 behavior change는 없다.

### 4.7 Indicator 계약

- 복구 대상은 PR #230 계약 그대로다.
- 성공 payload와 empty payload 모두 `indicators_included`를 포함한다.
- row-level indicator keys:
  - `rsi_14`
  - `ema_20`
  - `bb_upper`
  - `bb_mid`
  - `bb_lower`
  - `vwap`
- `vwap`은 intraday periods에서만 값이 있고, `day/week/month`는 `null`이다.
- KR intraday에도 동일 계약을 적용하되, crypto minute 전용 public keys(`timestamp`, `trade_amount`)는 그대로 유지한다.

### 4.8 에러 처리

- invalid period 에러 메시지는 MCP/service가 같은 constants를 사용한다.
- KR universe missing/inactive는 현재 `1h`와 동일하게 empty result로 graceful degradation 한다.
- same-day KIS overlay partial failure는 현재 KR `1h` semantics와 일치하게 유지한다. 즉, 기존 `1h`에서 실패를 명시적으로 surface하던 경계는 minute periods에서도 동일하게 맞춘다.
- Redis 장애는 KR `day`에서만 cache 기능을 포기하고 raw fetch로 degrade한다. KR intraday는 원래 cache bypass라 영향이 없다.

## 5. 테스트 전략

### MCP contract

- `tests/test_mcp_ohlcv_tools.py`
  - KR `1m/5m/15m/30m` 성공
  - US minute reject
  - crypto/US 기존 `1h/4h` 회귀
  - KR intraday `include_indicators=True` row shape + `indicators_included`
  - KR intraday `kis_ohlcv_cache` bypass

### Service parity

- `tests/test_market_data_service.py`
  - 동일 period matrix validation
  - KR intraday는 shared reader path reuse
  - service `Candle` contract 유지 (`session`, `venues`, indicators 미노출)

### KR read service

- `tests/test_kr_hourly_candles_read_service.py`
  - `1m` raw DB merge
  - `5m/15m/30m` CAGG history + current partial bucket overlay
  - KRX price priority + volume/value sum
  - `end_date` 과거일 때 closed bucket only
  - same-day count 부족 시 KIS pagination fallback

### Schema/runtime

- `tests/test_kr_candles_sync.py`
  - migration/sql script에 `kr_candles_5m/15m/30m` view/policy/retention 존재
  - TaskIQ cron unchanged snapshot (`*/10 * * * 1-5`)

### Verification commands

- `uv run pytest --no-cov tests/test_mcp_ohlcv_tools.py tests/test_market_data_service.py tests/test_kr_hourly_candles_read_service.py tests/test_kr_candles_sync.py -q`
- `make lint`
- `uv run pyright app/mcp_server/tooling/market_data_quotes.py app/services/market_data/service.py app/services/kr_hourly_candles_read_service.py`

## 6. 비목표

- TaskIQ `candles.kr.sync`를 1분 주기로 바꾸지 않는다.
- 새 Celery 경로를 추가하지 않는다.
- `app/routers/trading.py`의 day-only HTTP endpoint를 이번 이슈 범위에 넣지 않는다.
- crypto minute shape나 existing US/KR day/week/month contract를 바꾸지 않는다.

## 7. 주의할 점

- indicator field를 DataFrame에만 추가하고 `_normalize_rows`로 그대로 흘리면 예상치 못한 public schema drift가 생길 수 있다. 명시적 row shaping이 필요하다.
- CAGG refresh window와 raw retention이 충돌하면 aggregate data가 삭제될 수 있으므로 새 policy의 `start_offset`/retention을 기존 90일 정책과 같이 검토해야 한다.
- MCP와 service 둘 중 하나만 period matrix를 바꾸면 다시 validation drift가 생긴다.
