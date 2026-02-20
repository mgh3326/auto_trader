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

STATE_FILE="tmp/deploy/zero-downtime-state.env"
ACTIVE_UPSTREAM_FILE="caddy/upstreams/api_active.caddy"
HEARTBEAT_FILE="tmp/deploy/healthcheck-heartbeat"
HEARTBEAT_MAX_AGE_SECONDS="${HEALTHCHECK_HEARTBEAT_MAX_AGE_SECONDS:-900}"
FAILED_CHECKS=0

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
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
        return 1
    fi
}

get_mtime_epoch() {
    local target_file="$1"

    if stat -c %Y "$target_file" >/dev/null 2>&1; then
        stat -c %Y "$target_file"
        return 0
    fi

    stat -f %m "$target_file"
}

detect_active_slot() {
    if [ -f "$STATE_FILE" ]; then
        local active_slot
        active_slot=$(grep -E '^ACTIVE_SLOT=' "$STATE_FILE" | tail -n 1 | cut -d= -f2)
        if [ "$active_slot" = "blue" ] || [ "$active_slot" = "green" ]; then
            echo "$active_slot"
            return 0
        fi
    fi

    if [ -f "$ACTIVE_UPSTREAM_FILE" ]; then
        if grep -Eq 'api_green\.caddy|18001' "$ACTIVE_UPSTREAM_FILE"; then
            echo "green"
            return 0
        fi
        if grep -Eq 'api_blue\.caddy|18000' "$ACTIVE_UPSTREAM_FILE"; then
            echo "blue"
            return 0
        fi
    fi

    echo "unknown"
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
check_service "upbit_websocket" "docker ps --filter 'name=auto_trader_upbit_ws_prod' --filter 'status=running' | grep -q auto_trader_upbit_ws_prod" "Upbit WebSocket Container"
check_service "kis_websocket" "docker ps --filter 'name=auto_trader_kis_ws_prod' --filter 'status=running' | grep -q auto_trader_kis_ws_prod" "KIS WebSocket Container"

echo -e "${BLUE}🔄 Zero-Downtime Slot Status${NC}"
active_slot=$(detect_active_slot)
echo "Active slot: $active_slot"

if [ "$active_slot" = "blue" ] || [ "$active_slot" = "green" ]; then
    inactive_slot="blue"
    if [ "$active_slot" = "blue" ]; then
        inactive_slot="green"
    fi

    active_api_container="auto_trader_api_${active_slot}_prod"
    inactive_api_container="auto_trader_api_${inactive_slot}_prod"
    active_mcp_container="auto_trader_mcp_${active_slot}_prod"
    inactive_mcp_container="auto_trader_mcp_${inactive_slot}_prod"

    check_service "active-api-slot" "docker ps --filter 'name=${active_api_container}' --filter 'status=running' | grep -q ${active_api_container}" "Active API Slot (${active_slot})"
    check_service "active-mcp-slot" "docker ps --filter 'name=${active_mcp_container}' --filter 'status=running' | grep -q ${active_mcp_container}" "Active MCP Slot (${active_slot})"

    if docker ps --filter "name=auto_trader_api_prod" --filter 'status=running' | grep -q "auto_trader_api_prod"; then
        echo -e "${YELLOW}ℹ️  Legacy API container is running (reference only): auto_trader_api_prod${NC}"
    else
        echo -e "${BLUE}ℹ️  Legacy API container is not running (reference only): auto_trader_api_prod${NC}"
    fi

    if docker ps --filter "name=auto_trader_mcp_prod" --filter 'status=running' | grep -q "auto_trader_mcp_prod"; then
        echo -e "${YELLOW}ℹ️  Legacy MCP container is running (reference only): auto_trader_mcp_prod${NC}"
    else
        echo -e "${BLUE}ℹ️  Legacy MCP container is not running (reference only): auto_trader_mcp_prod${NC}"
    fi

    if docker ps --filter "name=${inactive_api_container}" --filter 'status=running' | grep -q "$inactive_api_container"; then
        echo -e "${YELLOW}⚠️  Inactive API slot is running (${inactive_api_container})${NC}"
    else
        echo -e "${GREEN}✅ Inactive API slot is stopped (${inactive_api_container})${NC}"
    fi

    if docker ps --filter "name=${inactive_mcp_container}" --filter 'status=running' | grep -q "$inactive_mcp_container"; then
        echo -e "${YELLOW}⚠️  Inactive MCP slot is running (${inactive_mcp_container})${NC}"
    else
        echo -e "${GREEN}✅ Inactive MCP slot is stopped (${inactive_mcp_container})${NC}"
    fi
else
    echo -e "${YELLOW}⚠️  Active slot state not found. Using legacy single-slot checks for PASS/FAIL.${NC}"
    check_service "api" "docker ps --filter 'name=auto_trader_api_prod' --filter 'status=running' | grep -q auto_trader_api_prod" "API Container (legacy)"
fi

echo -e "${BLUE}🗓️ Scheduler Singleton${NC}"
scheduler_running_count=$(docker ps --filter 'name=auto_trader_scheduler_prod' --filter 'status=running' --format '{{.Names}}' | wc -l)
if [ "$scheduler_running_count" -eq 1 ]; then
    echo -e "${GREEN}✅ Scheduler single instance running${NC}"
elif [ "$scheduler_running_count" -eq 0 ]; then
    echo -e "${RED}❌ Scheduler is not running${NC}"
    FAILED_CHECKS=$((FAILED_CHECKS + 1))
else
    echo -e "${RED}❌ Multiple scheduler instances detected: ${scheduler_running_count}${NC}"
    FAILED_CHECKS=$((FAILED_CHECKS + 1))
fi

# API 엔드포인트 체크
echo -e "${BLUE}🌐 API Endpoints${NC}"
if [ "$active_slot" = "blue" ] || [ "$active_slot" = "green" ]; then
    check_service "active-api-health" "curl -f -s http://127.0.0.1:18080/healthz >/dev/null" "Active API Health Endpoint (18080)"
    check_service "active-api-ready" "curl -f -s http://127.0.0.1:18080/readyz >/dev/null" "Active API Ready Endpoint (18080)"

    active_mcp_http_code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18065/mcp || true)
    if [[ "$active_mcp_http_code" =~ ^[0-9]{3}$ ]] && [ "$active_mcp_http_code" -lt 500 ]; then
        echo -e "${GREEN}✅ Active MCP Endpoint (18065) reachable (HTTP ${active_mcp_http_code})${NC}"
    else
        echo -e "${RED}❌ Active MCP Endpoint (18065) unreachable (HTTP ${active_mcp_http_code})${NC}"
        FAILED_CHECKS=$((FAILED_CHECKS + 1))
    fi

    if curl -f -s http://localhost:8000/healthz >/dev/null 2>&1; then
        echo -e "${BLUE}ℹ️  Legacy API Health Endpoint (localhost:8000) reachable (reference only)${NC}"
    else
        echo -e "${YELLOW}ℹ️  Legacy API Health Endpoint (localhost:8000) unavailable (reference only)${NC}"
    fi
else
    check_service "api-health" "curl -f -s http://localhost:8000/healthz >/dev/null" "API Health Endpoint (legacy)"

    if curl -f -s http://127.0.0.1:18080/healthz >/dev/null 2>&1; then
        echo -e "${BLUE}ℹ️  Active API Health Endpoint (18080) reachable (reference only)${NC}"
    else
        echo -e "${YELLOW}ℹ️  Active API Health Endpoint (18080) unavailable (reference only)${NC}"
    fi

    if curl -f -s http://127.0.0.1:18080/readyz >/dev/null 2>&1; then
        echo -e "${BLUE}ℹ️  Active API Ready Endpoint (18080) reachable (reference only)${NC}"
    else
        echo -e "${YELLOW}ℹ️  Active API Ready Endpoint (18080) unavailable (reference only)${NC}"
    fi

    active_mcp_http_code=$(curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:18065/mcp || true)
    if [[ "$active_mcp_http_code" =~ ^[0-9]{3}$ ]] && [ "$active_mcp_http_code" -lt 500 ]; then
        echo -e "${BLUE}ℹ️  Active MCP Endpoint (18065) reachable (reference only, HTTP ${active_mcp_http_code})${NC}"
    else
        echo -e "${YELLOW}ℹ️  Active MCP Endpoint (18065) unavailable (reference only, HTTP ${active_mcp_http_code})${NC}"
    fi
fi

# 로그 체크 (최근 에러)
echo -e "${BLUE}📋 Recent Logs${NC}"
echo "🔍 Checking for recent errors..."

api_log_container="auto_trader_api_prod"
if [ "$active_slot" = "blue" ] || [ "$active_slot" = "green" ]; then
    slot_api_container="auto_trader_api_${active_slot}_prod"
    if docker inspect "$slot_api_container" >/dev/null 2>&1; then
        api_log_container="$slot_api_container"
    fi
fi

if docker inspect "$api_log_container" >/dev/null 2>&1; then
    api_errors=$(docker logs "$api_log_container" --since=10m 2>&1 | grep -i "error\|exception\|fail" | wc -l)
    if [ $api_errors -gt 0 ]; then
        echo -e "${YELLOW}⚠️  Found $api_errors error(s) in ${api_log_container} logs (last 10 minutes)${NC}"
    else
        echo -e "${GREEN}✅ No recent errors in ${api_log_container} logs${NC}"
    fi
else
    echo -e "${YELLOW}⚠️  API log target container not found: ${api_log_container}${NC}"
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

echo -e "${BLUE}⏱️ Cron Heartbeat${NC}"
mkdir -p "$(dirname "$HEARTBEAT_FILE")"

if [ -f "$HEARTBEAT_FILE" ]; then
    heartbeat_mtime=$(get_mtime_epoch "$HEARTBEAT_FILE")
    now_epoch=$(date +%s)
    heartbeat_age=$((now_epoch - heartbeat_mtime))

    if [ "$heartbeat_age" -gt "$HEARTBEAT_MAX_AGE_SECONDS" ]; then
        echo -e "${YELLOW}⚠️  Heartbeat delay detected: ${heartbeat_age}s (threshold ${HEARTBEAT_MAX_AGE_SECONDS}s)${NC}"
    else
        echo -e "${GREEN}✅ Heartbeat age is ${heartbeat_age}s${NC}"
    fi
else
    echo -e "${YELLOW}⚠️  Heartbeat file not found (first run or cron not configured)${NC}"
fi

touch "$HEARTBEAT_FILE"

echo ""
echo -e "${BLUE}📈 Quick Stats${NC}"
echo "🕐 Current Time: $(date)"
echo "⏰ Uptime: $(uptime -p)"
echo "🔄 Docker Images:"
docker images --filter "reference=ghcr.io/*/*auto*" --format "table {{.Repository}}\t{{.Tag}}\t{{.Size}}\t{{.CreatedSince}}"

echo ""
if [ "$FAILED_CHECKS" -gt 0 ]; then
    echo -e "${RED}❌ Health check failed (${FAILED_CHECKS} check(s) failed)${NC}"
    echo "For detailed logs: docker compose -f docker-compose.prod.yml logs"
    exit 1
fi

echo -e "${GREEN}🎉 Health check completed!${NC}"
echo "For detailed logs: docker compose -f docker-compose.prod.yml logs"
