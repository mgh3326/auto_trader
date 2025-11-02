#!/bin/bash

# Auto Trader Migration Script - Host-based execution

set -e

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

# 설정
ENV_FILE=".env.prod"

echo -e "${BLUE}🔄 Auto Trader Database Migration${NC}"
echo "=================================="
echo ""

# 환경 확인
if [ ! -f "$ENV_FILE" ]; then
    echo -e "${RED}❌ $ENV_FILE not found!${NC}"
    exit 1
fi

source $ENV_FILE

if [ -z "$DATABASE_URL" ]; then
    echo -e "${RED}❌ DATABASE_URL not set in $ENV_FILE${NC}"
    exit 1
fi

# PostgreSQL 클라이언트 확인
if ! command -v psql >/dev/null 2>&1; then
    echo -e "${RED}❌ psql command not found!${NC}"
    echo "Please install PostgreSQL client:"
    echo "  Ubuntu: sudo apt install postgresql-client"
    echo "  CentOS: sudo dnf install postgresql"
    exit 1
fi

# Python/Alembic 환경 확인
check_python_env() {
    if command -v uv >/dev/null 2>&1; then
        echo -e "${GREEN}✅ Using UV environment${NC}"
        PYTHON_CMD="uv run python"
        ALEMBIC_CMD="uv run alembic"
    elif [ -f "venv/bin/activate" ]; then
        echo -e "${GREEN}✅ Using virtual environment${NC}"
        source venv/bin/activate
        PYTHON_CMD="python"
        ALEMBIC_CMD="alembic"
    elif command -v python3 >/dev/null 2>&1; then
        echo -e "${YELLOW}⚠️  Using system Python (not recommended for production)${NC}"
        PYTHON_CMD="python3"
        ALEMBIC_CMD="alembic"
    else
        echo -e "${RED}❌ Python environment not found!${NC}"
        exit 1
    fi
}

# 데이터베이스 연결 테스트
test_db_connection() {
    echo -e "${YELLOW}🔍 Testing database connection...${NC}"
    
    # asyncpg URL을 psql용으로 변환
    PSQL_URL=$(echo "$DATABASE_URL" | sed 's/postgresql+asyncpg:/postgresql:/')
    
    if psql "$PSQL_URL" -c "SELECT version();" >/dev/null 2>&1; then
        echo -e "${GREEN}✅ Database connection successful${NC}"
    else
        echo -e "${RED}❌ Database connection failed${NC}"
        echo "Please check your DATABASE_URL and ensure PostgreSQL is running"
        exit 1
    fi
}

# 현재 마이그레이션 상태 확인
check_migration_status() {
    echo -e "${YELLOW}📊 Current migration status:${NC}"
    
    # Alembic 버전 확인
    current_version=$(psql "$PSQL_URL" -t -c "SELECT version_num FROM alembic_version ORDER BY version_num DESC LIMIT 1;" 2>/dev/null | tr -d ' ' || echo "")
    
    if [ -z "$current_version" ]; then
        echo "  📋 No alembic_version table found - initial setup needed"
        INITIAL_SETUP=true
    else
        echo "  📋 Current version: $current_version"
        INITIAL_SETUP=false
    fi
    
    # 대기 중인 마이그레이션 확인
    echo -e "${YELLOW}🔍 Checking for pending migrations...${NC}"
    
    if [ "$INITIAL_SETUP" = true ]; then
        echo "  📁 Initial setup required"
    else
        # Alembic으로 펜딩 마이그레이션 확인
        if $ALEMBIC_CMD current >/dev/null 2>&1; then
            pending_info=$($ALEMBIC_CMD heads | head -1 2>/dev/null || echo "")
            if [ -n "$pending_info" ]; then
                echo "  📁 Pending migrations detected"
            else
                echo "  ✅ Database is up to date"
            fi
        fi
    fi
}

# 마이그레이션 실행
run_migration() {
    echo -e "${YELLOW}🔄 Running database migration...${NC}"
    
    if [ "$INITIAL_SETUP" = true ]; then
        echo "  🏗️  Initial database setup..."
        $ALEMBIC_CMD upgrade head
    else
        echo "  📈 Upgrading to latest version..."
        $ALEMBIC_CMD upgrade head
    fi
    
    echo -e "${GREEN}✅ Migration completed successfully${NC}"
}

# 마이그레이션 후 검증
verify_migration() {
    echo -e "${YELLOW}🔍 Verifying migration...${NC}"
    
    # 새 버전 확인
    new_version=$(psql "$PSQL_URL" -t -c "SELECT version_num FROM alembic_version ORDER BY version_num DESC LIMIT 1;" 2>/dev/null | tr -d ' ')
    echo "  📋 New version: $new_version"
    
    # 테이블 개수 확인
    table_count=$(psql "$PSQL_URL" -t -c "SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public';" 2>/dev/null | tr -d ' ')
    echo "  📊 Tables count: $table_count"
    
    # 기본 연결 테스트
    if psql "$PSQL_URL" -c "SELECT 1;" >/dev/null 2>&1; then
        echo -e "${GREEN}✅ Migration verification successful${NC}"
    else
        echo -e "${RED}❌ Migration verification failed${NC}"
        exit 1
    fi
}

# 메인 실행
main() {
    echo -e "${BLUE}🚀 Starting migration process...${NC}"
    
    # 1. 환경 확인
    check_python_env
    
    # 2. DB 연결 테스트
    test_db_connection
    
    # 3. 현재 상태 확인
    check_migration_status
    
    # 4. 사용자 확인
    echo ""
    echo -e "${BLUE}📋 Migration Summary:${NC}"
    if [ "$INITIAL_SETUP" = true ]; then
        echo "  🏗️  Initial database setup will be performed"
    else
        echo "  📈 Database will be upgraded to latest version"
    fi
    echo ""
    
    read -p "Continue with migration? (y/N): " -n 1 -r
    echo ""
    
    if [[ ! $REPLY =~ ^[Yy]$ ]]; then
        echo "Migration cancelled."
        exit 0
    fi
    
    # 5. 마이그레이션 실행
    run_migration
    
    # 6. 검증
    verify_migration
    
    echo ""
    echo -e "${GREEN}🎉 Migration completed successfully!${NC}"
}

# 스크립트 실행
main "$@"

