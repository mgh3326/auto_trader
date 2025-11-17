#!/bin/bash
# Integration test for Grafana Observability Stack
# Tests OTEL traces/metrics flow and datasource connectivity

set -e

echo "=========================================="
echo "Grafana Observability Stack Integration Test"
echo "=========================================="
echo ""

# Colors for output
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Test counters
PASSED=0
FAILED=0

# Helper functions
pass() {
    echo -e "${GREEN}✓ PASS${NC}: $1"
    ((PASSED++))
}

fail() {
    echo -e "${RED}✗ FAIL${NC}: $1"
    ((FAILED++))
}

warn() {
    echo -e "${YELLOW}⚠ WARN${NC}: $1"
}

# 1. Service Health Checks
echo "1. Testing service health endpoints..."
echo "----------------------------------------"

# Tempo
if curl -sf http://localhost:3200/status > /dev/null 2>&1; then
    pass "Tempo /status endpoint responding"
else
    fail "Tempo /status endpoint not responding"
fi

# Loki
if curl -sf http://localhost:3100/ready > /dev/null 2>&1; then
    pass "Loki /ready endpoint responding"
else
    fail "Loki /ready endpoint not responding"
fi

# Prometheus
if curl -sf http://localhost:9090/-/healthy > /dev/null 2>&1; then
    pass "Prometheus /-/healthy endpoint responding"
else
    fail "Prometheus /-/healthy endpoint not responding"
fi

# Grafana
if curl -sf http://localhost:3000/api/health > /dev/null 2>&1; then
    pass "Grafana /api/health endpoint responding"
else
    fail "Grafana /api/health endpoint not responding"
fi

# OTEL Collector
if curl -sf http://localhost:13133/ > /dev/null 2>&1; then
    pass "OTEL Collector health endpoint responding"
else
    fail "OTEL Collector health endpoint not responding"
fi

# Promtail
if curl -sf http://localhost:9080/ready > /dev/null 2>&1; then
    pass "Promtail /ready endpoint responding"
else
    warn "Promtail /ready endpoint not responding (may take time to start)"
fi

echo ""

# 2. Grafana Datasource Connectivity
echo "2. Testing Grafana datasources..."
echo "----------------------------------------"

DATASOURCES=$(curl -sf http://admin:admin@localhost:3000/api/datasources 2>/dev/null)

if echo "$DATASOURCES" | grep -q "Tempo"; then
    pass "Tempo datasource provisioned in Grafana"
else
    fail "Tempo datasource not found in Grafana"
fi

if echo "$DATASOURCES" | grep -q "Loki"; then
    pass "Loki datasource provisioned in Grafana"
else
    fail "Loki datasource not found in Grafana"
fi

if echo "$DATASOURCES" | grep -q "Prometheus"; then
    pass "Prometheus datasource provisioned in Grafana"
else
    fail "Prometheus datasource not found in Grafana"
fi

echo ""

# 3. OTLP Endpoints Availability
echo "3. Testing OTLP endpoints..."
echo "----------------------------------------"

if nc -zv localhost 4317 2>&1 | grep -q "succeeded"; then
    pass "OTLP gRPC port 4317 is open"
else
    fail "OTLP gRPC port 4317 is not accessible"
fi

if nc -zv localhost 4318 2>&1 | grep -q "succeeded"; then
    pass "OTLP HTTP port 4318 is open"
else
    fail "OTLP HTTP port 4318 is not accessible"
fi

echo ""

# 4. Send Test Trace via OTLP HTTP
echo "4. Testing OTLP trace ingestion..."
echo "----------------------------------------"

TRACE_JSON='{
  "resourceSpans": [{
    "resource": {
      "attributes": [{
        "key": "service.name",
        "value": {"stringValue": "integration-test"}
      }]
    },
    "scopeSpans": [{
      "spans": [{
        "traceId": "5B8EFFF798038103D269B633813FC60C",
        "spanId": "EEE19B7EC3C1B174",
        "name": "test-span",
        "kind": 1,
        "startTimeUnixNano": "1544712660000000000",
        "endTimeUnixNano": "1544712661000000000",
        "attributes": [{
          "key": "test.type",
          "value": {"stringValue": "integration"}
        }]
      }]
    }]
  }]
}'

if curl -sf -X POST http://localhost:4318/v1/traces \
    -H "Content-Type: application/json" \
    -d "$TRACE_JSON" > /dev/null 2>&1; then
    pass "Test trace sent successfully via OTLP HTTP"
    sleep 2  # Wait for processing
else
    fail "Failed to send test trace via OTLP HTTP"
fi

echo ""

# 5. Verify Metrics Collection
echo "5. Testing Prometheus metrics..."
echo "----------------------------------------"

# Check if Prometheus is scraping targets
TARGETS=$(curl -sf http://localhost:9090/api/v1/targets 2>/dev/null)

if echo "$TARGETS" | grep -q "tempo"; then
    pass "Prometheus scraping Tempo metrics"
else
    warn "Prometheus not scraping Tempo metrics (may take time)"
fi

if echo "$TARGETS" | grep -q "loki"; then
    pass "Prometheus scraping Loki metrics"
else
    warn "Prometheus not scraping Loki metrics (may take time)"
fi

if echo "$TARGETS" | grep -q "otel-collector"; then
    pass "Prometheus scraping OTEL Collector metrics"
else
    warn "Prometheus not scraping OTEL Collector metrics (may take time)"
fi

echo ""

# 6. Query Test Results
echo "6. Querying stored data..."
echo "----------------------------------------"

# Query Tempo for traces (via Grafana API)
TEMPO_QUERY=$(curl -sf "http://admin:admin@localhost:3000/api/datasources/proxy/uid/tempo/api/search?tags=" 2>/dev/null)

if [ -n "$TEMPO_QUERY" ]; then
    pass "Tempo query API responding"
else
    warn "Tempo query API not responding (traces may not be indexed yet)"
fi

# Query Prometheus metrics
PROM_QUERY=$(curl -sf "http://localhost:9090/api/v1/query?query=up" 2>/dev/null)

if echo "$PROM_QUERY" | grep -q "success"; then
    pass "Prometheus query API responding"
else
    fail "Prometheus query API not responding"
fi

# Query Loki logs
LOKI_QUERY=$(curl -sf "http://localhost:3100/loki/api/v1/query?query={job=\"dockerlogs\"}" 2>/dev/null)

if echo "$LOKI_QUERY" | grep -q "success"; then
    pass "Loki query API responding"
else
    warn "Loki query API not responding (logs may not be collected yet)"
fi

echo ""

# 7. Docker Container Status
echo "7. Checking Docker container status..."
echo "----------------------------------------"

CONTAINERS="tempo loki prometheus grafana otel-collector promtail"

for container in $CONTAINERS; do
    STATUS=$(docker inspect -f '{{.State.Status}}' $container 2>/dev/null)
    HEALTH=$(docker inspect -f '{{.State.Health.Status}}' $container 2>/dev/null)

    if [ "$STATUS" = "running" ]; then
        if [ "$HEALTH" = "healthy" ] || [ "$HEALTH" = "<no value>" ]; then
            pass "$container is running and healthy"
        else
            fail "$container is running but unhealthy: $HEALTH"
        fi
    else
        fail "$container is not running: $STATUS"
    fi
done

echo ""

# Summary
echo "=========================================="
echo "Test Summary"
echo "=========================================="
echo -e "Passed: ${GREEN}$PASSED${NC}"
echo -e "Failed: ${RED}$FAILED${NC}"
echo ""

if [ $FAILED -eq 0 ]; then
    echo -e "${GREEN}✓ All tests passed!${NC}"
    exit 0
else
    echo -e "${RED}✗ Some tests failed. Please check the output above.${NC}"
    exit 1
fi
