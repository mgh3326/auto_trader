# Watch Alerts — Redis 기반 조건부 알림 시스템 설계

작성일: 2026-02-17  
상태: 승인됨 (brainstorming 결과 고정)

## 1. 배경

- 기존 `/Users/robin/PycharmProjects/auto_trader/app/jobs/daily_scan.py`는 자동 감지형 스캔(크래시/과매수/과매도 등)이다.
- 신규 요구사항은 사용자가 직접 조건을 등록하고, 조건 충족 시 1회 알림 후 자동 삭제되는 watch 시스템이다.
- 두 시스템은 목적과 상태 관리 방식이 다르므로 런타임/키 공간을 분리한다.

목표:

- Redis hash 기반 사용자 지정 조건 알림을 도입한다.
- cron(TaskIQ) 주기 스캔으로 조건 충족을 평가한다.
- 알림은 OpenClaw + Telegram 미러링 경로(`send_watch_alert`, 내부 `_send_market_alert` 공유)를 사용한다.
- 조건 충족 알림 발송 성공 시 해당 조건을 자동 삭제한다.

## 2. 확정 요구사항

- watch 저장소는 Redis key `watch:alerts:{market}`를 사용한다.
- `market`: `crypto`, `kr`, `us` 모두 등록/조회/삭제/스캔 대상.
- 조건 유형: `price_above`, `price_below`, `rsi_above`, `rsi_below`.
- MCP 인터페이스는 내부 `condition_type` 노출 대신 `metric + operator` 입력을 받는다.
- 중복 등록 시 기존 생성 시각(`created_at`)을 보존한다 (idempotent add).
- 스캔은 5분 주기로 실행한다.
- 여러 종목 트리거는 market 단위 단일 배치 메시지로 묶어 발송한다 (분할 전송 없음).
- 장 시간 체크는 `exchange-calendars`를 사용해 정확한 세션 기준으로 처리한다.

## 3. 대안 검토 및 선택

### 대안 A: 신규 모듈 분리형 (선택)
- `WatchAlertService` / `WatchScanner` / `watch_scan_tasks`를 별도 파일로 구성.
- 장점: 기존 `daily_scan` 회귀 위험 최소, 테스트 범위 분리 용이.
- 단점: 신규 파일/테스트 수 증가.

### 대안 B: 기존 `daily_scan`에 통합
- 장점: 코드 위치 집중.
- 단점: 자동 감지/사용자 지정 감지 결합으로 복잡도와 회귀 위험 상승.

### 대안 C: 실시간(WebSocket) 감시 우선
- 장점: 지연 최소.
- 단점: 이번 범위 대비 과한 복잡도.

결론: 대안 A를 채택한다.

## 4. 아키텍처

## 4.1 신규 파일

- `/Users/robin/PycharmProjects/auto_trader/app/services/watch_alerts.py`
  - Redis CRUD, 정규화/검증, 중복 정책
- `/Users/robin/PycharmProjects/auto_trader/app/jobs/watch_scanner.py`
  - market별 watch 조회, 조건 평가, 배치 알림/삭제
- `/Users/robin/PycharmProjects/auto_trader/app/tasks/watch_scan_tasks.py`
  - TaskIQ `@broker.task` 스케줄 진입점
- `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/watch_alerts_registration.py` (신규)
  - MCP 도구 `manage_watch_alerts` 등록

## 4.2 기존 연계 변경

- `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/tooling/registry.py`
  - watch 툴 등록 함수 연결
- `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/__init__.py`
  - `AVAILABLE_TOOL_NAMES`에 `manage_watch_alerts` 추가
- `/Users/robin/PycharmProjects/auto_trader/app/tasks/__init__.py`
  - `watch_scan_tasks` 모듈 import 추가

## 4.3 알림 채널

- watch 스캐너는 `/Users/robin/PycharmProjects/auto_trader/app/services/openclaw_client.py`의 `send_watch_alert()`를 사용한다.
- `send_watch_alert()`와 `send_scan_alert()`는 내부 공통 경로(`_send_market_alert`)를 공유하며, OpenClaw 전송 성공 시 Telegram으로 미러링된다.

## 5. 데이터 모델 및 계약

## 5.1 Redis 키/필드

- Key: `watch:alerts:{market}`
- Field: `{symbol}:{condition_type}:{threshold}`
- Value(JSON): `{"created_at":"ISO8601_KST"}`

예시:

- `watch:alerts:crypto` + `BTC:rsi_below:30`
- `watch:alerts:us` + `AMZN:price_below:190`

## 5.2 내부 condition_type

- `price_above`
- `price_below`
- `rsi_above`
- `rsi_below`

## 5.3 MCP 입력/응답 계약

### 입력

- `action`: `add` | `remove` | `list`
- `market`: `crypto` | `kr` | `us` (`list` 전체조회 시 optional)
- `symbol`: string
- `metric`: `price` | `rsi` (`add/remove` 필수)
- `operator`: `above` | `below` (`add/remove` 필수)
- `threshold`: float (`add/remove` 필수)

### 변환

- `condition_type = f"{metric}_{operator}"`

### 응답

- `add`: `created`, `already_exists` 포함
- `remove`: `removed` 포함
- `list`: `condition_type` + 파생 `metric`, `operator`를 함께 반환

## 5.4 검증/정규화 규칙

- `symbol`: trim + upper
- `threshold`: float 변환 후 canonical 문자열로 field 구성
- `metric=rsi`이면 `0 <= threshold <= 100` 강제
- 잘못된 action/market/metric/operator는 에러 응답

## 5.5 중복 정책

- 동일 field가 이미 존재하면 값을 덮어쓰지 않는다.
- 즉 `created_at`은 최초 등록 시점 유지.
- `add` 응답은 `already_exists=true`, `created=false`로 반환.

## 6. 스캔 로직

## 6.1 스케줄

- TaskIQ schedule: `*/5 * * * *` (`Asia/Seoul`)

## 6.2 market open 판정

- `crypto`: 항상 open
- `kr`: `exchange_calendars.get_calendar("XKRX")`의 session open~close 사이에서만 스캔
- `us`: `exchange_calendars.get_calendar("XNYS")`의 session open~close 사이에서만 스캔

정책:

- DST/휴장일/조기마감은 캘린더가 반영한다.
- 장 외 시간에는 해당 market 스캔을 `skipped`로 반환하고 데이터 API 호출을 하지 않는다.

## 6.3 지표/가격 조회

- crypto
  - 가격: Upbit `fetch_multiple_tickers`
  - RSI: Upbit `fetch_ohlcv` + `_calculate_rsi`
- kr
  - 가격: `KISClient.inquire_price`
  - RSI: `KISClient.inquire_daily_itemchartprice` + `_calculate_rsi`
- us
  - 가격: Yahoo fast_info(`fetch_price` 계열)
  - RSI: Yahoo `fetch_ohlcv` + `_calculate_rsi`

## 6.4 알림/삭제 트랜잭션 규칙

- market별 트리거 결과를 하나의 메시지로 배치 전송한다.
- 전송 성공 시 해당 market의 triggered field들을 삭제한다.
- 전송 실패 시 삭제하지 않는다 (다음 주기 재시도 가능).

## 7. 에러 처리

- MCP 입력 검증 실패: `success=false` + 에러 메시지
- Redis read/parse 실패: 해당 market 결과에 `error`를 포함하고 다른 market 스캔 지속
- 개별 watch 평가 실패: 해당 watch만 skip, 나머지 진행
- OpenClaw 전송 예외: error 결과 기록, 삭제 수행 금지

## 8. 테스트 전략

## 8.1 서비스 테스트 (신규)

- `/Users/robin/PycharmProjects/auto_trader/tests/test_watch_alerts.py`
- 항목:
  - add/remove/list 정상 케이스
  - 중복 add에서 `created_at` 보존
  - `metric/operator` 변환 및 검증
  - RSI threshold 범위 검증

## 8.2 스캐너 테스트 (신규)

- `/Users/robin/PycharmProjects/auto_trader/tests/test_watch_scanner.py`
- 항목:
  - crypto/kr/us market open 분기
  - `price_*`, `rsi_*` 조건 충족/미충족
  - 배치 메시지 생성 포맷
  - 전송 성공 시 삭제 / 실패 시 미삭제

## 8.3 MCP 테스트 (보강)

- `/Users/robin/PycharmProjects/auto_trader/tests/test_mcp_server_tools.py` 또는 분리 파일
- 항목:
  - `manage_watch_alerts` add/remove/list 계약
  - 에러 케이스(action/market/metric/operator/threshold)

## 8.4 등록 테스트 (보강)

- `/Users/robin/PycharmProjects/auto_trader/tests/test_mcp_tool_registration.py`
- 항목:
  - 신규 tool name 포함 확인
  - domain registration idempotency 유지

## 9. 문서 반영

- `/Users/robin/PycharmProjects/auto_trader/app/mcp_server/README.md`
  - `manage_watch_alerts` 파라미터 및 예시 추가
- 필요 시 운영 문서에 TaskIQ 스케줄 설명 추가

## 10. 비목표

- WebSocket 실시간 watch 감시
- 조건 충족 후 자동 재등록/복구 로직
- 사용자별 멀티테넌트 watch 분리
- watch 메시지 분할 발송
