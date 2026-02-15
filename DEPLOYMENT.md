# Auto Trader 배포 가이드

이 문서는 `docker-compose.prod.yml` 기반 프로덕션 배포 절차를 설명합니다.

## 1. 사전 준비

- Docker / Docker Compose 설치
- `.env.prod` 작성
- GHCR 접근 권한 준비

```bash
cp env.prod.example .env.prod
```

필수 항목:
- `GITHUB_REPOSITORY`
- `DATABASE_URL`
- `REDIS_URL`
- API 키(KIS/Upbit/Google/OpenDART)
- `DOCS_ENABLED=false`

Sentry:
- `SENTRY_DSN`
- `SENTRY_ENVIRONMENT`
- `SENTRY_RELEASE`
- `SENTRY_TRACES_SAMPLE_RATE=1.0`
- `SENTRY_PROFILES_SAMPLE_RATE=1.0`
- `SENTRY_SEND_DEFAULT_PII=true`
- `SENTRY_ENABLE_LOG_EVENTS=true`

선택 항목 (HTTPS reverse proxy):
- `ACME_EMAIL`
- `DOMAIN_NAME`

## 2. 이미지 배포

```bash
# 이미지 pull
docker compose -f docker-compose.prod.yml pull

# 마이그레이션
docker compose -f docker-compose.prod.yml --profile migration up migration

# 서비스 실행
docker compose -f docker-compose.prod.yml up -d

# HTTPS reverse proxy가 필요하면 별도로 실행
docker compose -f docker-compose.monitoring-rpi.yml up -d caddy
```

## 3. 서비스 확인

```bash
docker compose -f docker-compose.prod.yml ps
curl http://localhost:8000/healthz
```

주요 서비스:
- `api`
- `worker`
- `mcp`
- `upbit_websocket`
- `kis_websocket`

## 4. 로그 확인

```bash
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml logs -f mcp
docker compose -f docker-compose.prod.yml logs -f upbit_websocket
docker compose -f docker-compose.prod.yml logs -f kis_websocket
```

## 5. Sentry 확인

- 단일 프로젝트에서 `service` 태그로 분리 조회:
  - `auto-trader-api`
  - `auto-trader-worker`
  - `auto-trader-mcp`
  - `auto-trader-upbit-ws`
  - `auto-trader-kis-ws`
- 에러/트랜잭션/프로파일 유입 확인

## 6. 업데이트 절차

```bash
git pull origin production
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

## 7. 롤백

```bash
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d
```

## 8. 문제 해결

### Sentry 이벤트 미수집

- `SENTRY_DSN` 값 확인
- 샘플링 설정(`SENTRY_TRACES_SAMPLE_RATE`, `SENTRY_PROFILES_SAMPLE_RATE`) 확인
- 로그 이벤트 정책(`SENTRY_ENABLE_LOG_EVENTS`) 확인

### 즉시 비활성화

- `SENTRY_DSN`을 비우고 재기동
