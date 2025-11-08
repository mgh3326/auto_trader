# 에러 리포팅 시스템

자동 거래 시스템의 에러를 Telegram으로 자동 전송하는 시스템입니다.

## 주요 기능

### 1. 자동 에러 감지 및 전송
- FastAPI 미들웨어를 통해 모든 API 요청의 에러를 자동으로 감지
- 에러 발생 시 텔레그램으로 즉시 알림 전송
- OpenTelemetry와 통합되어 SigNoz에서 추적 가능

### 2. Redis 기반 중복 방지
- 동일한 에러가 반복 발생할 경우 중복 알림 방지
- 기본 5분(300초) 윈도우 내에서 중복 검사
- SHA-256 해시를 사용한 에러 식별

### 3. 풍부한 에러 정보
- 에러 타입 및 메시지
- 스택 트레이스 (최대 3000자)
- HTTP 요청 정보 (method, URL, client IP, user-agent)
- 추가 컨텍스트 정보

## 환경 변수 설정

### .env 파일

```bash
# 에러 리포팅 활성화
ERROR_REPORTING_ENABLED=true

# 텔레그램 봇 토큰 (공통)
TELEGRAM_TOKEN=your_telegram_bot_token_here

# 에러 리포팅 전용 Chat ID (선택사항, 미설정 시 TELEGRAM_CHAT_ID 사용)
ERROR_REPORTING_CHAT_ID=your_error_chat_id

# 또는 기본 Telegram Chat ID 사용
TELEGRAM_CHAT_ID=your_chat_id

# Redis 연결 (중복 방지용)
REDIS_URL=redis://localhost:6379/0

# 중복 에러 방지 시간 (초, 기본: 300초 = 5분)
ERROR_DUPLICATE_WINDOW=300
```

## 사용 방법

### 1. 자동 에러 리포팅 (FastAPI)

FastAPI 애플리케이션에서 발생하는 모든 에러는 자동으로 텔레그램에 전송됩니다.

```python
# app/main.py에서 자동으로 설정됨
# MonitoringMiddleware가 모든 에러를 감지하고 ErrorReporter로 전송
```

### 2. 수동 에러 리포팅

특정 위치에서 에러를 수동으로 보고하고 싶을 때:

```python
from app.monitoring.error_reporter import get_error_reporter

async def some_function():
    error_reporter = get_error_reporter()

    try:
        # 위험한 작업
        risky_operation()
    except Exception as e:
        # 에러 리포팅
        await error_reporter.send_error_to_telegram(
            e,
            additional_context={
                "function": "some_function",
                "user_id": 12345,
                "operation": "risky_operation"
            }
        )
        raise
```

### 3. 백그라운드 작업에서 사용

```python
from app.monitoring.error_reporter import get_error_reporter

async def background_task():
    error_reporter = get_error_reporter()

    try:
        # 백그라운드 작업 수행
        await process_data()
    except Exception as e:
        # 에러 리포팅 (요청 정보 없이)
        await error_reporter.send_error_to_telegram(
            e,
            additional_context={
                "task": "background_task",
                "timestamp": datetime.now().isoformat()
            }
        )
```

## 테스트

### 연결 테스트

```bash
python test_error_reporting.py
```

테스트 항목:
1. ✅ Telegram 연결 테스트
2. 🚨 단순 에러 (ZeroDivisionError)
3. 🚨 컨텍스트 정보가 포함된 에러 (KeyError)
4. 🚨 중복 에러 방지 (IndexError, 첫 번째만 전송됨)
5. 🚨 복잡한 에러 (중첩된 스택 트레이스)

### 실제 API 에러 테스트

개발 서버를 실행한 후:

```bash
# 서버 실행
make dev

# 존재하지 않는 엔드포인트 호출 (404는 알림 안 감)
curl http://localhost:8000/api/nonexistent

# 내부 서버 에러 발생시키기 (테스트 라우트)
curl http://localhost:8000/test/error
```

## 아키텍처

### 에러 흐름

```
Application Error
    ↓
MonitoringMiddleware (app/middleware/monitoring.py)
    ↓
ErrorReporter.send_error_to_telegram()
    ↓
Redis 중복 검사
    ↓ (중복 아님)
Telegram API
```

### 통합 구조

```
OpenTelemetry → SigNoz (에러 추적 및 분석)
    ↓
ErrorReporter → Telegram (실시간 알림)
```

## 주요 파일

### 1. ErrorReporter 클래스
- **위치**: [app/monitoring/error_reporter.py](app/monitoring/error_reporter.py)
- **역할**: 에러를 포맷팅하고 Telegram으로 전송, Redis 기반 중복 방지

### 2. MonitoringMiddleware
- **위치**: [app/middleware/monitoring.py](app/middleware/monitoring.py)
- **역할**: FastAPI 요청을 모니터링하고 에러 발생 시 ErrorReporter 호출

### 3. Application Setup
- **위치**: [app/main.py](app/main.py)
- **역할**: 애플리케이션 시작 시 ErrorReporter 초기화 및 설정

### 4. 환경 변수 예시
- **위치**: [env.example](env.example:131-139)
- **내용**: ERROR_REPORTING_ENABLED, ERROR_REPORTING_CHAT_ID 등

## 텔레그램 메시지 형식

### 연결 테스트 메시지
```
✅ Telegram Error Reporter Test

Connection successful at 2025-01-15 12:34:56 UTC
Error reporting is working correctly.
```

### 에러 알림 메시지
```
🚨 Error Alert
🕒 2025-01-15 12:34:56 UTC

Type: `ValueError`
Message: Invalid input data

Request Info:
  • Method: `POST`
  • URL: `/api/v1/trade`
  • Client: `192.168.1.100:54321`
  • User-Agent: `python-requests/2.31.0`

Stack Trace:
```
File "/app/services/trading.py", line 123
    validate_order(data)
ValueError: Invalid input data
...
```
```

## 중복 방지 메커니즘

### Redis 키 구조
```
error_rate_limit:{hash}
```

- **hash**: SHA-256(error_type + error_message[:200] + first_stack_frame)
- **TTL**: ERROR_DUPLICATE_WINDOW 초 (기본 300초)

### 동작 방식
1. 에러 발생 시 고유 해시 생성
2. Redis에서 해시 키 존재 여부 확인
3. 존재하지 않으면 → 텔레그램 전송 + Redis에 키 저장 (TTL 설정)
4. 존재하면 → 스킵 (중복으로 판단)

## 보안 고려사항

### 1. 민감한 정보 제거
- 에러 메시지가 500자를 초과하면 자동 절삭
- 스택 트레이스가 3000자를 초과하면 자동 절삭
- 최종 메시지가 4000자를 초과하면 추가 절삭 (Telegram 제한)

### 2. API 키 보호
- Telegram 봇 토큰은 환경 변수로 관리
- .env 파일은 .gitignore에 포함

### 3. Rate Limiting
- Redis 기반 중복 방지로 스팸 방지
- 동일한 에러는 5분에 1번만 전송

## 문제 해결

### 1. 에러 알림이 오지 않음

**확인 사항:**
```bash
# 환경 변수 확인
grep ERROR_REPORTING .env
grep TELEGRAM .env

# Redis 연결 확인
docker-compose exec redis redis-cli ping

# 테스트 스크립트 실행
python test_error_reporting.py
```

### 2. Redis 연결 실패

```bash
# Redis 컨테이너 시작
docker-compose up -d redis

# Redis 상태 확인
docker-compose ps redis

# Redis 로그 확인
docker-compose logs redis
```

### 3. Telegram 봇 토큰 문제

1. [@BotFather](https://t.me/BotFather)에게 `/mybots` 명령 전송
2. 봇 선택 → API Token 확인
3. `.env` 파일의 `TELEGRAM_TOKEN` 업데이트

### 4. Chat ID 확인

1. 봇에게 메시지 전송
2. 다음 URL 접속:
   ```
   https://api.telegram.org/bot<YOUR_BOT_TOKEN>/getUpdates
   ```
3. `"chat":{"id":123456789}` 형태로 Chat ID 확인

## 모니터링 통합

### OpenTelemetry + SigNoz
- 에러는 OpenTelemetry span으로 기록됨
- SigNoz에서 에러율, 빈도, 패턴 분석 가능
- Telegram 알림과 SigNoz 대시보드를 함께 사용

### 사용 예시
1. **실시간 알림**: Telegram으로 즉시 에러 확인
2. **상세 분석**: SigNoz 대시보드에서 에러 트렌드 분석
3. **패턴 발견**: SigNoz Alert로 에러 패턴 감지

## 추가 개선 사항

향후 추가 가능한 기능:
- [ ] 에러 심각도 레벨 (ERROR, WARNING, CRITICAL)
- [ ] 알림 우선순위 (긴급 알림은 중복 방지 무시)
- [ ] 에러 통계 주기적 요약 전송
- [ ] Slack, Discord 등 다른 채널 지원
- [ ] 에러 발생 횟수 임계값 설정

## 참고 문서

- [Telegram Bot API](https://core.telegram.org/bots/api)
- [SigNoz Documentation](https://signoz.io/docs/)
- [OpenTelemetry Python](https://opentelemetry.io/docs/instrumentation/python/)
- [CLAUDE.md](CLAUDE.md) - 전체 프로젝트 가이드
