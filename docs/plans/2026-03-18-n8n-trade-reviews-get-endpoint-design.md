# n8n Trade Reviews: GET 조회 엔드포인트 추가 및 API 문서화

> **Issue**: [#341](https://github.com/mgh3326/auto_trader/issues/341)
> **Priority**: Low
> **Date**: 2026-03-18

## 목표

1. `GET /api/n8n/trade-reviews` 엔드포인트 추가 — 저장된 리뷰를 날짜/종목별로 조회
2. n8n 엔드포인트 전체 API 레퍼런스 문서 작성

## 현재 상태

### 기존 n8n 엔드포인트 (12개)

| # | Method | Path | 용도 |
|---|--------|------|------|
| 1 | `GET` | `/api/n8n/pending-orders` | 미체결 주문 조회 |
| 2 | `GET` | `/api/n8n/market-context` | 시장 기술적 지표 |
| 3 | `GET` | `/api/n8n/daily-brief` | 일일 트레이딩 브리프 |
| 4 | `GET` | `/api/n8n/filled-orders` | 체결 주문 이력 |
| 5 | `POST` | `/api/n8n/trade-reviews` | 리뷰 저장 |
| 6 | `GET` | `/api/n8n/trade-reviews/stats` | 리뷰 통계 |
| 7 | `GET` | `/api/n8n/pending-review` | 리뷰 대기 주문 |
| 8 | `POST` | `/api/n8n/pending-snapshots` | 스냅샷 저장 |
| 9 | `PATCH` | `/api/n8n/pending-snapshots/resolve` | 스냅샷 해결 |
| 10 | `GET` | `/api/n8n/crypto-scan` | 암호화폐 스캔 |
| 11 | `GET` | `/api/n8n/scan/strategy` | 전략 스캔 |
| 12 | `GET` | `/api/n8n/scan/crash` | 급락 감지 |

### 관련 코드

- **Router**: `app/routers/n8n.py`, `app/routers/n8n_scan.py`
- **Models**: `app/models/review.py` — `Trade`, `TradeReview`, `TradeSnapshot`
- **Schemas**: `app/schemas/n8n.py`
- **Service**: `app/services/n8n_trade_review_service.py`
- **Tests**: `tests/test_n8n_trade_review.py`

## 설계

### 1. GET `/api/n8n/trade-reviews` 엔드포인트

**Query Parameters:**

| Param | Type | Default | Validation | Description |
|-------|------|---------|------------|-------------|
| `period` | `str` | `"7d"` | regex `^\d+d$` | Duration 포맷 (7d, 30d, 90d 등) |
| `market` | `str \| None` | `None` | `crypto`, `kr`, `us` | 마켓 필터 |
| `symbol` | `str \| None` | `None` | — | 종목 필터 (BTC, 005930 등) |
| `limit` | `int` | `100` | `ge=1, le=500` | 최대 반환 건수 |

**Period 파싱 로직:**
- `"7d"` → `timedelta(days=7)`
- `now_kst() - delta`를 start로 사용
- 기존 stats 엔드포인트의 `week/month/quarter`와 다른 포맷이지만, n8n 워크플로우에서 동적 기간 설정이 가능하도록 duration 포맷 채택

**DB 쿼리:**
- `Trade` JOIN `TradeReview` JOIN `TradeSnapshot` (LEFT JOIN for snapshot)
- `Trade.trade_date >= start` AND `Trade.trade_date <= now`
- market 필터: `Trade.instrument_type` 매핑 (crypto→crypto, kr→equity_kr, us→equity_us)
- symbol 필터: `Trade.symbol == symbol`
- ORDER BY `Trade.trade_date DESC`
- LIMIT 적용

**응답 스키마:**

```python
class N8nTradeReviewListItem(BaseModel):
    # trade 정보
    order_id: str
    symbol: str
    market: str               # crypto, kr, us (instrument_type에서 역매핑)
    side: str                 # buy, sell
    price: float
    quantity: float
    total_amount: float
    fee: float
    currency: str
    filled_at: str            # trade_date ISO8601
    # review 정보
    verdict: str              # good, neutral, bad
    pnl_pct: float | None
    comment: str | None
    review_type: str          # daily, weekly, monthly, manual
    review_date: str          # ISO8601
    # snapshot 지표 (nullable)
    indicators: N8nTradeReviewIndicators | None

class N8nTradeReviewListResponse(BaseModel):
    success: bool
    period: str               # "2026-03-11 ~ 2026-03-18"
    total_count: int
    reviews: list[N8nTradeReviewListItem]
    errors: list[dict[str, object]]
```

기존 `N8nTradeReviewIndicators` 스키마를 그대로 재사용.

**에러 처리:** 기존 n8n 엔드포인트 패턴과 동일 — try/except에서 500 JSONResponse 반환.

### 2. API 문서화

**파일:** `docs/n8n-api-reference.md`

기존 `n8n/README.md`는 인프라/배포 문서이므로 분리. 내용:
- 인증 방식 (N8N_API_KEY 헤더)
- 전체 13개 엔드포인트 레퍼런스 테이블
- 각 엔드포인트: method, path, params, response 요약, curl 예시

## 변경 범위

| 파일 | 변경 | 설명 |
|------|------|------|
| `app/schemas/n8n.py` | 추가 | `N8nTradeReviewListItem`, `N8nTradeReviewListResponse` |
| `app/services/n8n_trade_review_service.py` | 추가 | `get_trade_reviews()` 함수 |
| `app/routers/n8n.py` | 추가 | GET `/trade-reviews` 핸들러 |
| `tests/test_n8n_trade_review.py` | 추가 | GET 엔드포인트 단위 테스트 |
| `docs/n8n-api-reference.md` | 생성 | API 레퍼런스 문서 |
