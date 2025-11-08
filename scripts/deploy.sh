#!/bin/bash

# Auto Trader Production Deployment Script

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

# 도움말
show_help() {
    echo "Auto Trader Deployment Script"
    echo ""
    echo "Usage: $0 [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --auto-migrate     Run migrations automatically (use with caution)"
    echo "  --manual-migrate   Run migration check and manual steps"
    echo "  --skip-migrate     Skip migrations entirely"
    echo "  --backup           Create database backup before deployment"
    echo "  --rollback         Rollback to previous version"
    echo "  --health-check     Run health check after deployment"
    echo "  --help             Show this help message"
    echo ""
    echo "Examples:"
    echo "  $0 --manual-migrate --backup    # Safe deployment with backup"
    echo "  $0 --auto-migrate --health-check # Quick deployment with auto migration"
    echo "  $0 --skip-migrate                # Deploy without touching database"
}

# 변수 초기화
AUTO_MIGRATE=false
MANUAL_MIGRATE=false
SKIP_MIGRATE=false
CREATE_BACKUP=false
ROLLBACK=false
HEALTH_CHECK=false

# 인수 파싱
while [[ $# -gt 0 ]]; do
    case $1 in
        --auto-migrate)
            AUTO_MIGRATE=true
            shift
            ;;
        --manual-migrate)
            MANUAL_MIGRATE=true
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
        --health-check)
            HEALTH_CHECK=true
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

# 기본값 설정 (아무 옵션이 없으면 수동 마이그레이션)
if [ "$AUTO_MIGRATE" = false ] && [ "$MANUAL_MIGRATE" = false ] && [ "$SKIP_MIGRATE" = false ]; then
    MANUAL_MIGRATE=true
fi

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

source $ENV_FILE
if [ -z "$GITHUB_REPOSITORY" ]; then
    echo -e "${RED}❌ GITHUB_REPOSITORY not set in $ENV_FILE${NC}"
    exit 1
fi

echo -e "${GREEN}✅ Environment check passed${NC}"

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

# 최신 이미지 가져오기
echo -e "${YELLOW}📦 Pulling latest Docker images...${NC}"
docker compose -f $COMPOSE_FILE pull

# 마이그레이션 처리
if [ "$SKIP_MIGRATE" = true ]; then
    echo -e "${YELLOW}⏭️  Skipping migrations${NC}"
elif [ "$AUTO_MIGRATE" = true ]; then
    echo -e "${YELLOW}🔄 Running automatic migrations...${NC}"
    
    # 마이그레이션 위험도 체크
    if [ -f "scripts/migration-check.sh" ]; then
        echo -e "${BLUE}🔍 Running migration safety check...${NC}"
        source $ENV_FILE
        ./scripts/migration-check.sh || true
        
        echo ""
        echo -e "${RED}⚠️  CAUTION: Auto-migration enabled!${NC}"
        echo "This will automatically apply database changes."
        echo -n "Continue? (y/N): "
        read -r response
        if [[ ! "$response" =~ ^[Yy]$ ]]; then
            echo "Deployment cancelled."
            exit 1
        fi
    fi
    
    # 호스트에서 마이그레이션 실행
    if [ -f "scripts/migrate.sh" ]; then
        echo -e "${BLUE}🔄 Running host-based migration...${NC}"
        ./scripts/migrate.sh
    else
        echo -e "${YELLOW}⚠️  Host migration script not found, using Docker...${NC}"
        docker compose -f $COMPOSE_FILE --profile migration up migration
    fi
    
elif [ "$MANUAL_MIGRATE" = true ]; then
    echo -e "${YELLOW}👨‍💻 Manual migration mode${NC}"
    
    # 마이그레이션 체크 실행
    if [ -f "scripts/migration-check.sh" ]; then
        source $ENV_FILE
        ./scripts/migration-check.sh
    fi
    
    echo ""
    echo -e "${BLUE}🔧 Migration Options:${NC}"
    echo "1. Run host-based migration (./scripts/migrate.sh)"
    echo "2. Run Docker-based migration"
    echo "3. Skip migrations for now"
    echo "4. Cancel deployment"
    echo ""
    echo -n "Choose option (1-4): "
    read -r choice
    
    case $choice in
        1)
            echo -e "${YELLOW}🔄 Running host-based migration...${NC}"
            if [ -f "scripts/migrate.sh" ]; then
                ./scripts/migrate.sh
            else
                echo -e "${RED}❌ scripts/migrate.sh not found${NC}"
                exit 1
            fi
            ;;
        2)
            echo -e "${YELLOW}🔄 Running Docker-based migration...${NC}"
            docker compose -f $COMPOSE_FILE --profile migration up migration
            ;;
        3)
            echo -e "${YELLOW}⏭️  Skipping migrations${NC}"
            ;;
        4)
            echo "Deployment cancelled."
            exit 1
            ;;
        *)
            echo -e "${RED}❌ Invalid choice. Deployment cancelled.${NC}"
            exit 1
            ;;
    esac
fi

# 롤백 처리
if [ "$ROLLBACK" = true ]; then
    echo -e "${YELLOW}🔄 Rolling back services...${NC}"
    docker compose -f $COMPOSE_FILE down
    
    # 이전 이미지로 롤백 (수동으로 태그 지정 필요)
    echo "Rollback requires manual image tag specification."
    echo "Example: docker tag ghcr.io/$GITHUB_REPOSITORY:previous ghcr.io/$GITHUB_REPOSITORY:latest"
    exit 0
fi

# 서비스 배포
echo -e "${YELLOW}🚀 Deploying services...${NC}"

# 기존 서비스 중지
docker compose -f $COMPOSE_FILE down

# 새 서비스 시작
docker compose -f $COMPOSE_FILE up -d

echo -e "${GREEN}✅ Services deployed successfully${NC}"

# 헬스체크
if [ "$HEALTH_CHECK" = true ]; then
    echo -e "${YELLOW}🏥 Running health check...${NC}"
    sleep 10  # 서비스 시작 대기
    
    if [ -f "scripts/healthcheck.sh" ]; then
        ./scripts/healthcheck.sh
    else
        # 간단한 헬스체크
        echo "🔍 Checking API health..."
        if curl -f -s http://localhost:8000/healthz >/dev/null; then
            echo -e "${GREEN}✅ API is healthy${NC}"
        else
            echo -e "${RED}❌ API health check failed${NC}"
        fi
    fi
fi

echo ""
echo -e "${GREEN}🎉 Deployment completed successfully!${NC}"
echo ""
echo "📋 Useful commands:"
echo "  docker compose -f $COMPOSE_FILE logs -f    # View logs"
echo "  docker compose -f $COMPOSE_FILE ps         # Check status"
echo "  ./scripts/healthcheck.sh                   # Run health check"
echo ""
echo "🌐 API URL: http://localhost:8000"
echo "🏥 Health Check: http://localhost:8000/healthz"
