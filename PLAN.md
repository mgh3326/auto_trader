# 외부 브로커 수동 잔고 등록 및 통합 포트폴리오 기능 구현 계획

## 개요
토스 증권 등 외부 브로커의 잔고를 수동 등록하고, 기존 KIS 보유 종목과 통합하여 표시하는 기능을 구현합니다.
매수/매도는 KIS로만 가능하지만, 가격 결정 시 모든 브로커의 평단가를 참고할 수 있습니다.

## Phase 1: 데이터베이스 모델 및 마이그레이션

### 1.1 새 모델 파일 생성
**파일:** `app/models/manual_holdings.py`

```python
# BrokerType Enum
class BrokerType(str, enum.Enum):
    kis = "kis"
    toss = "toss"
    upbit = "upbit"

# MarketType Enum
class MarketType(str, enum.Enum):
    KR = "KR"      # 국내주식
    US = "US"      # 해외주식
    CRYPTO = "CRYPTO"  # 암호화폐

# BrokerAccount 모델
- id: BigInteger, PK
- user_id: FK -> users.id (CASCADE)
- broker_type: Enum(BrokerType)
- account_name: Text (예: "토스 메인계좌")
- is_mock: Boolean (모의투자 여부)
- is_active: Boolean
- created_at, updated_at: TIMESTAMP(timezone=True)
- UNIQUE(user_id, broker_type, account_name)

# StockAlias 모델 (종목 별칭 - 토스에서 "버크셔 해서웨이 B" 등)
- id: BigInteger, PK
- ticker: Text (정규 종목코드)
- market_type: Enum(MarketType)
- alias: Text (별칭)
- source: Text (toss/user/kis)
- UNIQUE(alias, market_type)

# ManualHolding 모델
- id: BigInteger, PK
- broker_account_id: FK -> broker_accounts.id (CASCADE)
- ticker: Text (종목코드)
- market_type: Enum(MarketType)
- quantity: Numeric(18, 8)
- avg_price: Numeric(18, 8)
- display_name: Text (표시용 이름, nullable)
- created_at, updated_at: TIMESTAMP(timezone=True)
- UNIQUE(broker_account_id, ticker, market_type)
```

### 1.2 마이그레이션 생성
```bash
uv run alembic revision --autogenerate -m "add_manual_holdings_and_broker_accounts"
```

## Phase 2: 서비스 레이어

### 2.1 BrokerAccountService
**파일:** `app/services/broker_account_service.py`

```python
class BrokerAccountService:
    - create_account(user_id, broker_type, account_name, is_mock=False)
    - get_accounts(user_id)
    - get_account_by_id(account_id)
    - update_account(account_id, data)
    - delete_account(account_id)
    - get_or_create_default_account(user_id, broker_type)
```

### 2.2 ManualHoldingsService
**파일:** `app/services/manual_holdings_service.py`

```python
class ManualHoldingsService:
    - create_holding(broker_account_id, ticker, market_type, quantity, avg_price, display_name=None)
    - get_holdings(broker_account_id)
    - get_holdings_by_user(user_id)
    - update_holding(holding_id, data)
    - delete_holding(holding_id)
    - get_holding_by_ticker(broker_account_id, ticker, market_type)
```

### 2.3 StockAliasService
**파일:** `app/services/stock_alias_service.py`

```python
class StockAliasService:
    - create_alias(ticker, market_type, alias, source)
    - search_by_alias(query, market_type) -> List[StockAlias]
    - get_ticker_by_alias(alias, market_type) -> Optional[str]
    - bulk_create_aliases(aliases_data)
```

### 2.4 MergedPortfolioService
**파일:** `app/services/merged_portfolio_service.py`

```python
class MergedPortfolioService:
    - get_merged_portfolio(user_id, market_type=None) -> List[MergedHolding]
    - get_reference_prices(user_id, ticker, market_type) -> ReferencePrices
    - calculate_combined_avg(holdings: List[HoldingInfo]) -> float

# 데이터 클래스
@dataclass
class HoldingInfo:
    broker: str
    quantity: float
    avg_price: float

@dataclass
class ReferencePrices:
    kis_avg: Optional[float]
    kis_quantity: Optional[int]
    toss_avg: Optional[float]
    toss_quantity: Optional[int]
    combined_avg: Optional[float]
    total_quantity: int

@dataclass
class MergedHolding:
    ticker: str
    name: str
    market_type: str
    holdings: List[HoldingInfo]
    kis_quantity: int
    kis_avg_price: float
    toss_quantity: int
    toss_avg_price: float
    combined_avg_price: float
    current_price: float
```

### 2.5 TradingPriceService (매수/매도 가격 전략)
**파일:** `app/services/trading_price_service.py`

```python
class PriceStrategy(str, enum.Enum):
    # 매수 전략
    current = "current"
    kis_avg = "kis_avg"
    toss_avg = "toss_avg"
    combined_avg = "combined_avg"
    lowest_avg = "lowest_avg"
    lowest_minus_percent = "lowest_minus_percent"
    manual = "manual"

    # 매도 전략
    kis_avg_plus = "kis_avg_plus"
    toss_avg_plus = "toss_avg_plus"
    combined_avg_plus = "combined_avg_plus"

class TradingPriceService:
    - calculate_buy_price(
        reference_prices: ReferencePrices,
        current_price: float,
        strategy: PriceStrategy,
        discount_percent: float = 0,
        manual_price: float = None
      ) -> Tuple[float, str]  # (price, price_source_description)

    - calculate_sell_price(
        reference_prices: ReferencePrices,
        current_price: float,
        strategy: PriceStrategy,
        profit_percent: float = 5.0,
        manual_price: float = None
      ) -> Tuple[float, str]

    - calculate_expected_profit(
        quantity: int,
        sell_price: float,
        reference_prices: ReferencePrices
      ) -> Dict[str, ExpectedProfit]
```

## Phase 3: API 엔드포인트

### 3.1 브로커 계좌 관리
**파일:** `app/routers/broker_accounts.py`

```
POST   /api/broker-accounts         - 브로커 계좌 생성
GET    /api/broker-accounts         - 내 브로커 계좌 목록
PUT    /api/broker-accounts/{id}    - 계좌 수정
DELETE /api/broker-accounts/{id}    - 계좌 삭제
```

### 3.2 수동 잔고 관리
**파일:** `app/routers/manual_holdings.py`

```
POST   /api/manual-holdings         - 수동 잔고 등록
GET    /api/manual-holdings         - 수동 잔고 목록
PUT    /api/manual-holdings/{id}    - 잔고 수정
DELETE /api/manual-holdings/{id}    - 잔고 삭제
GET    /api/stock-aliases/search    - 종목 별칭 검색
```

### 3.3 통합 포트폴리오
**파일:** `app/routers/portfolio.py`

```
GET /api/portfolio/merged           - 통합 포트폴리오 조회
GET /api/portfolio/merged/{ticker}  - 특정 종목 상세 (참조 가격 포함)
```

### 3.4 트레이딩 API (기존 확장)
**파일:** `app/routers/trading.py` (신규)

```
POST /api/trading/buy
Request:
{
  "ticker": "005930",
  "market_type": "KR",
  "quantity": 10,
  "price_strategy": "combined_avg",
  "discount_percent": 1.0,
  "manual_price": null,
  "dry_run": true
}

Response:
{
  "status": "simulated" | "submitted",
  "order_price": 72270,
  "price_source": "통합 평단가 -1%",
  "current_price": 75000,
  "reference_prices": {
    "kis_avg": 74000,
    "toss_avg": 73000,
    "combined_avg": 73667
  }
}

POST /api/trading/sell
Request:
{
  "ticker": "005930",
  "market_type": "KR",
  "quantity": 5,
  "price_strategy": "combined_avg_plus",
  "profit_percent": 5.0,
  "manual_price": null,
  "dry_run": true
}

Response:
{
  "status": "simulated" | "submitted",
  "order_price": 77350,
  "price_source": "통합 평단가 +5%",
  "current_price": 75000,
  "reference_prices": {...},
  "expected_profit": {
    "based_on_kis_avg": {"amount": 33500, "percent": 4.53},
    "based_on_toss_avg": {"amount": 43500, "percent": 5.96},
    "based_on_combined_avg": {"amount": 36830, "percent": 5.0}
  },
  "warning": "KIS 보유 수량(10주) 내에서만 매도 가능"
}
```

## Phase 4: Pydantic 스키마

### 4.1 스키마 파일
**파일:** `app/schemas/manual_holdings.py`

```python
# Request 스키마
class BrokerAccountCreate(BaseModel)
class BrokerAccountUpdate(BaseModel)
class ManualHoldingCreate(BaseModel)
class ManualHoldingUpdate(BaseModel)
class BuyOrderRequest(BaseModel)
class SellOrderRequest(BaseModel)

# Response 스키마
class BrokerAccountResponse(BaseModel)
class ManualHoldingResponse(BaseModel)
class MergedHoldingResponse(BaseModel)
class ReferencePricesResponse(BaseModel)
class OrderSimulationResponse(BaseModel)
class ExpectedProfitResponse(BaseModel)
```

## Phase 5: 프론트엔드 UI

### 5.1 통합 대시보드 템플릿
**파일:** `app/templates/portfolio_dashboard.html`

- 상단: 총 자산 요약 (KIS + 토스 합산)
- 보유 종목 테이블:
  | 종목명 | 브로커별 보유 | 평단가 | 총수량 | 수익률 | AI분석 | 관리 |
- 브로커별 보유 컬럼: 토스 아이콘 + 수량, 한투 아이콘 + 수량
- 수익률: 통합 평단가 기준, 툴팁에 개별 평단가 기준 표시
- 관리 버튼: [분석] [매수] [매도] (KIS 보유분 있을 때만)

### 5.2 수동 잔고 관리 모달
**매수 모달:**
```
┌─────────────────────────────────────────────────┐
│  삼성전자 (005930) 매수                          │
├─────────────────────────────────────────────────┤
│  현재가: 75,000원                                │
│                                                  │
│  📊 보유 평단가 참고                             │
│  ┌─────────────────────────────────────────┐    │
│  │ 한투: 74,000원 (10주)                    │    │
│  │ 토스: 73,000원 (5주)                     │    │
│  │ 통합: 73,667원 (15주 가중평균)            │    │
│  └─────────────────────────────────────────┘    │
│                                                  │
│  매수 가격 선택:                                 │
│  ○ 현재가 (75,000원)                            │
│  ○ 한투 평단가 (74,000원)                       │
│  ○ 토스 평단가 (73,000원)                       │
│  ○ 통합 평단가 (73,667원)                       │
│  ○ 최저 평단가 -1% (72,270원)                   │
│  ○ 직접 입력: [________] 원                     │
│                                                  │
│  매수 수량: [____] 주                            │
│                                                  │
│        [취소]  [시뮬레이션]  [매수 주문]         │
└─────────────────────────────────────────────────┘
```

**매도 모달:**
```
┌─────────────────────────────────────────────────┐
│  삼성전자 (005930) 매도                          │
├─────────────────────────────────────────────────┤
│  현재가: 75,000원                                │
│  매도 가능: 10주 (한투 보유분만 매도 가능)        │
│                                                  │
│  📊 보유 평단가 참고                             │
│  ┌─────────────────────────────────────────┐    │
│  │ 한투: 74,000원 (10주) → 현재가 대비 +1.4% │    │
│  │ 토스: 73,000원 (5주)  → 현재가 대비 +2.7% │    │
│  │ 통합: 73,667원 (15주) → 현재가 대비 +1.8% │    │
│  └─────────────────────────────────────────┘    │
│                                                  │
│  매도 가격 선택:                                 │
│  ○ 현재가 (75,000원)                            │
│  ○ 한투 평단가 +5% (77,700원)                   │
│  ○ 토스 평단가 +5% (76,650원)                   │
│  ○ 통합 평단가 +5% (77,350원)                   │
│  ○ 직접 입력: [________] 원                     │
│                                                  │
│  목표 수익률: [__5__] %  (평단가 기준 선택 시)   │
│                                                  │
│  매도 수량: [____] 주 (최대 10주)                │
│                                                  │
│  📈 예상 수익 (10주 매도 시)                     │
│  ┌─────────────────────────────────────────┐    │
│  │ 한투 평단가 기준: +33,500원 (+4.5%)      │    │
│  │ 토스 평단가 기준: +43,500원 (+6.0%)      │    │
│  │ 통합 평단가 기준: +36,830원 (+5.0%)      │    │
│  └─────────────────────────────────────────┘    │
│                                                  │
│        [취소]  [시뮬레이션]  [매도 주문]         │
└─────────────────────────────────────────────────┘
```

### 5.3 수동 잔고 입력 폼
**파일 내 모달 또는 별도 페이지**

```
┌─────────────────────────────────────────────────┐
│  수동 잔고 등록                                  │
├─────────────────────────────────────────────────┤
│  브로커: [토스 ▼]                                │
│  시장: [국내주식 ▼] [해외주식 ▼]                 │
│                                                  │
│  종목 검색: [삼성전자______] (자동완성)          │
│  또는 직접 입력:                                 │
│  - 종목코드: [005930]                           │
│  - 종목명: [삼성전자]                           │
│                                                  │
│  보유 수량: [____] 주                           │
│  평균 매수가: [______] 원                       │
│                                                  │
│        [취소]  [등록]                           │
└─────────────────────────────────────────────────┘
```

## Phase 6: 기존 대시보드 통합

### 6.1 kis_domestic_trading_dashboard.html 수정
- 보유 종목 테이블에 "브로커별 보유" 컬럼 추가
- 수익률 컬럼을 통합 평단가 기준으로 변경
- 관리 버튼에 매수/매도 모달 연결
- API 호출을 `/api/portfolio/merged?market_type=KR`로 변경

### 6.2 kis_overseas_trading_dashboard.html 수정
- 동일한 패턴으로 해외주식 지원

### 6.3 네비게이션 메뉴 추가
- "수동 잔고 관리" 메뉴 항목 추가

## 구현 순서

1. **Phase 1**: DB 모델 및 마이그레이션 (1단계)
2. **Phase 2**: 서비스 레이어 구현 (2단계)
3. **Phase 4**: Pydantic 스키마 (3단계 - API 전)
4. **Phase 3**: API 엔드포인트 (4단계)
5. **Phase 5 & 6**: 프론트엔드 UI (5단계)
6. **테스트**: 단위 테스트 및 통합 테스트

## 파일 생성/수정 목록

### 새로 생성할 파일:
- `app/models/manual_holdings.py`
- `app/services/broker_account_service.py`
- `app/services/manual_holdings_service.py`
- `app/services/stock_alias_service.py`
- `app/services/merged_portfolio_service.py`
- `app/services/trading_price_service.py`
- `app/schemas/manual_holdings.py`
- `app/routers/broker_accounts.py`
- `app/routers/manual_holdings.py`
- `app/routers/portfolio.py`
- `app/routers/trading.py`
- `app/templates/portfolio_dashboard.html`
- `alembic/versions/xxx_add_manual_holdings.py`

### 수정할 파일:
- `app/models/__init__.py` - 새 모델 export
- `app/main.py` - 새 라우터 등록
- `app/templates/kis_domestic_trading_dashboard.html` - 통합 UI
- `app/templates/kis_overseas_trading_dashboard.html` - 통합 UI
- `app/templates/nav.html` - 메뉴 추가

## 기술적 고려사항

1. **현재가 조회**: KIS API를 통해 실시간 현재가 조회
2. **해외주식 환율**: USD 가격은 원화 환산 없이 USD로 표시
3. **캐싱**: 현재가는 짧은 TTL로 Redis 캐싱 고려
4. **트랜잭션**: 주문 시 dry_run으로 먼저 검증 후 실제 주문
5. **권한**: 사용자별 데이터 격리 (user_id 기반)
