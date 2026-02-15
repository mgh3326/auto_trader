# Celery → TaskIQ 마이그레이션 단계별 실행 플랜

## 요약
Celery/Flower 제거, TaskIQ 기반 async 태스크 실행/조회/스케줄링으로 전환, 라우터 호출/상태 조회 및 테스트를 모두 TaskIQ 방식으로 통일한다.

## Step 1. 의존성/정적설정 정리
- [x] `pyproject.toml`에서 `celery[redis]`, `flower` 제거
- [x] `pyproject.toml`에 `taskiq`, `taskiq-redis`, `taskiq-fastapi` 추가
- [x] `pyproject.toml`의 `sentry-sdk[fastapi,celery]`를 `sentry-sdk[fastapi]`로 변경
- [x] `pyproject.toml` Ruff `per-file-ignores`의 Celery용 E402 항목 제거

## Step 2. TaskIQ 코어 인프라 추가
- [x] `app/core/taskiq_broker.py` 신규 생성
- [x] `RedisAsyncResultBackend(result_ex_time=3600)` 구성
- [x] `ListQueueBroker(url=settings.get_redis_url(), queue_name="auto-trader")` 구성
- [x] Worker startup용 middleware(`WorkerInitMiddleware`)에 Sentry/Trade Notifier 초기화 로직 이관

## Step 3. 스케줄러 전환
- [x] `app/core/scheduler.py`를 APScheduler에서 `TaskiqScheduler + LabelScheduleSource` 구조로 교체
- [x] `app/tasks/scheduler_tasks.py` 신규 생성
- [x] `screen_once_async()`를 `@broker.task(schedule=[...])`로 cron 등록

## Step 4. 태스크 정의 전환 (Celery → TaskIQ)
- [x] `app/tasks/analyze.py` 전환
- [x] `app/tasks/kis.py` 전환
- [x] `app/tasks/krx.py` 전환
- [x] 공통 변환 규칙 적용: `@shared_task`→`@broker.task`, `def`→`async def`, `asyncio.run()` 제거, `self.update_state(PROGRESS)` 제거, `progress_cb/ProgressCallback` 제거

## Step 5. Task 상태 조회 공통 유틸 도입
- [x] `app/core/taskiq_result.py` 신규 생성
- [x] `await broker.result_backend.get_result(task_id)` 결과를 API 응답 포맷으로 매핑하는 헬퍼 구현
- [x] 실패(`is_err`)와 성공(`return_value`) 기준의 표준 상태(`PENDING/SUCCESS/FAILURE`) 규칙 고정

## Step 6. 라우터 호출부 전환
- [x] `app/routers/upbit_trading.py`: `send_task`/`AsyncResult` 제거, `await task_fn.kiq(...)` + 공통 상태 헬퍼 적용
- [x] `app/routers/kis_domestic_trading.py`: 동일 전환
- [x] `app/routers/kis_overseas_trading.py`: 동일 전환
- [x] `app/routers/kospi200.py`: 동일 전환
- [x] `app/routers/stock_latest.py`: 동일 전환

## Step 7. 앱/모니터링 통합 변경
- [x] `app/monitoring/sentry.py`에서 `CeleryIntegration`, `enable_celery`, `celery` 플래그 제거
- [x] `app/main.py`에 `taskiq_fastapi.init(broker, "app.main:api")` 추가
- [x] `app/main.py` lifespan에 `broker.startup()/shutdown()` 추가

## Step 8. 실행 명령(운영 스크립트) 전환
- [x] `Makefile`에서 `celery-worker`, `celery-flower` 제거
- [x] `Makefile`에 `taskiq-worker`, `taskiq-scheduler` 타겟 추가

## Step 9. 테스트 전환
- [x] `tests/test_celery_tasks.py`를 `tests/test_tasks.py`로 리네임
- [x] `tests/test_tasks.py`에서 `.apply().result`를 `await task_fn()`로 변경
- [x] `tests/test_kis_tasks.py`에서 `.apply().result`/`update_state` 관련 테스트 제거 및 async 호출로 변경
- [x] `tests/test_upbit_trading.py`에서 `celery_app` mock 제거, `.kiq()` mock으로 교체
- [x] `tests/test_sentry_init.py`에서 Celery integration 기대값 제거

## Step 10. Celery 제거 마무리 및 검증
- [x] `app/core/celery_app.py` 삭제
- [ ] 코드베이스에서 `celery_app`, `send_task`, `AsyncResult`, `shared_task`, `CeleryIntegration` 잔여 참조 0건 확인
- [x] 검증 순서 실행: `uv sync --all-groups` → `make lint` → `make test` → TaskIQ worker/scheduler 기동 확인

## 공개 인터페이스/타입 변경
1. 태스크 실행 API: 문자열 task name enqueue에서 함수 기반 `await task_fn.kiq(...)`로 변경
2. 태스크 상태 API: `ready`/`PROGRESS` 중심에서 `is_ready` + `state(PENDING/SUCCESS/FAILURE)` + `result/error` 중심으로 변경
3. Sentry init 함수 시그니처에서 `enable_celery` 인자 제거

## 테스트 케이스/시나리오
1. 태스크 enqueue API가 `task_id`를 정상 반환한다
2. 상태 조회 API가 `PENDING`, `SUCCESS`, `FAILURE`를 올바르게 매핑한다
3. 기존 `PROGRESS` 관련 응답/assertion이 제거되어도 핵심 기능 테스트가 통과한다
4. worker/scheduler 프로세스가 각각 단독 기동된다
5. Sentry 초기화 테스트에서 Celery 통합 비의존 상태로 통과한다

## 가정 및 기본값
1. `PROGRESS` 실시간 진행률은 요구사항에서 제외한다
2. 결과 TTL은 기존과 동일하게 3600초를 유지한다
3. 큐 이름은 `auto-trader`, cron timezone은 `Asia/Seoul`로 유지한다
4. 외부 시스템에서 Celery task name 문자열 호출 의존이 없다고 가정한다
