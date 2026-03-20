#!/bin/bash

# Auto Trader Production Health Check Script

echo "🏥 Auto Trader Health Check"
echo "=========================="

# 색상 정의
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 체크 함수
check_service() {
    local service=$1
    local command=$2
    local description=$3
    
    echo -n "🔍 $description... "
    
    if eval $command >/dev/null 2>&1; then
        echo -e "${GREEN}✅ OK${NC}"
        return 0
    else
        echo -e "${RED}❌ FAILED${NC}"
        return 1
    fi
}

# 시스템 리소스 체크
echo -e "${BLUE}📊 System Resources${NC}"
echo "Memory Usage: $(free -h | awk '/^Mem:/ {print $3 "/" $2}')"
echo "Disk Usage: $(df -h / | awk 'NR==2 {print $3 "/" $2 " (" $5 ")"}')"
echo "Load Average: $(uptime | awk '{print $NF}')"
echo ""

# PostgreSQL 체크
echo -e "${BLUE}🐘 PostgreSQL${NC}"
check_service "postgresql" "sudo systemctl is-active postgresql" "PostgreSQL Service"
check_service "postgresql" "sudo -u postgres psql -c 'SELECT 1;'" "PostgreSQL Connection"

# Redis 체크
echo -e "${BLUE}🔴 Redis${NC}"
check_service "redis" "sudo systemctl is-active redis" "Redis Service"
check_service "redis" "redis-cli ping" "Redis Connection"

# Docker 컨테이너 체크
echo -e "${BLUE}🐳 Docker Containers${NC}"
check_service "api" "docker ps --filter 'name=auto_trader_api_prod' --filter 'status=running' | grep -q auto_trader_api_prod" "API Container"
check_service "upbit_websocket" "docker ps --filter 'name=auto_trader_upbit_ws_prod' --filter 'status=running' | grep -q auto_trader_upbit_ws_prod" "Upbit WebSocket Container"
check_service "kis_websocket" "docker ps --filter 'name=auto_trader_kis_ws_prod' --filter 'status=running' | grep -q auto_trader_kis_ws_prod" "KIS WebSocket Container"
check_service "n8n" "docker ps --filter 'name=auto_trader_n8n_prod' --filter 'status=running' | grep -q auto_trader_n8n_prod" "n8n Container"

# API 엔드포인트 체크
echo -e "${BLUE}🌐 API Endpoints${NC}"
check_service "api-health" "curl -f -s http://localhost:8000/healthz >/dev/null" "API Health Endpoint"
check_service "n8n-health" "wget -q --spider http://127.0.0.1:5678/healthz 2>/dev/null || curl -f -s http://127.0.0.1:5678/healthz >/dev/null" "n8n Health Endpoint"

# 로그 체크 (최근 에러)
echo -e "${BLUE}📋 Recent Logs${NC}"
echo "🔍 Checking for recent errors..."

# API 컨테이너 로그에서 에러 검색
api_errors=$(docker logs auto_trader_api_prod --since=10m 2>&1 | grep -i "error\|exception\|fail" | wc -l)
if [ $api_errors -gt 0 ]; then
    echo -e "${YELLOW}⚠️  Found $api_errors error(s) in API logs (last 10 minutes)${NC}"
else
    echo -e "${GREEN}✅ No recent errors in API logs${NC}"
fi

# Upbit WebSocket 컨테이너 로그에서 에러 검색
upbit_ws_errors=$(docker logs auto_trader_upbit_ws_prod --since=10m 2>&1 | grep -i "error\|exception\|fail" | wc -l)
if [ $upbit_ws_errors -gt 0 ]; then
    echo -e "${YELLOW}⚠️  Found $upbit_ws_errors error(s) in Upbit WebSocket logs (last 10 minutes)${NC}"
else
    echo -e "${GREEN}✅ No recent errors in Upbit WebSocket logs${NC}"
fi

# KIS WebSocket 컨테이너 로그에서 에러 검색
kis_ws_errors=$(docker logs auto_trader_kis_ws_prod --since=10m 2>&1 | grep -i "error\|exception\|fail" | wc -l)
if [ $kis_ws_errors -gt 0 ]; then
    echo -e "${YELLOW}⚠️  Found $kis_ws_errors error(s) in KIS WebSocket logs (last 10 minutes)${NC}"
else
    echo -e "${GREEN}✅ No recent errors in KIS WebSocket logs${NC}"
fi

echo -e "${BLUE}🔌 WebSocket Services Status${NC}"
upbit_health_line=$(docker logs auto_trader_upbit_ws_prod --since=10m 2>&1 | tail -n 1)
kis_health_line=$(docker logs auto_trader_kis_ws_prod --since=10m 2>&1 | tail -n 1)

if [ -n "$upbit_health_line" ]; then
    echo -e "${GREEN}✅ Upbit status log found${NC}: $upbit_health_line"
else
    echo -e "${YELLOW}⚠️  No recent Upbit status log in upbit_websocket container${NC}"
fi

if [ -n "$kis_health_line" ]; then
    echo -e "${GREEN}✅ KIS status log found${NC}: $kis_health_line"
else
    echo -e "${YELLOW}⚠️  No recent KIS status log in kis_websocket container${NC}"
fi

# 디스크 용량 체크
echo -e "${BLUE}💾 Storage Check${NC}"
disk_usage=$(df / | awk 'NR==2 {print $5}' | sed 's/%//')
if [ $disk_usage -gt 90 ]; then
    echo -e "${RED}❌ Disk usage is ${disk_usage}% (Critical)${NC}"
elif [ $disk_usage -gt 80 ]; then
    echo -e "${YELLOW}⚠️  Disk usage is ${disk_usage}% (Warning)${NC}"
else
    echo -e "${GREEN}✅ Disk usage is ${disk_usage}% (OK)${NC}"
fi

# 메모리 사용량 체크
echo -e "${BLUE}🧠 Memory Check${NC}"
memory_usage=$(free | awk 'NR==2{printf "%.0f", $3*100/$2}')
if [ $memory_usage -gt 90 ]; then
    echo -e "${RED}❌ Memory usage is ${memory_usage}% (Critical)${NC}"
elif [ $memory_usage -gt 80 ]; then
    echo -e "${YELLOW}⚠️  Memory usage is ${memory_usage}% (Warning)${NC}"
else
    echo -e "${GREEN}✅ Memory usage is ${memory_usage}% (OK)${NC}"
fi

echo ""
echo -e "${BLUE}📈 Quick Stats${NC}"
echo "🕐 Current Time: $(date)"
echo "⏰ Uptime: $(uptime -p)"
echo "🔄 Docker Images:"
docker images --filter "reference=ghcr.io/*/*auto*" --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}"

echo ""
echo -e "${GREEN}🎉 Health check completed!${NC}"
echo "For detailed logs: docker compose -f docker-compose.prod.yml logs"
