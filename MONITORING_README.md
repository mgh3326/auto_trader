# Monitoring & Observability Guide

현재 브랜치는 OTEL/Grafana/Signoz 스택을 제거한 상태입니다.
New Relic 연동 변경도 롤백되어, Sentry 연동 전까지는 기본 로그/알림 기반으로 운영합니다.

## 계측 대상 프로세스

- API (`uvicorn`)
- Celery worker
- MCP server
- Upbit websocket monitor
- KIS websocket monitor

## 실행 커맨드

```bash
# API
uv run uvicorn app.main:api --reload --host 0.0.0.0 --port 8000

# Worker
uv run celery -A app.core.celery_app.celery_app worker --loglevel=info

# MCP
uv run python -m app.mcp_server.main
```

## Docker (프로덕션)

```bash
# 마이그레이션
docker compose -f docker-compose.prod.yml --profile migration up migration

# 서비스 기동
docker compose -f docker-compose.prod.yml up -d

# 상태 확인
docker compose -f docker-compose.prod.yml ps
```

## 운영 확인

```bash
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml logs -f mcp
docker compose -f docker-compose.prod.yml logs -f websocket
docker compose -f docker-compose.prod.yml logs -f kis_websocket
```

## Telegram 알림

```bash
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
```
