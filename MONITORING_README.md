# Monitoring & Observability Guide

현재 브랜치는 OTEL/Grafana/Signoz 스택이 제거된 상태이며, 표준 모니터링은 Sentry입니다.

## 운영 정책

- 단일 Sentry 프로젝트 사용
- 프로세스 구분은 `service` 태그로 처리
- `SENTRY_DSN` 값이 있으면 환경(dev/staging/prod)과 무관하게 활성화
- 수집 범위: 에러 + 트레이스 + 프로파일
- 샘플링: `traces=1.0`, `profiles=1.0`
- `send_default_pii=true` 유지, 민감키(`authorization`, `cookie`, `token`, `secret`, `password`)는 마스킹
- `logger.error`는 Sentry 이벤트로 전송

## 계측 대상 프로세스

- API (`auto-trader-api`)
- Celery worker (`auto-trader-worker`)
- MCP server (`auto-trader-mcp`)
- Upbit websocket (`auto-trader-upbit-ws`)
- KIS websocket (`auto-trader-kis-ws`)

## 환경 변수

```bash
SENTRY_DSN=
SENTRY_ENVIRONMENT=
SENTRY_RELEASE=
SENTRY_TRACES_SAMPLE_RATE=1.0
SENTRY_PROFILES_SAMPLE_RATE=1.0
SENTRY_SEND_DEFAULT_PII=true
SENTRY_ENABLE_LOG_EVENTS=true
```

## 실행 커맨드

```bash
# API
uv run uvicorn app.main:api --reload --host 0.0.0.0 --port 8000

# Worker
uv run celery -A app.core.celery_app.celery_app worker --loglevel=info

# MCP
uv run python -m app.mcp_server.main

# WS
uv run python upbit_websocket_monitor.py
uv run python kis_websocket_monitor.py
```

## 운영 확인

```bash
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml logs -f mcp
docker compose -f docker-compose.prod.yml logs -f websocket
docker compose -f docker-compose.prod.yml logs -f kis_websocket
```

Sentry UI 확인 항목:
- `service:auto-trader-api` 등 태그 필터로 프로세스 분리 조회
- API/worker/ws/mcp 이벤트 유입 확인
- 트랜잭션 및 프로파일 생성 확인
