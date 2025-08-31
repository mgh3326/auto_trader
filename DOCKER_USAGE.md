# Docker 사용 가이드

## 개요

Auto Trader 프로젝트는 여러 Docker 구성을 제공합니다:

- **기본 구성**: DB(PostgreSQL) + Redis + Adminer
- **API 구성**: Auto Trader API 서버
- **전체 구성**: 모든 서비스를 포함

## 파일 구조

```
├── docker-compose.yml          # DB + Redis + Adminer (기본)
├── docker-compose.api.yml      # API 서버만
├── docker-compose.full.yml     # 전체 스택
├── Dockerfile.api              # API 서버 이미지
├── run_docker.sh              # 단일 API 컨테이너 실행
└── run_api_compose.sh         # API Compose 실행
```

## 사용 방법

### 1. 기본 구성 (DB + Redis)

개발 환경에서 DB와 Redis만 필요한 경우:

```bash
# 실행
docker-compose up -d

# 중지
docker-compose down
```

- PostgreSQL: `localhost:5432`
- Redis: `localhost:6379`
- Adminer: `http://localhost:8080`

### 2. API만 실행

기본 구성이 실행된 상태에서 API만 추가로 실행:

```bash
# 스크립트 사용 (권장)
./run_api_compose.sh

# 또는 직접 실행
docker-compose -f docker-compose.api.yml up -d --build
```

- API 서버: `http://localhost:8001`

### 3. 전체 스택 실행

모든 서비스를 한번에 실행:

```bash
docker-compose -f docker-compose.full.yml up -d --build
```

### 4. 단일 API 컨테이너 실행

Docker Compose 없이 API만 실행:

```bash
# 스크립트 사용 (권장)
./run_docker.sh

# 또는 직접 실행
docker build -f Dockerfile.api -t auto_trader-api:local .
docker run --env-file .env -p 8001:8000 \
  -v "$(pwd)/tmp:/app/tmp" \
  auto_trader-api:local
```

## 주요 개선사항

### 권한 문제 해결

- Docker 컨테이너에서 `tmp` 디렉토리 권한 문제 해결
- `appuser` 사용자로 안전한 실행
- 볼륨 마운트를 통한 토큰 캐시 영속성

### 에러 처리 개선

- `token_cache.py`에 예외 처리 추가
- 로깅을 통한 디버깅 지원
- 원자적 파일 쓰기로 데이터 무결성 보장

### 빌드 최적화

- `.dockerignore` 파일로 빌드 컨텍스트 최적화
- 멀티 스테이지 빌드 준비
- Poetry를 통한 의존성 관리

## 환경 설정

### 필수 파일

1. `.env` 파일 생성 (`env.example` 참고)
2. `tmp/` 디렉토리는 자동 생성됨

### 볼륨 마운트

- `./tmp:/app/tmp` - 토큰 캐시 저장
- `./logs:/app/logs` - 로그 파일 저장 (선택적)

## 문제 해결

### 권한 에러

```bash
# 호스트에서 권한 설정
mkdir -p tmp logs
chmod 755 tmp logs
```

### 네트워크 문제

```bash
# 네트워크 재생성
docker network rm auto_trader_local_dev
docker-compose up -d
```

### 컨테이너 정리

```bash
# 모든 Auto Trader 컨테이너 정리
docker rm -f auto_trader_api auto_trader_pg auto_trader_redis adminer

# 볼륨 정리 (주의: 데이터 삭제됨)
docker volume rm auto_trader_pg_data auto_trader_redis_data
```

## 유용한 명령어

```bash
# 로그 확인
docker-compose logs -f api
docker logs -f auto_trader_api

# 컨테이너 상태 확인
docker-compose ps
docker ps --filter "name=auto_trader"

# 컨테이너 접속
docker exec -it auto_trader_api bash

# 이미지 재빌드
docker-compose -f docker-compose.api.yml up -d --build --force-recreate
```

