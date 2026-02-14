# Monitoring Validation Guide

이 문서는 Sentry 연동 전 기준의 런타임 검증 절차를 설명합니다.

## 1. 사전 조건

- `.env` 또는 `.env.prod` 설정 완료
- 대상 프로세스가 표준 커맨드로 실행 중

## 2. 로컬 검증

```bash
# API
uv run uvicorn app.main:api --reload

# Worker
uv run celery -A app.core.celery_app.celery_app worker --loglevel=info

# MCP
uv run python -m app.mcp_server.main
```

확인 항목:
- 서비스 기동 로그
- 예외 발생 시 오류 로그
- Telegram 알림(설정 시)

## 3. Docker 검증

```bash
docker compose -f docker-compose.prod.yml up -d

docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml logs -f mcp
docker compose -f docker-compose.prod.yml logs -f websocket
docker compose -f docker-compose.prod.yml logs -f kis_websocket
```

## 4. 수동 시나리오

1. API 요청 1건 수행
2. 의도적 예외 1건 발생
3. Celery task 1건 실행
4. websocket 이벤트 1건 수신
