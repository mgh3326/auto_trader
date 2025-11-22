# 🚀 Auto Trader 배포 가이드

## GitHub Container Registry (GHCR) 자동 배포

### 📋 사전 준비

1. **GitHub 저장소 설정**
   - Repository가 public이거나 GitHub Pro/Organization 계정 필요
   - Actions 권한 확인: Settings > Actions > General > Workflow permissions

2. **GHCR 패키지 권한 설정**
   - Settings > Actions > General > Workflow permissions
   - "Read and write permissions" 선택

### 🔄 자동 배포 프로세스

#### 트리거 조건
- `production` 브랜치에 push
- GitHub Release 생성

#### 빌드되는 이미지
- **API 서버**: `ghcr.io/your-username/auto_trader:latest`
- **WebSocket 서버**: `ghcr.io/your-username/auto_trader-ws:latest`

### 🏗️ 배포 워크플로우

```yaml
# .github/workflows/deploy.yml
name: Deploy to GHCR
on:
  push:
    branches: [ production ]
  release:
    types: [ published ]
```

### 📦 생성되는 이미지 태그

| 이벤트 | API 이미지 태그 | WebSocket 이미지 태그 |
|--------|----------------|----------------------|
| Production 브랜치 | `ghcr.io/owner/repo:production` | `ghcr.io/owner/repo-ws:production` |
| Latest 태그 | `ghcr.io/owner/repo:latest` | `ghcr.io/owner/repo-ws:latest` |
| Release v1.0.0 | `ghcr.io/owner/repo:1.0.0` | `ghcr.io/owner/repo-ws:1.0.0` |

## 🖥️ Production 서버 배포

### 1. 서버 준비

```bash
# Docker 및 Docker Compose 설치
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER

# Docker Compose 설치
sudo curl -L "https://github.com/docker/compose/releases/download/v2.24.0/docker compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker compose
sudo chmod +x /usr/local/bin/docker compose

# 네이티브 PostgreSQL 및 Redis 설치
# 자세한 설치 방법은 NATIVE_SETUP.md 참고
```

### 2. 프로젝트 클론

```bash
git clone https://github.com/your-username/auto_trader.git
cd auto_trader
git checkout production
```

### 3. 환경 설정

```bash
# Production 환경변수 파일 생성
cp env.prod.example .env.prod

# 환경변수 편집
nano .env.prod
```

**필수 설정 항목:**
- `GITHUB_REPOSITORY`: GitHub 저장소명 (예: `your-username/auto_trader`)
- `DATABASE_URL`: 네이티브 PostgreSQL 연결 URL
- `REDIS_URL`: 네이티브 Redis 연결 URL
- API 키들 (KIS, Upbit, Telegram, Google, OpenDART)

**네이티브 DB/Redis 설정 참고**: [NATIVE_SETUP.md](NATIVE_SETUP.md)

### 4. 이미지 Pull 및 실행

```bash
# GitHub Container Registry 로그인 (필요시)
echo $GITHUB_TOKEN | docker login ghcr.io -u your-username --password-stdin

# 이미지 Pull
docker compose -f docker-compose.prod.yml pull

# 서비스 실행
docker compose -f docker-compose.prod.yml up -d

# 로그 확인
docker compose -f docker-compose.prod.yml logs -f
```

## 🔐 보안 설정

### 1. 방화벽 설정

```bash
# UFW 기본 설정
sudo ufw default deny incoming
sudo ufw default allow outgoing

# 필요한 포트만 허용
sudo ufw allow ssh
sudo ufw allow 8000/tcp  # API 포트 (필요시)
sudo ufw enable
```

### 2. Docker 보안

```bash
# 권한 제한된 사용자 생성
sudo useradd -m -s /bin/bash autotrader
sudo usermod -aG docker autotrader

# 서비스 파일 생성
sudo tee /etc/systemd/system/auto-trader.service > /dev/null <<EOF
[Unit]
Description=Auto Trader Application
Requires=docker.service
After=docker.service

[Service]
Type=oneshot
RemainAfterExit=yes
User=autotrader
WorkingDirectory=/home/autotrader/auto_trader
ExecStart=/usr/local/bin/docker compose -f docker-compose.prod.yml up -d
ExecStop=/usr/local/bin/docker compose -f docker-compose.prod.yml down
TimeoutStartSec=0

[Install]
WantedBy=multi-user.target
EOF

# 서비스 활성화
sudo systemctl enable auto-trader.service
sudo systemctl start auto-trader.service
```

## 🔄 업데이트 프로세스

### 자동 업데이트 (추천)

```bash
# 업데이트 스크립트 생성
cat > update-auto-trader.sh << 'EOF'
#!/bin/bash
set -e

echo "🔄 Auto Trader 업데이트 시작..."

# Git 업데이트
git fetch origin
git reset --hard origin/production

# 이미지 업데이트
docker compose -f docker-compose.prod.yml pull

# 서비스 재시작
docker compose -f docker-compose.prod.yml up -d

echo "✅ 업데이트 완료!"
EOF

chmod +x update-auto-trader.sh
```

### 수동 업데이트

```bash
# 1. 서비스 중지
docker compose -f docker-compose.prod.yml down

# 2. 최신 코드 가져오기
git pull origin production

# 3. 최신 이미지 가져오기
docker compose -f docker-compose.prod.yml pull

# 4. 서비스 재시작
docker compose -f docker-compose.prod.yml up -d
```

## 📊 모니터링

### 헬스체크

```bash
# API 상태 확인
curl http://localhost:8000/healthz

# 컨테이너 상태 확인
docker compose -f docker-compose.prod.yml ps

# 로그 확인
docker compose -f docker-compose.prod.yml logs api
docker compose -f docker-compose.prod.yml logs websocket
```

### 리소스 모니터링

```bash
# 컨테이너 리소스 사용량
docker stats

# 디스크 사용량
df -h
docker system df
```

## 🔧 문제 해결

### 일반적인 문제들

1. **이미지 Pull 실패**
   ```bash
   # GitHub 토큰으로 로그인
   echo $GITHUB_TOKEN | docker login ghcr.io -u your-username --password-stdin
   ```

2. **권한 오류**
   ```bash
   # 디렉토리 권한 확인
   sudo chown -R autotrader:autotrader /home/autotrader/auto_trader
   ```

3. **메모리 부족**
   ```bash
   # 사용하지 않는 Docker 리소스 정리
   docker system prune -a
   ```

4. **네트워크 문제**
   ```bash
   # Docker 네트워크 재생성
   docker compose -f docker-compose.prod.yml down
   docker network prune
   docker compose -f docker-compose.prod.yml up -d
   ```

## 📈 성능 최적화

### 1. 리소스 제한 조정

`docker-compose.prod.yml`에서 리소스 제한을 환경에 맞게 조정:

```yaml
deploy:
  resources:
    limits:
      memory: 2G      # 메모리 늘리기
      cpus: '1.0'     # CPU 늘리기
```

### 2. 로그 로테이션

```bash
# 로그 크기 제한
echo '{"log-driver":"json-file","log-opts":{"max-size":"10m","max-file":"3"}}' | sudo tee /etc/docker/daemon.json
sudo systemctl restart docker
```

이제 production 브랜치에 push하면 자동으로 GHCR에 이미지가 빌드되고 배포할 수 있습니다! 🚀

## 🌐 HTTPS 및 Reverse Proxy 설정 (Caddy)

### 개요

프로덕션 환경에서는 Caddy를 사용하여 자동 HTTPS 및 Reverse Proxy를 설정할 수 있습니다.
Caddy는 Let's Encrypt를 통해 자동으로 SSL/TLS 인증서를 발급하고 갱신합니다.

### 배포 전 필수 설정

**1. 환경 변수 설정 (.env 파일)**

```bash
# Caddy 설정
ACME_EMAIL=your_email@example.com        # Let's Encrypt 인증서 발급용 이메일
DOMAIN_NAME=your_domain.com              # 실제 도메인 (예: mgh3326.duckdns.org)
```

**2. DNS 레코드 설정**

도메인이 서버 IP를 가리키도록 DNS A 레코드를 설정해야 합니다:

```
A 레코드 예시:
  호스트: @ (또는 서브도메인)
  타입: A
  값: 123.456.789.012 (서버의 공인 IP)
  TTL: 3600 (또는 자동)
```

**DuckDNS 사용 시:**
- https://www.duckdns.org 에서 계정 생성
- 서브도메인 생성 (예: mgh3326)
- IP 주소를 서버의 공인 IP로 설정
- 최종 도메인: `mgh3326.duckdns.org`

**3. 방화벽 포트 열기**

Caddy가 HTTP(80)와 HTTPS(443) 요청을 받을 수 있도록 방화벽 설정:

```bash
# Ubuntu/Debian (ufw)
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw reload

# CentOS/RHEL (firewalld)
sudo firewall-cmd --permanent --add-service=http
sudo firewall-cmd --permanent --add-service=https
sudo firewall-cmd --reload

# 클라우드 환경 (AWS, GCP 등)
# 보안 그룹/방화벽 규칙에서 포트 80, 443 인바운드 허용 필요
```

### Caddy 스택 시작

```bash
# 1. Caddy를 포함한 모니터링 스택 시작
docker compose -f docker-compose.monitoring-rpi.yml up -d

# 2. 서비스 상태 확인
docker compose -f docker-compose.monitoring-rpi.yml ps

# 3. Caddy 로그 확인 (인증서 발급 확인)
docker compose -f docker-compose.monitoring-rpi.yml logs caddy

# 4. 인증서 자동 갱신 확인
# Caddy는 자동으로 인증서를 갱신하므로 별도 작업 불필요
```

### 접근 경로

**HTTPS 접근 (프로덕션):**
- Auto-trader 앱: `https://your_domain.com`
- Grafana: `https://your_domain.com/grafana`

**직접 접근 (개발/내부):**
- Auto-trader 앱: `http://localhost:8000`
- Grafana: `http://localhost:3000`

### 아키텍처

```
인터넷
  ↓
Caddy (포트 80, 443)
  ├─→ https://domain.com → host.docker.internal:8000 (Auto-trader)
  └─→ https://domain.com/grafana → grafana:3000 (Grafana 컨테이너)
```

**주요 특징:**
- Auto-trader는 호스트에서 실행되므로 `host.docker.internal` 사용
- Grafana는 Docker 네트워크 내부이므로 서비스 이름(`grafana`) 사용
- 모든 트래픽은 자동으로 HTTPS로 리디렉션

### 보안 설정

Caddy는 자동으로 다음 보안 헤더를 적용합니다:

```
- Strict-Transport-Security: 1년간 HTTPS 강제
- X-Content-Type-Options: MIME 스니핑 방지
- X-Frame-Options: 클릭재킹 방지
- X-XSS-Protection: XSS 공격 방어 (deprecated, CSP 사용 권장)
- Referrer-Policy: 리퍼러 정보 제어
- Rate Limiting: DDoS/Brute Force 공격 방지
```

### 인증서 백업

Let's Encrypt 인증서는 `caddy_data` Docker 볼륨에 저장됩니다:

```bash
# 인증서 백업
docker run --rm -v caddy_data:/data -v $(pwd):/backup alpine \
  tar czf /backup/caddy_certificates_$(date +%Y%m%d).tar.gz -C /data certificates

# 인증서 복원 (필요시)
docker run --rm -v caddy_data:/data -v $(pwd):/backup alpine \
  tar xzf /backup/caddy_certificates_YYYYMMDD.tar.gz -C /data

# 볼륨 위치 확인
docker volume inspect caddy_data
```

**중요:** 인증서는 정기적으로 백업하고, 서버 이전 시 반드시 복원해야 합니다.

### 테스트 및 검증

**자동화된 테스트 스크립트 (권장)**

```bash
# 전체 HTTPS 및 보안 테스트 실행 (실행 권한 부여 필요)
chmod +x scripts/test-caddy-https.sh
bash scripts/test-caddy-https.sh your_domain.com

# 또는 .env의 DOMAIN_NAME 사용
bash scripts/test-caddy-https.sh

# localhost 테스트 (개발 환경)
bash scripts/test-caddy-https.sh localhost
```

이 스크립트는 다음을 자동으로 검증합니다:
- Caddy 서비스 실행 상태
- HTTP → HTTPS 리디렉션
- HTTPS 접속 및 SSL 인증서
- 보안 헤더 (HSTS, X-Content-Type-Options 등)
- Grafana 서브패스 접근
- Auto-trader 앱 접근
- Rate limiting 작동 여부
- 환경 변수 설정

**수동 테스트 (개별 검증 필요 시)**

**1. HTTPS 접속 테스트**
```bash
# HTTP가 HTTPS로 리디렉션되는지 확인
curl -I http://your_domain.com

# HTTPS 직접 접속
curl -I https://your_domain.com
```

**2. 보안 헤더 검증**
```bash
curl -I https://your_domain.com | grep -E "Strict-Transport-Security|X-Content-Type-Options"
```

**3. 인증서 유효성 확인**
```bash
echo | openssl s_client -connect your_domain.com:443 2>/dev/null | \
  openssl x509 -noout -dates -subject -issuer
```

**4. Grafana 서브패스 접근**
```bash
curl -I https://your_domain.com/grafana/login
```

### 문제 해결

**1. Let's Encrypt 인증서 발급 실패**

**증상:** Caddy 로그에 ACME 에러 메시지
```bash
docker compose -f docker-compose.monitoring-rpi.yml logs caddy | grep -i error
```

**원인 및 해결:**
- DNS가 올바르게 설정되지 않음 → DNS 전파 대기 (최대 48시간)
- 포트 80/443이 닫혀있음 → 방화벽 규칙 확인
- Let's Encrypt 속도 제한 → ZeroSSL로 전환 (Caddyfile에서 `acme ca https://acme.zerossl.com/v2/DV90` 추가)

**2. Grafana 서브패스 404 에러**

**원인:** `GF_SERVER_ROOT_URL` 설정 불일치

**해결:**
```bash
# .env 파일에서 DOMAIN_NAME 확인
grep DOMAIN_NAME .env

# Grafana 환경변수 확인
docker compose -f docker-compose.monitoring-rpi.yml exec grafana env | grep GF_SERVER_ROOT_URL
```

**3. Auto-trader 연결 실패 (502 Bad Gateway)**

**원인:** Auto-trader가 포트 8000에서 실행되지 않음

**해결:**
```bash
# Auto-trader 실행 확인
curl http://localhost:8000

# 실행되지 않았다면 시작
uv run uvicorn app.main:app --host 0.0.0.0 --port 8000
```

**4. 인증서 갱신 실패**

Caddy는 만료 30일 전 자동 갱신하지만, 실패 시:

```bash
# Caddy 재시작으로 강제 갱신 시도
docker compose -f docker-compose.monitoring-rpi.yml restart caddy

# 로그 확인
docker compose -f docker-compose.monitoring-rpi.yml logs -f caddy
```
