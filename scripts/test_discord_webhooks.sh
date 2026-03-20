#!/bin/bash
# Discord Webhook E2E Verification Script
# This script tests Discord webhook integration using curl commands

set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Helper functions
print_header() {
    echo ""
    echo "======================================================================"
    echo "  $1"
    echo "======================================================================"
}

print_success() {
    echo -e "${GREEN}✅ $1${NC}"
}

print_error() {
    echo -e "${RED}❌ $1${NC}"
}

print_info() {
    echo -e "${BLUE}ℹ️  $1${NC}"
}

print_warning() {
    echo -e "${YELLOW}⚠️  $1${NC}"
}

# Load environment variables
load_env() {
    if [ -f .env ]; then
        export $(cat .env | grep -v '^#' | grep 'DISCORD_WEBHOOK' | xargs)
    else
        print_error ".env file not found"
        exit 1
    fi
}

# Test a single webhook
test_webhook() {
    local webhook_name=$1
    local webhook_url=$2
    local test_type=$3  # buy, sell, analysis

    if [ -z "$webhook_url" ]; then
        print_warning "${webhook_name} webhook not configured (skip)"
        return 0
    fi

    print_info "Testing ${webhook_name} webhook (${test_type} notification)..."

    # Prepare test payload based on type
    local title=""
    local color=""
    local description=""
    local fields=""

    case $test_type in
        buy)
            title="💰 매수 주문 접수"
            color="65280"  # 0x00FF00 green
            description="🕒 $(date '+%Y-%m-%d %H:%M:%S')"
            fields='[
                {"name": "종목", "value": "비트코인 (BTC)", "inline": true},
                {"name": "시장", "value": "암호화폐", "inline": true},
                {"name": "주문 수", "value": "1건", "inline": true},
                {"name": "총 금액", "value": "100,000원", "inline": true},
                {"name": "주문 상세", "value": "1. 가격: 100,000원 × 수량: 0.001", "inline": false}
            ]'
            ;;
        sell)
            title="💸 매도 주문 접수"
            color="16711680"  # 0xFF0000 red
            description="🕒 $(date '+%Y-%m-%d %H:%M:%S')"
            fields='[
                {"name": "종목", "value": "이더리움 (ETH)", "inline": true},
                {"name": "시장", "value": "암호화폐", "inline": true},
                {"name": "주문 수", "value": "1건", "inline": true},
                {"name": "총 수량", "value": "0.5", "inline": true},
                {"name": "예상 금액", "value": "50,000원", "inline": true}
            ]'
            ;;
        analysis)
            title="📊 AI 분석 완료"
            color="255"  # 0x0000FF blue
            description="🕒 $(date '+%Y-%m-%d %H:%M:%S')"
            fields='[
                {"name": "종목", "value": "비트코인 (BTC)", "inline": true},
                {"name": "시장", "value": "암호화폐", "inline": true},
                {"name": "판단", "value": "📈 매수", "inline": true},
                {"name": "신뢰도", "value": "85%", "inline": true},
                {"name": "주요 근거", "value": "1. 상승 추세 지속\n2. 거래량 증가\n3. RSI 과매도 탈출", "inline": false}
            ]'
            ;;
        *)
            print_error "Unknown test type: $test_type"
            return 1
            ;;
    esac

    # Send the webhook
    local response=$(curl -s -w "\n%{http_code}" -X POST "$webhook_url" \
        -H "Content-Type: application/json" \
        -d "{
            \"embeds\": [{
                \"title\": \"$title\",
                \"description\": \"$description\",
                \"color\": $color,
                \"fields\": $fields,
                \"timestamp\": \"$(date -u +%Y-%m-%dT%H:%M:%S.%3NZ)\"
            }]
        }" 2>&1)

    local http_code=$(echo "$response" | tail -n1)
    local body=$(echo "$response" | sed '$d')

    if [ "$http_code" = "204" ] || [ "$http_code" = "200" ]; then
        print_success "${webhook_name} webhook test passed (HTTP $http_code)"
        return 0
    else
        print_error "${webhook_name} webhook test failed (HTTP $http_code)"
        if [ -n "$body" ]; then
            echo "Response: $body"
        fi
        return 1
    fi
}

# Main execution
main() {
    print_header "Discord Webhook E2E Verification"

    # Load environment
    load_env

    # Check which webhooks are configured
    print_header "Webhook Configuration"

    local total=0
    local configured=0

    if [ -n "$DISCORD_WEBHOOK_US" ]; then
        configured=$((configured + 1))
        print_success "US Stocks webhook configured"
    else
        print_warning "US Stocks webhook not configured"
    fi
    total=$((total + 1))

    if [ -n "$DISCORD_WEBHOOK_KR" ]; then
        configured=$((configured + 1))
        print_success "KR Stocks webhook configured"
    else
        print_warning "KR Stocks webhook not configured"
    fi
    total=$((total + 1))

    if [ -n "$DISCORD_WEBHOOK_CRYPTO" ]; then
        configured=$((configured + 1))
        print_success "Crypto webhook configured"
    else
        print_warning "Crypto webhook not configured"
    fi
    total=$((total + 1))

    if [ -n "$DISCORD_WEBHOOK_ALERTS" ]; then
        configured=$((configured + 1))
        print_success "Alerts webhook configured"
    else
        print_warning "Alerts webhook not configured"
    fi
    total=$((total + 1))

    echo ""
    print_info "Configured: $configured/$total webhooks"

    if [ $configured -eq 0 ]; then
        print_error "No Discord webhooks configured. Please set DISCORD_WEBHOOK_* variables in .env"
        exit 1
    fi

    # Run tests
    print_header "Sending Test Notifications"

    local tests_passed=0
    local tests_total=0

    # Test US webhook
    if [ -n "$DISCORD_WEBHOOK_US" ]; then
        tests_total=$((tests_total + 1))
        if test_webhook "US Stocks" "$DISCORD_WEBHOOK_US" "buy"; then
            tests_passed=$((tests_passed + 1))
        fi
    fi

    # Test KR webhook
    if [ -n "$DISCORD_WEBHOOK_KR" ]; then
        tests_total=$((tests_total + 1))
        if test_webhook "KR Stocks" "$DISCORD_WEBHOOK_KR" "sell"; then
            tests_passed=$((tests_passed + 1))
        fi
    fi

    # Test Crypto webhook
    if [ -n "$DISCORD_WEBHOOK_CRYPTO" ]; then
        tests_total=$((tests_total + 1))
        if test_webhook "Crypto" "$DISCORD_WEBHOOK_CRYPTO" "analysis"; then
            tests_passed=$((tests_passed + 1))
        fi
    fi

    # Test Alerts webhook
    if [ -n "$DISCORD_WEBHOOK_ALERTS" ]; then
        tests_total=$((tests_total + 1))
        if test_webhook "Alerts" "$DISCORD_WEBHOOK_ALERTS" "analysis"; then
            tests_passed=$((tests_passed + 1))
        fi
    fi

    # Summary
    print_header "Test Results Summary"

    echo ""
    echo "Tests passed: $tests_passed/$tests_total"

    if [ $tests_passed -eq $tests_total ]; then
        print_success "🎉 All tests passed! Discord webhook integration is working correctly."
        echo ""
        echo "Check your Discord channels to verify the test notifications arrived."
        exit 0
    else
        print_error "❌ $((tests_total - tests_passed)) test(s) failed"
        echo ""
        echo "Please check:"
        echo "  1. Webhook URLs are correct in .env"
        echo "  2. Webhooks still exist in your Discord server"
        echo "  3. Network connectivity is working"
        exit 1
    fi
}

# Run main function
main "$@"
