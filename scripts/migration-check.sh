#!/bin/bash

# Auto Trader Migration Safety Check Script

set -e

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

echo -e "${BLUE}🔍 Migration Safety Check${NC}"
echo "========================="

# 환경변수 확인
if [ -z "$DATABASE_URL" ]; then
    echo -e "${RED}❌ DATABASE_URL not set${NC}"
    exit 1
fi

# 현재 DB 상태 확인
echo -e "${YELLOW}📊 Current Database Status:${NC}"

# PostgreSQL 연결 테스트
if command -v psql >/dev/null 2>&1; then
    # 환경변수에서 연결 정보 파싱
    if [ -n "$DATABASE_URL" ]; then
        # DATABASE_URL 파싱 (postgresql://user:password@host:port/dbname)
        DB_INFO=$(echo $DATABASE_URL | sed 's/postgresql+asyncpg:/postgresql:/' | sed 's/postgresql://')
        
        echo "🔍 Testing database connection..."
        if psql "$DATABASE_URL" -c "SELECT version();" >/dev/null 2>&1; then
            echo "✅ Database connection successful"
            
            # Alembic 버전 확인
            echo "📋 Current Alembic version:"
            psql "$DATABASE_URL" -t -c "SELECT version_num FROM alembic_version ORDER BY version_num DESC LIMIT 1;" 2>/dev/null || echo "  No alembic_version table found"
            
            # 테이블 개수 확인
            table_count=$(psql "$DATABASE_URL" -t -c "SELECT COUNT(*) FROM pg_tables WHERE schemaname = 'public';" 2>/dev/null || echo "0")
            echo "📊 Number of tables: $(echo $table_count | tr -d ' ')"
        else
            echo -e "${RED}❌ Database connection failed${NC}"
            echo "Please check your DATABASE_URL in .env.prod"
            exit 1
        fi
    else
        echo -e "${RED}❌ DATABASE_URL not set${NC}"
        exit 1
    fi
else
    echo -e "${YELLOW}⚠️  psql command not found. Install postgresql-client to run DB checks.${NC}"
    echo "On Ubuntu: sudo apt install postgresql-client"
    echo "On CentOS: sudo dnf install postgresql"
fi

# 마이그레이션 파일 확인
echo -e "${YELLOW}📁 Migration Files:${NC}"
if [ -d "alembic/versions" ]; then
    migration_count=$(ls -1 alembic/versions/*.py 2>/dev/null | wc -l || echo 0)
    echo "Number of migration files: $migration_count"
    
    if [ $migration_count -gt 0 ]; then
        echo "Latest migration files:"
        ls -lt alembic/versions/*.py | head -3 | awk '{print "  " $9 " (" $6 " " $7 " " $8 ")"}'
    fi
else
    echo -e "${RED}❌ alembic/versions directory not found${NC}"
    exit 1
fi

# 마이그레이션 위험도 평가
echo -e "${YELLOW}⚠️  Migration Risk Assessment:${NC}"

# 위험한 키워드 검색
risk_keywords=("DROP TABLE" "DROP COLUMN" "ALTER TABLE.*DROP" "TRUNCATE" "DELETE FROM")
high_risk=false

for file in alembic/versions/*.py; do
    if [ -f "$file" ]; then
        for keyword in "${risk_keywords[@]}"; do
            if grep -qi "$keyword" "$file"; then
                echo -e "${RED}🚨 HIGH RISK: Found '$keyword' in $(basename $file)${NC}"
                high_risk=true
            fi
        done
    fi
done

if [ "$high_risk" = false ]; then
    echo -e "${GREEN}✅ No high-risk operations detected${NC}"
fi

# 백업 권장사항
echo -e "${YELLOW}💾 Backup Recommendations:${NC}"
if [ "$high_risk" = true ]; then
    echo -e "${RED}🚨 HIGH RISK MIGRATIONS DETECTED!${NC}"
    echo "STRONGLY RECOMMENDED:"
    echo "  1. Create full database backup before migration"
    echo "  2. Test migration on staging environment"
    echo "  3. Plan rollback strategy"
    echo "  4. Consider manual migration execution"
else
    echo -e "${GREEN}✅ Low risk migrations - automated execution should be safe${NC}"
fi

echo ""
echo -e "${BLUE}🔧 Migration Options:${NC}"
echo "1. Safe Auto Migration (recommended for low risk)"
echo "2. Manual Migration (recommended for high risk)"
echo "3. Staged Migration (test first, then apply)"
