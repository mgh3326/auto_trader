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
- `websocket`
- `kis_websocket`

## 4. 로그 확인

```bash
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml logs -f mcp
docker compose -f docker-compose.prod.yml logs -f websocket
docker compose -f docker-compose.prod.yml logs -f kis_websocket
```

## 5. 업데이트 절차

```bash
git pull origin production
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d
```

## 6. 롤백

```bash
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d
```
