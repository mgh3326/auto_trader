# 라즈베리파이 5 배포 가이드

라즈베리파이 5 (8GB RAM + SSD)에서 auto_trader를 실행하기 위한 가이드입니다.

## 빠른 시작 (TL;DR)

```bash
# 1. 저장소 클론 및 환경 설정
git clone <your-repo> ~/auto_trader && cd ~/auto_trader
cp env.example .env.prod
nano .env.prod  # 환경 변수 설정

# 2. SigNoz 시작 (모니터링)
docker compose -f docker-compose.monitoring-rpi.yml up -d

# 3. 마이그레이션 (최초 1회)
docker compose -f docker-compose.prod.yml --profile migration up migration

# 4. 애플리케이션 시작
docker compose -f docker-compose.prod.yml up -d

# 5. 확인
docker ps
curl http://localhost:8000/health
open http://localhost:3301  # SigNoz UI
```

## 하드웨어 요구사항

- **라즈베리파이 5** (권장)
- **RAM**: 8GB (최소 4GB)
- **스토리지**: SSD 128GB 이상 (SD 카드 사용 시 성능 저하)
- **운영체제**: Raspberry Pi OS (64-bit) 또는 Ubuntu Server 22.04 ARM64

## 아키텍처

```
라즈베리파이 5 (8GB RAM + 128GB SSD)
├── PostgreSQL (네이티브) - 포트 5432
├── Redis (네이티브) - 포트 6379
├── Python App (네이티브) - 포트 8000
└── SigNoz (Docker) - 메모리 최적화
    ├── Zookeeper (384MB 제한)
    ├── ClickHouse (2GB 제한)
    ├── OTEL Collector (512MB 제한)
    ├── Query Service (768MB 제한)
    └── Frontend (256MB 제한)
    -----------------------------------
    총 메모리: ~4GB (8GB 중 50%)
```

## 1. 시스템 준비

### 1.1 시스템 업데이트

```bash
sudo apt update && sudo apt upgrade -y
```

### 1.2 필수 패키지 설치

```bash
# Python 및 개발 도구
sudo apt install -y python3.11 python3.11-venv python3-pip git

# Docker 설치
curl -fsSL https://get.docker.com -o get-docker.sh
sudo sh get-docker.sh
sudo usermod -aG docker $USER
newgrp docker

# Docker Compose V2
sudo apt install -y docker-compose-plugin
```

### 1.3 PostgreSQL 네이티브 설치 (이미 완료)

```bash
# 상태 확인
sudo systemctl status postgresql

# 외부 접속 허용 (필요시)
sudo nano /etc/postgresql/*/main/postgresql.conf
# listen_addresses = '*'

sudo nano /etc/postgresql/*/main/pg_hba.conf
# host all all 0.0.0.0/0 md5

sudo systemctl restart postgresql
```

### 1.4 Redis 네이티브 설치 (이미 완료)

```bash
# 상태 확인
sudo systemctl status redis-server

# 메모리 최적화
sudo nano /etc/redis/redis.conf
# maxmemory 512mb
# maxmemory-policy allkeys-lru

sudo systemctl restart redis-server
```

## 2. 애플리케이션 배포

### 2.1 코드 클론

```bash
cd ~
git clone https://github.com/your-username/auto_trader.git
cd auto_trader
```

### 2.2 UV 설치 (Python 패키지 관리자)

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
source $HOME/.cargo/env
```

### 2.3 의존성 설치

```bash
# 프로덕션 의존성만
uv sync

# 개발 도구 포함
uv sync --all-groups
```

### 2.4 환경 변수 설정

```bash
cp env.example .env.prod
nano .env.prod
```

**라즈베리파이용 .env.prod 설정:**
```bash
# 데이터베이스 (네이티브 PostgreSQL, Docker host 네트워크로 localhost 접근)
DATABASE_URL=postgresql+asyncpg://user:password@localhost:5432/auto_trader

# Redis (네이티브, Docker host 네트워크로 localhost 접근)
REDIS_URL=redis://localhost:6379/0

# SigNoz (Docker 컨테이너, localhost로 접근 가능)
SIGNOZ_ENABLED=true
SIGNOZ_ENDPOINT=localhost:4317
OTEL_SERVICE_NAME=auto-trader-rpi5
OTEL_ENVIRONMENT=production

# Telegram 알림
TELEGRAM_TOKEN=your_bot_token
TELEGRAM_CHAT_ID=your_chat_id
ERROR_REPORTING_ENABLED=true

# GitHub Container Registry (Docker 이미지)
GITHUB_REPOSITORY=your-username/auto_trader

# API 포트
API_PORT=8000

# API 키들
KIS_APP_KEY=your_key
KIS_APP_SECRET=your_secret
UPBIT_ACCESS_KEY=your_key
UPBIT_SECRET_KEY=your_secret
GOOGLE_API_KEY=your_key
```

### 2.5 Docker 이미지 Pull

```bash
# GitHub Container Registry에서 프로덕션 이미지 다운로드
export GITHUB_REPOSITORY=your-username/auto_trader
docker pull ghcr.io/${GITHUB_REPOSITORY}:production

# 또는 로컬에서 빌드 (선택사항)
docker build -t ghcr.io/${GITHUB_REPOSITORY}:production .
```

## 3. SigNoz 배포 (Docker)

### 3.1 라즈베리파이용 SigNoz 시작

```bash
# 메모리 최적화된 라즈베리파이 전용 설정 사용
docker compose -f docker-compose.monitoring-rpi.yml up -d
```

### 3.2 상태 확인

```bash
# 컨테이너 상태
docker compose -f docker-compose.monitoring-rpi.yml ps

# 메모리 사용량 확인
docker stats

# 로그 확인
docker compose -f docker-compose.monitoring-rpi.yml logs -f
```

### 3.3 SigNoz UI 접속

브라우저에서 접속:
```
http://raspberrypi.local:3301
또는
http://192.168.x.x:3301
```

## 4. 애플리케이션 배포 (Docker)

### 4.1 데이터베이스 마이그레이션 (최초 1회)

```bash
# 마이그레이션 프로파일로 실행
docker compose -f docker-compose.prod.yml --profile migration up migration

# 성공 확인
# ✅ Migrations completed successfully
```

### 4.2 프로덕션 배포

```bash
# 전체 스택 시작 (API + Worker + WebSocket)
docker compose -f docker-compose.prod.yml up -d

# 상태 확인
docker compose -f docker-compose.prod.yml ps
```

**실행되는 컨테이너:**
- `auto_trader_api_prod`: FastAPI 서버 (포트 8000)
- `auto_trader_worker_prod`: Celery Worker (백그라운드 작업)
- `auto_trader_ws_prod`: Upbit WebSocket Monitor

### 4.3 전체 스택 시작 (한 번에)

```bash
# 1. SigNoz 시작 (백그라운드)
docker compose -f docker-compose.monitoring-rpi.yml up -d

# 2. Python App 시작
docker compose -f docker-compose.prod.yml up -d

# 3. 전체 상태 확인
docker ps -a
docker stats
```

### 4.4 로그 확인

```bash
# Python App 로그
docker compose -f docker-compose.prod.yml logs -f api
docker compose -f docker-compose.prod.yml logs -f worker
docker compose -f docker-compose.prod.yml logs -f websocket

# 모든 로그 함께 보기
docker compose -f docker-compose.prod.yml logs -f

# SigNoz 로그
docker compose -f docker-compose.monitoring-rpi.yml logs -f

# 로컬 파일 로그
tail -f logs/app.log
```

### 4.5 서비스 제어

```bash
# 정지
docker compose -f docker-compose.prod.yml stop

# 재시작
docker compose -f docker-compose.prod.yml restart

# 중지 및 제거
docker compose -f docker-compose.prod.yml down

# 특정 서비스만 재시작
docker compose -f docker-compose.prod.yml restart api
```

## 5. 성능 모니터링

### 5.1 시스템 리소스

```bash
# 메모리 사용량
free -h

# CPU 온도 (라즈베리파이)
vcgencmd measure_temp

# 디스크 사용량
df -h

# 실시간 모니터링
htop
```

### 5.2 Docker 리소스

```bash
# 컨테이너별 리소스 사용량
docker stats --no-stream

# 디스크 사용량
docker system df

# 정리 (사용하지 않는 이미지/컨테이너 삭제)
docker system prune -a
```

### 5.3 SigNoz 대시보드

- **메모리 사용량**: http://localhost:3301
- **트레이스**: 애플리케이션 요청 추적
- **메트릭**: 시스템 및 애플리케이션 메트릭
- **로그**: 중앙 집중식 로그 관리

## 6. 메모리 최적화 팁

### 6.1 스왑 메모리 증가 (필요시)

```bash
# 현재 스왑 확인
free -h

# 스왑 증가 (2GB → 4GB)
sudo dphys-swapfile swapoff
sudo nano /etc/dphys-swapfile
# CONF_SWAPSIZE=4096

sudo dphys-swapfile setup
sudo dphys-swapfile swapon
```

### 6.2 불필요한 서비스 중지

```bash
# 사용하지 않는 서비스 확인
systemctl list-units --type=service --state=running

# 예: Bluetooth 중지 (필요 없는 경우)
sudo systemctl disable bluetooth
sudo systemctl stop bluetooth
```

### 6.3 AlertManager 비활성화 (메모리 절약)

AlertManager가 필요 없다면 `docker-compose.monitoring-rpi.yml`에서 주석 처리됨 (기본값).

## 7. 백업 및 복구

### 7.1 PostgreSQL 백업

```bash
# 백업
pg_dump -U postgres auto_trader > backup_$(date +%Y%m%d).sql

# 복구
psql -U postgres auto_trader < backup_20240101.sql
```

### 7.2 SigNoz 데이터 백업

```bash
# Docker 볼륨 백업
docker run --rm -v signoz_clickhouse_data:/data -v $(pwd):/backup \
  alpine tar czf /backup/signoz_backup_$(date +%Y%m%d).tar.gz /data
```

## 8. 문제 해결

### 8.1 메모리 부족

**증상:**
```
OOMKilled (Out of Memory)
```

**해결:**
1. `docker stats`로 어떤 컨테이너가 메모리를 많이 사용하는지 확인
2. `docker-compose.monitoring-rpi.yml`에서 `mem_limit` 값 조정
3. AlertManager 비활성화
4. 스왑 메모리 증가

### 8.2 ClickHouse 연결 실패

**증상:**
```
Connection refused to clickhouse:9000
```

**해결:**
```bash
# ClickHouse 로그 확인
docker logs signoz_clickhouse

# ClickHouse 재시작
docker compose -f docker-compose.monitoring-rpi.yml restart clickhouse
```

### 8.3 SSD 성능 확인

```bash
# 읽기 속도 테스트
sudo hdparm -t /dev/sda

# 쓰기 속도 테스트
dd if=/dev/zero of=testfile bs=1M count=1024 oflag=direct
```

## 9. 업데이트

### 9.1 애플리케이션 업데이트

```bash
cd ~/auto_trader
git pull

# GitHub에서 최신 이미지 받기
export GITHUB_REPOSITORY=your-username/auto_trader
docker pull ghcr.io/${GITHUB_REPOSITORY}:production

# 재시작
docker compose -f docker-compose.prod.yml down
docker compose -f docker-compose.prod.yml up -d
```

### 9.2 SigNoz 업데이트

```bash
docker compose -f docker-compose.monitoring-rpi.yml pull
docker compose -f docker-compose.monitoring-rpi.yml up -d
```

## 10. 예상 성능

### 메모리 사용량 (8GB 총 메모리)

```
시스템: ~500MB
PostgreSQL (네이티브): ~200MB
Redis (네이티브): ~100MB
Python App (Docker):
  - API: ~512MB (제한 1GB)
  - Worker: ~256MB (제한 512MB)
  - WebSocket: ~256MB (제한 512MB)
SigNoz Docker:
  - Zookeeper: ~256MB (제한 384MB)
  - ClickHouse: ~1.5GB (제한 2GB)
  - OTEL Collector: ~384MB (제한 512MB)
  - Query Service: ~512MB (제한 768MB)
  - Frontend: ~128MB (제한 256MB)
-------------------------------------------
총 사용: ~4.6GB
여유: ~3.4GB
```

### CPU 사용률

- **유휴**: 5-10%
- **API 요청**: 20-30%
- **AI 분석**: 40-60%
- **배치 작업**: 60-80%

### 온도

- **정상**: 40-50°C
- **부하 시**: 60-70°C
- **주의**: 80°C 이상 (쿨링 필요)

## 참고 링크

- [라즈베리파이 공식 문서](https://www.raspberrypi.com/documentation/)
- [Docker on Raspberry Pi](https://docs.docker.com/engine/install/debian/)
- [SigNoz 공식 문서](https://signoz.io/docs/)
- [프로젝트 문서](./CLAUDE.md)
