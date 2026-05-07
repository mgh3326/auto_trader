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
make lint                         # Ruff + ty 검사
make format                       # Ruff로 코드 포맷팅
make typecheck                    # ty 타입 체킹
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
python manage_users.py list                 # 사용자 권한/상태 확인
python websocket_monitor.py --mode both     # 통합 WebSocket 모니터링
python kis_websocket_monitor.py             # KIS WebSocket 모니터링
python upbit_websocket_monitor.py           # Upbit WebSocket 모니터링
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
4. 제한 해제: TTL 만료 시 자동 해제 (필요 시 Redis 키/TTL 점검으로 상태 확인)

**Redis 키 구조:**
- `model_rate_limit:{model}:{masked_api_key}` - 제한 정보
- `model_retry_info:{model}:{masked_api_key}` - 재시도 정보

### Alpaca Paper 실행 레저 (ROB-84)

`review.alpaca_paper_order_ledger` — Alpaca Paper 주문 라이프사이클 레코드 (previewed → canceled/filled/unexpected).

- **ORM 모델**: `app/models/review.AlpacaPaperOrderLedger`
- **서비스**: `app/services/alpaca_paper_ledger_service.AlpacaPaperLedgerService` — 모든 쓰기는 이 서비스를 통해서만 허용
- **라우터**: `app/routers/alpaca_paper_ledger.py` — GET 전용 (`/trading/api/alpaca-paper/ledger/...`)
- **MCP 도구**: `alpaca_paper_ledger_list_recent`, `alpaca_paper_ledger_get` (read-only)
- **런북**: `docs/runbooks/alpaca-paper-ledger.md`

**주의**: 서비스는 브로커 mutation 없음. 직접 SQL INSERT/UPDATE/DELETE 금지.

### Weekend Crypto Paper Cycle Runner (ROB-94)

주말 암호화폐 Alpaca Paper 매수/매도 사이클 실행 러너.

- **서비스**: `app/services/weekend_crypto_paper_cycle_runner.WeekendCryptoPaperCycleRunner`
- **MCP 도구**: `weekend_crypto_paper_cycle_run` (기본: dry-run)
- **CLI**: `scripts/run_weekend_crypto_paper_cycle.py`
- **런북**: `docs/runbooks/weekend-crypto-paper-cycle.md`

**안전 경계**: max 3 candidates, $10 notional 상한, BTC/USD|ETH/USD|SOL/USD 한정, limit 주문 전용, Alpaca Paper 전용.
기본값은 항상 `dry_run=True`. execute는 `dry_run=False`, `confirm=True`, operator token, per-candidate approval token 필요.

### KIS WebSocket Mock Smoke (ROB-104)

`scripts/kis_websocket_mock_smoke.py` — KIS 모의 WebSocket 핸드셰이크 검증 (주문/체결/Redis publish 없음).

- **CLI**: `uv run python -m scripts.kis_websocket_mock_smoke`
- **런북**: `docs/runbooks/kis-websocket-mock-smoke.md`
- **이벤트 태깅**: `app/services/kis_websocket_internal/events.py::build_lifecycle_event` (ROB-100 `OrderLifecycleEvent`)

### Market Events Ingestion Foundation (ROB-128)

시장 이벤트 (US earnings, KR DART 공시, 향후 crypto/economic) 수집·저장·조회 foundation.

- **모델**: `app/models/market_events.py` — `MarketEvent`, `MarketEventValue`, `MarketEventIngestionPartition`
- **서비스**: `app/services/market_events/` — `repository`, `ingestion`, `query_service`, `normalizers`, `taxonomy`
- **라우터**: `app/routers/market_events.py` — GET `/trading/api/market-events/today`, `/range` (read-only)
- **CLI**: `scripts/ingest_market_events.py` — `--source finnhub|dart --category earnings|disclosure --market us|kr --from-date --to-date [--dry-run]`
- **런북**: `docs/runbooks/market-events-ingestion.md`

**안전 경계**: read-mostly 마켓 데이터, 브로커/주문/감시 mutation 없음. `raw_payload_json` 은 저장 전 `_redact_sensitive_keys` 적용. 모든 DB 쓰기는 `MarketEventsRepository` 경유. Prefect 배포는 후속 작업.

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

**KR/US 심볼 유니버스 (DB 단일 소스):**
```
app/services/
├── kr_symbol_universe_service.py   # KR 심볼 조회/동기화
├── upbit_symbol_universe_service.py # Upbit 심볼 조회/동기화
└── us_symbol_universe_service.py   # US 심볼 조회/동기화

scripts/
├── sync_kr_symbol_universe.py      # KR 유니버스 DB 동기화
├── sync_upbit_symbol_universe.py   # Upbit 유니버스 DB 동기화
└── sync_us_symbol_universe.py      # US 유니버스 DB 동기화

DB Tables:
├── kr_symbol_universe
├── upbit_symbol_universe
└── us_symbol_universe
```

**특징:**
- KR/US 종목 검색 및 라우팅은 DB 테이블을 단일 소스로 사용
- Upbit 심볼/마켓 해석도 DB 테이블(`upbit_symbol_universe`)을 단일 소스로 사용
- 배포/마이그레이션 직후 심볼 유니버스 sync 스크립트 실행이 필요

## 브랜치 & PR 워크플로우

### 브랜치 보호
- **main**, **production** 브랜치는 보호됨 — 직접 push 금지
- 모든 코드 변경은 Pull Request를 통해 머지

### 브랜치 역할
- **main**: 개발 브랜치 (모든 PR의 base)
- **production**: 배포 브랜치 (GHCR 이미지 빌드 트리거)

### 브랜치 네이밍
```
feature/<task-id>-<설명>     # 새 기능 (예: feature/ROB-16-branch-protection)
fix/<task-id>-<설명>         # 버그 수정
chore/<설명>                 # 유지보수
```

### 워크플로우
1. `main` 브랜치에서 feature branch 생성
2. 코드 변경 후 커밋 (`Co-Authored-By: Paperclip <noreply@paperclip.ing>`)
3. PR 생성 (base: `main`)
4. 리뷰 후 머지
5. 배포 시 `main` → `production` 머지

### Worktree 운영 규칙 (필수)

**루트 `/home/mgh3326/auto_trader` 는 항상 `main` 체크아웃 고정. 배포 머지 시에만 `production` 으로 일시 전환.** 루트에서 feature/fix 브랜치를 체크아웃하거나 작업하지 않습니다.

모든 코드 변경은 worktree 에서 수행합니다:

```bash
# 새 작업 시작
cd ~/auto_trader && git switch main && git pull
git worktree add ~/auto_trader/.worktrees/<ISSUE-ID> -b feature/<ISSUE-ID>-<desc> main

# 작업
cd ~/auto_trader/.worktrees/<ISSUE-ID>
# ... 커밋, 푸시, PR ...

# PR 머지 후 (24h 내)
cd ~/auto_trader
git worktree remove .worktrees/<ISSUE-ID>
git branch -D feature/<ISSUE-ID>-<desc>
```

- **표준 worktree 경로**: `~/auto_trader/.worktrees/<ISSUE-ID>` (대문자 ISSUE-ID 권장)
- 이전 경로 `.claude/worktrees/`, `~/.claude/worktrees/` 는 deprecated — 남아 있다면 표준 경로로 이관하거나 prune
- 이관: `git worktree move <old-path> <new-path>` (dirty 없는 상태에서)
- 삭제된 원격 브랜치(`upstream gone`) 는 주기적으로 `git fetch --prune && git branch -vv | grep ': gone\]'` 로 확인하고 정리

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
# Redis 연결 확인
docker compose exec redis redis-cli ping

# 제한 키 조회
docker compose exec redis redis-cli --scan --pattern "model_rate_limit:*"

# 특정 제한 키 TTL 확인
docker compose exec redis redis-cli ttl "model_rate_limit:<model>:<masked_api_key>"
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

# TradingAgents advisory runner (ROB-9, 선택사항)
TRADINGAGENTS_REPO_PATH=/path/to/TradingAgents
TRADINGAGENTS_PYTHON=/path/to/TradingAgents/.venv/bin/python
TRADINGAGENTS_RUNNER_PATH=/path/to/run_auto_trader_research.py
# 전체 설정과 안전 제약은 docs/plans/ROB-9-tradingagents-auto-trader-integration-plan.md 참고
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
- 제한은 TTL 만료로 자동 해제되며, 필요 시 `model_rate_limit:*` 키와 TTL을 Redis에서 점검
- 여러 API 키 사용: `GOOGLE_API_KEYS=key1,key2,key3`

## 참고 문서

프로젝트 루트의 다음 문서들을 참고하세요:

- `docs/archive/JSON_ANALYSIS_README.md` - JSON 분석 시스템 상세 가이드
- `docs/archive/ANALYSIS_REFACTOR_README.md` - 분석 시스템 아키텍처 및 Redis 모델 제한
- `STOCK_INFO_GUIDE.md` - 데이터베이스 정규화 구조 및 SQL 쿼리 패턴
- `UPBIT_WEBSOCKET_README.md` - Upbit WebSocket 실시간 시세
- `DEPLOYMENT.md` - 배포 가이드
- `DOCKER_USAGE.md` - Docker 사용법
