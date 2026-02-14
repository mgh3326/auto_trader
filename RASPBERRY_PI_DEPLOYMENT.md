# Raspberry Pi 배포 가이드

라즈베리파이(권장: Pi 5, 8GB)에서 Auto Trader를 `docker-compose.prod.yml`로 배포하는 절차입니다.

## 빠른 시작

```bash
git clone <repo> ~/auto_trader
cd ~/auto_trader
cp env.prod.example .env.prod
nano .env.prod

# 마이그레이션
docker compose -f docker-compose.prod.yml --profile migration up migration

# 서비스 실행
docker compose -f docker-compose.prod.yml up -d

# HTTPS reverse proxy가 필요하면
docker compose -f docker-compose.monitoring-rpi.yml up -d caddy
```

## 1. 시스템 준비

```bash
sudo apt update && sudo apt upgrade -y
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
newgrp docker
sudo apt install -y docker-compose-plugin
```

## 2. .env.prod 필수 값

- `DATABASE_URL` (네이티브 PostgreSQL)
- `REDIS_URL` (네이티브 Redis)
- `API_PORT`
- `GITHUB_REPOSITORY`
- API 키들
- `DOCS_ENABLED=false`

Sentry:

```bash
SENTRY_DSN=https://<key>@o0.ingest.sentry.io/0
SENTRY_ENVIRONMENT=production
SENTRY_RELEASE=
SENTRY_TRACES_SAMPLE_RATE=1.0
SENTRY_PROFILES_SAMPLE_RATE=1.0
SENTRY_SEND_DEFAULT_PII=true
SENTRY_ENABLE_LOG_EVENTS=true
```

선택 (Caddy):

```bash
ACME_EMAIL=your_email@example.com
DOMAIN_NAME=your_domain.com
```

## 3. 실행 및 확인

```bash
docker compose -f docker-compose.prod.yml pull
docker compose -f docker-compose.prod.yml up -d

docker compose -f docker-compose.prod.yml ps
curl http://localhost:8000/healthz
```

로그 확인:

```bash
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml logs -f mcp
docker compose -f docker-compose.prod.yml logs -f websocket
docker compose -f docker-compose.prod.yml logs -f kis_websocket

# Caddy 로그
docker compose -f docker-compose.monitoring-rpi.yml logs -f caddy
```

## 4. Sentry 확인

- 단일 프로젝트에서 `service` 태그 조회
- api/worker/mcp/ws 5개 서비스 이벤트 분리 확인
- Errors + Transactions + Profiles 유입 확인

## 5. 운영 명령어

```bash
# 재시작
docker compose -f docker-compose.prod.yml restart

# 중지
docker compose -f docker-compose.prod.yml stop

# 중지 및 제거
docker compose -f docker-compose.prod.yml down
```

## 6. 성능 점검

```bash
docker stats
free -h
df -h
```
