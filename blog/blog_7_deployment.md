# 라즈베리파이 홈서버에 자동 HTTPS로 안전하게 배포하기: Caddy + Docker Compose로 프로덕션 환경 구축

![라즈베리파이 배포](images/deployment_thumbnail.png)

> 이 글은 AI 기반 자동매매 시스템 시리즈의 **7편**입니다.
>
> **전체 시리즈:**
> - [1편: 한투 API로 실시간 주식 데이터 수집하기](https://mgh3326.tistory.com/227)
> - [2편: yfinance로 애플·테슬라 분석하기](https://mgh3326.tistory.com/228)
> - [3편: Upbit으로 비트코인 24시간 분석하기](https://mgh3326.tistory.com/229)
> - [4편: AI 분석 결과 DB에 저장하기](https://mgh3326.tistory.com/230)
> - [5편: Upbit 웹 트레이딩 대시보드 구축하기](https://mgh3326.tistory.com/232)
> - [6편: 실전 운영을 위한 모니터링 시스템 구축](https://mgh3326.tistory.com/233)
> - **7편: 라즈베리파이 홈서버에 자동 HTTPS로 안전하게 배포하기** ← 현재 글

## 들어가며

### 지금까지의 여정

우리는 지금까지:
- ✅ 한투/yfinance/Upbit API로 데이터 수집
- ✅ AI 분석 자동화 (Gemini)
- ✅ DB 저장 및 정규화
- ✅ 웹 대시보드 구축
- ✅ Grafana 관찰성 스택으로 모니터링 시스템 구축

까지 완성했습니다.

### 드디어 배포!

이제 마지막 단계입니다. 개발 환경에서 `localhost:8000`으로 접속하던 시스템을:
- 🌍 **인터넷에서 접근 가능**하도록
- 🔒 **HTTPS로 안전**하게
- 🏠 **라즈베리파이 홈서버**에서 24시간 실행

하도록 만들어보겠습니다!

### 왜 라즈베리파이인가?

**클라우드 vs 라즈베리파이**

| 항목 | AWS/GCP (클라우드) | 라즈베리파이 홈서버 |
|------|-------------------|-------------------|
| 비용 | 월 $20~50+ | 초기 $100 (이후 전기세만) |
| 성능 | 높음 | 충분 (Raspberry Pi 5 기준) |
| 관리 | 쉬움 | 직접 관리 필요 |
| 학습 | 추상화됨 | 모든 것을 배움 |
| 재미 | 보통 | ⭐⭐⭐⭐⭐ |

**라즈베리파이를 선택한 이유:**
1. **비용**: 한 번 구매로 평생 사용 (전기세 월 1,000원 수준)
2. **학습**: 네트워크, 보안, 인프라를 직접 경험
3. **재미**: 내 손으로 직접 만드는 홈서버!
4. **성능**: Raspberry Pi 5 (8GB)는 개인 프로젝트에 충분

## 배포 아키텍처

![배포 아키텍처](images/deployment_architecture.png)
*Caddy + Docker Compose 기반 프로덕션 배포 구조*

우리가 구축할 시스템의 전체 구조:

```
인터넷
  ↓
공유기 포트포워딩 (80, 443)
  ↓
라즈베리파이 5 (홈서버)
  ├─ Caddy (Reverse Proxy + 자동 HTTPS)
  │   ├─ Let's Encrypt 자동 인증서 발급/갱신
  │   ├─ HTTP → HTTPS 자동 리디렉션
  │   └─ Rate Limiting + 보안 헤더
  │
  ├─ Auto-trader 앱 (FastAPI)
  │   ├─ 포트 8000 (내부)
  │   └─ https://your-domain.com (외부)
  │
  ├─ Grafana 관찰성 스택
  │   ├─ Grafana: 포트 3000 (내부)
  │   │   └─ https://your-domain.com/grafana (외부)
  │   ├─ Tempo: 분산 추적
  │   ├─ Loki: 로그 수집
  │   ├─ Prometheus: 메트릭
  │   └─ Promtail: 로그 수집기
  │
  └─ PostgreSQL + Redis (네이티브)
```

### 핵심 컴포넌트

1. **Caddy**: 자동 HTTPS + Reverse Proxy
   - Let's Encrypt를 통한 자동 SSL/TLS 인증서 발급
   - 인증서 자동 갱신 (만료 30일 전)
   - HTTP → HTTPS 자동 리디렉션
   - Rate Limiting으로 DDoS 방어
   - 보안 헤더 자동 적용

2. **Docker Compose**: 컨테이너 오케스트레이션
   - 모든 서비스를 하나의 파일로 관리
   - 자동 재시작 (`restart: unless-stopped`)
   - 리소스 제한으로 안정성 확보

3. **DuckDNS**: 무료 동적 DNS
   - 고정 도메인 제공 (예: `your-domain.duckdns.org`)
   - 공유기 IP 변경 시 자동 업데이트

## 사전 준비

### 1. 라즈베리파이 초기 설정

**하드웨어:**
- Raspberry Pi 5 (8GB RAM 권장)
- 64GB+ microSD 카드 (Class 10 이상)
- 전원 어댑터 (5V 3A 이상)
- 랜선 (유선 네트워크 권장)

**OS 설치:**
```bash
# Raspberry Pi OS Lite 64-bit 설치 (권장)
# Raspberry Pi Imager 사용: https://www.raspberrypi.com/software/

# SSH 접속 활성화
sudo systemctl enable ssh
sudo systemctl start ssh

# 시스템 업데이트
sudo apt update && sudo apt upgrade -y
```

### 2. Docker 설치

```bash
# Docker 설치 스크립트 실행
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh

# 현재 사용자를 docker 그룹에 추가
sudo usermod -aG docker $USER

# 로그아웃 후 재로그인 (그룹 권한 적용)
# 또는 다음 명령으로 즉시 적용:
newgrp docker

# Docker 버전 확인
docker --version
docker compose version
```

### 3. DuckDNS 도메인 생성

**DuckDNS란?**
> 무료 동적 DNS 서비스로, 변경되는 공유기 IP를 고정 도메인으로 연결해줍니다.

**설정 방법:**

1. **DuckDNS 계정 생성**: https://www.duckdns.org
2. **도메인 생성**:
   - 원하는 서브도메인 입력 (예: `your-subdomain`)
   - 최종 도메인: `your-domain.duckdns.org`
3. **IP 주소 설정**:
   - "current ip" 필드에 공유기 공인 IP 입력
   - 또는 "update ip" 버튼으로 자동 감지
4. **토큰 저장**:
   - DuckDNS 토큰을 복사해 두기 (자동 업데이트에 필요)

**IP 자동 업데이트 (선택사항):**

공유기 IP가 변경될 때마다 자동으로 DuckDNS 업데이트:

```bash
# cron 작업 추가
crontab -e

# 5분마다 IP 업데이트 (YOUR_TOKEN을 실제 토큰으로 변경)
*/5 * * * * curl "https://www.duckdns.org/update?domains=your-subdomain&token=YOUR_TOKEN&ip="
```

### 4. 공유기 포트포워딩

**설정 항목:**
- **외부 포트**: 80 (HTTP)
- **내부 포트**: 80
- **내부 IP**: 라즈베리파이 IP (예: 192.168.0.100)
- **프로토콜**: TCP

동일하게 443 포트도 포워딩:
- **외부 포트**: 443 (HTTPS)
- **내부 포트**: 443
- **내부 IP**: 라즈베리파이 IP
- **프로토콜**: TCP

**주요 공유기별 설정 메뉴:**
- ipTIME: `고급 설정 > NAT/라우터 관리 > 포트포워드 설정`
- 공유기마다 메뉴 위치가 다르므로 모델명으로 검색 권장

**라즈베리파이 고정 IP 설정 (권장):**

```bash
# /etc/dhcpcd.conf 파일 수정
sudo nano /etc/dhcpcd.conf

# 다음 내용 추가 (IP 주소는 환경에 맞게 조정)
interface eth0
static ip_address=192.168.0.100/24
static routers=192.168.0.1
static domain_name_servers=8.8.8.8 8.8.4.4

# 재부팅
sudo reboot
```

## Caddy 설정

### 1. Caddyfile 이해하기

Caddy는 `Caddyfile`이라는 간단한 설정 파일로 모든 것을 설정합니다.

**우리 프로젝트의 Caddyfile:**

```caddyfile
# Caddyfile
{
	# Global options
	email {$ACME_EMAIL}  # Let's Encrypt 알림 이메일

	# JSON 형식 로그 (Loki 연동 용이)
	log {
		output file /data/logs/access.log
		format json
	}
}

{$DOMAIN_NAME} {
	# Rate limiting - DDoS/Brute Force 방어
	# IP당 분당 500 요청 제한
	rate_limit {
		zone dynamic {
			key {remote_host}
			events 500
			window 1m
		}
	}

	# 보안 헤더 자동 적용
	header {
		# HSTS: 1년간 HTTPS 강제
		Strict-Transport-Security "max-age=31536000; includeSubDomains; preload"

		# MIME 스니핑 방지
		X-Content-Type-Options "nosniff"

		# 클릭재킹 방지 (Grafana iframe 허용)
		X-Frame-Options "SAMEORIGIN"

		# Content Security Policy
		Content-Security-Policy "default-src 'self'; script-src 'self' 'unsafe-inline' 'unsafe-eval' https://cdn.jsdelivr.net; style-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net; img-src 'self' data:; font-src 'self' data: https://cdn.jsdelivr.net; connect-src 'self' https://cdn.jsdelivr.net;"

		# Referrer Policy
		Referrer-Policy "strict-origin-when-cross-origin"
	}

	# Favicon 제공
	handle /favicon.ico {
		root * /srv
		file_server
	}

	# Grafana 서브패스 (우선순위 높음 - 먼저 매칭)
	handle /grafana* {
		reverse_proxy grafana:3000 {
			header_up Host {host}
			header_up X-Real-IP {remote_host}
		}
	}

	# Auto-trader 메인 앱 (기본 경로)
	handle {
		reverse_proxy host.docker.internal:8000 {
			header_up Host {host}
			header_up X-Real-IP {remote_host}
		}
	}

	# 도메인별 액세스 로그 (로테이션 설정)
	log {
		output file /data/logs/domain_access.log {
			roll_size 100mb   # 100MB마다 로테이션
			roll_keep 10      # 최근 10개 파일 유지
			roll_keep_for 720h  # 30일간 보관
		}
		format json
	}
}
```

**핵심 포인트:**

1. **자동 HTTPS**:
   - 도메인명만 입력하면 Let's Encrypt 자동 인증서 발급
   - 인증서 만료 30일 전 자동 갱신
   - HTTP → HTTPS 자동 리디렉션

2. **Reverse Proxy**:
   - Grafana: Docker 네트워크 내부 → `grafana:3000` 사용
   - Auto-trader: 호스트에서 실행 → `host.docker.internal:8000` 사용

3. **보안**:
   - Rate Limiting으로 DDoS/Brute Force 방어
   - 보안 헤더 자동 적용 (HSTS, CSP 등)
   - JSON 로그로 분석 용이

4. **로그 관리**:
   - 100MB마다 자동 로테이션
   - 최근 10개 파일, 30일간 보관
   - JSON 형식으로 Loki 연동 가능

### 2. 환경 변수 설정

```bash
# .env 파일 (프로젝트 루트)
ACME_EMAIL=your_email@example.com        # Let's Encrypt 알림 이메일
DOMAIN_NAME=your-domain.duckdns.org          # DuckDNS 도메인
```

**주의사항:**
- `ACME_EMAIL`: 인증서 만료 알림을 받을 이메일 (실제 이메일 권장)
- `DOMAIN_NAME`: DuckDNS에서 생성한 도메인 (프로토콜 제외)

### 3. Dockerfile.caddy

Caddy는 기본 이미지에 rate limiting 플러그인이 없어서 커스텀 이미지를 빌드합니다.

```dockerfile
# Dockerfile.caddy
FROM caddy:2.10.2-builder AS builder

# Install rate limiting plugin
RUN xcaddy build \
    --with github.com/mholt/caddy-ratelimit

FROM caddy:2.10.2

# Copy the custom Caddy binary with rate limiting
COPY --from=builder /usr/bin/caddy /usr/bin/caddy
```

**왜 커스텀 이미지인가?**
- Rate limiting 플러그인 (`caddy-ratelimit`)은 공식 이미지에 미포함
- `xcaddy`로 플러그인 포함한 커스텀 빌드

## Docker Compose 설정

### 전체 스택 구성

**docker-compose.monitoring-rpi.yml (일부):**

```yaml
version: '3.8'

services:
  # ==========================================
  # Caddy - Reverse Proxy with Automatic HTTPS
  # ==========================================
  caddy:
    build:
      context: .
      dockerfile: Dockerfile.caddy
    container_name: caddy
    restart: unless-stopped
    ports:
      - "80:80"     # HTTP (자동 HTTPS 리디렉션)
      - "443:443"   # HTTPS
    volumes:
      - ./Caddyfile:/etc/caddy/Caddyfile:ro  # 설정 파일
      - caddy_data:/data                      # 인증서 저장
      - caddy_config:/config                  # Caddy 설정
      - ./static:/srv:ro                      # 정적 파일 (favicon 등)
    environment:
      - DOMAIN_NAME=${DOMAIN_NAME}            # DuckDNS 도메인
      - ACME_EMAIL=${ACME_EMAIL}              # Let's Encrypt 이메일
    networks:
      - monitoring
    extra_hosts:
      - "host.docker.internal:host-gateway"   # 호스트 접근용
    deploy:
      resources:
        limits:
          memory: 256m
          cpus: '0.5'

  # ==========================================
  # Grafana - Dashboard
  # ==========================================
  grafana:
    image: grafana/grafana:latest
    container_name: grafana
    restart: unless-stopped
    ports:
      - "3000:3000"
    environment:
      - GF_SERVER_ROOT_URL=https://${DOMAIN_NAME}/grafana
      - GF_SERVER_SERVE_FROM_SUB_PATH=true
      - GF_SECURITY_ADMIN_PASSWORD=${GRAFANA_ADMIN_PASSWORD:-admin}
    volumes:
      - grafana_data:/var/lib/grafana
      - ./grafana-config/grafana-datasources.yaml:/etc/grafana/provisioning/datasources/datasources.yaml:ro
    networks:
      - monitoring
    depends_on:
      - tempo
      - loki
      - prometheus
    deploy:
      resources:
        limits:
          memory: 512m
          cpus: '1.0'

  # ==========================================
  # Tempo - Distributed Tracing
  # ==========================================
  tempo:
    image: grafana/tempo:latest
    container_name: tempo
    restart: unless-stopped
    ports:
      - "3200:3200"   # Tempo HTTP
      - "4317:4317"   # OTLP gRPC (FastAPI 연결)
      - "4318:4318"   # OTLP HTTP
    volumes:
      - tempo_data:/var/tempo
      - ./grafana-config/tempo.yaml:/etc/tempo/tempo.yaml:ro
    networks:
      - monitoring
    deploy:
      resources:
        limits:
          memory: 512m
          cpus: '1.0'

  # ==========================================
  # Loki - Log Aggregation
  # ==========================================
  loki:
    image: grafana/loki:latest
    container_name: loki
    user: "10001:10001"
    restart: unless-stopped
    ports:
      - "3100:3100"
    volumes:
      - loki_data:/loki
      - ./grafana-config/loki.yaml:/etc/loki/local-config.yaml:ro
    networks:
      - monitoring
    healthcheck:
      test: ["CMD-SHELL", "wget --no-verbose --tries=1 --spider http://localhost:3100/ready || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5
      start_period: 30s
    deploy:
      resources:
        limits:
          memory: 512m
          cpus: '1.0'

  # ==========================================
  # Prometheus - Metrics
  # ==========================================
  prometheus:
    image: prom/prometheus:latest
    container_name: prometheus
    restart: unless-stopped
    ports:
      - "9090:9090"
    volumes:
      - prometheus_data:/prometheus
      - ./grafana-config/prometheus.yaml:/etc/prometheus/prometheus.yml:ro
    networks:
      - monitoring
    healthcheck:
      test: ["CMD-SHELL", "wget --no-verbose --tries=1 --spider http://localhost:9090/-/healthy || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5
    deploy:
      resources:
        limits:
          memory: 512m
          cpus: '1.0'

  # ==========================================
  # Promtail - Log Collector
  # ==========================================
  promtail:
    image: grafana/promtail:latest
    container_name: promtail
    restart: unless-stopped
    ports:
      - "9080:9080"
    volumes:
      - /var/log:/var/log:ro
      - /var/run/docker.sock:/var/run/docker.sock:ro
      - /var/lib/docker/containers:/var/lib/docker/containers:ro
      - ./grafana-config/promtail.yaml:/etc/promtail/config.yml:ro
    networks:
      - monitoring
    healthcheck:
      test: ["CMD-SHELL", "wget --no-verbose --tries=1 --spider http://localhost:9080/ready || exit 1"]
      interval: 30s
      timeout: 10s
      retries: 5
    deploy:
      resources:
        limits:
          cpus: '0.5'

networks:
  monitoring:
    driver: bridge

volumes:
  caddy_data:
  caddy_config:
  grafana_data:
  tempo_data:
  loki_data:
  prometheus_data:
```

**주요 설정 포인트:**

1. **Caddy 설정**:
   - `extra_hosts`: 호스트 접근을 위한 `host.docker.internal` 설정
   - 포트 80, 443 외부 노출
   - 인증서는 `caddy_data` 볼륨에 안전하게 저장

2. **Grafana 설정**:
   - `GF_SERVER_ROOT_URL`: 서브패스 설정 (`/grafana`)
   - `GF_SERVER_SERVE_FROM_SUB_PATH`: 서브패스에서 제공

3. **리소스 제한**:
   - 각 서비스마다 메모리/CPU 제한 설정
   - Raspberry Pi 5 (8GB)에서 안정적으로 실행

4. **자동 재시작**:
   - `restart: unless-stopped`: 시스템 재부팅 시 자동 시작

5. **헬스체크**:
   - 주요 서비스에 healthcheck 설정
   - 실패 시 자동 재시작

## 배포 프로세스

### 1. 프로젝트 클론 및 설정

```bash
# 프로젝트 클론
git clone https://github.com/mgh3326/auto_trader.git
cd auto_trader

# 환경 변수 설정
cp env.example .env
nano .env

# 필수 환경 변수 설정:
# - ACME_EMAIL=your_email@example.com
# - DOMAIN_NAME=your-domain.duckdns.org
# - GRAFANA_ADMIN_PASSWORD=<strong_password>
# - GOOGLE_API_KEY=xxx
# - KIS_APP_KEY=xxx
# - KIS_APP_SECRET=xxx
# - UPBIT_ACCESS_KEY=xxx
# - UPBIT_SECRET_KEY=xxx
# - TELEGRAM_TOKEN=xxx
# - TELEGRAM_CHAT_IDS_STR=xxx
# - DATABASE_URL=postgresql+asyncpg://user:pass@localhost/dbname
# - REDIS_URL=redis://localhost:6379/0
```

### 2. 데이터베이스 및 Redis 설정

**PostgreSQL 설치:**
```bash
sudo apt install postgresql postgresql-contrib -y

# PostgreSQL 사용자 및 데이터베이스 생성
sudo -u postgres psql

postgres=# CREATE USER autotrader WITH PASSWORD 'your_password';
postgres=# CREATE DATABASE autotrader OWNER autotrader;
postgres=# GRANT ALL PRIVILEGES ON DATABASE autotrader TO autotrader;
postgres=# \q

# DATABASE_URL 업데이트 (.env 파일)
DATABASE_URL=postgresql+asyncpg://autotrader:your_password@localhost/autotrader
```

**Redis 설치:**
```bash
sudo apt install redis-server -y

# Redis 시작 및 자동 시작 설정
sudo systemctl enable redis-server
sudo systemctl start redis-server

# Redis 연결 테스트
redis-cli ping
# PONG 응답 확인

# REDIS_URL 업데이트 (.env 파일)
REDIS_URL=redis://localhost:6379/0
```

### 3. Python 환경 설정

```bash
# UV 설치 (Python 패키지 관리자)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 의존성 설치
uv sync

# 데이터베이스 마이그레이션
uv run alembic upgrade head
```

### 4. Grafana 스택 시작

```bash
# Caddy 포함 전체 모니터링 스택 시작
docker compose -f docker-compose.monitoring-rpi.yml up -d

# 서비스 상태 확인
docker compose -f docker-compose.monitoring-rpi.yml ps

# Caddy 로그 확인 (Let's Encrypt 인증서 발급 확인)
docker compose -f docker-compose.monitoring-rpi.yml logs caddy

# 성공 로그 예시:
# certificate obtained successfully
# serving initial configuration
```

**주의사항:**
- DNS 전파 시간: DuckDNS는 빠르지만 최대 5분 소요 가능
- 포트포워딩 확인: 공유기 설정이 올바른지 재확인
- 인증서 발급 실패 시: Caddy 로그에서 상세 에러 확인

### 5. Auto-trader 앱 시작

```bash
# systemd 서비스로 등록 (자동 재시작)
sudo tee /etc/systemd/system/auto-trader.service > /dev/null <<EOF
[Unit]
Description=Auto Trader Application
After=network.target postgresql.service redis.service

[Service]
Type=simple
User=$USER
WorkingDirectory=$HOME/auto_trader
Environment="PATH=$HOME/.local/bin:/usr/local/bin:/usr/bin:/bin"
ExecStart=$HOME/.local/bin/uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
EOF

# 서비스 활성화 및 시작
sudo systemctl daemon-reload
sudo systemctl enable auto-trader.service
sudo systemctl start auto-trader.service

# 상태 확인
sudo systemctl status auto-trader.service

# 로그 확인
sudo journalctl -u auto-trader.service -f
```

### 6. 접속 확인

![배포 전후 비교](images/deployment_before_after.png)
*localhost vs HTTPS 도메인 접속 비교*

**HTTPS 접속 (프로덕션):**
```bash
# 브라우저에서:
https://your-domain.duckdns.org           # Auto-trader 앱
https://your-domain.duckdns.org/grafana   # Grafana 대시보드

# 명령줄에서 테스트:
curl -I https://your-domain.duckdns.org
# HTTP/2 200 OK 확인

# 인증서 확인
echo | openssl s_client -connect your-domain.duckdns.org:443 2>/dev/null | \
  openssl x509 -noout -dates -subject -issuer
# subject=CN=your-domain.duckdns.org
# issuer=C=US, O=Let's Encrypt, CN=R10
# notBefore=...
# notAfter=... (3개월 후)
```

**내부 접속 (개발/디버깅):**
```bash
http://localhost:8000           # Auto-trader 앱
http://localhost:3000           # Grafana (직접)
```

## 테스트 및 검증

### 자동화된 테스트 스크립트

프로젝트에 포함된 검증 스크립트로 전체 스택을 테스트할 수 있습니다:

```bash
# 실행 권한 부여
chmod +x scripts/test-caddy-https.sh

# 전체 HTTPS 및 보안 테스트 실행
bash scripts/test-caddy-https.sh your-domain.duckdns.org

# 또는 .env의 DOMAIN_NAME 자동 사용
bash scripts/test-caddy-https.sh
```

**테스트 항목 (자동 검증):**

1. ✅ Caddy 서비스 실행 상태
2. ✅ HTTP → HTTPS 자동 리디렉션
3. ✅ HTTPS 접속 및 SSL 인증서
4. ✅ 보안 헤더 (HSTS, X-Content-Type-Options 등)
5. ✅ Grafana 서브패스 접근 (`/grafana`)
6. ✅ Auto-trader 앱 접근
7. ✅ Rate limiting 작동 여부
8. ✅ 환경 변수 설정

**출력 예시:**

```bash
========================================
  Caddy HTTPS 테스트 시작
========================================
🔍 테스트할 도메인: your-domain.duckdns.org

[1/10] Caddy 컨테이너 상태 확인...
✅ Caddy 컨테이너가 실행 중입니다

[2/10] 환경 변수 설정 확인...
✅ DOMAIN_NAME: your-domain.duckdns.org
✅ ACME_EMAIL: your_email@example.com

[3/10] HTTP → HTTPS 리디렉션 확인...
✅ HTTP가 HTTPS로 리디렉션됩니다

[4/10] HTTPS 접속 테스트...
✅ HTTPS 접속 성공 (200 OK)

[5/10] SSL 인증서 확인...
✅ SSL 인증서가 유효합니다
  발급자: Let's Encrypt
  만료일: 2025-02-20

[6/10] 보안 헤더 확인...
✅ Strict-Transport-Security 헤더 존재
✅ X-Content-Type-Options 헤더 존재

[7/10] Grafana 서브패스 접근 확인...
✅ /grafana 경로 접근 성공

[8/10] Auto-trader 앱 접근 확인...
✅ Auto-trader 앱 응답 정상

[9/10] Rate Limiting 테스트...
✅ Rate limiting이 작동합니다

[10/10] Favicon 제공 확인...
✅ Favicon이 제공됩니다

========================================
  모든 테스트 통과! ✨
========================================
```

### 수동 테스트 (개별 검증)

**1. HTTPS 접속 테스트:**
```bash
# HTTP가 HTTPS로 리디렉션되는지 확인
curl -I http://your-domain.duckdns.org
# Location: https://your-domain.duckdns.org/ 확인

# HTTPS 직접 접속
curl -I https://your-domain.duckdns.org
# HTTP/2 200 확인
```

**2. 보안 헤더 검증:**
```bash
curl -I https://your-domain.duckdns.org | grep -E "Strict-Transport-Security|X-Content-Type-Options"
# Strict-Transport-Security: max-age=31536000; includeSubDomains; preload
# X-Content-Type-Options: nosniff
```

**3. Rate Limiting 테스트:**
```bash
# 짧은 시간에 많은 요청 전송 (600회)
for i in {1..600}; do
  curl -s -o /dev/null -w "%{http_code}\n" https://your-domain.duckdns.org
done

# 500회 이후 429 (Too Many Requests) 응답 확인
```

**4. Grafana 서브패스 테스트:**
```bash
curl -I https://your-domain.duckdns.org/grafana/login
# HTTP/2 200 확인
# Location 헤더가 /grafana로 시작하는지 확인
```

## 보안 강화

### 1. 방화벽 설정

라즈베리파이에서 필요한 포트만 열어두기:

```bash
# UFW (Uncomplicated Firewall) 설치
sudo apt install ufw -y

# 기본 정책: 모든 incoming 차단, outgoing 허용
sudo ufw default deny incoming
sudo ufw default allow outgoing

# SSH 허용 (원격 접속용)
sudo ufw allow ssh

# HTTP/HTTPS 허용 (Caddy용)
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# 방화벽 활성화
sudo ufw enable

# 상태 확인
sudo ufw status verbose
```

**출력 예시:**
```
Status: active
Logging: on (low)
Default: deny (incoming), allow (outgoing), disabled (routed)
New profiles: skip

To                         Action      From
--                         ------      ----
22/tcp                     ALLOW IN    Anywhere
80/tcp                     ALLOW IN    Anywhere
443/tcp                    ALLOW IN    Anywhere
```

### 2. Fail2Ban 설치 (Brute Force 방어)

SSH 및 웹 서비스에 대한 무차별 대입 공격 방어:

```bash
# Fail2Ban 설치
sudo apt install fail2ban -y

# 설정 파일 생성
sudo tee /etc/fail2ban/jail.local > /dev/null <<EOF
[DEFAULT]
bantime = 3600
findtime = 600
maxretry = 5

[sshd]
enabled = true
port = ssh
logpath = /var/log/auth.log

[caddy]
enabled = true
port = http,https
logpath = /var/lib/docker/volumes/caddy_data/_data/logs/*.log
maxretry = 10
EOF

# Fail2Ban 시작 및 자동 시작 설정
sudo systemctl enable fail2ban
sudo systemctl start fail2ban

# 상태 확인
sudo fail2ban-client status
```

### 3. Grafana 관리자 비밀번호 변경

기본 비밀번호(`admin`)는 즉시 변경:

```bash
# .env 파일에서 강력한 비밀번호 설정
GRAFANA_ADMIN_PASSWORD=your_strong_password_here

# Grafana 재시작
docker compose -f docker-compose.monitoring-rpi.yml restart grafana
```

또는 Grafana UI에서 직접 변경:
1. https://your-domain.com/grafana 접속
2. admin/admin으로 로그인
3. 새 비밀번호 설정 프롬프트에서 변경

### 4. 정기 보안 업데이트

```bash
# 자동 보안 업데이트 설정
sudo apt install unattended-upgrades -y
sudo dpkg-reconfigure --priority=low unattended-upgrades

# Docker 이미지 정기 업데이트 (주 1회 권장)
# crontab -e
0 3 * * 0 cd $HOME/auto_trader && docker compose -f docker-compose.monitoring-rpi.yml pull && docker compose -f docker-compose.monitoring-rpi.yml up -d
```

## 운영 및 유지보수

### 1. 로그 확인

**Caddy 로그:**
```bash
# 실시간 로그
docker compose -f docker-compose.monitoring-rpi.yml logs -f caddy

# 특정 패턴 검색 (예: 에러)
docker compose -f docker-compose.monitoring-rpi.yml logs caddy | grep -i error

# 로그 파일 직접 확인 (JSON 형식)
docker exec caddy cat /data/logs/access.log | jq .
```

**Auto-trader 앱 로그:**
```bash
# systemd 로그
sudo journalctl -u auto-trader.service -f

# 최근 100줄
sudo journalctl -u auto-trader.service -n 100

# 특정 시간 범위
sudo journalctl -u auto-trader.service --since "1 hour ago"
```

**Grafana 스택 로그:**
```bash
# 모든 컨테이너 로그
docker compose -f docker-compose.monitoring-rpi.yml logs -f

# 특정 서비스 로그
docker compose -f docker-compose.monitoring-rpi.yml logs -f grafana
docker compose -f docker-compose.monitoring-rpi.yml logs -f loki
```

### 2. 서비스 재시작

**전체 스택 재시작:**
```bash
# Grafana 스택
docker compose -f docker-compose.monitoring-rpi.yml restart

# Auto-trader 앱
sudo systemctl restart auto-trader.service
```

**특정 서비스만 재시작:**
```bash
# Caddy만
docker compose -f docker-compose.monitoring-rpi.yml restart caddy

# Grafana만
docker compose -f docker-compose.monitoring-rpi.yml restart grafana
```

### 3. 디스크 공간 관리

```bash
# 디스크 사용량 확인
df -h

# Docker 디스크 사용량
docker system df

# 사용하지 않는 Docker 리소스 정리
docker system prune -a

# 볼륨 확인
docker volume ls

# 특정 볼륨 삭제 (주의: 데이터 손실)
docker volume rm volume_name
```

### 4. 백업

**중요한 데이터:**
1. PostgreSQL 데이터베이스
2. Caddy 인증서 (`caddy_data` 볼륨)
3. Grafana 설정 (`grafana_data` 볼륨)
4. 환경 변수 파일 (`.env`)

**백업 스크립트:**

```bash
#!/bin/bash
# backup.sh

BACKUP_DIR=$HOME/backups/$(date +%Y%m%d)
mkdir -p $BACKUP_DIR

# PostgreSQL 백업
sudo -u postgres pg_dump autotrader > $BACKUP_DIR/database.sql

# Caddy 인증서 백업
docker run --rm -v caddy_data:/data -v $BACKUP_DIR:/backup alpine \
  tar czf /backup/caddy_certificates.tar.gz -C /data certificates

# Grafana 데이터 백업
docker run --rm -v grafana_data:/data -v $BACKUP_DIR:/backup alpine \
  tar czf /backup/grafana_data.tar.gz -C /data .

# 환경 변수 백업
cp .env $BACKUP_DIR/env

echo "✅ 백업 완료: $BACKUP_DIR"
```

**자동 백업 설정:**
```bash
# crontab -e
# 매일 새벽 2시 백업
0 2 * * * $HOME/auto_trader/backup.sh
```

### 5. 모니터링

**Grafana 대시보드:**
- https://your-domain.com/grafana
- Tempo: 트레이스 확인
- Loki: 로그 검색
- Prometheus: 메트릭 분석

**시스템 리소스 모니터링:**
```bash
# CPU, 메모리 사용량
htop

# Docker 컨테이너 리소스
docker stats

# 네트워크 연결
sudo netstat -tulpn | grep -E '80|443|8000|3000'
```

**Telegram 알림:**
- 에러 발생 시 즉시 알림
- 거래 체결 알림
- 시스템 장애 알림

## 트러블슈팅

### 문제 1: Let's Encrypt 인증서 발급 실패

**증상:**
```bash
docker compose -f docker-compose.monitoring-rpi.yml logs caddy
# acme: error: ... unable to get challenge ...
```

**원인 및 해결:**

1. **DNS 미전파**
   ```bash
   # DNS 전파 확인
   nslookup your-domain.duckdns.org
   # 공유기 IP가 맞는지 확인

   # 해결: DNS 전파 대기 (최대 48시간, 보통 5분 이내)
   ```

2. **포트포워딩 미설정**
   ```bash
   # 외부에서 포트 접근 테스트
   # 다른 네트워크(모바일 데이터)에서:
   curl -I http://your-domain.duckdns.org

   # 해결: 공유기에서 포트 80, 443 포워딩 재확인
   ```

3. **Let's Encrypt 속도 제한**
   ```bash
   # 증상: "too many certificates already issued"

   # 해결: ZeroSSL로 전환
   # Caddyfile에 추가:
   {
     acme_ca https://acme.zerossl.com/v2/DV90
   }
   ```

### 문제 2: Grafana 서브패스 404 에러

**증상:**
- https://your-domain.com/grafana → 404 Not Found

**해결:**

```bash
# 1. 환경 변수 확인
docker compose -f docker-compose.monitoring-rpi.yml exec grafana env | grep GF_SERVER_ROOT_URL
# GF_SERVER_ROOT_URL=https://your-domain.duckdns.org/grafana 확인

# 2. 없다면 .env 파일에 DOMAIN_NAME 추가
echo "DOMAIN_NAME=your-domain.duckdns.org" >> .env

# 3. Grafana 재시작
docker compose -f docker-compose.monitoring-rpi.yml restart grafana
```

### 문제 3: Auto-trader 502 Bad Gateway

**증상:**
- https://your-domain.com → 502 Bad Gateway

**원인:**
- Auto-trader 앱이 포트 8000에서 실행되지 않음

**해결:**

```bash
# 1. Auto-trader 실행 확인
curl http://localhost:8000
# 실패 시:

# 2. 서비스 상태 확인
sudo systemctl status auto-trader.service

# 3. 로그 확인
sudo journalctl -u auto-trader.service -n 50

# 4. 수동 실행 (디버깅용)
cd $HOME/auto_trader
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000

# 5. 서비스 재시작
sudo systemctl restart auto-trader.service
```

### 문제 4: 메모리 부족

**증상:**
- 시스템이 느려지거나 서비스가 OOM(Out of Memory)으로 종료됨

**해결:**

```bash
# 1. 메모리 사용량 확인
free -h

# 2. Docker 컨테이너별 메모리 사용량
docker stats

# 3. 리소스 제한 조정
# docker-compose.monitoring-rpi.yml에서:
deploy:
  resources:
    limits:
      memory: 256m  # 512m → 256m으로 줄이기

# 4. Swap 메모리 추가 (Raspberry Pi)
sudo dphys-swapfile swapoff
sudo nano /etc/dphys-swapfile
# CONF_SWAPSIZE=2048 (2GB로 증가)
sudo dphys-swapfile setup
sudo dphys-swapfile swapon
```

### 문제 5: 인증서 자동 갱신 실패

**증상:**
- 인증서 만료 후에도 갱신되지 않음

**해결:**

```bash
# 1. Caddy 로그 확인
docker compose -f docker-compose.monitoring-rpi.yml logs caddy | grep -i renew

# 2. Caddy 재시작으로 강제 갱신
docker compose -f docker-compose.monitoring-rpi.yml restart caddy

# 3. 인증서 만료일 확인
echo | openssl s_client -connect your-domain.duckdns.org:443 2>/dev/null | \
  openssl x509 -noout -dates

# 4. 여전히 실패 시: 볼륨 삭제 후 재발급
docker compose -f docker-compose.monitoring-rpi.yml down
docker volume rm caddy_data
docker compose -f docker-compose.monitoring-rpi.yml up -d
```

## 성능 최적화

### 1. Raspberry Pi 5 최적화

**CPU Governor 설정:**
```bash
# 성능 모드로 변경
echo "performance" | sudo tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor

# 부팅 시 자동 적용
sudo tee /etc/rc.local > /dev/null <<EOF
#!/bin/sh -e
echo "performance" | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
exit 0
EOF
sudo chmod +x /etc/rc.local
```

**GPU 메모리 줄이기 (헤드리스 서버):**
```bash
# /boot/config.txt 수정
sudo nano /boot/config.txt

# 추가:
gpu_mem=16

# 재부팅
sudo reboot
```

### 2. Docker 최적화

**로그 드라이버 설정:**
```bash
# /etc/docker/daemon.json 생성
sudo tee /etc/docker/daemon.json > /dev/null <<EOF
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
EOF

# Docker 재시작
sudo systemctl restart docker
```

**BuildKit 활성화:**
```bash
# ~/.bashrc에 추가
export DOCKER_BUILDKIT=1
```

### 3. PostgreSQL 튜닝

```bash
# /etc/postgresql/*/main/postgresql.conf
sudo nano /etc/postgresql/*/main/postgresql.conf

# Raspberry Pi 5 (8GB) 권장 설정:
shared_buffers = 256MB
effective_cache_size = 2GB
maintenance_work_mem = 128MB
checkpoint_completion_target = 0.9
wal_buffers = 16MB
default_statistics_target = 100
random_page_cost = 1.1
effective_io_concurrency = 200
work_mem = 4MB
min_wal_size = 1GB
max_wal_size = 4GB

# PostgreSQL 재시작
sudo systemctl restart postgresql
```

## 비용 분석

### 초기 비용

| 항목 | 가격 |
|------|------|
| Raspberry Pi 5 (8GB) | $80 |
| microSD 카드 (64GB) | $10 |
| 전원 어댑터 | $10 |
| 케이스 (선택사항) | $10 |
| **총 초기 비용** | **$110** |

### 운영 비용 (월)

| 항목 | 가격 |
|------|------|
| 전기세 (15W × 24h × 30일 × 300원/kWh) | ₩3,240 (~$2.50) |
| DuckDNS (무료 DDNS) | $0 |
| Let's Encrypt (무료 SSL) | $0 |
| **총 월 비용** | **~₩3,300 ($2.50)** |

### 클라우드 비교 (월)

| 서비스 | 스펙 | 월 비용 |
|--------|------|---------|
| AWS EC2 (t4g.small) | 2 vCPU, 2GB | $13 |
| GCP Compute Engine (e2-small) | 2 vCPU, 2GB | $14 |
| DigitalOcean (Basic) | 2 vCPU, 2GB | $12 |
| **라즈베리파이** | **4 cores, 8GB** | **$2.50** |

**투자 회수 기간:**
- 초기 비용 $110 ÷ 절감액 $10/월 = **11개월**
- 1년 후부터는 순수익!

## 마치며

### 배운 교훈

이번 배포 과정을 통해 가장 크게 배운 점:

> **"프로덕션 배포는 단순히 서버에 올리는 것이 아니다"**

처음에는 "그냥 Docker Compose로 띄우면 되지 않나?"라고 생각했습니다.

하지만 실제로는:
- HTTPS 인증서 자동 발급/갱신
- Reverse Proxy 설정
- 보안 헤더 적용
- Rate Limiting
- 로그 관리
- 자동 재시작
- 리소스 최적화
- 백업 전략

등 **고려해야 할 것이 산더미**처럼 많았습니다.

### Caddy의 마법

**Before (nginx 수동 설정):**
```
1. nginx 설정 파일 작성 (100줄)
2. Let's Encrypt 인증서 수동 발급
3. cron으로 인증서 자동 갱신 스크립트
4. HTTP → HTTPS 리디렉션 설정
5. 보안 헤더 일일이 추가
→ 설정 파일 복잡, 관리 어려움
```

**After (Caddy 자동 설정):**
```
1. Caddyfile 작성 (30줄)
→ 끝!
- HTTPS 자동
- 인증서 자동 갱신
- 리디렉션 자동
- 보안 헤더 자동
```

Caddy 덕분에 **복잡한 인프라 설정을 단순화**할 수 있었습니다!

### 실전에서 체감한 효과

**Before (localhost 개발):**
```
[노트북] "uvicorn으로 실행... 테스트... 괜찮네"
[노트북 종료] 시스템 중지
[외출 중] "집에서 확인해야 하는데..."
```

**After (라즈베리파이 배포):**
```
[라즈베리파이] 24시간 실행
[스마트폰] https://your-domain.duckdns.org 접속
  → 거래 현황 확인
  → Grafana로 모니터링
  → 언제 어디서나 접근
[Telegram] 에러 발생 시 즉시 알림
```

### 투자 대비 효과

**투자:**
- 초기 비용: $110 (라즈베리파이)
- 개발 시간: 3일 (Caddy 설정, 배포 자동화)
- 학습 시간: 2일 (HTTPS, Reverse Proxy)

**효과:**
- 월 클라우드 비용 절감: $10~50
- 24시간 자동 운영
- 언제 어디서나 접근 가능
- 인프라 직접 경험 → 실력 향상
- 홈서버 운영의 재미 😊

### 다음 단계

이제 우리의 자동매매 시스템은:
- ✅ 데이터 수집 (한투/Upbit/yfinance)
- ✅ AI 분석 (Gemini)
- ✅ DB 저장 및 정규화
- ✅ 웹 대시보드
- ✅ 모니터링 (Grafana Stack)
- ✅ 프로덕션 배포 (HTTPS + 24시간 운영)

**모두 완성**되었습니다!

**추가로 고려할 수 있는 기능:**
- CI/CD 파이프라인 (GitHub Actions)
- 자동 백트레이딩 (과거 데이터 시뮬레이션)
- 멀티 전략 포트폴리오
- ML 모델 성능 분석 대시보드
- Slack/Discord 알림 연동

하지만 가장 중요한 것은:

> **"지금 이 시스템을 실제로 운영하면서 개선해나가는 것"**

이론보다 실전, 계획보다 실행!

이제 여러분의 자동매매 시스템이 24시간 안전하게 돌아갑니다. 🎉

---

**참고 자료:**
- [Caddy 공식 문서](https://caddyserver.com/docs/)
- [Docker Compose 문서](https://docs.docker.com/compose/)
- [DuckDNS 가이드](https://www.duckdns.org/)
- [Let's Encrypt 문서](https://letsencrypt.org/docs/)
- [Raspberry Pi 공식 문서](https://www.raspberrypi.com/documentation/)
- [전체 프로젝트 코드 (GitHub)](https://github.com/mgh3326/auto_trader)
- [PR #71: Caddy HTTPS 및 Reverse Proxy 추가](https://github.com/mgh3326/auto_trader/pull/71)
- [DEPLOYMENT.md (상세 배포 가이드)](https://github.com/mgh3326/auto_trader/blob/main/DEPLOYMENT.md)
