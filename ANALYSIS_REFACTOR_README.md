# 분석 시스템 리팩토링 가이드

## 개요
기존의 `debug_upbit.py`, `debug_yahoo.py`, `debug_kis.py` 파일에서 중복되던 프롬프트 생성, Gemini 실행, DB 저장 로직을 공통 모듈로 분리하여 재사용 가능한 구조로 개선했습니다.

## 새로운 구조

### 1. 핵심 클래스들

#### `Analyzer` (app/analysis/analyzer.py)
- 프롬프트 생성, Gemini 실행, DB 저장을 담당하는 공통 클래스
- 스마트 재시도 로직 포함 (429 에러 시 모델 전환)
- 모든 서비스에서 공통으로 사용

#### `DataProcessor` (app/analysis/analyzer.py)
- 데이터 전처리를 담당하는 유틸리티 클래스
- 과거 데이터와 현재 데이터 병합 로직

#### 서비스별 분석기들 (app/analysis/service_analyzers.py)
- `UpbitAnalyzer`: 암호화폐 분석
- `YahooAnalyzer`: 미국주식 분석  
- `KISAnalyzer`: 국내주식 분석

### 2. 사용법

#### 개별 서비스 분석
```python
# Upbit 암호화폐 분석
from app.analysis.service_analyzers import UpbitAnalyzer

analyzer = UpbitAnalyzer()
await analyzer.analyze_coins(["비트코인", "이더리움"])

# Yahoo Finance 주식 분석
from app.analysis.service_analyzers import YahooAnalyzer

analyzer = YahooAnalyzer()
await analyzer.analyze_stocks(["TSLA", "AAPL"])

# KIS 국내주식 분석
from app.analysis.service_analyzers import KISAnalyzer

analyzer = KISAnalyzer()
await analyzer.analyze_stocks(["삼성전자", "SK하이닉스"])
```

#### 통합 분석
```python
# 모든 서비스를 한 번에 실행
python debug_unified.py
```

### 3. 새로운 Debug 파일들

- `debug_upbit_new.py`: 리팩토링된 Upbit 분석기
- `debug_yahoo_new.py`: 리팩토링된 Yahoo 분석기  
- `debug_kis_new.py`: 리팩토링된 KIS 분석기
- `debug_unified.py`: 모든 서비스 통합 실행

### 4. 장점

1. **코드 중복 제거**: 공통 로직을 한 곳에서 관리
2. **유지보수성 향상**: 버그 수정이나 기능 추가 시 한 곳만 수정
3. **확장성**: 새로운 서비스 추가 시 간단한 상속만으로 구현 가능
4. **일관성**: 모든 서비스에서 동일한 에러 처리 및 재시도 로직
5. **테스트 용이성**: 공통 로직을 독립적으로 테스트 가능
6. **스마트 모델 제한**: Redis 기반으로 429 에러 시 자동 모델 사용 제한

### 5. 마이그레이션 가이드

기존 debug 파일들을 사용 중이라면:

1. 새로운 분석기 클래스들 import
2. 기존의 중복 코드를 분석기 메서드 호출로 교체
3. 필요에 따라 커스터마이징

### 6. 커스터마이징

특정 서비스에만 필요한 로직이 있다면:

```python
class CustomUpbitAnalyzer(UpbitAnalyzer):
    async def custom_analysis(self, coin_name: str):
        # 커스텀 로직 구현
        pass
```

## 파일 구조
```
app/analysis/
├── __init__.py              # 모듈 export
├── analyzer.py              # 핵심 분석기 클래스 (Redis 기반 모델 제한 포함)
├── service_analyzers.py     # 서비스별 분석기
├── prompt.py                # 프롬프트 생성 (기존)
└── indicators.py            # 기술적 지표 (기존)

app/core/
├── config.py                # 설정 (Redis URL 포함)
├── model_rate_limiter.py    # Redis 기반 모델 제한 관리자
└── ...

debug_*.py                   # 새로운 debug 파일들
debug_model_status.py        # 모델 상태 확인 및 관리 도구
```

## Redis 기반 모델 제한 시스템

### 개요
Google Gemini API에서 429 에러(할당량 초과)가 발생하면, `retry_delay` 정보를 활용하여 **API 키별로** 해당 모델의 사용을 자동으로 제한합니다.

### 주요 기능

1. **API 키별 모델 제한**: 429 에러 발생 시 특정 API 키의 모델 사용을 제한
2. **자동 모델 제한**: `retry_delay` 정보를 Redis에 저장하여 정확한 제한 시간 적용
3. **스마트 재시도**: 제한된 API 키의 모델은 자동으로 건너뛰고 다음 모델 시도
4. **실시간 상태 확인**: Redis를 통해 모델별, API 키별 제한 상태 실시간 모니터링
5. **수동 제한 해제**: 필요시 특정 모델의 특정 API 키 또는 전체 제한을 수동으로 해제
6. **보안 강화**: API 키를 마스킹하여 Redis에 저장

### 사용법

#### 환경 설정

**방법 1: 전체 URL 설정 (권장)**
```bash
# .env 파일에 Redis URL 직접 설정
REDIS_URL=redis://localhost:6379/0
```

**방법 2: 개별 설정 (REDIS_URL이 설정되지 않은 경우에만 사용)**
```bash
# Redis 개별 설정
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=your_password  # 비밀번호가 있는 경우
REDIS_SSL=false  # SSL 사용 시 true

# Redis 연결 풀 설정
REDIS_MAX_CONNECTIONS=10
REDIS_SOCKET_TIMEOUT=5
REDIS_SOCKET_CONNECT_TIMEOUT=5
```

**환경별 Redis URL 설정 예시:**
```bash
# 개발 환경
REDIS_URL=redis://localhost:6379/0

# 프로덕션 환경 (비밀번호 포함)
REDIS_URL=redis://:your_password@your-redis-host.com:6379/0

# 프로덕션 환경 (SSL 사용)
REDIS_URL=rediss://:your_password@your-redis-host.com:6379/0

# Docker environment
REDIS_URL=redis://auto_trader_redis:6379/0

# 클라우드 Redis (예: AWS ElastiCache)
REDIS_URL=redis://:your_password@your-elasticache-endpoint.cache.amazonaws.com:6379/0

#### Docker Compose 사용법

**1. 서비스 시작**
```bash
# 모든 서비스 시작 (PostgreSQL, Redis, Adminer)
docker compose up -d

# 특정 서비스만 시작
docker compose up -d redis
```

**2. 서비스 상태 확인**
```bash
# 모든 서비스 상태 확인
docker compose ps

# Redis 로그 확인
docker compose logs redis

# Redis 상태 확인
docker compose exec redis redis-cli -a redis_password ping
```

**3. 서비스 중지**
```bash
# 모든 서비스 중지
docker compose down

# 볼륨까지 삭제 (데이터 손실 주의!)
docker compose down -v
```

**4. 환경 변수 설정**
```bash
# .env 파일에 Docker 환경용 Redis URL 설정
REDIS_URL=redis://auto_trader_redis:6379/0
```

**5. Redis 연결 테스트**
```bash
# Redis 컨테이너에 접속하여 연결 테스트
docker compose exec auto_trader_redis redis-cli ping
# 응답: PONG
```

#### 모델 상태 확인
```bash
python debug_model_status.py
```

**주요 기능:**
- 모델별, API 키별 제한 상태 확인
- 전체 제한 상태 요약
- 특정 API 키의 제한 해제
- 모델별 모든 API 키 제한 해제
- 모든 제한 일괄 해제

#### 코드에서 사용
```python
from app.analysis.service_analyzers import UpbitAnalyzer

analyzer = UpbitAnalyzer()
# 자동으로 Redis 기반 API 키별 모델 제한 적용
await analyzer.analyze_coins(["비트코인"])
```

#### Redis 키 구조
```
model_rate_limit:gemini-2.5-pro:AIza...abc123  # 모델:API키별 제한 정보
model_retry_info:gemini-2.5-pro:AIza...abc123  # 모델:API키별 재시도 정보
```

#### 제한 정보 예시
```json
{
  "model": "gemini-2.5-pro",
  "api_key": "AIza...abc123",
  "error_code": 429,
  "until": "2024-01-15T14:30:00",
  "retry_delay": {"seconds": 300},
  "set_at": "2024-01-15T14:25:00"
}
```
