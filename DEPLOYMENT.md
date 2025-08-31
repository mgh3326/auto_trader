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
sudo curl -L "https://github.com/docker/compose/releases/download/v2.24.0/docker-compose-$(uname -s)-$(uname -m)" -o /usr/local/bin/docker-compose
sudo chmod +x /usr/local/bin/docker-compose

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
docker-compose -f docker-compose.prod.yml pull

# 서비스 실행
docker-compose -f docker-compose.prod.yml up -d

# 로그 확인
docker-compose -f docker-compose.prod.yml logs -f
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
ExecStart=/usr/local/bin/docker-compose -f docker-compose.prod.yml up -d
ExecStop=/usr/local/bin/docker-compose -f docker-compose.prod.yml down
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
docker-compose -f docker-compose.prod.yml pull

# 서비스 재시작
docker-compose -f docker-compose.prod.yml up -d

echo "✅ 업데이트 완료!"
EOF

chmod +x update-auto-trader.sh
```

### 수동 업데이트

```bash
# 1. 서비스 중지
docker-compose -f docker-compose.prod.yml down

# 2. 최신 코드 가져오기
git pull origin production

# 3. 최신 이미지 가져오기
docker-compose -f docker-compose.prod.yml pull

# 4. 서비스 재시작
docker-compose -f docker-compose.prod.yml up -d
```

## 📊 모니터링

### 헬스체크

```bash
# API 상태 확인
curl http://localhost:8000/healthz

# 컨테이너 상태 확인
docker-compose -f docker-compose.prod.yml ps

# 로그 확인
docker-compose -f docker-compose.prod.yml logs api
docker-compose -f docker-compose.prod.yml logs websocket
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
   docker-compose -f docker-compose.prod.yml down
   docker network prune
   docker-compose -f docker-compose.prod.yml up -d
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
