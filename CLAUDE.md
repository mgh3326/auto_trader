# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 프로젝트 개요

AI 기반 자동 거래 분석 시스템으로, 다양한 금융 데이터를 수집하고 Google Gemini AI를 활용하여 투자 분석을 제공합니다.

**주요 특징:**
- 다중 시장 지원: 국내주식(KIS), 해외주식(KIS/Yahoo Finance), 암호화폐(Upbit)
- 다중 시간대 분석: 일봉 200개 + 분봉(60분/5분/1분)
- AI 분석: Google Gemini API를 통한 구조화된 JSON 분석
- Redis 기반 API 키별 모델 제한 시스템

## 개발 환경 설정

### 필수 요구사항
- Python 3.11+
- UV (의존성 관리)
- PostgreSQL (데이터베이스)
- Redis (모델 제한 관리 및 캐싱)

### 초기 설정
```bash
# UV 설치 (미설치 시)
pip install uv

# 의존성 설치
uv sync                           # 프로덕션 의존성만
uv sync --all-groups              # 모든 의존성 (test, dev 포함)

# 환경 변수 설정
cp env.example .env
# .env 파일 편집하여 API 키 설정

# 데이터베이스 마이그레이션
uv run alembic upgrade head

# 개발 서버 실행
make dev                          # 또는 uv run uvicorn app.main:app --reload
```

### Docker 환경
```bash
docker compose up -d              # PostgreSQL, Redis, Adminer 시작
docker compose ps                 # 서비스 상태 확인
docker compose down               # 서비스 중지
```

## 핵심 명령어

### 테스트
```bash
make test                         # 모든 테스트 실행
make test-unit                    # 단위 테스트만
make test-integration             # 통합 테스트만
make test-cov                     # 커버리지 리포트 포함
uv run pytest tests/test_*.py -v -k "test_name"  # 특정 테스트만
```

### 코드 품질
```bash
make lint                         # flake8, black, isort, mypy 검사
make format                       # black, isort로 코드 포맷팅
make security                     # bandit, safety 보안 검사
```

### 데이터베이스
```bash
# 마이그레이션 생성 및 적용
uv run alembic revision --autogenerate -m "migration message"
uv run alembic upgrade head

# 마이그레이션 롤백
uv run alembic downgrade -1

# 현재 버전 확인
uv run alembic current
```

### 개발 도구
```bash
python debug_upbit.py             # Upbit 암호화폐 분석 테스트
python debug_yahoo.py             # Yahoo Finance 주식 분석 테스트
python debug_kis.py               # KIS 국내주식 분석 테스트
python debug_unified.py           # 모든 서비스 통합 테스트
python debug_model_status.py     # Redis 모델 제한 상태 확인/관리
```

## 아키텍처

### 분석 시스템 아키텍처

**핵심 설계 원칙: 공통 로직 분리 + 서비스별 특화**

```
app/analysis/
├── analyzer.py              # 핵심 Analyzer 클래스 (공통 로직)
│   ├── Analyzer             # 프롬프트 생성, AI 호출, DB 저장, 재시도 로직
│   └── DataProcessor        # 데이터 전처리 유틸리티
└── service_analyzers.py     # 서비스별 분석기 (상속)
    ├── UpbitAnalyzer        # 암호화폐 (분봉 지원)
    ├── YahooAnalyzer        # 미국 주식 (분봉 미지원)
    └── KISAnalyzer          # 국내/해외 주식 (분봉 지원)
```

**각 서비스 분석기는:**
- `Analyzer`를 상속하여 공통 기능 재사용
- 데이터 수집 로직만 서비스별로 구현 (`_collect_*_data` 메서드)
- 보유 자산 정보가 있으면 `position_info`로 전달
- 분봉 데이터가 있으면 `minute_candles`로 전달

### Redis 기반 스마트 모델 제한 시스템

**목적:** Google Gemini API 429 에러(할당량 초과) 발생 시 API 키별로 해당 모델 사용 자동 제한

**구조:**
```
app/core/model_rate_limiter.py   # ModelRateLimiter 클래스
app/analysis/analyzer.py          # _call_gemini_with_retry 메서드에서 활용
```

**동작 방식:**
1. API 호출 전: Redis에서 해당 API 키의 모델 제한 상태 확인
2. 429 에러 발생 시: `retry_delay` 정보를 Redis에 저장하여 API 키별로 모델 제한
3. 제한된 경우: 다음 사용 가능한 모델로 자동 전환
4. 제한 해제: TTL 만료 시 자동 해제 또는 `debug_model_status.py`로 수동 해제

**Redis 키 구조:**
- `model_rate_limit:{model}:{masked_api_key}` - 제한 정보
- `model_retry_info:{model}:{masked_api_key}` - 재시도 정보

### 데이터베이스 정규화 구조

**주식 정보와 분석 결과 분리:**

```
stock_info (마스터 테이블)        stock_analysis_results (분석 결과)
├── id (PK)                      ├── id (PK)
├── symbol (UNIQUE)              ├── stock_info_id (FK) → stock_info.id
├── name                         ├── model_name
├── instrument_type              ├── decision (buy/hold/sell)
├── exchange                     ├── confidence (0-100)
├── sector                       ├── price_analysis (4가지 범위)
├── market_cap                   ├── reasons (JSON)
└── is_active                    ├── detailed_text (markdown)
                                 └── prompt
```

**장점:**
- 종목 정보 중복 방지
- 종목별 분석 히스토리 추적 용이
- `stock_info_service.py`의 `create_stock_if_not_exists`로 자동 생성/조회

**조회 패턴:**
- 최신 분석: Correlated Subquery 또는 Window Function 사용
- 히스토리: `stock_info_id`로 JOIN하여 시간순 정렬

### API 서비스 클라이언트

```
app/services/
├── upbit.py                 # Upbit API (암호화폐)
├── yahoo.py                 # Yahoo Finance API
├── kis.py                   # 한국투자증권 API (30,000+ 라인)
├── upbit_websocket.py       # Upbit 실시간 시세
└── redis_token_manager.py   # Redis 기반 토큰 관리
```

**주의사항:**
- `kis.py`는 매우 큰 파일(30,000+ 라인)이므로 읽을 때 offset/limit 사용
- KIS 분봉 API는 `time_unit` 파라미터가 제대로 작동하지 않는 알려진 이슈 있음
- Upbit은 실시간 WebSocket과 REST API 모두 지원

### 데이터 구조

**Lazy Loading 시스템:**
```
data/
├── stocks_info/             # 주식 종목 코드 (lazy loading)
│   ├── kis_kospi_code_mst.py
│   ├── kis_kosdaq_code_mst.py
│   ├── overseas_nasdaq_code.py
│   └── overseas_us_stocks.py
└── coins_info/              # 암호화폐 페어 (lazy loading)
    └── upbit_pairs.py
```

**특징:**
- `prime_*_constants()` 호출로 초기화
- 첫 접근 시에만 데이터 로딩 (메모리 효율)
- `NAME_TO_CODE`, `CODE_TO_NAME` 딕셔너리 제공

## 주요 워크플로우

### 1. 새로운 서비스 분석기 추가

```python
# app/analysis/service_analyzers.py에 추가
class NewServiceAnalyzer(Analyzer):
    """새로운 서비스 분석기"""

    async def _collect_data(self, symbol: str):
        """데이터 수집 로직 구현"""
        # 1. 일봉/현재가/기본정보 수집
        df_historical = await new_service.fetch_ohlcv(symbol)
        df_current = await new_service.fetch_price(symbol)
        fundamental_info = await new_service.fetch_fundamental_info(symbol)

        # 2. 분봉 수집 (있는 경우)
        minute_candles = await new_service.fetch_minute_candles(symbol)

        # 3. 데이터 병합
        df_merged = DataProcessor.merge_historical_and_current(
            df_historical, df_current
        )

        return df_merged, fundamental_info, minute_candles

    async def analyze_symbols(self, symbols: List[str]):
        """심볼 분석"""
        for symbol in symbols:
            df, info, candles = await self._collect_data(symbol)

            # 공통 Analyzer의 analyze_and_save 사용
            result, model = await self.analyze_and_save(
                df=df,
                symbol=symbol,
                name=symbol,
                instrument_type="new_type",
                currency="$",
                unit_shares="주",
                fundamental_info=info,
                minute_candles=candles,
            )
```

### 2. 데이터베이스 모델 변경

```bash
# 1. app/models/에서 모델 수정
# 2. 마이그레이션 자동 생성
uv run alembic revision --autogenerate -m "description"

# 3. 생성된 마이그레이션 파일 검토 (alembic/versions/)
# 4. 적용
uv run alembic upgrade head

# 5. 문제 시 롤백
uv run alembic downgrade -1
```

**중요:** Alembic은 async 엔진 사용 - `alembic/env.py` 참고

### 3. JSON 분석 결과 사용

```python
from app.analysis.service_analyzers import UpbitAnalyzer

analyzer = UpbitAnalyzer()

# JSON 형식으로 분석 (StockAnalysisResult 테이블에 저장)
result, model = await analyzer.analyze_coins_json(["비트코인"])

if hasattr(result, 'decision'):
    # 구조화된 JSON 응답
    print(f"결정: {result.decision}")  # buy/hold/sell
    print(f"신뢰도: {result.confidence}%")  # 0-100
    print(f"근거: {result.reasons}")  # 최대 3개
    print(f"매수 범위: {result.price_analysis.appropriate_buy_range}")
else:
    # fallback 텍스트 응답 (PromptResult 테이블에 저장)
    print(f"텍스트 응답: {result}")
```

### 4. Redis 모델 제한 관리

```bash
# 상태 확인
python debug_model_status.py

# 사용 가능한 명령:
# - status: 전체 상태 확인
# - clear <api_key_prefix>: 특정 API 키 제한 해제
# - clear_model <model_name>: 모델별 모든 제한 해제
# - clear_all: 모든 제한 해제
```

## 환경 변수

**필수 환경 변수 (.env 파일):**

```bash
# Google AI
GOOGLE_API_KEY=xxx                    # 또는
GOOGLE_API_KEYS=key1,key2,key3        # 콤마로 구분된 여러 키

# 한국투자증권 (KIS)
KIS_APP_KEY=xxx
KIS_APP_SECRET=xxx
KIS_ACCOUNT_NO=12345678-01            # 선택사항

# Upbit
UPBIT_ACCESS_KEY=xxx
UPBIT_SECRET_KEY=xxx
UPBIT_BUY_AMOUNT=100000               # 분할 매수 금액 (기본 10만원)
UPBIT_MIN_KRW_BALANCE=105000          # 최소 KRW 잔고

# 데이터베이스
DATABASE_URL=postgresql+asyncpg://user:pass@localhost/dbname

# Redis (방법 1: URL로 통합 설정 - 권장)
REDIS_URL=redis://localhost:6379/0

# Redis (방법 2: 개별 설정 - REDIS_URL이 없을 때만 사용)
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_DB=0
REDIS_PASSWORD=                       # 선택사항
REDIS_SSL=false

# Telegram (선택사항)
TELEGRAM_TOKEN=xxx
TELEGRAM_CHAT_IDS_STR=chat_id1,chat_id2

# OpenDART (선택사항)
OPENDART_API_KEY=xxx
```

## 테스트 작성

**테스트 마커 사용:**
```python
import pytest

@pytest.mark.unit
async def test_analyzer_prompt_generation():
    """단위 테스트"""
    pass

@pytest.mark.integration
async def test_upbit_api_integration():
    """통합 테스트 (실제 API 호출)"""
    pass

@pytest.mark.slow
async def test_heavy_operation():
    """느린 테스트"""
    pass
```

**테스트 실행:**
```bash
pytest tests/test_file.py -v -k "test_name"  # 특정 테스트
pytest tests/ -v -m "not integration"        # 통합 테스트 제외
pytest tests/ -v -m "not slow"               # 느린 테스트 제외
```

## 웹 대시보드

### JSON 분석 대시보드
- URL: `http://localhost:8000/analysis-json/`
- 기능: 필터링, 페이지네이션, 상세 모달
- 통계: 투자 결정 분포, 평균 신뢰도

### 최신 종목 정보 대시보드
- URL: `http://localhost:8000/stock-latest/`
- 기능: 종목별 최신 분석 결과 조회

## HTTPS 및 Reverse Proxy 설정 (Caddy)

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
- X-XSS-Protection: XSS 공격 방어
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
# 전체 HTTPS 및 보안 테스트 실행
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

**5. Rate limiting 테스트**
```bash
# 빠른 연속 요청으로 429 에러 확인
for i in {1..150}; do curl -s -o /dev/null -w "%{http_code}\n" https://your_domain.com; done
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

## 문제 해결

### KIS 분봉 API 문제
- **증상:** `time_unit` 파라미터가 제대로 작동하지 않아 모든 시간대에서 동일한 데이터 반환
- **해결:** 현재 KIS API 자체의 문제로 향후 업데이트 대기 중
- **대응:** 분봉 수집 실패 시에도 일봉 데이터로 분석 진행

### Redis 연결 실패
- Docker Compose로 Redis 실행: `docker compose up -d redis`
- 연결 테스트: `docker compose exec redis redis-cli ping`
- 로그 확인: `docker compose logs redis`

### 데이터베이스 마이그레이션 충돌
```bash
# 현재 버전 확인
uv run alembic current

# 특정 버전으로 롤백
uv run alembic downgrade <revision>

# 마이그레이션 히스토리 확인
uv run alembic history
```

### Google API 429 에러
- Redis 기반 자동 제한 시스템이 작동 중
- `debug_model_status.py`로 상태 확인
- 여러 API 키 사용: `GOOGLE_API_KEYS=key1,key2,key3`

## 참고 문서

프로젝트 루트의 다음 문서들을 참고하세요:

- `JSON_ANALYSIS_README.md` - JSON 분석 시스템 상세 가이드
- `ANALYSIS_REFACTOR_README.md` - 분석 시스템 아키텍처 및 Redis 모델 제한
- `STOCK_INFO_GUIDE.md` - 데이터베이스 정규화 구조 및 SQL 쿼리 패턴
- `UPBIT_WEBSOCKET_README.md` - Upbit WebSocket 실시간 시세
- `DEPLOYMENT.md` - 배포 가이드
- `DOCKER_USAGE.md` - Docker 사용법
