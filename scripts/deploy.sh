#!/bin/bash

# Auto Trader Production Deployment Script
# 개선: down 없이 up -d로 순단 최소화, health check 기본 활성화,
#       마이그레이션은 컨테이너 내에서 실행 (DATABASE_URL 호스트 불필요)

set -e

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 설정
COMPOSE_FILE="docker-compose.prod.yml"
ENV_FILE=".env.prod"
HEALTH_URL="http://localhost:8000/healthz"
HEALTH_RETRIES=15
HEALTH_INTERVAL=3

# 도움말
show_help() {
    echo "Auto Trader Deployment Script"
    echo ""
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --auto-migrate     Run migrations automatically in Docker container"
    echo "  --skip-migrate     Skip migrations entirely"
    echo "  --backup           Create database backup before deployment"
    echo "  --rollback         Rollback to previous version"
    echo "  --no-health-check  Skip health check after deployment"
    echo "  --help             Show this help message"
    echo ""
    echo "Default behavior (no options):"
    echo "  - Pull latest images"
    echo "  - Skip migrations (use --auto-migrate to run)"
    echo "  - Deploy with zero-downtime (no docker compose down)"
    echo "  - Run health check"
    echo ""
    echo "Examples:"
    echo "  $0                              # Quick deploy (no migration)"
    echo "  $0 --auto-migrate --backup      # Safe deployment with migration + backup"
    echo "  $0 --auto-migrate               # Deploy with auto migration"
    echo "  $0 --skip-migrate               # Explicit skip migrations"
}

# 변수 초기화
AUTO_MIGRATE=false
SKIP_MIGRATE=true  # 기본값: 마이그레이션 스킵 (안전)
CREATE_BACKUP=false
ROLLBACK=false
HEALTH_CHECK=true  # 기본값: 항상 헬스체크

# 인수 파싱
while [[ $# -gt 0 ]]; do
    case $1 in
        --auto-migrate)
            AUTO_MIGRATE=true
            SKIP_MIGRATE=false
            shift
            ;;
        --skip-migrate)
            SKIP_MIGRATE=true
            shift
            ;;
        --backup)
            CREATE_BACKUP=true
            shift
            ;;
        --rollback)
            ROLLBACK=true
            shift
            ;;
        --no-health-check)
            HEALTH_CHECK=false
            shift
            ;;
        --help)
            show_help
            exit 0
            ;;
        *)
            echo "Unknown option: $1"
            show_help
            exit 1
            ;;
    esac
done

# 배포 시작 시간
DEPLOY_START=$(date +%s)

echo -e "${BLUE}🚀 Auto Trader Production Deployment${NC}"
echo "====================================="
echo ""

# 환경 확인
echo -e "${YELLOW}🔍 Environment Check${NC}"
if [ ! -f "$ENV_FILE" ]; then
    echo -e "${RED}❌ $ENV_FILE not found! Copy from env.prod.example and configure.${NC}"
    exit 1
fi

if [ ! -f "$COMPOSE_FILE" ]; then
    echo -e "${RED}❌ $COMPOSE_FILE not found!${NC}"
    exit 1
fi

source "$ENV_FILE"
if [ -z "$GITHUB_REPOSITORY" ]; then
    echo -e "${RED}❌ GITHUB_REPOSITORY not set in $ENV_FILE${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Environment check passed${NC}"

# 롤백 처리 (다른 단계 건너뜀)
if [ "$ROLLBACK" = true ]; then
    echo -e "${YELLOW}🔄 Rolling back services...${NC}"
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" down

    echo "Rollback requires manual image tag specification."
    echo "Example: docker tag ghcr.io/$GITHUB_REPOSITORY:previous ghcr.io/$GITHUB_REPOSITORY:latest"
    exit 0
fi

# 백업 생성
if [ "$CREATE_BACKUP" = true ]; then
    echo -e "${YELLOW}💾 Creating database backup...${NC}"
    backup_file="/var/backups/postgresql/auto_trader_$(date +%Y%m%d_%H%M%S).sql"
    sudo mkdir -p /var/backups/postgresql

    if command -v pg_dump >/dev/null 2>&1; then
        sudo -u postgres pg_dump auto_trader_prod > "$backup_file"
        echo -e "${GREEN}✅ Backup created: $backup_file${NC}"
    else
        echo -e "${YELLOW}⚠️  pg_dump not found, skipping backup${NC}"
    fi
fi

# 1. 최신 이미지 Pull (기존 서비스 유지 — 순단 없음)
echo -e "${YELLOW}📦 Pulling latest Docker images...${NC}"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" pull
echo -e "${GREEN}✅ Images pulled${NC}"

# 2. 마이그레이션 (컨테이너 안에서 실행)
if [ "$SKIP_MIGRATE" = true ]; then
    echo -e "${YELLOW}⏭️  Skipping migrations${NC}"
elif [ "$AUTO_MIGRATE" = true ]; then
    echo -e "${YELLOW}🔄 Running migrations (in Docker container)...${NC}"
    docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" --profile migration run --rm migration
    echo -e "${GREEN}✅ Migrations completed${NC}"
fi

# 3. 서비스 배포 (핵심: down 없이 up -d로 변경분만 재생성)
echo -e "${YELLOW}🚀 Deploying services...${NC}"
docker compose --env-file "$ENV_FILE" -f "$COMPOSE_FILE" up -d --remove-orphans
echo -e "${GREEN}✅ Services updated${NC}"

# 4. 헬스체크
if [ "$HEALTH_CHECK" = true ]; then
    echo -e "${YELLOW}🏥 Running health check...${NC}"
    for i in $(seq 1 $HEALTH_RETRIES); do
        if curl -sf "$HEALTH_URL" > /dev/null 2>&1; then
            DEPLOY_END=$(date +%s)
            DEPLOY_DURATION=$((DEPLOY_END - DEPLOY_START))
            echo -e "${GREEN}✅ API health check passed!${NC}"
            echo ""
            echo -e "${GREEN}🎉 Deployment completed in ${DEPLOY_DURATION}s${NC}"
            echo ""
            echo "📋 Useful commands:"
            echo "  docker compose --env-file $ENV_FILE -f $COMPOSE_FILE logs -f    # View logs"
            echo "  docker compose --env-file $ENV_FILE -f $COMPOSE_FILE ps         # Check status"
            echo ""

            # 오래된 이미지 정리
            docker image prune -f > /dev/null 2>&1 || true

            exit 0
        fi
        echo -e "  ⏳ Waiting for service... ($i/$HEALTH_RETRIES)"
        sleep $HEALTH_INTERVAL
    done

    echo -e "${RED}🔴 Health check failed after $((HEALTH_RETRIES * HEALTH_INTERVAL))s!${NC}"
    echo ""
    echo "📋 Debug commands:"
    echo "  curl -f $HEALTH_URL"
    echo "  docker compose --env-file $ENV_FILE -f $COMPOSE_FILE logs --tail=50 api"
    echo "  docker compose --env-file $ENV_FILE -f $COMPOSE_FILE logs --tail=50"
    echo "  docker compose --env-file $ENV_FILE -f $COMPOSE_FILE ps"
    exit 1
else
    DEPLOY_END=$(date +%s)
    DEPLOY_DURATION=$((DEPLOY_END - DEPLOY_START))
    echo ""
    echo -e "${GREEN}🎉 Deployment completed in ${DEPLOY_DURATION}s (health check skipped)${NC}"
fi
