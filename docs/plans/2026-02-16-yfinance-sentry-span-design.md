# yfinance 요청 Sentry Span 가시화 설계 (METHOD + path)

작성일: 2026-02-16  
상태: 승인됨 (brainstorming 결과 고정)

## 1. 배경 및 목표

현재 MCP/API 트레이스에서 yfinance 연동 구간은 `mcp.server` 또는 HTTP 상위 스팬만 보이고, yfinance 내부의 Yahoo 요청 endpoint별 호출 내역이 보이지 않는다.  
원인은 yfinance(1.1.0)가 `httpx`가 아닌 `curl_cffi`(libcurl) 경로를 사용해 Sentry의 기존 자동 `httpx` 계측에 걸리지 않기 때문이다.

핵심 목표:

- MCP/API 요청(트랜잭션) 내부에서 yfinance가 생성한 실제 HTTP 요청을 span으로 기록한다.
- span 이름은 `METHOD + path`로 통일한다.
- 함수 호출 횟수 집계/커스텀 카운터 없이, Sentry Trace에서 바로 읽히는 span 중심으로 관측한다.

## 2. 확정된 요구사항

- 범위: MCP 서버 + API 서버의 yfinance 사용 경로 전체
- 단위: 트랜잭션(요청) 단위
- 표현: span 이름 `METHOD /path` (전체 URL을 이름에 직접 노출하지 않음)
- 비목표:
  - 함수 호출 횟수 자체 집계
  - 프로세스 누적 카운터
  - yfinance 제거/대체 리라이트

## 3. 접근 대안 및 선택

### 대안 A (선택): `curl_cffi` Session 래퍼에서 수동 span 생성

- 장점:
  - 목표(endpoint별 요청 가시화)에 정확히 부합
  - yfinance 내부 HTTP 단위로 span 기록 가능
  - MCP/API 공통 적용 가능
- 단점:
  - yfinance 호출 지점마다 `session=` 주입 누락 점검 필요

### 대안 B: yfinance 함수 단위(`yf.screen`, `yf.download`) span만 기록

- 장점: 구현 단순
- 단점: 실제 endpoint 호출 횟수/종류 확인이 어려움

### 대안 C: Yahoo 호출을 `httpx`로 전면 교체

- 장점: 자동 계측 활용 극대화
- 단점: 리라이트 범위/회귀 리스크가 큼

결론: 대안 A가 요구사항 대비 가장 실용적이다.

## 4. 아키텍처 변경

## 4.1 공통 계측 모듈 추가

- 생성: `app/monitoring/yfinance_sentry.py`
- 책임:
  - `SentryTracingCurlSession` 제공 (`curl_cffi.requests.Session` 상속)
  - `request()` 호출 전/후 수동 span 생성/완료
  - span naming 규칙: `METHOD + path`
  - 최소 data 첨부:
    - `url`
    - `http.request.method`
    - `http.response.status_code` (응답 시)

## 4.2 yfinance 호출 지점 Session 주입

yfinance API가 제공하는 `session` 파라미터를 통해 공통 계측 session을 주입한다.

영향 경로(예정):

- `app/services/yahoo.py`
  - `yf.download(...)`
  - `yf.Ticker(...).fast_info`
  - `yf.Ticker(...).info`
- `app/mcp_server/tooling/analysis_screen_core.py`
  - `yf.screen(...)`
- `app/mcp_server/tooling/analysis_rankings.py`
  - `yf.screen(...)`
- `app/mcp_server/tooling/analysis_tool_handlers.py`
  - `yf.Ticker(...).info/dividends`
- `app/mcp_server/tooling/market_data_quotes.py`
  - `yf.Ticker(...).fast_info`
- `app/mcp_server/tooling/fundamentals_sources_indices.py`
  - `yf.Ticker(...)`, `yf.download(...)`
- `app/mcp_server/tooling/fundamentals_sources_naver.py`
  - `yf.Ticker(...).info/financials/...`

## 5. 데이터 플로우

1. 상위 transaction/span(MCP 또는 API 요청) 활성 상태에서 yfinance 호출 진입
2. yfinance 호출 시 `session=SentryTracingCurlSession(...)` 전달
3. yfinance 내부 `curl_cffi` 요청마다 `request()`가 호출되고 child span 생성
4. 요청 완료 시 status code가 span data에 기록되고 span 종료
5. Sentry Trace에서 상위 span 하위에 `POST /v1/finance/screener` 등 endpoint span이 표시됨

## 6. 오류/안전 정책

- URL 파싱 실패:
  - path는 `"/unknown"`으로 기록
  - span 생성은 유지
- 요청 예외:
  - span에 예외/상태를 남기고 원래 예외를 그대로 전파
- 계측 실패:
  - 비즈니스 요청 경로는 차단하지 않고 no-op fallback
- 민감정보:
  - span `name`에는 query/body를 포함하지 않음 (`METHOD + path`만)

## 7. 테스트 전략

## 7.1 단위 테스트

- `tests/test_yfinance_sentry.py` (신규)
  - `request("GET", "https://.../path?a=1")` 시 span name이 `GET /path`인지
  - status code data 기록 여부
  - URL 파싱 실패 fallback(`"/unknown"`)
  - 예외 발생 시 전파 보장

## 7.2 통합/회귀 테스트

- 기존 yfinance 사용 기능 테스트를 보강:
  - MCP 경로 1개 이상 (`screen_stocks` US)
  - API 경로 1개 이상 (`services/yahoo.py` 경로)
- 주입 누락 점검:
  - yfinance 호출부에서 `session=` 전달 여부 정적 검사/테스트로 확인

## 8. 롤아웃 및 관측

- 1단계: 공통 모듈 + 핵심 경로(`screen_stocks`, `services/yahoo`) 적용
- 2단계: 나머지 yfinance 호출부 전면 적용
- 검증 쿼리(예시):
  - `op:http.client transaction:"tools/call screen_stocks"`
  - `span.description:"GET /v1/finance/screener"` (환경에 맞는 필드명으로 조회)

## 9. 비목표

- endpoint별 호출 수를 별도 집계 필드로 저장하는 기능
- 멀티 프로세스 통합 카운팅
- Yahoo 연동 라이브러리 교체
