# Monitoring Validation Guide

이 문서는 Sentry 연동 검증 절차를 설명합니다.

## 1. 사전 조건

- `.env` 또는 `.env.prod`에 Sentry 변수 설정
- `SENTRY_DSN` 주입 확인
- 대상 프로세스 실행 중

## 2. 로컬 검증

```bash
# API
uv run uvicorn app.main:api --reload

# Worker
uv run celery -A app.core.celery_app.celery_app worker --loglevel=info

# MCP
uv run python -m app.mcp_server.main
```

## 3. Docker 검증

```bash
docker compose -f docker-compose.prod.yml up -d

docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml logs -f mcp
docker compose -f docker-compose.prod.yml logs -f websocket
```

## 4. 수동 시나리오

1. API 요청 1건 수행
2. 의도적 예외 1건 발생
3. Celery task 실패 1건 발생
4. MCP 잘못된 `MCP_TYPE` 실행
5. 통합 websocket 프로세스에서 Upbit/KIS 중 1개 연결 실패 유도

## 5. Sentry 확인 포인트

- `service` 태그로 4개 프로세스(api/worker/mcp/websocket) 분리 조회
- Errors/Transactions/Profiles 데이터 유입
- 민감 필드(`authorization`, `cookie`, `token`, `secret`, `password`) 마스킹 여부
