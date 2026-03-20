# SQLAlchemy 트레이싱 수정 가이드

## 문제 상황

SigNoz UI의 Traces 탭에서 API 엔드포인트 호출 시 HTTP 요청 span만 보이고, 데이터베이스 쿼리 span이 보이지 않는 문제가 발생했습니다.

### 증상
- API 엔드포인트: `GET /upbit-trading/api/my-coins`
- 데이터베이스 쿼리가 실행되고 있음 (로그에서 확인 가능)
- SQLAlchemy instrumentation이 활성화되어 있음 (`SQLAlchemyInstrumentor().instrument()`)
- 하지만 trace에서 DB 쿼리 span이 보이지 않음

## 원인 분석

이 프로젝트는 **async SQLAlchemy**를 사용합니다:

```python
# app/core/db.py
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine

engine = create_async_engine(
    settings.DATABASE_URL,  # postgresql+asyncpg://...
    echo=True,
    pool_pre_ping=True,
    poolclass=NullPool,
)
```

### 핵심 이슈

OpenTelemetry의 `SQLAlchemyInstrumentor`는 async SQLAlchemy 엔진을 **직접 계측할 수 없습니다**.
대신 async 엔진 내부의 `sync_engine` 속성을 계측해야 합니다.

SQLAlchemy의 async 구현은 내부적으로 sync 엔진을 사용하며, OpenTelemetry instrumentation은 이 sync 레이어에 훅을 걸어야 제대로 작동합니다.

## 해결 방법

### 수정 전 코드 (작동하지 않음)

```python
# app/monitoring/telemetry.py
def _instrument_libraries() -> None:
    """Auto-instrument supported libraries."""
    try:
        # ❌ 이 방법은 async 엔진에서 작동하지 않음
        SQLAlchemyInstrumentor().instrument()
        logger.debug("sqlalchemy instrumented")
    except Exception as e:
        logger.debug(f"sqlalchemy instrumentation skipped: {e}")
```

### 수정 후 코드 (정상 작동)

```python
# app/monitoring/telemetry.py
def _instrument_libraries() -> None:
    """Auto-instrument supported libraries."""
    try:
        from app.core.db import engine

        # ✅ async 엔진의 경우 sync_engine을 계측
        if hasattr(engine, 'sync_engine'):
            SQLAlchemyInstrumentor().instrument(
                engine=engine.sync_engine,  # 핵심: sync_engine 사용
                enable_commenter=True,      # SQL 주석 추가 (선택 사항)
            )
            logger.debug("async sqlalchemy instrumented via sync_engine")
        else:
            # sync 엔진의 경우 직접 계측
            SQLAlchemyInstrumentor().instrument(
                engine=engine,
                enable_commenter=True,
            )
            logger.debug("sqlalchemy instrumented")
    except Exception as e:
        logger.debug(f"sqlalchemy instrumentation skipped: {e}")
```

## 검증

### 수정 전
```bash
$ docker exec signoz_clickhouse clickhouse-client --query \
  "SELECT name, count(*) FROM signoz_traces.signoz_index_v2 \
   WHERE traceID = 'xxx' GROUP BY name"

GET /upbit-trading/api/my-coins    1
GET                                2
```

### 수정 후
```bash
$ docker exec signoz_clickhouse clickhouse-client --query \
  "SELECT name, count(*) FROM signoz_traces.signoz_index_v2 \
   WHERE traceID = '46ad98857b2b4cda40a31079a2badfc2' GROUP BY name"

SELECT auto_trader                 23  ✅ DB 쿼리 span 추가됨!
GET                                2
GET /upbit-trading/api/my-coins    1
connect                            1   ✅ DB 연결 span도 추가됨!
```

## Trace 구조

수정 후 단일 API 호출의 전체 trace 구조:

```
GET /upbit-trading/api/my-coins (336ms)
├── GET (Upbit API call #1)
│   └── ...
├── GET (Upbit API call #2)
│   └── ...
├── connect (DB connection)
├── SELECT auto_trader (코인 1)
├── SELECT auto_trader (코인 2)
├── SELECT auto_trader (코인 3)
...
└── SELECT auto_trader (코인 23)
```

## 추가 참고사항

### enable_commenter 옵션

`enable_commenter=True`를 사용하면 SQLAlchemy가 생성하는 SQL 쿼리에 OpenTelemetry trace context가 주석으로 추가됩니다:

```sql
-- Before
SELECT * FROM stock_analysis_results WHERE ...

-- After (with enable_commenter=True)
/*traceparent='00-46ad98857b2b4cda40a31079a2badfc2-...'*/
SELECT * FROM stock_analysis_results WHERE ...
```

이는 데이터베이스 레벨에서도 trace를 추적할 수 있게 해줍니다.

### Async vs Sync 엔진 판별

코드는 `hasattr(engine, 'sync_engine')`를 사용하여 자동으로 async/sync 엔진을 판별합니다:

- **Async 엔진** (`create_async_engine`): `sync_engine` 속성이 있음
- **Sync 엔진** (`create_engine`): `sync_engine` 속성이 없음

## 관련 문서

- [OpenTelemetry SQLAlchemy Instrumentation](https://opentelemetry-python-contrib.readthedocs.io/en/latest/instrumentation/sqlalchemy/sqlalchemy.html)
- [LOGGING_SETUP.md](LOGGING_SETUP.md) - 로깅 시스템 설정 가이드
- [app/monitoring/telemetry.py](app/monitoring/telemetry.py) - 텔레메트리 설정 코드
- [app/core/db.py](app/core/db.py) - 데이터베이스 연결 설정

## 문제 해결

### DB span이 여전히 보이지 않을 경우

1. **애플리케이션 재시작 확인**
   ```bash
   # uvicorn --reload가 자동으로 재시작했는지 확인
   # 또는 수동으로 재시작
   ```

2. **Instrumentation 로그 확인**
   ```bash
   # 애플리케이션 로그에서 다음 메시지 확인:
   # "async sqlalchemy instrumented via sync_engine"
   ```

3. **ClickHouse에서 trace 확인**
   ```bash
   docker exec signoz_clickhouse clickhouse-client --query \
     "SELECT timestamp, name FROM signoz_traces.signoz_index_v2 \
      WHERE serviceName = 'auto-trader' AND timestamp > now() - INTERVAL 5 MINUTE \
      ORDER BY timestamp DESC LIMIT 50"
   ```

4. **OTEL Collector 로그 확인**
   ```bash
   docker compose -f docker-compose.monitoring.yml logs otel-collector | grep -i trace
   ```

## 요약

- **문제**: Async SQLAlchemy 엔진을 직접 계측하면 trace span이 생성되지 않음
- **해결**: `engine.sync_engine`을 계측하여 async 엔진의 내부 sync 레이어에 훅 설치
- **결과**: DB 쿼리가 trace에 child span으로 정상 표시됨
- **검증**: 단일 API 호출에서 23개의 DB 쿼리 span 확인
