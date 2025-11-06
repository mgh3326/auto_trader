# Monitoring & Observability Guide

이 문서는 Auto Trader 프로젝트의 모니터링 및 관찰 가능성(Observability) 기능에 대한 가이드입니다.

## 목차

1. [개요](#개요)
2. [아키텍처](#아키텍처)
3. [OpenTelemetry & SigNoz 설정](#opentelemetry--signoz-설정)
4. [Telegram 에러 리포팅](#telegram-에러-리포팅)
5. [사용 방법](#사용-방법)
6. [트러블슈팅](#트러블슈팅)

## 개요

이 프로젝트는 다음과 같은 모니터링 기능을 제공합니다:

### OpenTelemetry & SigNoz
- **분산 추적(Distributed Tracing)**: HTTP 요청, DB 쿼리, Redis 작업 자동 추적
- **메트릭(Metrics)**: 요청 처리 시간, 에러 카운트, 커스텀 메트릭
- **자동 계측(Auto-instrumentation)**: FastAPI, httpx, SQLAlchemy, Redis
- **커스텀 스팬**: 비즈니스 로직의 세부 추적

### Telegram 에러 리포팅
- **실시간 에러 알림**: ERROR/CRITICAL 레벨 에러를 Telegram으로 즉시 전송
- **중복 방지**: 동일 에러 5분간 1회만 전송 (설정 가능)
- **풍부한 컨텍스트**: 타임스탬프, 에러 타입, 메시지, 스택 트레이스, 요청 정보
- **다중 채팅방 지원**: 여러 Telegram 채팅방으로 동시 전송

## 아키텍처

```
┌─────────────────────────────────────────────────────────────┐
│                        FastAPI App                           │
│                                                               │
│  ┌────────────────────────────────────────────────────────┐  │
│  │         MonitoringMiddleware                           │  │
│  │  - 요청/응답 타이밍                                       │  │
│  │  - 에러 캐치 및 리포팅                                    │  │
│  │  - 메트릭 수집                                           │  │
│  └────────────────────────────────────────────────────────┘  │
│                                                               │
│  ┌─────────────────┐          ┌─────────────────────────┐   │
│  │ TelemetryManager│          │ TelegramErrorReporter   │   │
│  │ - Traces        │          │ - 에러 감지              │   │
│  │ - Metrics       │          │ - 중복 제거              │   │
│  │ - Custom Spans  │          │ - 메시지 포매팅          │   │
│  └─────────────────┘          └─────────────────────────┘   │
│          │                              │                    │
└──────────┼──────────────────────────────┼────────────────────┘
           │                              │
           ▼                              ▼
    ┌─────────────┐              ┌──────────────┐
    │   SigNoz    │              │  Telegram    │
    │  (OTLP)     │              │   Bot API    │
    └─────────────┘              └──────────────┘
```

### 핵심 컴포넌트

1. **app/monitoring/telemetry.py**
   - `TelemetryManager`: OpenTelemetry 초기화 및 관리
   - `TelemetryConfig`: 설정 클래스
   - 자동 계측 및 커스텀 트레이싱 헬퍼

2. **app/monitoring/error_reporter.py**
   - `TelegramErrorReporter`: Telegram 에러 리포팅
   - 중복 방지 로직
   - 에러 메시지 포매팅

3. **app/middleware/monitoring.py**
   - `MonitoringMiddleware`: 요청/응답 모니터링
   - 전역 예외 핸들러
   - 메트릭 수집

## OpenTelemetry & SigNoz 설정

### 1. SigNoz 설치 (로컬)

Docker Compose를 사용하여 SigNoz를 설치합니다:

```bash
# SigNoz 저장소 클론
git clone -b main https://github.com/SigNoz/signoz.git
cd signoz/deploy/

# Docker Compose로 실행
docker-compose -f docker/clickhouse-setup/docker-compose.yaml up -d

# 상태 확인
docker-compose -f docker/clickhouse-setup/docker-compose.yaml ps
```

### 2. SigNoz 접속

브라우저에서 다음 주소로 접속:
- **UI**: http://localhost:3301
- **OTLP gRPC endpoint**: localhost:4317

### 3. 환경 변수 설정

`.env` 파일에 다음 설정을 추가:

```bash
# OpenTelemetry / SigNoz
TELEMETRY_ENABLED=true
OTLP_ENDPOINT=localhost:4317
SERVICE_NAME=auto-trader
ENVIRONMENT=development
```

### 4. 의존성 설치

```bash
# pyproject.toml에 이미 추가되어 있음
uv sync
```

### 5. 애플리케이션 실행

```bash
make dev
# 또는
uv run uvicorn app.main:app --reload
```

### 6. SigNoz에서 데이터 확인

1. http://localhost:3301 접속
2. **Services** 탭에서 `auto-trader` 서비스 확인
3. **Traces** 탭에서 HTTP 요청 추적 확인
4. **Metrics** 탭에서 메트릭 확인

## Telegram 에러 리포팅

### 1. Telegram Bot 생성

1. Telegram에서 [@BotFather](https://t.me/botfather) 검색
2. `/newbot` 명령으로 새 봇 생성
3. Bot Token 저장 (예: `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)

### 2. Chat ID 확인

1. 봇에게 메시지 전송 (아무 메시지나)
2. 다음 URL 접속:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
3. 응답에서 `chat.id` 값 확인

### 3. 환경 변수 설정

`.env` 파일에 다음 설정을 추가:

```bash
# Telegram Bot (기존 설정)
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_IDS=123456789,987654321

# Telegram 에러 리포팅
TELEGRAM_ERROR_REPORTING_ENABLED=true
TELEGRAM_ERROR_DEDUP_MINUTES=5
```

### 4. 테스트

애플리케이션 실행 후 다음 엔드포인트로 에러 테스트:

```bash
# 의도적으로 에러 발생
curl http://localhost:8000/test-error

# 또는 Python으로 테스트
python -c "
from app.monitoring.error_reporter import TelegramErrorReporter
import asyncio

async def test():
    reporter = TelegramErrorReporter(
        bot_token='YOUR_TOKEN',
        chat_ids=['YOUR_CHAT_ID'],
        enabled=True
    )
    await reporter.initialize()
    result = await reporter.test_connection()
    print('Test result:', result)
    await reporter.shutdown()

asyncio.run(test())
"
```

## 사용 방법

### 커스텀 스팬 추가

비즈니스 로직에 커스텀 트레이싱 추가:

```python
from app.monitoring.telemetry import get_telemetry_manager

async def process_data(user_id: int):
    telemetry = get_telemetry_manager()

    # 커스텀 스팬으로 작업 추적
    with telemetry.trace_operation(
        "process_user_data",
        attributes={"user_id": user_id}
    ) as span:
        # 비즈니스 로직
        result = await fetch_user_data(user_id)

        # 스팬에 추가 정보
        span.set_attribute("data_size", len(result))

        return result
```

### 메트릭 기록

```python
from app.monitoring.telemetry import get_telemetry_manager

async def process_order(order_id: int):
    telemetry = get_telemetry_manager()

    # 카운터 증가
    telemetry.record_counter(
        "orders.processed",
        value=1,
        attributes={"status": "success"}
    )

    # 처리 시간 기록
    telemetry.record_histogram(
        "orders.processing_time",
        value=processing_time,
        attributes={"order_type": "buy"}
    )
```

### 수동 에러 리포팅

```python
from app.monitoring.error_reporter import get_error_reporter
import logging

async def critical_operation():
    error_reporter = get_error_reporter()

    try:
        # 위험한 작업
        result = await risky_operation()
    except Exception as e:
        # Telegram으로 에러 리포팅
        if error_reporter:
            await error_reporter.report_error(
                e,
                level=logging.CRITICAL,
                additional_context={"operation": "critical_operation"}
            )
        raise
```

### 현재 스팬에 속성 추가

```python
from app.monitoring.telemetry import get_telemetry_manager

async def process_request(request_data: dict):
    telemetry = get_telemetry_manager()

    # 현재 스팬에 속성 추가
    if telemetry:
        telemetry.add_span_attribute("request_type", request_data["type"])
        telemetry.add_span_attribute("user_id", request_data["user_id"])

    # 처리 로직
    result = await process(request_data)
    return result
```

## 프로덕션 환경 설정

### 1. 원격 SigNoz 사용

SigNoz Cloud 또는 자체 호스팅 SigNoz:

```bash
# .env
TELEMETRY_ENABLED=true
OTLP_ENDPOINT=your-signoz-host:4317  # 또는 ingest.signoz.io:443 (Cloud)
SERVICE_NAME=auto-trader
ENVIRONMENT=production
```

### 2. 보안 설정

프로덕션 환경에서는 TLS를 사용하는 것을 권장합니다:

```bash
# TLS/SSL을 사용하는 SigNoz Cloud
OTLP_ENDPOINT=ingest.signoz.io:443
```

### 3. 샘플링 설정

고트래픽 환경에서는 샘플링 비율 조정이 필요할 수 있습니다. 현재는 모든 트레이스를 수집하지만, 필요시 `app/monitoring/telemetry.py`에서 샘플링 설정을 추가할 수 있습니다.

## 모니터링 비활성화

개발 중 모니터링이 필요 없는 경우:

```bash
# .env
TELEMETRY_ENABLED=false
TELEGRAM_ERROR_REPORTING_ENABLED=false
```

## 트러블슈팅

### SigNoz에 데이터가 보이지 않음

1. **OTLP endpoint 확인**:
   ```bash
   # 포트가 열려있는지 확인
   telnet localhost 4317
   ```

2. **SigNoz 컨테이너 상태 확인**:
   ```bash
   cd signoz/deploy/
   docker-compose -f docker/clickhouse-setup/docker-compose.yaml ps
   docker-compose -f docker/clickhouse-setup/docker-compose.yaml logs
   ```

3. **애플리케이션 로그 확인**:
   ```bash
   # 텔레메트리 초기화 로그 확인
   uv run uvicorn app.main:app --reload
   ```

### Telegram 메시지가 전송되지 않음

1. **Bot Token 확인**:
   ```bash
   curl https://api.telegram.org/bot<YOUR_TOKEN>/getMe
   ```

2. **Chat ID 확인**:
   ```bash
   curl https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```

3. **테스트 연결**:
   ```python
   # test_telegram.py
   import asyncio
   from app.monitoring.error_reporter import TelegramErrorReporter

   async def test():
       reporter = TelegramErrorReporter(
           bot_token="YOUR_TOKEN",
           chat_ids=["YOUR_CHAT_ID"],
           enabled=True
       )
       await reporter.initialize()
       result = await reporter.test_connection()
       print(f"Test result: {result}")
       await reporter.shutdown()

   asyncio.run(test())
   ```

### 중복 에러가 계속 전송됨

중복 방지 시간 조정:

```bash
# .env
TELEGRAM_ERROR_DEDUP_MINUTES=10  # 10분으로 증가
```

### 메모리 사용량이 높음

고트래픽 환경에서 메모리 사용량이 높다면:

1. 스팬 배치 크기 조정 (코드 수정 필요)
2. 샘플링 비율 감소
3. 메트릭 수집 간격 조정

## 추가 리소스

- [OpenTelemetry Python 문서](https://opentelemetry.io/docs/instrumentation/python/)
- [SigNoz 문서](https://signoz.io/docs/)
- [FastAPI 계측 가이드](https://opentelemetry.io/docs/instrumentation/python/automatic/fastapi/)
- [Telegram Bot API](https://core.telegram.org/bots/api)

## 참고사항

- 모니터링 기능은 기본적으로 비활성화되어 있습니다
- 프로덕션 환경에서는 텔레메트리와 에러 리포팅 모두 활성화를 권장합니다
- SigNoz는 리소스를 많이 사용하므로 로컬 개발 시 필요할 때만 실행하세요
- Telegram 에러 리포팅은 중요한 에러만 전송하도록 설계되었습니다 (ERROR/CRITICAL)
