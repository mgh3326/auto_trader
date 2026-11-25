# Sentry 트랜잭션 DB 호출 수 (`db.call_count`, hk 6378)

Sentry 트랜잭션마다 그 요청이 실행한 DB 문장 수를 측정값 `db.call_count` 로 붙인다.
span 수 상한(`SENTRY_MAX_SPANS_PER_TRANSACTION`)과 무관하므로, 요청 안 N+1 을
요청 사이 폴링과 구분할 수 있다.

- 계수기: `app/monitoring/db_call_count.py` 의 `ContextVar` 정수 1개. asyncio task 가
  다르면 수가 섞이지 않는다.
- 리스너: `app/core/db.py` 의 `build_engine` 이 모든 앱 엔진의 `sync_engine` 에
  `before_cursor_execute` 로 등록한다. 문장당 정수 1 증가가 전부이며 문장 내용은
  읽지도 저장하지도 않는다.
- 부착: `app/monitoring/sentry.py` 의 `_before_send_transaction` 이 트랜잭션 종료
  시점에 측정값을 싣고 계수기를 0으로 되돌린다. 수가 0이면 측정값을 싣지 않는다.
- Fail-open: 계수기·리스너·부착의 모든 오류는 삼킨다. 문장 실행과 트랜잭션 전송에
  영향을 주지 않는다.

HTTP 라우트·MCP 도구 호출·TaskIQ/CLI 트랜잭션에서 트랜잭션이 있을 때 동작하고,
없으면 아무것도 하지 않는다. 계약 테스트:
`tests/monitoring/test_sentry_db_call_count.py`.
