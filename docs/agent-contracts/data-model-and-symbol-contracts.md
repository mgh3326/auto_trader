# 데이터 모델·심볼 계약

이 파일은 작업 분야와 무관하게 작업 시작 전에 끝까지 읽어야 하는 필독 계약의 일부다. 찾아보기는 탐색 보조일 뿐 선택 읽기 면제가 아니다.

## 목차

- 데이터베이스 정규화 구조
- 해외주식 심볼 변환 시스템
- API 서비스 클라이언트
- 데이터 구조
- Trading Policy YAML 단일 소스 (ROB-646)

## 기준 원문 계약

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
- `app/services/brokers/kis/` - KIS API 호출 시 자동 변환
- `app/services/brokers/yahoo/client.py` - Yahoo Finance 호출 시 자동 변환
- `app/jobs/` - 심볼 비교 시 정규화 (주요 브로커/job 호출부에 배선)
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
app/services/brokers/
├── upbit/       # Upbit API (암호화폐) — client.py, orders.py, public_trades.py
├── yahoo/       # Yahoo Finance API — client.py
├── kis/         # 한국투자증권 API — client.py, account.py, domestic/overseas_orders.py, market_data 등 (파일 분할)
└── toss/ · kiwoom/ · alpaca/ · binance/   # 기타 브로커
app/services/
├── upbit_websocket.py       # Upbit 실시간 시세
└── redis_token_manager.py   # Redis 기반 토큰 관리
```

**주의사항:**
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

## 웹 대시보드

### Trading Policy YAML 단일 소스 (ROB-646)

`config/trading_policy.yaml` = 매매 판단 임계값/decision rule 단일 소스 (ROB-643 플레이북 policy_keys에서 시드). **operator PR로만 편집 — 쓰기 도구 없음.**
정규 발굴 독립 지지 계열 최소 개수는 `screen.independent_support_source_count_min`으로 reserve-net과 분리되며, 이는 완화가 아니다.

- **스키마/로더**: `app/schemas/trading_policy.py`, `app/services/trading_policy_service.py`
- **MCP 도구**: `get_trading_policy(market, lane)` — market×lane 임계값 + lane-scoped `decision_rules` + `{version, content_hash}` echo; 없는 키는 `success=false, error=unknown_key`
- **버전 스탬핑 계약**: 판정 기록(evidence_snapshot·trade_retrospectives·forecast)은 `{version, content_hash}` 인용. `get_operating_briefing`가 run-start에 `policy_version` echo.
- **강제 범위**: 섹터 클러스터 집중도는 매수 프리뷰와 reserve-net consumer에서 계산한다. `order_validation`은 `sector_concentration` 필드로, reserve-net은 `plan.sector_cluster_cap_advisories` 및 생성 proposal의 `source_asof.sector_cluster_cap_advisories`로 **fail-open 경고·기록만** 남긴다. `portfolio.sector_cluster_cap_pct` 초과 자체는 차단 근거가 아니다. 단, 섹터 미상·음수 집중도 데이터·`max_symbols_per_sector_cluster` 등 별도 코드 가드는 그대로 fail-closed다. 나머지 임계값은 advisory.
- **관할**: 판단 임계값/decision rule 전용. fail-closed 코드 가드(손실매도/ladder/RSI 스코어링)·`symbol_trade_settings`(라이브 사이징)·`trade_profile`(dead)와 분리. migration 0.


## 유지 규약

새 기능·안전 경계·계약을 추가하거나 변경할 때에는 관련 분야 계약을 같은 PR에서 갱신하고, 필요하면 두 진입점의 최소 하드룰과 명시적 필독 목록도 함께 갱신한다.
