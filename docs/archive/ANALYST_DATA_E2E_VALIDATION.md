# 애널리스트 데이터 강화 E2E 검증 체크리스트

## 검증 완료 항목

다음 항목들은 이미 자동화 테스트 및 런타임 스모크 테스트로 검증 완료되었습니다:

### ✅ 1. 심볼 정규화 (숫자 입력 지원)

**검증된 함수:**
- `get_investment_opinions(symbol: str | int, ...)`
- `analyze_stock(symbol: str | int, ...)`
- `analyze_portfolio(symbols: list[str | int], ...)`
- `get_quote(symbol: str | int, ...)` ✨ **새로 추가**
- `get_valuation(symbol: str | int, ...)` ✨ **새로 추가**
- `get_news(symbol: str | int, ...)` ✨ **새로 추가**

**테스트 케이스:**
```python
# 숫자 입력이 6자리 문자열로 정규화됨
get_investment_opinions(12450, market="kr")  # → symbol="012450"
analyze_portfolio([12450, 5930], market="kr")  # → ["012450", "005930"]
get_quote(12450, market="kr")  # → symbol="012450"
get_valuation(12450, market="kr")  # → symbol="012450"
get_news(12450, market="kr")  # → symbol="012450"
```

**자동화 테스트:**
- `tests/test_mcp_server_tools.py::TestSymbolNormalizationIntegration`
- `tests/test_mcp_server_tools.py::TestAnalyzeStock::test_numeric_symbol_normalization_*`

### ✅ 2. 의견 데이터 구조 (opinions + consensus)

**검증된 항목:**
- KR: `fetch_investment_opinions()` → `opinions` + `consensus` 필드 생성
- US: `_fetch_investment_opinions_yfinance()` → `opinions` + `recommendations` (호환성 유지)
- `consensus` 필드 포함: `buy_count`, `strong_buy_count`, `hold_count`, `sell_count`, `total_count`, `count`, `avg_target_price`, `median_target_price`, `min_target_price`, `max_target_price`, `upside_pct`, `upside_potential`, `current_price`

**테스트 케이스:**
```python
# KR 응답 구조
{
  "symbol": "012450",
  "count": 10,
  "opinions": [...],
  "consensus": {
    "buy_count": 5,
    "hold_count": 3,
    "sell_count": 0,
    "avg_target_price": 60000,
    "upside_pct": 20.0,
    ...
  }
}

# US 응답 구조 (하위 호환성)
{
  "symbol": "AAPL",
  "opinions": [...],        # ✨ 새 키
  "recommendations": [...],  # 기존 키 유지
}
```

**자동화 테스트:**
- `tests/test_naver_finance.py::TestFetchInvestmentOpinions`
- `tests/test_mcp_server_tools.py::TestAnalyzeStock::test_us_investment_opinions_backwards_compatibility`

### ✅ 3. 통합 분석 응답 (recommendation 필드)

**검증된 함수:**
- `analyze_stock()` → KR/US 주식에 대해 `recommendation` 필드 생성
- `analyze_portfolio()` → 각 종목별 `recommendation` 필드 포함

**recommendation 구조 (v2):**
```python
{
  "action": "buy",              # buy/hold/sell
  "confidence": "medium",       # low/medium/high
  "buy_prices": [50000, 52000],  # [하위호환] 매수 가격 리스트
  "buy_zones": [              # [v2] 매수 구역 리스트
    {"price": 50000, "type": "support", "reasoning": "Support at 50000"},
    {"price": 49000, "type": "bollinger_lower", "reasoning": "BB lower band"},
    {"price": 49500, "type": "support_near", "reasoning": "Near support (1.0% below)"}
  ],
  "sell_prices": [60000, 65000],  # [하위호환] 매도 가격 리스트
  "sell_targets": [            # [v2] 매도 타겟 리스트
    {"price": 59500, "type": "resistance", "reasoning": "Resistance at 59500"},
    {"price": 60000, "type": "consensus_avg", "reasoning": "Analyst consensus average target"},
    {"price": 65000, "type": "consensus_max", "reasoning": "Analyst consensus max target"}
  ],
  "stop_loss": 45000,          # 손절가
  "reasoning": "기술적 지표 긍정적, 애널리스트 컨센서스 매수 우위..."  # 종합 판단 근거
}
```

**자동화 테스트:**
- `tests/test_mcp_server_tools.py::TestGetInvestmentOpinions::test_analyze_stock_generates_recommendation_kr`
- `tests/test_mcp_server_tools.py::TestGetInvestmentOpinions::test_analyze_stock_no_recommendation_crypto`

### ✅ 4. 레이팅 정규화

**검증된 항목:**
- `normalize_rating_label()` 함수: 대소문자 무관, 공백 처리, 한글/영문 매핑 → 영문 Label 반환
- `rating_to_bucket()` 함수: 영문 Label → 집계 bucket (buy/hold/sell)
- 매핑 예시:
  - `"매수"` → Label: `"Buy"`, bucket: `"buy"`
  - `"강력매수"`, `"Strong Buy"` → Label: `"Strong Buy"`, bucket: `"buy"`
  - `"중립"`, `"Hold"`, `"Market Perform"` → Label: `"Hold"`, bucket: `"hold"`
  - `"매도"`, `"Sell"`, `"Underweight"` → Label: `"Sell"` or `"Underweight"`, bucket: `"sell"`
- 각 opinion에 `rating` (Label)과 `rating_bucket` (bucket) 모두 포함

**자동화 테스트:**
- `tests/test_analyst_normalizer.py`
  - `TestNormalizeRatingLabel` (Label 정규화)
  - `TestRatingToBucket` (bucket 분류)
  - `TestIsStrongBuy` (Strong Buy 판별)
  - `TestBuildConsensus` (consensus 생성)
- `tests/test_naver_finance.py::TestFetchInvestmentOpinions::test_success`
  - rating Label 및 rating_bucket 검증

---

## 🔍 mcporter CLI E2E 검증 (수동)

> **참고:** MCP 서버가 실행 중인 환경에서 `mcporter` CLI를 통해 E2E 검증을 수행할 수 있습니다.
> 현재는 자동화 테스트로 핵심 기능을 검증했으며, 실제 MCP 클라이언트 연동은 서버 실행 환경에서 수동 검증이 필요합니다.

### 환경 요구사항
```bash
# mcporter 설치 확인
which mcporter

# MCP 서버 실행 확인 및 서버명 확인
mcporter list
# 출력 예시: auto-trader, auto_trader 등
# 아래 예시에서는 <server-name>을 실제 서버명으로 대체하세요
```

### 테스트 시나리오

#### 1️⃣ 숫자 심볼 입력 테스트

```bash
# 한화에어로스페이스 (코드: 012450)
mcporter call <server-name> get_investment_opinions '{"symbol": 12450, "market": "kr"}'
# 예상: symbol="012450", opinions 배열, consensus 객체 포함

mcporter call <server-name> get_quote '{"symbol": 12450, "market": "kr"}'
# 예상: symbol="012450", price 정보 포함

mcporter call <server-name> get_valuation '{"symbol": 12450, "market": "kr"}'
# 예상: symbol="012450", PER/PBR 등 평가지표 포함

mcporter call <server-name> analyze_portfolio '{"symbols": [12450, 5930], "market": "kr"}'
# 예상: results 키에 "012450", "005930" 포함
```

#### 2️⃣ 의견 데이터 구조 검증

```bash
# KR 주식 - consensus 확인
mcporter call <server-name> get_investment_opinions '{"symbol": "005930", "market": "kr"}' | jq '.consensus'
# 예상: buy_count, hold_count, sell_count, avg_target_price, upside_pct 등 포함

# US 주식 - opinions + recommendations 동시 존재 확인
mcporter call <server-name> get_investment_opinions '{"symbol": "AAPL", "market": "us"}' | jq 'keys'
# 예상: ["opinions", "recommendations", "symbol"] (순서 무관)
```

#### 3️⃣ 통합 분석 - recommendation 검증

```bash
# KR 주식 분석
mcporter call <server-name> analyze_stock '{"symbol": "005930", "market": "kr"}' | jq '.recommendation'
# 예상: action, confidence, buy_zones, sell_targets 포함

# US 주식 분석
mcporter call <server-name> analyze_stock '{"symbol": "AAPL", "market": "us"}' | jq '.recommendation'
# 예상: action, confidence, buy_zones, sell_targets 포함

# 암호화폐 (recommendation 없음)
mcporter call <server-name> analyze_stock '{"symbol": "KRW-BTC"}' | jq '.recommendation'
# 예상: null (암호화폐는 recommendation 미생성)
```

---

## ✅ 검증 결과 요약

| 항목 | 상태 | 검증 방법 |
|------|------|----------|
| 숫자 심볼 정규화 (6개 함수) | ✅ 완료 | 자동화 테스트 |
| opinions + consensus 구조 | ✅ 완료 | 자동화 테스트 |
| US opinions/recommendations 호환 | ✅ 완료 | 자동화 테스트 |
| recommendation 생성 (KR/US) | ✅ 완료 | 자동화 테스트 |
| 레이팅 정규화 | ✅ 완료 | 자동화 테스트 |
| mcporter CLI E2E | ⏳ 수동 검증 필요 | 위 커맨드 참조 |

---

## 🐛 알려진 제한사항

1. **mcporter CLI 자동 테스트 불가**: 현재 환경에 mcporter가 설치되지 않아 CLI 파서 경로 검증은 수동으로 수행해야 함
2. **심볼 정규화 범위**: 현재는 투자/분석 관련 주요 툴에만 적용됨. 추가 툴 적용 가능성 검토 필요
3. **레이팅 매핑**: 현재 매핑은 포괄적이나, 새로운 증권사 레이팅 용어 발견 시 `app/services/analyst_normalizer.py`의 `RATING_LABEL_MAP` 업데이트 필요

## 🚨 에러 대응 절차

### MCP 서버 실행 에러
```bash
# 1. Import 에러 발생 시
# 증상: ModuleNotFoundError, ImportError
# 해결: 필수 import 확인 (app/mcp_server/tools.py)
# - from app.services import naver_finance
# - from app.services.analyst_normalizer import build_consensus, normalize_rating_label, rating_to_bucket

# 2. 함수 호출 에러 발생 시
# 증상: AttributeError, NameError
# 해결: 사용 중인 함수가 올바른 모듈에서 import되었는지 확인
# - _normalize_rating (제거됨) → naver_finance._normalize_rating (하위 호환) 또는 normalize_rating_label + rating_to_bucket 사용

# 3. 테스트 실패 시
# 증상: pytest 실패, assertion 에러
# 해결:
uv run pytest tests/test_analyst_normalizer.py --no-cov -v  # 정규화 로직 검증
uv run pytest tests/test_naver_finance.py --no-cov -v       # Naver Finance 통합 검증
uv run pytest tests/test_mcp_server_tools.py --no-cov -v    # MCP 툴 검증
```

---

## 📝 다음 단계 (선택 사항)

- [ ] mcporter 설치 환경에서 E2E 테스트 실행 및 결과 기록
- [ ] 다른 툴(예: `get_stock_info`, `get_support_resistance`)에도 심볼 정규화 적용 검토
- [ ] 레이팅 매핑에 추가 변형 발견 시 `RATING_LABEL_MAP` (`app/services/analyst_normalizer.py`) 및 테스트 업데이트
- [ ] buy_zones/sell_targets 구조 기반 실전 매매 전략 테스트 (지지/저항 기반 분할 매수/매도)
