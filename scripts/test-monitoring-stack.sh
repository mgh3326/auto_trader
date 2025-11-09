#!/bin/bash
# Smoke test script for Grafana Observability Stack
# Tests that all services are running and accessible

set -e

COMPOSE_FILE="docker-compose.monitoring-rpi.yml"
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

echo "=================================================="
echo "Grafana Observability Stack - Smoke Test"
echo "=================================================="
echo ""

# Test 1: Check if all containers are running
echo "Test 1: Checking container status..."
EXPECTED_CONTAINERS=("tempo" "loki" "promtail" "prometheus" "grafana")
FAILED=0

for container in "${EXPECTED_CONTAINERS[@]}"; do
    if docker compose -f "$COMPOSE_FILE" ps | grep -q "$container.*Up"; then
        echo -e "${GREEN}✓${NC} $container is running"
    else
        echo -e "${RED}✗${NC} $container is NOT running"
        FAILED=1
    fi
done

if [ $FAILED -eq 1 ]; then
    echo -e "\n${RED}Some containers are not running. Exiting.${NC}"
    exit 1
fi

echo ""

# Test 2: Check Tempo health
echo "Test 2: Checking Tempo health..."
if curl -sf http://localhost:3200/ready > /dev/null; then
    echo -e "${GREEN}✓${NC} Tempo is ready"
else
    echo -e "${RED}✗${NC} Tempo is not ready"
    FAILED=1
fi

# Test 3: Check Loki health
echo "Test 3: Checking Loki health..."
if curl -sf http://localhost:3100/ready > /dev/null; then
    echo -e "${GREEN}✓${NC} Loki is ready"
else
    echo -e "${RED}✗${NC} Loki is not ready"
    FAILED=1
fi

# Test 4: Check Prometheus health
echo "Test 4: Checking Prometheus health..."
if curl -sf http://localhost:9090/-/healthy > /dev/null; then
    echo -e "${GREEN}✓${NC} Prometheus is healthy"
else
    echo -e "${RED}✗${NC} Prometheus is not healthy"
    FAILED=1
fi

# Test 5: Check Grafana health
echo "Test 5: Checking Grafana health..."
if curl -sf http://localhost:3000/api/health > /dev/null; then
    echo -e "${GREEN}✓${NC} Grafana is healthy"
else
    echo -e "${RED}✗${NC} Grafana is not healthy"
    FAILED=1
fi

# Test 6: Check OTLP endpoints
echo "Test 6: Checking OTLP endpoints..."
if nc -z localhost 4317 2>/dev/null; then
    echo -e "${GREEN}✓${NC} OTLP gRPC endpoint (4317) is open"
else
    echo -e "${YELLOW}⚠${NC} OTLP gRPC endpoint (4317) is not accessible (nc not available or port closed)"
fi

if nc -z localhost 4318 2>/dev/null; then
    echo -e "${GREEN}✓${NC} OTLP HTTP endpoint (4318) is open"
else
    echo -e "${YELLOW}⚠${NC} OTLP HTTP endpoint (4318) is not accessible (nc not available or port closed)"
fi

echo ""

# Test 7: Check Grafana datasources
echo "Test 7: Checking Grafana datasources..."
DATASOURCES=$(curl -sf http://localhost:3000/api/datasources 2>/dev/null)
if echo "$DATASOURCES" | grep -q "tempo"; then
    echo -e "${GREEN}✓${NC} Tempo datasource is configured"
else
    echo -e "${YELLOW}⚠${NC} Tempo datasource not found (may need time to provision)"
fi

if echo "$DATASOURCES" | grep -q "loki"; then
    echo -e "${GREEN}✓${NC} Loki datasource is configured"
else
    echo -e "${YELLOW}⚠${NC} Loki datasource not found (may need time to provision)"
fi

if echo "$DATASOURCES" | grep -q "prometheus"; then
    echo -e "${GREEN}✓${NC} Prometheus datasource is configured"
else
    echo -e "${YELLOW}⚠${NC} Prometheus datasource not found (may need time to provision)"
fi

echo ""
echo "=================================================="
if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}All critical tests passed!${NC}"
    echo ""
    echo "Next steps:"
    echo "1. Open Grafana: http://localhost:3000 (admin/admin)"
    echo "2. Go to Explore and verify data sources"
    echo "3. Enable telemetry in your app (.env):"
    echo "   OTEL_ENABLED=true"
    echo "   OTEL_EXPORTER_OTLP_ENDPOINT=localhost:4317"
    echo "   OTEL_INSECURE=true"
    exit 0
else
    echo -e "${RED}Some tests failed. Check the logs:${NC}"
    echo "  docker compose -f $COMPOSE_FILE logs"
    exit 1
fi
