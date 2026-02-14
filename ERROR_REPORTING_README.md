# Error Reporting Guide

Auto Trader의 에러 알림은 Telegram 기반으로 동작합니다.

## 주요 기능

- FastAPI 전역 예외 로깅
- Telegram 실시간 알림
- Redis 기반 중복 전송 방지

## 환경 변수

```bash
TELEGRAM_TOKEN=your_telegram_bot_token
TELEGRAM_CHAT_ID=your_chat_id
REDIS_URL=redis://localhost:6379/0
```

## 실행

```bash
# API
uv run uvicorn app.main:api --reload

# Worker
uv run celery -A app.core.celery_app.celery_app worker --loglevel=info
```

## 동작 흐름

```text
Application Exception
  -> app/main.py global exception handler logging
  -> app/monitoring/trade_notifier.py (Telegram notifier)
```

## 트러블슈팅

### Telegram 메시지가 오지 않는 경우

- `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_ID` 값 확인
- 봇이 해당 채팅방에 초대되었는지 확인
- 애플리케이션 로그에서 notifier 초기화 오류 확인
