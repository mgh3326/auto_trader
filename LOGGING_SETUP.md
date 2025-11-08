# SigNoz 로깅 시스템 설정 가이드

Python 애플리케이션의 로그를 OpenTelemetry를 통해 SigNoz로 전송하는 시스템입니다.

## 🎯 개요

### 수집되는 텔레메트리 데이터

| 데이터 타입 | 상태 | 설명 |
|------------|------|------|
| **Traces** | ✅ 수집 중 | API 요청, DB 쿼리, HTTP 클라이언트 호출 등 |
| **Metrics** | ✅ 수집 중 | HTTP 요청 수, 응답 시간, 에러 카운트 등 |
| **Logs** | ✅ 수집 중 | Python logging 모듈의 모든 로그 (INFO 이상) |

## 📝 로그 수집 아키텍처

```
Python Application (logging)
    ↓
OpenTelemetry LoggingHandler
    ↓
OTLP Log Exporter (gRPC)
    ↓
OTEL Collector (localhost:4317)
    ↓
ClickHouse (signoz_logs database)
    ↓
SigNoz UI (http://localhost:3301)
```

## 🔧 구현 상세

### 1. OpenTelemetry 통합

[app/monitoring/telemetry.py](app/monitoring/telemetry.py)에서 로그 수집 설정:

```python
from opentelemetry._logs import set_logger_provider
from opentelemetry.exporter.otlp.proto.grpc._log_exporter import OTLPLogExporter
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor

# Setup logging provider with OTLP exporter
log_exporter = OTLPLogExporter(
    endpoint=otlp_endpoint,  # localhost:4317
    insecure=insecure,
)
logger_provider = LoggerProvider(resource=resource)
logger_provider.add_log_record_processor(BatchLogRecordProcessor(log_exporter))
set_logger_provider(logger_provider)

# Attach OTEL handler to root logger
handler = LoggingHandler(level=logging.INFO, logger_provider=logger_provider)
root_logger = logging.getLogger()
root_logger.addHandler(handler)

# Ensure root logger level allows INFO and above
if root_logger.level > logging.INFO:
    root_logger.setLevel(logging.INFO)
```

### 2. 자동 수집되는 로그

#### 애플리케이션 로그
- FastAPI 시작/종료 로그
- 미들웨어 로그
- ErrorReporter, TradeNotifier 설정 로그
- 비즈니스 로직 로그

#### 라이브러리 로그 (Auto-instrumentation)
- **SQLAlchemy**: 모든 SQL 쿼리 자동 로깅 및 트레이싱
  - Async SQLAlchemy (`create_async_engine`) 지원
  - `engine.sync_engine`을 통한 계측으로 trace span 생성
- **HTTPx/Requests**: HTTP 클라이언트 요청
- **Redis**: Redis 명령어
- **FastAPI**: API 요청/응답

### 3. 로그 레벨

- **INFO 이상**: OpenTelemetry로 전송됨
- **DEBUG**: 로컬 파일/콘솔에만 출력 (SigNoz로 전송 안 됨)
- **WARNING, ERROR, CRITICAL**: 모두 전송됨

## 📊 SigNoz에서 로그 확인

### 1. SigNoz UI 접속
```
http://localhost:3301/logs
```

### 2. 로그 필터링

**Service로 필터링:**
```
service_name = "auto-trader"
```

**Severity로 필터링:**
```
severity_text = "ERROR"
```

**시간 범위 설정:**
- Last 15 minutes
- Last 1 hour
- Last 24 hours
- Custom range

### 3. 유용한 쿼리

**에러 로그만 보기:**
```sql
SELECT * FROM signoz_logs.logs
WHERE severity_text IN ('ERROR', 'CRITICAL')
ORDER BY timestamp DESC
LIMIT 100
```

**특정 시간대의 로그:**
```sql
SELECT * FROM signoz_logs.logs
WHERE timestamp > now() - INTERVAL 1 HOUR
ORDER BY timestamp DESC
```

**로그 통계:**
```sql
SELECT
    severity_text,
    count(*) as count
FROM signoz_logs.logs
WHERE timestamp > now() - INTERVAL 1 HOUR
GROUP BY severity_text
ORDER BY count DESC
```

## 🐛 SQL 쿼리 로깅 및 트레이싱

SQLAlchemy instrumentation이 활성화되어 있어 모든 DB 쿼리가 자동으로 로깅되고 trace span으로 기록됩니다.

### Async SQLAlchemy 지원

이 프로젝트는 `create_async_engine`을 사용하는 async SQLAlchemy를 사용합니다.
OpenTelemetry는 `engine.sync_engine`을 통해 async 엔진을 계측합니다:

```python
# app/monitoring/telemetry.py
from app.core.db import engine

if hasattr(engine, 'sync_engine'):
    SQLAlchemyInstrumentor().instrument(
        engine=engine.sync_engine,  # async 엔진의 내부 sync 엔진 사용
        enable_commenter=True,
    )
```

### 로그 예시
```
INFO  SELECT stock_analysis_results.id, stock_analysis_results.stock_info_id, ...
FROM stock_analysis_results
JOIN stock_info ON stock_info.id = stock_analysis_results.stock_info_id
WHERE stock_info.symbol = $1::VARCHAR
ORDER BY stock_analysis_results.created_at DESC
LIMIT $2::INTEGER
```

### 쿼리 성능 분석

SigNoz Traces에서 각 쿼리의 실행 시간을 확인할 수 있습니다:
1. Traces 탭으로 이동
2. `db.statement` 속성으로 필터링
3. 느린 쿼리 식별 및 최적화

## ⚙️ 환경 변수 설정

### .env 파일

```bash
# SigNoz 활성화
SIGNOZ_ENABLED=true
SIGNOZ_ENDPOINT=localhost:4317

# 서비스 정보
OTEL_SERVICE_NAME=auto-trader
OTEL_SERVICE_VERSION=0.1.0
OTEL_ENVIRONMENT=development
```

### Docker Compose

SigNoz 스택 시작:
```bash
# 일반 환경
docker-compose -f docker-compose.monitoring.yml up -d

# Raspberry Pi
docker-compose -f docker-compose.monitoring-rpi.yml up -d
```

## 📈 로그 볼륨 관리

### 현재 수집량 (1시간 기준)

- **Traces**: ~55개
- **Logs**: ~66개
- **Metrics**: 실시간 수집

### 로그 레벨 조정

애플리케이션 로그 레벨을 변경하려면:

```python
# app/monitoring/telemetry.py
handler = LoggingHandler(
    level=logging.WARNING,  # INFO에서 WARNING으로 변경
    logger_provider=logger_provider
)
```

### ClickHouse 데이터 보관 기간

기본 설정: 30일 (SigNoz 기본값)

변경하려면 ClickHouse 설정 파일 수정:
```bash
signoz-config/clickhouse-config.xml
```

## 🔍 디버깅 및 문제 해결

### 로그가 보이지 않을 때

1. **OTEL Collector 확인**
```bash
docker compose -f docker-compose.monitoring.yml logs otel-collector | grep -i "log"
```

다음과 같은 메시지가 있어야 합니다:
```
LogsExporter {"kind": "exporter", "data_type": "logs", "name": "logging", "resource logs": 1, "log records": X}
```

2. **ClickHouse 확인**
```bash
docker exec signoz_clickhouse clickhouse-client --query \
  "SELECT count(*) FROM signoz_logs.logs WHERE timestamp > now() - INTERVAL 10 MINUTE"
```

0이 아닌 숫자가 나와야 합니다.

3. **애플리케이션 재시작**
```bash
# uvicorn이 자동으로 재시작하거나
# 수동으로 프로세스 재시작
```

### 로그가 너무 많을 때

**특정 로거 비활성화:**
```python
# 특정 모듈의 로그 레벨 조정
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)
logging.getLogger('httpx').setLevel(logging.WARNING)
```

**OTEL Handler 레벨 조정:**
```python
# telemetry.py에서
handler = LoggingHandler(
    level=logging.ERROR,  # ERROR 이상만 전송
    logger_provider=logger_provider
)
```

## 🎨 로그 Best Practices

### 1. 구조화된 로깅

```python
logger.info(
    "User action completed",
    extra={
        "user_id": user_id,
        "action": "buy",
        "symbol": "BTC",
        "amount": 100000
    }
)
```

### 2. 에러 로깅

```python
try:
    risky_operation()
except Exception as e:
    logger.error(
        "Operation failed",
        exc_info=True,  # 스택 트레이스 포함
        extra={
            "operation": "trade",
            "symbol": symbol
        }
    )
```

### 3. 성능 로깅

```python
import time

start_time = time.time()
result = expensive_operation()
duration = time.time() - start_time

logger.info(
    "Operation completed",
    extra={
        "operation": "analysis",
        "duration_ms": duration * 1000,
        "result_count": len(result)
    }
)
```

## 📚 관련 문서

- [SQLALCHEMY_TRACING_FIX.md](SQLALCHEMY_TRACING_FIX.md) - Async SQLAlchemy 트레이싱 수정 가이드
- [ERROR_REPORTING_README.md](ERROR_REPORTING_README.md) - Telegram 에러 리포팅
- [CLAUDE.md](CLAUDE.md) - 전체 프로젝트 가이드
- [OpenTelemetry Python Docs](https://opentelemetry.io/docs/instrumentation/python/)
- [SigNoz Documentation](https://signoz.io/docs/)

## ✅ 체크리스트

설정이 완료되었는지 확인:

- [ ] `SIGNOZ_ENABLED=true` in .env
- [ ] SigNoz 컨테이너 실행 중 (`docker compose ps`)
- [ ] OTEL Collector healthy (`docker compose ps | grep otel-collector`)
- [ ] ClickHouse에 로그 저장 확인 (`SELECT count(*) FROM signoz_logs.logs`)
- [ ] SigNoz UI에서 로그 확인 가능 (http://localhost:3301/logs)
- [ ] Traces와 Logs 연결 확인 (Trace ID로 관련 로그 찾기)

## 🎉 완료!

이제 애플리케이션의 모든 로그가 SigNoz에 수집됩니다:
- ✅ Python 로그 (INFO 이상)
- ✅ SQL 쿼리
- ✅ HTTP 요청/응답
- ✅ 에러 및 예외
- ✅ 비즈니스 로직 이벤트

SigNoz UI에서 실시간으로 로그를 모니터링하고 분석할 수 있습니다! 🚀
