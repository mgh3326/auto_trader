# Error Reporting Guide

Auto Trader의 오류 보고는 Telegram 알림 + Sentry 수집을 함께 사용합니다.

## 주요 기능

- FastAPI 전역 예외 로깅
- Celery/WS/MCP 프로세스 예외 수집
- Telegram 실시간 알림
- Redis 기반 중복 전송 방지

## 환경 변수

```bash
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
REDIS_URL=redis://localhost:6379/0

SENTRY_DSN=
SENTRY_ENVIRONMENT=
SENTRY_RELEASE=
SENTRY_TRACES_SAMPLE_RATE=1.0
SENTRY_PROFILES_SAMPLE_RATE=1.0
SENTRY_SEND_DEFAULT_PII=true
SENTRY_ENABLE_LOG_EVENTS=true
```

## 실행

```bash
# API
uv run uvicorn app.main:api --reload

# Worker
uv run celery -A app.core.celery_app.celery_app worker --loglevel=info

# MCP
uv run python -m app.mcp_server.main
```

## 동작 흐름

```text
Application Exception
  -> app/main.py global exception handler
  -> app/monitoring/sentry.py capture_exception
  -> Sentry event + (Telegram notifier when enabled)
```

## 트러블슈팅

### Telegram 메시지가 오지 않는 경우

- `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` 확인
- 봇 채팅방 초대 여부 확인
- 로그의 notifier 초기화 오류 확인

### Sentry 이벤트가 보이지 않는 경우

- `SENTRY_DSN` 값 확인
- `SENTRY_ENABLE_LOG_EVENTS` / 샘플링 설정 확인
- 프로세스 로그에서 sentry init 메시지 확인
