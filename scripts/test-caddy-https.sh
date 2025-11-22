#!/bin/bash

# ============================================================================
# Caddy HTTPS 및 보안 테스트 스크립트
# ============================================================================
# 이 스크립트는 Caddy reverse proxy의 HTTPS 설정과 보안 헤더를 검증합니다.
#
# 사용법:
#   bash scripts/test-caddy-https.sh [domain]
#
# 예시:
#   bash scripts/test-caddy-https.sh mgh3326.duckdns.org
#
# 환경 변수로 도메인 지정 가능:
#   DOMAIN_NAME=mgh3326.duckdns.org bash scripts/test-caddy-https.sh
# ============================================================================

# set -e  # 에러 발생 시 즉시 종료 (테스트 연속성을 위해 주석 처리)

# 색상 코드
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# 도메인 설정
DOMAIN="${1:-${DOMAIN_NAME:-localhost}}"
HTTP_URL="http://${DOMAIN}"
HTTPS_URL="https://${DOMAIN}"
GRAFANA_URL="${HTTPS_URL}/grafana"

# 결과 카운터
PASSED=0
FAILED=0
WARNINGS=0

# 헬퍼 함수
print_header() {
    echo -e "\n${BLUE}========================================${NC}"
    echo -e "${BLUE}$1${NC}"
    echo -e "${BLUE}========================================${NC}\n"
}

print_test() {
    echo -e "${YELLOW}[TEST]${NC} $1"
}

print_success() {
    echo -e "${GREEN}[✓ PASS]${NC} $1"
    ((PASSED++))
}

print_failure() {
    echo -e "${RED}[✗ FAIL]${NC} $1"
    ((FAILED++))
}

print_warning() {
    echo -e "${YELLOW}[⚠ WARN]${NC} $1"
    ((WARNINGS++))
}

print_info() {
    echo -e "${BLUE}[INFO]${NC} $1"
}

# 필수 도구 확인
check_prerequisites() {
    local missing_tools=()

    if ! command -v curl &> /dev/null; then
        missing_tools+=("curl")
    fi

    if ! command -v docker &> /dev/null; then
        missing_tools+=("docker")
    fi

    if ! command -v openssl &> /dev/null; then
        missing_tools+=("openssl")
    fi

    if [ ${#missing_tools[@]} -ne 0 ]; then
        print_warning "다음 필수 도구가 설치되지 않았습니다: ${missing_tools[*]}"
        print_info "일부 테스트가 건너뛰어지거나 실패할 수 있습니다."
    fi
}

# 도메인이 localhost가 아닌지 확인
check_domain() {
    if [ "$DOMAIN" = "localhost" ]; then
        print_warning "도메인이 'localhost'로 설정되어 있습니다."
        print_info "실제 도메인으로 테스트하려면 다음과 같이 실행하세요:"
        print_info "  bash scripts/test-caddy-https.sh your_domain.com"
        print_info "또는 .env 파일에서 DOMAIN_NAME을 설정하세요."
        echo ""
        read -p "localhost로 계속 진행하시겠습니까? (y/n) " -n 1 -r
        echo
        if [[ ! $REPLY =~ ^[Yy]$ ]]; then
            exit 1
        fi
    fi
}

# Caddy 서비스 실행 확인
check_caddy_running() {
    print_test "Caddy 서비스 실행 상태 확인"

    if docker ps --format '{{.Names}}' | grep -q "^caddy$"; then
        print_success "Caddy 컨테이너가 실행 중입니다."

        # Caddy 헬스체크 상태 확인
        HEALTH_STATUS=$(docker inspect --format='{{.State.Health.Status}}' caddy 2>/dev/null || echo "none")
        if [ "$HEALTH_STATUS" = "healthy" ]; then
            print_success "Caddy 헬스체크 상태: healthy"
        elif [ "$HEALTH_STATUS" = "none" ]; then
            print_info "Caddy 헬스체크가 설정되지 않았습니다."
        else
            print_warning "Caddy 헬스체크 상태: $HEALTH_STATUS"
        fi
    else
        print_failure "Caddy 컨테이너가 실행되지 않았습니다."
        print_info "다음 명령으로 시작하세요: docker compose -f docker-compose.monitoring-rpi.yml up -d caddy"
        return 1
    fi
}

# HTTP to HTTPS 리디렉션 테스트
test_http_redirect() {
    print_test "HTTP → HTTPS 자동 리디렉션 테스트"

    if [ "$DOMAIN" = "localhost" ]; then
        print_info "localhost는 HTTPS 인증서가 없으므로 리디렉션 테스트를 건너뜁니다."
        return
    fi

    RESPONSE=$(curl -sI -m 10 "$HTTP_URL" 2>/dev/null || echo "ERROR")

    if echo "$RESPONSE" | grep -qi "location.*https"; then
        print_success "HTTP 요청이 HTTPS로 리디렉션됩니다."
        REDIRECT_URL=$(echo "$RESPONSE" | grep -i "location:" | awk '{print $2}' | tr -d '\r')
        print_info "리디렉션 URL: $REDIRECT_URL"
    elif [ "$RESPONSE" = "ERROR" ]; then
        print_failure "HTTP 연결 실패 (타임아웃 또는 연결 거부)"
    else
        print_failure "HTTP → HTTPS 리디렉션이 작동하지 않습니다."
    fi
}

# HTTPS 접속 테스트
test_https_connection() {
    print_test "HTTPS 접속 테스트"

    if [ "$DOMAIN" = "localhost" ]; then
        print_info "localhost는 자체 서명 인증서를 사용하므로 -k 옵션으로 테스트합니다."
        CURL_OPTS="-k"
    else
        CURL_OPTS=""
    fi

    RESPONSE=$(curl -sI -m 10 $CURL_OPTS "$HTTPS_URL" 2>/dev/null || echo "ERROR")

    if [ "$RESPONSE" = "ERROR" ]; then
        print_failure "HTTPS 연결 실패 (타임아웃 또는 연결 거부)"
        print_info "방화벽에서 포트 443이 열려있는지 확인하세요."
    elif echo "$RESPONSE" | grep -q "HTTP/"; then
        HTTP_STATUS=$(echo "$RESPONSE" | head -n 1 | awk '{print $2}')
        if [ "$HTTP_STATUS" = "200" ] || [ "$HTTP_STATUS" = "302" ] || [ "$HTTP_STATUS" = "301" ]; then
            print_success "HTTPS 접속 성공 (HTTP 상태: $HTTP_STATUS)"
        else
            print_warning "HTTPS 접속되었으나 예상치 못한 상태 코드: $HTTP_STATUS"
        fi
    else
        print_failure "HTTPS 응답을 받지 못했습니다."
    fi
}

# 보안 헤더 검증
test_security_headers() {
    print_test "보안 헤더 검증"

    if [ "$DOMAIN" = "localhost" ]; then
        CURL_OPTS="-k"
    else
        CURL_OPTS=""
    fi

    HEADERS=$(curl -sI -m 10 $CURL_OPTS "$HTTPS_URL" 2>/dev/null || echo "ERROR")

    if [ "$HEADERS" = "ERROR" ]; then
        print_failure "헤더 검증을 위한 HTTPS 연결 실패"
        return
    fi

    # HSTS 헤더 확인
    if echo "$HEADERS" | grep -qi "strict-transport-security"; then
        HSTS_VALUE=$(echo "$HEADERS" | grep -i "strict-transport-security" | cut -d: -f2- | tr -d '\r')
        print_success "HSTS 헤더 설정됨:$HSTS_VALUE"
    else
        print_failure "HSTS 헤더가 설정되지 않았습니다."
    fi

    # X-Content-Type-Options 헤더 확인
    if echo "$HEADERS" | grep -qi "x-content-type-options.*nosniff"; then
        print_success "X-Content-Type-Options: nosniff 설정됨"
    else
        print_failure "X-Content-Type-Options 헤더가 설정되지 않았습니다."
    fi

    # X-Frame-Options 헤더 확인
    if echo "$HEADERS" | grep -qi "x-frame-options"; then
        XFO_VALUE=$(echo "$HEADERS" | grep -i "x-frame-options" | cut -d: -f2- | tr -d '\r')
        print_success "X-Frame-Options 설정됨:$XFO_VALUE"
    else
        print_failure "X-Frame-Options 헤더가 설정되지 않았습니다."
    fi

    # X-XSS-Protection 헤더 확인
    if echo "$HEADERS" | grep -qi "x-xss-protection"; then
        print_success "X-XSS-Protection 헤더 설정됨"
    else
        print_warning "X-XSS-Protection 헤더가 설정되지 않았습니다."
    fi

    # Referrer-Policy 헤더 확인
    if echo "$HEADERS" | grep -qi "referrer-policy"; then
        print_success "Referrer-Policy 헤더 설정됨"
    else
        print_warning "Referrer-Policy 헤더가 설정되지 않았습니다."
    fi
}

# SSL/TLS 인증서 검증
test_ssl_certificate() {
    print_test "SSL/TLS 인증서 검증"

    if [ "$DOMAIN" = "localhost" ]; then
        print_info "localhost는 자체 서명 인증서를 사용하므로 검증을 건너뜁니다."
        return
    fi

    if ! command -v openssl &> /dev/null; then
        print_warning "openssl이 설치되지 않아 인증서 검증을 건너뜁니다."
        return
    fi

    CERT_INFO=$(echo | openssl s_client -connect "${DOMAIN}:443" -servername "$DOMAIN" 2>/dev/null | openssl x509 -noout -dates -subject -issuer 2>/dev/null || echo "ERROR")

    if [ "$CERT_INFO" = "ERROR" ]; then
        print_failure "인증서 정보를 가져올 수 없습니다."
        return
    fi

    # 인증서 발급자 확인
    ISSUER=$(echo "$CERT_INFO" | grep "issuer=" | cut -d= -f2-)
    if echo "$ISSUER" | grep -qi "let's encrypt\|zerossl"; then
        print_success "신뢰할 수 있는 CA에서 발급된 인증서입니다: $ISSUER"
    else
        print_info "인증서 발급자: $ISSUER"
    fi

    # 인증서 만료일 확인
    NOT_AFTER=$(echo "$CERT_INFO" | grep "notAfter=" | cut -d= -f2-)
    print_info "인증서 만료일: $NOT_AFTER"

    # 도메인 확인
    SUBJECT=$(echo "$CERT_INFO" | grep "subject=" | cut -d= -f2-)
    print_info "인증서 주체: $SUBJECT"
}

# Grafana 서브패스 접근 테스트
test_grafana_subpath() {
    print_test "Grafana 서브패스 접근 테스트"

    if [ "$DOMAIN" = "localhost" ]; then
        CURL_OPTS="-k"
    else
        CURL_OPTS=""
    fi

    RESPONSE=$(curl -sI -m 10 $CURL_OPTS "$GRAFANA_URL/login" 2>/dev/null || echo "ERROR")

    if [ "$RESPONSE" = "ERROR" ]; then
        print_failure "Grafana 서브패스 연결 실패"
        print_info "Grafana 컨테이너가 실행 중인지 확인하세요."
    elif echo "$RESPONSE" | grep -q "HTTP/"; then
        HTTP_STATUS=$(echo "$RESPONSE" | head -n 1 | awk '{print $2}')
        if [ "$HTTP_STATUS" = "200" ] || [ "$HTTP_STATUS" = "302" ]; then
            print_success "Grafana 서브패스 접근 성공 (HTTP 상태: $HTTP_STATUS)"
        else
            print_warning "Grafana 응답 상태 코드: $HTTP_STATUS"
        fi
    else
        print_failure "Grafana 서브패스에서 응답을 받지 못했습니다."
    fi
}



# Auto-trader 앱 접근 테스트
test_autotrader_access() {
    print_test "Auto-trader 앱 접근 테스트"

    if [ "$DOMAIN" = "localhost" ]; then
        CURL_OPTS="-k"
    else
        CURL_OPTS=""
    fi

    RESPONSE=$(curl -s -m 10 $CURL_OPTS "$HTTPS_URL" 2>/dev/null || echo "ERROR")

    if [ "$RESPONSE" = "ERROR" ]; then
        print_failure "Auto-trader 앱 연결 실패"
        print_info "Auto-trader가 포트 8000에서 실행 중인지 확인하세요: curl http://localhost:8000"
    elif [ -n "$RESPONSE" ]; then
        print_success "Auto-trader 앱에서 응답을 받았습니다."
        # 응답 길이 표시
        RESPONSE_LENGTH=$(echo "$RESPONSE" | wc -c)
        print_info "응답 크기: $RESPONSE_LENGTH bytes"
    else
        print_warning "Auto-trader 앱에서 빈 응답을 받았습니다."
    fi
}

# Caddy 설정 검증
test_caddy_config() {
    print_test "Caddy 설정 파일 검증"

    if docker exec caddy caddy validate --config /etc/caddy/Caddyfile > /dev/null 2>&1; then
        print_success "Caddyfile 설정이 유효합니다."
    else
        print_failure "Caddyfile 설정이 유효하지 않습니다."
        print_info "설정 검증 결과:"
        docker exec caddy caddy validate --config /etc/caddy/Caddyfile
    fi
}

# 환경 변수 확인
test_environment_variables() {
    print_test "환경 변수 설정 확인"

    ACME_EMAIL=$(docker exec caddy sh -c 'echo $ACME_EMAIL' 2>/dev/null)
    DOMAIN_NAME_ENV=$(docker exec caddy sh -c 'echo $DOMAIN_NAME' 2>/dev/null)

    if [ -n "$ACME_EMAIL" ]; then
        print_success "ACME_EMAIL 설정됨: $ACME_EMAIL"
    else
        print_failure "ACME_EMAIL이 설정되지 않았습니다."
    fi

    if [ -n "$DOMAIN_NAME_ENV" ]; then
        print_success "DOMAIN_NAME 설정됨: $DOMAIN_NAME_ENV"

        if [ "$DOMAIN_NAME_ENV" != "$DOMAIN" ]; then
            print_warning "입력된 도메인($DOMAIN)과 환경변수($DOMAIN_NAME_ENV)가 다릅니다."
        fi
    else
        print_failure "DOMAIN_NAME이 설정되지 않았습니다."
    fi
}

# 메인 실행
main() {
    print_header "Caddy HTTPS 및 보안 테스트"
    print_info "테스트 대상 도메인: $DOMAIN"
    print_info "HTTPS URL: $HTTPS_URL"
    echo ""

    # 도메인 확인
    check_domain

    # 테스트 실행
    check_caddy_running || exit 1
    test_caddy_config
    test_environment_variables
    test_http_redirect
    test_https_connection
    test_security_headers
    test_ssl_certificate
    test_grafana_subpath
    test_autotrader_access


    # 결과 요약
    print_header "테스트 결과 요약"
    echo -e "${GREEN}통과: $PASSED${NC}"
    echo -e "${RED}실패: $FAILED${NC}"
    echo -e "${YELLOW}경고: $WARNINGS${NC}"
    echo ""

    if [ $FAILED -eq 0 ]; then
        echo -e "${GREEN}모든 필수 테스트를 통과했습니다! ✓${NC}"
        exit 0
    else
        echo -e "${RED}일부 테스트가 실패했습니다. 위 로그를 확인하세요.${NC}"
        exit 1
    fi
}

# 스크립트 실행
main
