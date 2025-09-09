# 🖥️ 네이티브 DB/Redis 설치 가이드

Production 환경에서 PostgreSQL과 Redis를 네이티브로 설치하는 방법입니다.

## 🐘 PostgreSQL 설치

### Ubuntu/Debian

```bash
# PostgreSQL 공식 APT 저장소 추가
sudo sh -c 'echo "deb http://apt.postgresql.org/pub/repos/apt $(lsb_release -cs)-pgdg main" > /etc/apt/sources.list.d/pgdg.list'
wget --quiet -O - https://www.postgresql.org/media/keys/ACCC4CF8.asc | sudo apt-key add -
sudo apt-get update

# PostgreSQL 17 설치
sudo apt-get install -y postgresql-17 postgresql-client-17

# 서비스 시작 및 활성화
sudo systemctl start postgresql
sudo systemctl enable postgresql
```

### CentOS/RHEL/Rocky Linux

```bash
# PostgreSQL 공식 RPM 저장소 추가
sudo dnf install -y https://download.postgresql.org/pub/repos/yum/reporpms/EL-9-x86_64/pgdg-redhat-repo-latest.noarch.rpm

# PostgreSQL 17 설치
sudo dnf install -y postgresql17-server postgresql17

# 데이터베이스 초기화
sudo /usr/pgsql-17/bin/postgresql-17-setup initdb

# 서비스 시작 및 활성화
sudo systemctl start postgresql-17
sudo systemctl enable postgresql-17
```

### PostgreSQL 설정

```bash
# postgres 사용자로 전환
sudo -u postgres psql

-- 데이터베이스 및 사용자 생성
CREATE DATABASE auto_trader_prod;
CREATE USER auto_trader WITH ENCRYPTED PASSWORD 'your_secure_password';
GRANT ALL PRIVILEGES ON DATABASE auto_trader_prod TO auto_trader;

-- 권한 확장 (필요시)
\c auto_trader_prod
GRANT ALL ON SCHEMA public TO auto_trader;
GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO auto_trader;
GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO auto_trader;

\q
```

### 연결 설정 수정

```bash
# pg_hba.conf 수정 (인증 방법 설정)
sudo nano /etc/postgresql/17/main/pg_hba.conf

# 다음 라인을 추가/수정
local   auto_trader_prod    auto_trader                     md5
host    auto_trader_prod    auto_trader     127.0.0.1/32   md5
```

```bash
# postgresql.conf 수정 (필요시)
sudo nano /etc/postgresql/17/main/postgresql.conf

# 성능 최적화 설정 예시
shared_buffers = 256MB
effective_cache_size = 1GB
maintenance_work_mem = 64MB
checkpoint_completion_target = 0.9
wal_buffers = 16MB
default_statistics_target = 100
random_page_cost = 1.1
effective_io_concurrency = 200

# 서비스 재시작
sudo systemctl restart postgresql
```

## 🔴 Redis 설치

### Ubuntu/Debian

```bash
# Redis 설치
sudo apt update
sudo apt install -y redis-server

# 서비스 시작 및 활성화
sudo systemctl start redis-server
sudo systemctl enable redis-server
```

### CentOS/RHEL/Rocky Linux

```bash
# EPEL 저장소 활성화
sudo dnf install -y epel-release

# Redis 설치
sudo dnf install -y redis

# 서비스 시작 및 활성화
sudo systemctl start redis
sudo systemctl enable redis
```

### Redis 설정

```bash
# Redis 설정 파일 편집
sudo nano /etc/redis/redis.conf
```

**주요 설정 항목:**

```conf
# 패스워드 설정
requirepass your_secure_redis_password

# 메모리 정책 설정
maxmemory 512mb
maxmemory-policy allkeys-lru

# 영속성 설정
save 900 1
save 300 10
save 60 10000

# 로그 설정
loglevel notice
logfile /var/log/redis/redis-server.log

# 네트워크 설정 (로컬만 허용)
bind 127.0.0.1

# 백그라운드 실행
daemonize yes
```

```bash
# 설정 적용
sudo systemctl restart redis
```

## 🔧 방화벽 설정

### 포트 보안 설정

```bash
# UFW 사용시
sudo ufw allow from 127.0.0.1 to any port 5432  # PostgreSQL
sudo ufw allow from 127.0.0.1 to any port 6379  # Redis

# 외부 접근 차단 (기본적으로 localhost만 허용)
```

## 📊 모니터링 및 관리

### PostgreSQL 모니터링

```bash
# 연결 테스트
psql -h localhost -U auto_trader -d auto_trader_prod

# 데이터베이스 상태 확인
sudo -u postgres psql -c "SELECT version();"
sudo -u postgres psql -c "SELECT * FROM pg_stat_activity;"

# 로그 확인
sudo tail -f /var/log/postgresql/postgresql-17-main.log
```

### Redis 모니터링

```bash
# Redis 연결 테스트
redis-cli -a your_secure_redis_password ping

# Redis 정보 확인
redis-cli -a your_secure_redis_password info

# 메모리 사용량 확인
redis-cli -a your_secure_redis_password info memory

# 실시간 모니터링
redis-cli -a your_secure_redis_password monitor
```

## 🔄 백업 설정

### PostgreSQL 자동 백업

```bash
# 백업 스크립트 생성
sudo tee /usr/local/bin/backup-postgres.sh > /dev/null <<'EOF'
#!/bin/bash
BACKUP_DIR="/var/backups/postgresql"
DATE=$(date +%Y%m%d_%H%M%S)
DB_NAME="auto_trader_prod"

mkdir -p $BACKUP_DIR

pg_dump -h localhost -U auto_trader -d $DB_NAME > $BACKUP_DIR/auto_trader_$DATE.sql

# 7일 이상 된 백업 파일 삭제
find $BACKUP_DIR -name "auto_trader_*.sql" -type f -mtime +7 -delete

echo "Backup completed: auto_trader_$DATE.sql"
EOF

sudo chmod +x /usr/local/bin/backup-postgres.sh

# Cron 설정 (매일 새벽 2시 백업)
echo "0 2 * * * /usr/local/bin/backup-postgres.sh" | sudo crontab -
```

### Redis 자동 백업

```bash
# Redis는 자동으로 dump.rdb 생성
# 추가 백업이 필요하면:
sudo tee /usr/local/bin/backup-redis.sh > /dev/null <<'EOF'
#!/bin/bash
BACKUP_DIR="/var/backups/redis"
DATE=$(date +%Y%m%d_%H%M%S)

mkdir -p $BACKUP_DIR

# Redis 백그라운드 저장 명령
redis-cli -a your_secure_redis_password BGSAVE

# dump.rdb 파일 복사
sleep 5
cp /var/lib/redis/dump.rdb $BACKUP_DIR/dump_$DATE.rdb

# 7일 이상 된 백업 파일 삭제
find $BACKUP_DIR -name "dump_*.rdb" -type f -mtime +7 -delete

echo "Redis backup completed: dump_$DATE.rdb"
EOF

sudo chmod +x /usr/local/bin/backup-redis.sh
```

## 🚀 애플리케이션 연동

### 환경변수 설정

```bash
# .env.prod 파일에서 설정
DATABASE_URL=postgresql+asyncpg://auto_trader:your_secure_password@localhost:5432/auto_trader_prod
REDIS_URL=redis://:your_secure_redis_password@localhost:6379/0
```

### 연결 테스트

```bash
# Docker 컨테이너에서 DB/Redis 연결 테스트
docker run --rm --network host \
  -e DATABASE_URL="postgresql+asyncpg://auto_trader:password@localhost:5432/auto_trader_prod" \
  -e REDIS_URL="redis://:password@localhost:6379/0" \
  ghcr.io/your-repo/auto_trader:latest \
  python -c "
import asyncio
import asyncpg
import redis

async def test_db():
    conn = await asyncpg.connect('postgresql://auto_trader:password@localhost:5432/auto_trader_prod')
    result = await conn.fetchval('SELECT version()')
    print('✅ PostgreSQL:', result)
    await conn.close()

def test_redis():
    r = redis.from_url('redis://:password@localhost:6379/0')
    r.ping()
    print('✅ Redis: Connection successful')

asyncio.run(test_db())
test_redis()
"
```

## ⚡ 성능 최적화

### PostgreSQL 최적화

```bash
# 성능 분석 도구 설치
sudo apt install -y postgresql-contrib-17

# pg_stat_statements 확장 활성화
sudo -u postgres psql auto_trader_prod -c "CREATE EXTENSION IF NOT EXISTS pg_stat_statements;"

# 느린 쿼리 로깅 설정
echo "log_min_duration_statement = 1000" | sudo tee -a /etc/postgresql/17/main/postgresql.conf
```

### Redis 최적화

```bash
# 시스템 최적화
echo 'vm.overcommit_memory = 1' | sudo tee -a /etc/sysctl.conf
echo 'net.core.somaxconn = 65535' | sudo tee -a /etc/sysctl.conf
sudo sysctl -p
```

이제 네이티브 DB/Redis 환경에서 Auto Trader를 안정적으로 실행할 수 있습니다! 🚀


