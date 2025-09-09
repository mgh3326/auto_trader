# 🗂️ 데이터베이스 마이그레이션 가이드

Production 환경에서 안전한 데이터베이스 마이그레이션을 위한 가이드입니다.

## 🎯 권장 전략

### 📊 위험도별 접근 방식

| 마이그레이션 유형 | 위험도 | 권장 방식 | 백업 필요 |
|------------------|--------|-----------|-----------|
| 테이블/컬럼 추가 | 🟢 낮음 | 자동 | 선택 |
| 인덱스 추가/삭제 | 🟡 중간 | 수동 검토 후 자동 | 권장 |
| 컬럼 타입 변경 | 🟠 높음 | 수동 | 필수 |
| 테이블/컬럼 삭제 | 🔴 매우 높음 | 수동 | 필수 |
| 데이터 변환 | 🔴 매우 높음 | 수동 | 필수 |

## 🛠️ 배포 방식

### 1. 🤖 자동 마이그레이션 (권장: 낮은 위험도)

```bash
# 안전한 마이그레이션 (테이블/컬럼 추가 등)
./scripts/deploy.sh --auto-migrate --health-check

# 백업과 함께 자동 마이그레이션
./scripts/deploy.sh --auto-migrate --backup --health-check
```

**사용 시기:**
- 새 테이블/컬럼 추가
- 인덱스 추가
- 간단한 제약조건 추가

### 2. 👨‍💻 수동 마이그레이션 (권장: 높은 위험도)

```bash
# 마이그레이션 체크와 수동 선택
./scripts/deploy.sh --manual-migrate --backup

# 또는 마이그레이션만 별도 실행
./scripts/migration-check.sh
docker-compose -f docker-compose.prod.yml --profile migration up migration
```

**사용 시기:**
- 테이블/컬럼 삭제
- 데이터 타입 변경
- 복잡한 데이터 변환
- 대량 데이터 처리

### 3. ⏭️ 마이그레이션 스킵

```bash
# 마이그레이션 없이 애플리케이션만 업데이트
./scripts/deploy.sh --skip-migrate
```

**사용 시기:**
- 코드만 변경되고 DB 스키마 변경 없음
- 마이그레이션을 별도 시점에 실행할 계획

## 🔍 마이그레이션 전 체크리스트

### 1. 개발/스테이징 환경 테스트

```bash
# 로컬에서 마이그레이션 테스트
alembic upgrade head

# 스테이징 환경에서 실제 데이터로 테스트
```

### 2. 위험도 평가

```bash
# 마이그레이션 안전성 체크
./scripts/migration-check.sh
```

**체크 항목:**
- [ ] DROP TABLE/COLUMN 포함 여부
- [ ] 대량 데이터 처리 여부
- [ ] 다운타임 예상 시간
- [ ] 롤백 계획 수립

### 3. 백업 전략

```bash
# 수동 백업 생성
sudo -u postgres pg_dump auto_trader_prod > backup_$(date +%Y%m%d_%H%M%S).sql

# 또는 스크립트로 자동 백업
./scripts/deploy.sh --backup
```

## 📋 실행 가이드

### 🟢 안전한 마이그레이션 (자동)

```bash
# 1. 최신 코드 가져오기
git pull origin production

# 2. 마이그레이션 체크
./scripts/migration-check.sh

# 3. 위험도가 낮으면 자동 실행
./scripts/deploy.sh --auto-migrate --health-check
```

### 🟡 중간 위험도 마이그레이션

```bash
# 1. 백업 생성
./scripts/deploy.sh --backup

# 2. 스테이징에서 테스트 후 수동 검토
./scripts/migration-check.sh

# 3. 수동 승인 후 실행
./scripts/deploy.sh --manual-migrate
```

### 🔴 고위험 마이그레이션

```bash
# 1. 유지보수 모드 활성화 (선택)
# nginx에서 maintenance page 활성화

# 2. 전체 백업
./scripts/deploy.sh --backup

# 3. 마이그레이션만 별도 실행
docker-compose -f docker-compose.prod.yml --profile migration up migration

# 4. 검증 후 애플리케이션 배포
./scripts/deploy.sh --skip-migrate --health-check

# 5. 유지보수 모드 해제
```

## 🔄 롤백 절차

### 1. 애플리케이션 롤백

```bash
# 이전 이미지로 태그 변경 (수동)
docker tag ghcr.io/$GITHUB_REPOSITORY:backup ghcr.io/$GITHUB_REPOSITORY:latest

# 롤백 배포
./scripts/deploy.sh --skip-migrate
```

### 2. 데이터베이스 롤백

```bash
# Alembic으로 이전 버전으로 롤백
docker run --rm --network host \
  -e DATABASE_URL="$DATABASE_URL" \
  ghcr.io/$GITHUB_REPOSITORY:latest \
  alembic downgrade -1

# 또는 백업에서 복원 (최후 수단)
sudo -u postgres psql auto_trader_prod < backup_file.sql
```

## ⚡ 무중단 마이그레이션 전략

### 1. 호환 가능한 변경사항

```sql
-- ✅ 안전: 새 컬럼 추가 (nullable)
ALTER TABLE users ADD COLUMN new_field VARCHAR(255);

-- ✅ 안전: 새 테이블 추가
CREATE TABLE new_feature (...);

-- ✅ 안전: 인덱스 추가 (CONCURRENTLY 사용)
CREATE INDEX CONCURRENTLY idx_users_email ON users(email);
```

### 2. 단계적 마이그레이션

```bash
# 1단계: 새 컬럼 추가 (nullable)
# 2단계: 애플리케이션 배포 (새 컬럼 사용)
# 3단계: 데이터 마이그레이션
# 4단계: 제약조건 추가
# 5단계: 구 컬럼 제거
```

## 📊 모니터링

### 마이그레이션 중 모니터링

```bash
# 실시간 DB 상태 확인
watch "sudo -u postgres psql auto_trader_prod -c \"SELECT * FROM pg_stat_activity WHERE state = 'active';\""

# 테이블 락 확인
sudo -u postgres psql auto_trader_prod -c "SELECT * FROM pg_locks WHERE granted = false;"

# 마이그레이션 로그 확인
docker logs -f auto_trader_migration
```

### 마이그레이션 후 검증

```bash
# 애플리케이션 헬스체크
./scripts/healthcheck.sh

# 데이터 검증 쿼리 실행
docker run --rm --network host \
  -e DATABASE_URL="$DATABASE_URL" \
  ghcr.io/$GITHUB_REPOSITORY:latest \
  python -c "
import asyncio
import asyncpg
import os

async def verify_data():
    conn = await asyncpg.connect(os.environ['DATABASE_URL'])
    
    # 데이터 무결성 검사
    result = await conn.fetchval('SELECT COUNT(*) FROM users')
    print(f'Total users: {result}')
    
    # 필요한 검증 쿼리 추가
    
    await conn.close()

asyncio.run(verify_data())
"
```

## 🚨 응급 상황 대응

### 마이그레이션 실패 시

1. **즉시 애플리케이션 중지**
   ```bash
   docker-compose -f docker-compose.prod.yml down
   ```

2. **에러 로그 확인**
   ```bash
   docker logs auto_trader_migration
   ```

3. **데이터베이스 상태 확인**
   ```bash
   sudo -u postgres psql auto_trader_prod -c "SELECT version_num FROM alembic_version;"
   ```

4. **필요시 수동 수정 후 재시도**

### 데이터 손실 시

1. **즉시 서비스 중지**
2. **백업에서 복원**
3. **근본 원인 분석**
4. **복구 절차 문서화**

## 📚 추가 자료

- [Alembic 공식 문서](https://alembic.sqlalchemy.org/)
- [PostgreSQL 무중단 마이그레이션](https://www.postgresql.org/docs/current/sql-createindex.html#SQL-CREATEINDEX-CONCURRENTLY)
- [Django-style 마이그레이션 패턴](https://docs.djangoproject.com/en/stable/topics/migrations/)

---

**⚠️ 중요**: Production 환경에서는 항상 신중하게 접근하고, 의심스러우면 수동으로 진행하세요!


