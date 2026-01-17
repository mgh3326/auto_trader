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
- Python 3.13+
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
make lint                         # Ruff + Pyright 검사
make format                       # Ruff로 코드 포맷팅
make typecheck                    # Pyright 타입 체킹
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

### 해외주식 심볼 변환 시스템

**배경:** 해외주식 심볼은 서비스마다 다른 구분자를 사용함 (예: 버크셔 해서웨이 B)
- Yahoo Finance: `BRK-B` (하이픈)
- 한국투자증권 API: `BRK/B` (슬래시)
- DB 저장 형식: `BRK.B` (점) ← **기준**

**구조:**
```
app/core/symbol.py              # 심볼 변환 유틸리티
├── to_kis_symbol()             # DB → KIS API (. → /)
├── to_yahoo_symbol()           # DB → Yahoo Finance (. → -)
└── to_db_symbol()              # 외부 → DB (- 또는 / → .)
```

**적용된 파일:**
- `app/services/kis.py` - KIS API 호출 시 자동 변환
- `app/services/yahoo.py` - Yahoo Finance 호출 시 자동 변환
- `app/tasks/kis.py` - 심볼 비교 시 정규화
- `app/services/kis_holdings_service.py` - 보유주식 조회 시 정규화
- `app/services/kis_trading_service.py` - 매도 주문 시 정규화

**DB 테이블 (해외주식 심볼 저장):**
| 테이블 | 컬럼 | 설명 |
|--------|------|------|
| `stock_info` | `symbol` | 종목 마스터 |
| `manual_holdings` | `ticker` | 수동 잔고 (토스 등) |
| `stock_aliases` | `ticker` | 종목 별칭 매핑 |
| `symbol_trade_settings` | `symbol` | 종목별 거래 설정 |

**마이그레이션:** 기존 데이터가 `-` 또는 `/` 형식이면 `.` 형식으로 변환 필요
```bash
# scripts/migrate_symbols_to_dot_format.sql 실행
psql -d your_db -f scripts/migrate_symbols_to_dot_format.sql
```

**테스트:**
```bash
uv run pytest tests/test_symbol_conversion.py -v
```

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
