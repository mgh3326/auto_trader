# Sentry 트랜잭션 DB 호출 수 (`db.call_count`, hk 6378)

Sentry 트랜잭션마다 그 트랜잭션이 살아 있는 동안 실행된 DB 문장 수를 측정값
`db.call_count` 로 붙인다. span 수 상한(`SENTRY_MAX_SPANS_PER_TRANSACTION`)과
무관하므로 요청 안 N+1 을 요청 사이 폴링과 구분할 수 있다.

- 계수기: `app/monitoring/db_call_count.py`. 문장이 실행되는 시점의 활성 Sentry
  트랜잭션(`get_current_span().containing_transaction`)의
  `(trace_id, span_id)` 를 키로 정수 1을 더한다. task 문맥이 아니라 트랜잭션
  객체에 귀속되므로, MCP 도구 본문(세션 task)이 내는 문장도 요청 task에서
  끝나가는 `/mcp` 트랜잭션에 붙는다 (SDK MCP integration이 요청 scope를
  핸들러에 그대로 다시 적용하기 때문).
- 활성 트랜잭션이 없는 문장(트랜잭션 시작 전 sweep, 샘플링 탈락 등)은 어디에도
  귀속되지 않고 다음 트랜잭션으로 새지 않는다. 끝나지 않은 트랜잭션 항목은
  1024개 상한에서 가장 오래된 것부터 버린다.
- 리스너: `app/core/db.py` 의 `build_engine` (공유 앱 엔진)과
  `app/services/decision_evidence.py` 의 `observation_session`
  (주문 증거 관측 엔진)이 각자 `sync_engine` 에 `before_cursor_execute` 로
  등록한다. 문장당 딕셔너리 1회가 전부이며 문장 내용은 읽지도 저장하지도 않는다.
  이 두 엔진 밖의 문장은 세지 않는다.
- 부착: `app/monitoring/sentry.py` 의 `_before_send_transaction` 이 끝난
  트랜잭션의 trace 문맥으로 수를 찾아 측정값을 싣고 키를 버린다. 수가 없으면
  측정값을 싣지 않는다. 순차 트랜잭션은 키가 달라 누적되지 않는다.
- Fail-open: 계수기·리스너·부착의 모든 오류는 삼킨다. 문장 실행과 트랜잭션
  전송에 영향을 주지 않는다.

HTTP 라우트·MCP 도구 호출·TaskIQ/CLI 트랜잭션에서 트랜잭션이 있을 때 동작하고,
없으면 아무것도 하지 않는다. 계약 테스트:
`tests/monitoring/test_sentry_db_call_count.py` (스레드 경계 MCP 위상 포함).
