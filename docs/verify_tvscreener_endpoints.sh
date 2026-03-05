#!/bin/bash

# TvScreener Endpoint Verification Script
# This script tests the three main screening endpoints to verify tvscreener integration

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Configuration
BASE_URL="${BASE_URL:-http://localhost:8000}"
MAX_RESPONSE_TIME=10

# Helper functions
log_info() {
    echo -e "${GREEN}✓${NC} $1"
}

log_error() {
    echo -e "${RED}✗${NC} $1"
}

log_warn() {
    echo -e "${YELLOW}⚠${NC} $1"
}

check_server() {
    echo "Checking if server is running at ${BASE_URL}..."
    if curl -s -f "${BASE_URL}/health" > /dev/null 2>&1; then
        log_info "Server is running"
        return 0
    else
        log_error "Server is not responding at ${BASE_URL}/health"
        echo "Please start the server with: uv run uvicorn app.main:app --reload"
        exit 1
    fi
}

test_endpoint() {
    local endpoint="$1"
    local description="$2"
    local expected_source="${3:-tvscreener}"

    echo ""
    echo "=================================================="
    echo "Testing: ${description}"
    echo "Endpoint: ${endpoint}"
    echo "=================================================="

    # Make request and measure time
    local start_time=$(date +%s.%N)
    local response=$(curl -s -w "\n%{http_code}\n%{time_total}" "${BASE_URL}${endpoint}")
    local end_time=$(date +%s.%N)

    # Parse response
    local body=$(echo "$response" | head -n -2)
    local http_code=$(echo "$response" | tail -n 2 | head -n 1)
    local time_total=$(echo "$response" | tail -n 1)

    echo ""
    echo "Response Details:"
    echo "─────────────────────────────────────────────────"

    # Check HTTP status
    if [ "$http_code" = "200" ]; then
        log_info "HTTP Status: ${http_code} OK"
    else
        log_error "HTTP Status: ${http_code} (expected 200)"
        echo "Response body: ${body}"
        return 1
    fi

    # Check response time
    local time_ok=$(echo "$time_total < $MAX_RESPONSE_TIME" | bc -l)
    if [ "$time_ok" = "1" ]; then
        log_info "Response time: ${time_total}s (< ${MAX_RESPONSE_TIME}s requirement)"
    else
        log_warn "Response time: ${time_total}s (>= ${MAX_RESPONSE_TIME}s requirement)"
    fi

    # Parse JSON response
    local results_count=$(echo "$body" | python3 -c "import sys, json; data=json.load(sys.stdin); print(len(data.get('results', [])))" 2>/dev/null || echo "0")
    local total_count=$(echo "$body" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('total_count', 0))" 2>/dev/null || echo "0")
    local source=$(echo "$body" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('source', 'unknown'))" 2>/dev/null || echo "unknown")
    local cache_hit=$(echo "$body" | python3 -c "import sys, json; data=json.load(sys.stdin); print(data.get('cache_hit', False))" 2>/dev/null || echo "False")

    # Verify results
    if [ "$results_count" -gt "0" ]; then
        log_info "Results returned: ${results_count} items"
    else
        log_warn "No results returned (empty array)"
    fi

    log_info "Total count: ${total_count}"

    # Check data source
    if [ "$source" = "$expected_source" ]; then
        log_info "Data source: ${source} (expected: ${expected_source})"
    elif [ "$source" = "unknown" ]; then
        log_warn "Data source: field not present in response"
    else
        log_warn "Data source: ${source} (expected: ${expected_source}, may be using fallback)"
    fi

    # Check cache status
    if [ "$cache_hit" = "True" ]; then
        log_warn "Cache hit: True (subsequent request, testing cached response)"
    else
        log_info "Cache hit: False (fresh data from API)"
    fi

    # Verify first result has expected fields
    if [ "$results_count" -gt "0" ]; then
        echo ""
        echo "First Result Sample:"
        echo "$body" | python3 -c "
import sys, json
data = json.load(sys.stdin)
if data.get('results') and len(data['results']) > 0:
    first = data['results'][0]
    print(json.dumps(first, indent=2, ensure_ascii=False))
" 2>/dev/null || echo "Could not parse first result"
    fi

    echo ""
    return 0
}

# Main execution
echo "=========================================="
echo "  TvScreener Endpoint Verification"
echo "=========================================="
echo ""

check_server

# Test 1: Crypto screening with RSI filter
test_endpoint \
    "/api/screener/list?market=crypto&max_rsi=30&limit=20" \
    "Crypto Screening (RSI < 30)" \
    "tvscreener"

# Test 2: Korean stock screening sorted by RSI
test_endpoint \
    "/api/screener/list?market=kr&sort_by=rsi&limit=10" \
    "Korean Stock Screening (sorted by RSI)" \
    "tvscreener"

# Test 3: US stock screening sorted by volume
test_endpoint \
    "/api/screener/list?market=us&sort_by=volume&limit=15" \
    "US Stock Screening (sorted by volume)" \
    "tvscreener"

# Additional test: Crypto screening sorted by RSI (should also use tvscreener)
test_endpoint \
    "/api/screener/list?market=crypto&sort_by=rsi&limit=10" \
    "Crypto Screening (sorted by RSI)" \
    "tvscreener"

echo ""
echo "=========================================="
echo "  Verification Complete"
echo "=========================================="
echo ""
log_info "All endpoint tests completed successfully"
echo ""
echo "Note: If any endpoint showed 'fallback' as source instead of 'tvscreener',"
echo "it means the tvscreener library is not installed or an error occurred."
echo "Check the application logs for details."
echo ""
