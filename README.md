# Auto Trader

자동 거래 시스템으로, 다양한 금융 데이터를 수집하고 분석하여 거래 신호를 제공합니다.

## 기능

- 주식 및 암호화폐 데이터 수집
- 기술적 분석 지표 계산
- **다중 시간대 분석 (일봉 + 분봉)**
  - 일봉 200개: 장기적 추세 분석
  - 60분 캔들 12개: 중기적 방향성 (하루 전반적 상승/하락 추세)
  - 5분 캔들 12개: 단기적 모멘텀과 변동성
  - 1분 캔들 10개: 초단기 급등락 및 거래량 폭증 포착
- 자동 거래 신호 생성
- Telegram 봇을 통한 알림
- 웹 대시보드

## 설치

### 요구사항

- Python 3.13+
- UV (패키지 관리)
- PostgreSQL
- Redis

### 설치 방법

1. 저장소 클론
```bash
git clone <repository-url>
cd auto_trader
```

2. 의존성 설치
```bash
uv sync --all-groups
```

3. 환경 변수 설정
```bash
cp env.example .env
# .env 파일을 편집하여 필요한 설정값 입력
```

**필수 환경 변수:**
- `UPBIT_ACCESS_KEY`: 업비트 API 액세스 키
- `UPBIT_SECRET_KEY`: 업비트 API 시크릿 키
- `KIS_APP_KEY`: 한국투자증권 API 앱 키
- `KIS_APP_SECRET`: 한국투자증권 API 시크릿
- `GOOGLE_API_KEY`: Google Gemini API 키
- `TELEGRAM_TOKEN`: Telegram 봇 토큰
- `DATABASE_URL`: PostgreSQL 데이터베이스 연결 URL
- `REDIS_URL`: Redis 연결 URL

4. 데이터베이스 마이그레이션
```bash
uv run alembic upgrade head
```

5. 애플리케이션 실행
```bash
uv run uvicorn app.main:app --reload
```

### MCP 서버 실행

MCP 서버는 시장/보유종목 조회용 read-only 도구를 제공합니다.

필수 환경 변수:
- `MCP_TYPE` (기본: streamable-http)
- `MCP_HOST` (기본: 0.0.0.0)
- `MCP_PORT` (기본: 8765)
- `MCP_PATH` (기본: /mcp)
- `MCP_USER_ID` (기본: 1, 수동 보유종목 조회용)

Docker (production compose):
```bash
docker compose -f docker-compose.prod.yml up -d mcp
```

자세한 내용은 `app/mcp_server/README.md`를 참고하세요.

## 사용법

### 암호화폐 분석 (업비트)

업비트 API를 사용하여 암호화폐를 분석합니다. 일봉 200개와 함께 다음 분봉 데이터를 자동으로 수집합니다:

- **60분 캔들 (최근 12개)**: 중기적 방향성 분석
- **5분 캔들 (최근 12개)**: 단기적 모멘텀 분석  
- **1분 캔들 (최근 10개)**: 초단기 변동성 분석

```python
from app.analysis.service_analyzers import UpbitAnalyzer

analyzer = UpbitAnalyzer()
await analyzer.analyze_coins(["KRW-BTC", "KRW-ETH"])
```

### 주식 분석 (Yahoo Finance)

Yahoo Finance API를 사용하여 미국 주식을 분석합니다:

```python
from app.analysis.service_analyzers import YahooAnalyzer

analyzer = YahooAnalyzer()
await analyzer.analyze_stocks(["AAPL", "GOOGL", "MSFT"])
```

### 국내주식 분석 (KIS)

한국투자증권 API를 사용하여 국내 주식을 분석합니다:

```python
from app.analysis.service_analyzers import KISAnalyzer

analyzer = KISAnalyzer()
await analyzer.analyze_stock("삼성전자")
```

**참고**: KIS 분봉 데이터는 API 제한으로 인해 일부 상황에서 작동하지 않을 수 있습니다. 이 경우 일봉 데이터만으로 분석을 수행하며, 분봉 데이터 수집 실패 시에도 분석은 정상적으로 진행됩니다.

**KIS 분봉 API 제한사항**: 현재 KIS 분봉 API의 `time_unit` 파라미터가 제대로 작동하지 않아 모든 시간대에서 동일한 데이터가 반환됩니다. 이는 API 자체의 문제로, 향후 한국투자증권의 API 문서 업데이트나 기술지원을 통해 해결될 예정입니다.

## 테스트

### 테스트 환경 설정

개발 의존성 설치:
```bash
uv sync --all-groups
```

### 테스트 실행

모든 테스트 실행:
```bash
make test
# 또는
uv run pytest tests/ -v
```

단위 테스트만 실행:
```bash
make test-unit
# 또는
uv run pytest tests/ -v -m "not integration"
```

통합 테스트만 실행:
```bash
make test-integration
# 또는
uv run pytest tests/ -v -m "integration"
```

커버리지 리포트와 함께 테스트 실행:
```bash
make test-cov
# 또는
uv run pytest tests/ -v --cov=app --cov-report=html
```

### 테스트 마커

- `@pytest.mark.unit`: 단위 테스트
- `@pytest.mark.integration`: 통합 테스트
- `@pytest.mark.slow`: 느린 테스트 (선택적 실행)

### 코드 품질

코드 포맷팅:
```bash
make format
```

린팅 검사:
```bash
make lint
```

보안 검사:
```bash
make security
```

## 개발

### Makefile 명령어

```bash
make help          # 사용 가능한 명령어 목록
make install       # 프로덕션 의존성 설치
make install-dev   # 개발 의존성 설치
make test          # 모든 테스트 실행
make test-cov      # 커버리지와 함께 테스트 실행
make lint          # 코드 품질 검사
make format        # 코드 포맷팅
make clean         # 생성된 파일 정리
make dev           # 개발 서버 시작
```

### 테스트 구조

```
tests/
├── __init__.py
├── conftest.py           # 공통 fixture 및 설정
├── test_settings.py      # 테스트 환경 설정
├── test_config.py        # 설정 모듈 테스트
├── test_routers.py       # API 라우터 테스트
├── test_analysis.py      # 분석 모듈 테스트
├── test_services.py      # 서비스 모듈 테스트
└── test_integration.py   # 통합 테스트
```

## CI/CD

GitHub Actions를 통해 자동으로 다음을 실행합니다:

- **린팅**: Ruff 린터 + 포맷터, Pyright 타입 체커
- **테스트**: Python 3.13에서 테스트 실행 (lint 통과 후)
- **보안**: bandit, safety 검사
- **커버리지**: 테스트 커버리지 리포트 생성

## 모니터링 및 관찰성 (Monitoring & Observability)

이 프로젝트는 **SigNoz**와 **OpenTelemetry**를 사용하여 분산 추적, 메트릭 수집, 에러 리포팅을 지원합니다.

### SigNoz 설정

#### 1. SigNoz 로컬 실행 (Docker Compose)

```bash
# 애플리케이션 스택 + SigNoz
docker compose -f docker-compose.yml -f docker-compose.monitoring.yml up -d

# SigNoz만 실행
docker compose -f docker-compose.monitoring.yml up -d

# 상태 확인
docker compose -f docker-compose.monitoring.yml ps
```

**SigNoz 서비스 접속:**
- **UI 대시보드**: http://localhost:3301
- **OTLP gRPC Endpoint**: localhost:4317

#### 2. 환경 변수 설정

`.env` 파일에 다음 설정을 추가:

```bash
# OpenTelemetry 설정 (Grafana Stack)
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=localhost:4317
OTEL_INSECURE=true
OTEL_SERVICE_NAME=auto-trader
OTEL_SERVICE_VERSION=0.1.0
OTEL_ENVIRONMENT=development

# Telegram 에러 리포팅
ERROR_REPORTING_ENABLED=true
ERROR_REPORTING_CHAT_ID=your_telegram_chat_id
TELEGRAM_TOKEN=your_telegram_bot_token
ERROR_DUPLICATE_WINDOW=300
```

#### 3. Telegram Chat ID 찾기

1. **Telegram 봇 생성:**
   - Telegram에서 [@BotFather](https://t.me/botfather) 검색
   - `/newbot` 명령으로 새 봇 생성
   - Bot Token 저장

2. **Chat ID 확인:**
   - 생성한 봇에게 아무 메시지나 전송
   - 브라우저에서 다음 URL 접속:
     ```
     https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
     ```
   - 응답에서 `"chat":{"id":123456789}` 부분의 숫자가 Chat ID

3. **.env에 설정:**
   ```bash
   TELEGRAM_TOKEN=123456789:ABCdefGHIjklMNOpqrsTUVwxyz
   ERROR_REPORTING_CHAT_ID=123456789
   ```

#### 4. 애플리케이션 실행 및 확인

```bash
# 의존성 설치 (OpenTelemetry 패키지 포함)
uv sync

# 애플리케이션 실행
uv run uvicorn app.main:app --reload
```

**로그 확인:**
```
INFO: Telemetry initialized: auto-trader v0.1.0 (development)
INFO: Error reporting initialized: chat_id=123456789, duplicate_window=300s
INFO: FastAPI instrumented for telemetry
```

#### 5. 모니터링 테스트 엔드포인트

애플리케이션에는 모니터링 기능을 테스트할 수 있는 엔드포인트가 포함되어 있습니다:

```bash
# 헬스 체크
curl http://localhost:8000/api/test/health-check

# 일반 에러 테스트 (Telegram으로 에러 전송)
curl http://localhost:8000/api/test/error

# 크리티컬 에러 테스트
curl http://localhost:8000/api/test/critical

# 커스텀 트레이싱 테스트 (SigNoz에서 스팬 확인)
curl http://localhost:8000/api/test/trace

# 느린 요청 테스트 (메트릭 확인)
curl http://localhost:8000/api/test/slow

# HTTP 에러 테스트
curl http://localhost:8000/api/test/http-error
```

#### 6. SigNoz 대시보드 사용

1. **http://localhost:3301** 접속
2. **Services** 탭에서 `auto-trader` 서비스 확인
3. **Traces** 탭:
   - HTTP 요청 추적
   - 커스텀 스팬 확인 (`/api/test/trace` 호출 후)
   - 에러 스팬 확인
4. **Metrics** 탭:
   - `http.server.request.duration`: 요청 처리 시간
   - `http.server.request.count`: 요청 카운트
   - `http.server.error.count`: 에러 카운트

#### 7. 프로덕션 환경 설정

**원격 Grafana Stack 사용 (Grafana Cloud 또는 자체 호스팅):**

```bash
# .env
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=your-tempo-host:443  # Grafana Cloud 또는 자체 호스팅 Tempo
OTEL_INSECURE=false  # TLS 사용
OTEL_SERVICE_NAME=auto-trader
OTEL_ENVIRONMENT=production
```

**자체 호스팅 Grafana Stack:**

```bash
# .env
OTEL_ENABLED=true
OTEL_EXPORTER_OTLP_ENDPOINT=your-tempo-server:4317
OTEL_INSECURE=false  # TLS 권장
OTEL_SERVICE_NAME=auto-trader
OTEL_ENVIRONMENT=production
```

### 모니터링 기능

#### 자동 수집 데이터

- **HTTP 요청 추적**: 모든 API 요청의 상세 트레이싱
- **데이터베이스 쿼리**: SQLAlchemy 쿼리 추적
- **Redis 작업**: Redis 명령 추적
- **HTTP 클라이언트**: httpx, requests 호출 추적
- **메트릭**: 요청 시간, 카운트, 에러율

#### Telegram 에러 리포팅

- **자동 에러 감지**: ERROR/CRITICAL 레벨 자동 전송
- **중복 방지**: 동일 에러 5분간 1회만 전송 (Redis 기반)
- **풍부한 컨텍스트**:
  - 에러 타입 및 메시지
  - 전체 스택 트레이스
  - 요청 정보 (URL, method, client IP)
  - 타임스탬프

#### 커스텀 트레이싱

코드에서 커스텀 스팬 추가:

```python
from app.monitoring.telemetry import get_tracer

tracer = get_tracer(__name__)

with tracer.start_as_current_span("my_operation") as span:
    span.set_attribute("user_id", user_id)
    span.set_attribute("operation_type", "analysis")
    # Your code here
```

#### 커스텀 메트릭

코드에서 커스텀 메트릭 추가:

```python
from app.monitoring.telemetry import get_meter

meter = get_meter(__name__)

# Counter
counter = meter.create_counter(
    name="trades.executed",
    description="Number of trades executed",
    unit="1"
)
counter.add(1, {"market": "upbit", "result": "success"})

# Histogram
histogram = meter.create_histogram(
    name="trade.amount",
    description="Trade amount in KRW",
    unit="KRW"
)
histogram.record(100000, {"market": "upbit"})
```

### 모니터링 비활성화

개발 중 모니터링이 필요 없는 경우:

```bash
# .env
OTEL_ENABLED=false
ERROR_REPORTING_ENABLED=false
```

### 트러블슈팅

**SigNoz에 데이터가 보이지 않는 경우:**

1. OTLP endpoint 연결 확인:
   ```bash
   telnet localhost 4317
   ```

2. SigNoz 컨테이너 상태 확인:
   ```bash
   cd signoz/deploy/
   docker compose -f docker/clickhouse-setup/docker-compose.yaml logs
   ```

3. 애플리케이션 로그 확인:
   ```bash
   # "Telemetry initialized" 로그 확인
   uv run uvicorn app.main:app --reload
   ```

**Telegram 메시지가 전송되지 않는 경우:**

1. Bot Token 확인:
   ```bash
   curl https://api.telegram.org/bot<YOUR_TOKEN>/getMe
   ```

2. Chat ID 확인:
   ```bash
   curl https://api.telegram.org/bot<YOUR_TOKEN>/getUpdates
   ```

3. Redis 연결 확인 (에러 중복 방지용):
   ```bash
   docker compose exec redis redis-cli ping
   ```

### 추가 문서

자세한 내용은 다음 문서를 참고하세요:
- [MONITORING_README.md](MONITORING_README.md) - 모니터링 상세 가이드
- [OpenTelemetry 문서](https://opentelemetry.io/docs/)
- [SigNoz 문서](https://signoz.io/docs/)

## 라이센스

이 프로젝트는 MIT 라이센스 하에 배포됩니다.
