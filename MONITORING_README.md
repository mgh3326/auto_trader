# Monitoring & Observability Guide

이 문서는 Auto Trader 프로젝트의 모니터링 및 관찰 가능성(Observability) 기능에 대한 가이드입니다.

## 목차

1. [개요](#개요)
2. [아키텍처](#아키텍처)
3. [SigNoz에서 Grafana 스택으로 마이그레이션](#signoz에서-grafana-스택으로-마이그레이션)
4. [OpenTelemetry & Grafana 관찰성 스택 설정](#opentelemetry--grafana-관찰성-스택-설정)
5. [Telegram 에러 리포팅](#telegram-에러-리포팅)
6. [사용 방법](#사용-방법)
7. [Raspberry Pi 리소스 최적화](#raspberry-pi-리소스-최적화)
8. [트러블슈팅](#트러블슈팅)

## 개요

이 프로젝트는 다음과 같은 모니터링 기능을 제공합니다:

### OpenTelemetry & Grafana 관찰성 스택
- **분산 추적(Distributed Tracing)**: Tempo를 통한 HTTP 요청, DB 쿼리, Redis 작업 자동 추적
- **로그 수집(Logs)**: Loki + Promtail을 통한 Docker 컨테이너 로그 수집 및 검색
- **메트릭(Metrics)**: Prometheus를 통한 요청 처리 시간, 에러 카운트, 커스텀 메트릭
- **통합 시각화**: Grafana를 통한 Traces, Logs, Metrics 통합 대시보드
- **자동 계측(Auto-instrumentation)**: FastAPI, httpx, SQLAlchemy, Redis
- **커스텀 스팬**: 비즈니스 로직의 세부 추적
- **Trace-to-Log 연동**: 트레이스에서 관련 로그로 바로 이동

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
           │ (OTLP gRPC/HTTP)             │
           ▼                              ▼
  ┌─────────────────────┐        ┌──────────────┐
  │  Grafana Stack      │        │  Telegram    │
  │  ┌──────────────┐   │        │   Bot API    │
  │  │   Grafana    │   │        └──────────────┘
  │  │ (Dashboards) │   │
  │  └──────────────┘   │
  │  ┌──────────────┐   │
  │  │    Tempo     │◄──┼─── Traces (OTLP)
  │  │   (Traces)   │   │
  │  └──────────────┘   │
  │  ┌──────────────┐   │
  │  │     Loki     │◄──┼─── Logs (Promtail)
  │  │    (Logs)    │   │
  │  └──────────────┘   │
  │  ┌──────────────┐   │
  │  │  Prometheus  │◄──┼─── Metrics (OTLP)
  │  │  (Metrics)   │   │
  │  └──────────────┘   │
  └─────────────────────┘
           ▲
           │
  ┌────────┴─────────┐
  │    Promtail      │
  │ (Log Collector)  │
  └──────────────────┘
           ▲
           │
    Docker Container Logs
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

## SigNoz에서 Grafana 스택으로 마이그레이션

이전에 SigNoz를 사용하고 있었다면 다음 단계를 따라 Grafana 스택으로 마이그레이션하세요:

### 1. 기존 SigNoz 스택 중지 및 정리

```bash
# SigNoz 컨테이너 중지
docker compose -f docker-compose.signoz.yml down

# (선택사항) SigNoz 데이터 백업
# 필요한 경우 트레이스/로그 데이터 백업

# (선택사항) SigNoz 볼륨 삭제
docker volume rm signoz_data
```

### 2. 환경 변수 업데이트

`.env` 파일에서 환경 변수 이름 변경:

```bash
# 기존 (SigNoz)
SIGNOZ_ENDPOINT=localhost:4317
SIGNOZ_ENABLED=true
SIGNOZ_INSECURE=true

# 변경 후 (Grafana Stack - vendor-neutral)
OTEL_EXPORTER_OTLP_ENDPOINT=localhost:4317
OTEL_ENABLED=true
OTEL_INSECURE=true
OTEL_SERVICE_NAME=auto-trader
OTEL_SERVICE_VERSION=0.1.0
OTEL_ENVIRONMENT=development
```

### 3. Grafana 스택 시작

```bash
docker compose -f docker-compose.monitoring-rpi.yml up -d
```

### 4. 검증

```bash
# 자동 검증
bash scripts/test-monitoring-stack.sh

# Grafana 접속 확인
open http://localhost:3000  # admin/admin
```

### 주요 차이점

| 항목 | SigNoz | Grafana Stack |
|------|--------|---------------|
| **UI** | 통합 UI | Grafana (별도) |
| **Traces** | SigNoz | Tempo |
| **Logs** | SigNoz | Loki + Promtail |
| **Metrics** | SigNoz | Prometheus |
| **포트** | 4317 (OTLP) | 4317 (OTLP) - 동일 |
| **데이터 상관관계** | 자동 | Grafana에서 설정 필요 |
| **리소스 사용량** | 높음 | 중간 (Pi 최적화) |

### 롤백 방법

문제가 발생하면 SigNoz로 롤백:

```bash
# Grafana 스택 중지
docker compose -f docker-compose.monitoring-rpi.yml down

# 환경 변수 원복
# OTEL_* → SIGNOZ_*

# SigNoz 재시작
docker compose -f docker-compose.signoz.yml up -d
```

## OpenTelemetry & Grafana 관찰성 스택 설정

### 1. Grafana 스택 설치 (로컬 - Raspberry Pi 5 최적화)

프로젝트에 포함된 `docker-compose.monitoring-rpi.yml`을 사용합니다:

```bash
# Grafana 관찰성 스택 실행 (Tempo, Loki, Promtail, Prometheus, Grafana)
docker compose -f docker-compose.monitoring-rpi.yml up -d

# 상태 확인
docker compose -f docker-compose.monitoring-rpi.yml ps

# 로그 확인
docker compose -f docker-compose.monitoring-rpi.yml logs -f
```

### 2. 접속 포인트

브라우저에서 다음 주소로 접속:
- **Grafana UI**: http://localhost:3000 (admin/admin)
- **Tempo HTTP**: http://localhost:3200
- **Loki HTTP**: http://localhost:3100
- **Prometheus**: http://localhost:9090
- **OTLP gRPC endpoint**: localhost:4317
- **OTLP HTTP endpoint**: localhost:4318

### 3. 환경 변수 설정

`.env` 파일에 다음 설정을 추가:

```bash
# OpenTelemetry / Grafana Stack
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=localhost:4317
OTEL_INSECURE=true
OTEL_SERVICE_NAME=auto-trader
OTEL_SERVICE_VERSION=0.1.0
OTEL_ENVIRONMENT=development
```

### 4. 스택 검증 (Smoke Test)

스택이 올바르게 실행되는지 자동으로 확인:

```bash
# 자동화된 smoke test 실행 (실행 권한 부여 필요)
chmod +x scripts/test-monitoring-stack.sh
bash scripts/test-monitoring-stack.sh

# 또는 수동 검증
docker compose -f docker-compose.monitoring-rpi.yml ps  # 모든 컨테이너 Up 확인
curl http://localhost:3200/ready  # Tempo 확인
curl http://localhost:3100/ready  # Loki 확인
curl http://localhost:9090/-/healthy  # Prometheus 확인
curl http://localhost:3000/api/health  # Grafana 확인
```

### 5. 의존성 설치

```bash
# pyproject.toml에 이미 추가되어 있음
uv sync
```

### 6. 애플리케이션 실행

```bash
make dev
# 또는
uv run uvicorn app.main:app --reload
```

### 7. Grafana에서 데이터 확인

1. http://localhost:3000 접속 (admin/admin)
2. **Configuration > Data Sources**에서 Tempo, Loki, Prometheus 연결 확인
3. **Explore** 탭 선택:
   - **Tempo**: 분산 추적(Traces) 확인, `auto-trader` 서비스의 HTTP 요청 추적
   - **Loki**: 로그 확인, 컨테이너별 로그 검색
   - **Prometheus**: 메트릭 확인, HTTP 요청 카운트, 응답 시간 등
4. **Trace-to-Log 연동 테스트**:
   - Tempo에서 트레이스 선택
   - "Logs for this span" 버튼 클릭
   - 관련 로그가 자동으로 표시되는지 확인

## Telegram 에러 리포팅

### 1. Telegram Bot 생성

1. Telegram에서 [@BotFather](https://t.me/botfather) 검색
2. `/newbot` 명령으로 새 봇 생성
3. Bot Token 저장 (예: `123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11`)

### 2. ZeroSSL 가이드 (Caddyfile 설정)

- Let's Encrypt 속도 제한 → ZeroSSL로 전환
  - `Caddyfile`의 `global options` 블록에 다음 설정 추가:
    ```caddyfile
    {
        email {$ACME_EMAIL}
        acme_ca https://acme.zerossl.com/v2/DV90
    }
    ```

### 3. Chat ID 확인

1. 봇에게 메시지 전송 (아무 메시지나)
2. 다음 URL 접속:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
3. 응답에서 `chat.id` 값 확인

### 4. 환경 변수 설정

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

### 1. 원격 Grafana 스택 사용

Grafana Cloud 또는 자체 호스팅 Grafana 스택:

```bash
# .env
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=your-tempo-host:4317  # OTLP 엔드포인트
OTEL_INSECURE=false                               # TLS 사용
OTEL_SERVICE_NAME=auto-trader
OTEL_SERVICE_VERSION=0.1.0
OTEL_ENVIRONMENT=production
```

### 2. 보안 설정

프로덕션 환경에서는 TLS를 사용하는 것을 권장합니다:

```bash
# TLS/SSL을 사용하는 원격 엔드포인트
OTEL_EXPORTER_OTLP_ENDPOINT=your-tempo-host:443
OTEL_INSECURE=false
```

### 3. Grafana 인증 강화

프로덕션에서는 반드시 기본 admin/admin 비밀번호를 변경하고 익명 접근을 비활성화하세요:

```yaml
# docker-compose.monitoring-rpi.yml에서 수정
environment:
  - GF_SECURITY_ADMIN_PASSWORD=strong_password_here
  - GF_AUTH_ANONYMOUS_ENABLED=false
```

### 4. 샘플링 설정

고트래픽 환경에서는 샘플링 비율 조정이 필요할 수 있습니다. 현재는 모든 트레이스를 수집하지만, 필요시 `app/monitoring/telemetry.py`에서 샘플링 설정을 추가할 수 있습니다.

## 모니터링 비활성화

개발 중 모니터링이 필요 없는 경우:

```bash
# .env
OTEL_ENABLED=false
ERROR_REPORTING_ENABLED=false
```

## Raspberry Pi 리소스 최적화

### 현재 리소스 설정

Raspberry Pi 5 (8GB RAM)에서 안정적으로 실행되도록 최적화된 설정:

| 서비스 | 메모리 제한 | 메모리 예약 | CPU 제한 | 비고 |
|--------|-------------|-------------|---------|------|
| Tempo | 512MB | 256MB | 1.0 core | 트레이스 저장 (7일) |
| Loki | 512MB | 256MB | 1.0 core | 로그 저장 (7일) |
| Promtail | - | - | 0.5 core | 경량 로그 수집기 |
| Prometheus | 512MB | 256MB | 1.0 core | 메트릭 저장 |
| Grafana | 512MB | 256MB | 1.0 core | 시각화 대시보드 |
| **총합** | **~2GB** | **~1GB** | **4.5 cores** | Pi 5 (8GB): 여유 충분 |

### 권장 시스템 요구사항

**최소 사양 (개발):**
- RAM: 4GB
- CPU: 4 cores
- 디스크: 10GB (로그/트레이스 저장용)

**권장 사양 (프로덕션):**
- RAM: 8GB
- CPU: 4 cores
- 디스크: 50GB+ (retention 기간에 따라)

### 리소스 부족 시 조정 방법

#### 1. 메모리 제한 줄이기

`docker-compose.monitoring-rpi.yml` 수정:

```yaml
services:
  tempo:
    mem_limit: 256m      # 512m → 256m
    mem_reservation: 128m  # 256m → 128m

  loki:
    mem_limit: 256m
    mem_reservation: 128m
```

#### 2. Retention 기간 단축

**Tempo** (`grafana-config/tempo.yaml`):
```yaml
compactor:
  compaction:
    block_retention: 48h  # 168h → 48h (2일)
```

**Loki** (`grafana-config/loki.yaml`):
```yaml
limits_config:
  retention_period: 48h  # 168h → 48h (2일)
```

#### 3. 불필요한 서비스 비활성화

Prometheus만 필요한 경우:

```bash
# Tempo, Loki, Promtail 제외하고 실행
docker compose -f docker-compose.monitoring-rpi.yml up -d prometheus grafana
```

#### 4. 샘플링 비율 조정

고트래픽 시 모든 트레이스를 수집하지 않도록 샘플링:

`app/monitoring/telemetry.py`에서:
```python
# 10%만 샘플링
sampler = TraceIdRatioBased(0.1)
```

### 리소스 모니터링

Docker 컨테이너 리소스 사용량 확인:

```bash
# 실시간 모니터링
docker stats

# 특정 컨테이너만 확인
docker stats tempo loki prometheus grafana

# 메모리 사용량만 확인
docker stats --no-stream --format "table {{.Name}}\t{{.MemUsage}}"
```

### 성능 최적화 팁

1. **로그 필터링**: Promtail에서 불필요한 로그 제외
2. **메트릭 간격**: Prometheus scrape interval 증가 (15s → 30s)
3. **트레이스 샘플링**: 전체 대신 10-20% 샘플링
4. **디스크 I/O**: SSD 사용 권장 (SD 카드보다 성능 향상)

## 트러블슈팅

### Grafana에 데이터가 보이지 않음

1. **OTLP endpoint 확인**:
   ```bash
   # Tempo 포트가 열려있는지 확인
   curl http://localhost:3200/status
   curl http://localhost:4317  # OTLP gRPC (연결 확인)
   ```

2. **Grafana 스택 컨테이너 상태 확인**:
   ```bash
   docker compose -f docker-compose.monitoring-rpi.yml ps
   docker compose -f docker-compose.monitoring-rpi.yml logs tempo
   docker compose -f docker-compose.monitoring-rpi.yml logs loki
   docker compose -f docker-compose.monitoring-rpi.yml logs promtail
   ```

3. **데이터소스 연결 확인**:
   - Grafana (http://localhost:3000) 접속
   - Configuration > Data Sources
   - Tempo, Loki, Prometheus 상태 확인
   - "Save & test" 버튼으로 연결 테스트

4. **애플리케이션 로그 확인**:
   ```bash
   # 텔레메트리 초기화 로그 확인
   uv run uvicorn app.main:app --reload
   # "Telemetry initialized" 메시지 확인
   ```

### Trace-to-Log 연동이 작동하지 않음

1. **Promtail 로그 확인**:
   ```bash
   docker compose -f docker-compose.monitoring-rpi.yml logs promtail
   # Docker 소켓 접근 및 Loki 연결 확인
   ```

2. **Loki 쿼리 테스트**:
   ```bash
   # Loki에서 로그 확인
   curl 'http://localhost:3100/loki/api/v1/query?query={container="auto-trader"}'
   ```

3. **Grafana datasource 설정 확인**:
   - `grafana-config/grafana-datasources.yaml` 파일의 `tracesToLogs.tags` 확인
   - `['service', 'container']` 태그가 설정되어 있는지 확인

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
- [Grafana 문서](https://grafana.com/docs/)
- [Grafana Tempo 문서](https://grafana.com/docs/tempo/latest/)
- [Grafana Loki 문서](https://grafana.com/docs/loki/latest/)
- [Prometheus 문서](https://prometheus.io/docs/)
- [FastAPI 계측 가이드](https://opentelemetry.io/docs/instrumentation/python/automatic/fastapi/)
- [Telegram Bot API](https://core.telegram.org/bots/api)

## 참고사항

- 모니터링 기능은 기본적으로 비활성화되어 있습니다
- 프로덕션 환경에서는 텔레메트리와 에러 리포팅 모두 활성화를 권장합니다
- Grafana 스택은 Raspberry Pi 5에 최적화되어 있으며 메모리 제한이 설정되어 있습니다
- 로컬 개발 시에는 필요할 때만 스택을 실행하세요
- Telegram 에러 리포팅은 중요한 에러만 전송하도록 설계되었습니다 (ERROR/CRITICAL)
- Trace-to-Log 연동을 통해 트레이스에서 관련 로그를 바로 확인할 수 있습니다
