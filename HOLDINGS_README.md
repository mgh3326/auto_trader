# 보유 자산 관리 시스템

KIS(한국투자증권)와 Upbit(업비트)에서 보유 중인 주식 및 암호화폐 정보를 자동으로 가져와 `user_watch_items` 테이블에 저장하고 관리하는 시스템입니다.

## 주요 기능

### 1. 자동 보유 자산 수집
- **국내 주식**: KIS API를 통해 보유 중인 국내 주식 조회
- **미국 주식**: KIS API를 통해 보유 중인 미국 주식 조회 (NASDAQ, NYSE, AMEX)
- **암호화폐**: Upbit API를 통해 보유 중인 암호화폐 조회

### 2. 데이터베이스 저장
- `user_watch_items` 테이블에 보유 자산 정보 저장
- 기존 데이터는 업데이트, 신규 자산은 자동 추가
- `updated_at` 필드로 마지막 업데이트 시각 추적

### 3. 웹 대시보드
- 보유 자산 목록을 테이블 형식으로 표시
- 상품 타입별 필터링 (국내주식/미국주식/암호화폐)
- 실시간 갱신 기능
- 통계 대시보드 (보유 종목 수, 타입별 개수)

## 파일 구조

```
app/
├── services/
│   └── holdings_service.py         # 보유 자산 관리 서비스
├── routers/
│   └── holdings.py                  # API 엔드포인트
└── templates/
    └── holdings_dashboard.html      # 웹 대시보드 UI

test_holdings.py                     # 테스트 스크립트
```

## API 엔드포인트

### 1. 웹 대시보드
```
GET /holdings/
```
보유 자산 관리 웹 인터페이스

### 2. 보유 자산 갱신
```
POST /holdings/api/refresh?is_mock={true|false}
```
KIS와 Upbit에서 현재 보유 자산을 가져와 데이터베이스 업데이트

**파라미터:**
- `is_mock` (boolean): KIS 모의투자 여부 (기본값: false)

**응답 예시:**
```json
{
  "success": true,
  "message": "보유 자산 갱신 완료",
  "data": {
    "updated_at": "2025-10-22T...",
    "kr_stocks": {
      "count": 5,
      "items": [{"symbol": "005930", "name": "삼성전자", "quantity": 10}]
    },
    "us_stocks": {
      "count": 3,
      "items": [{"symbol": "AAPL", "name": "Apple Inc.", "quantity": 5}]
    },
    "crypto": {
      "count": 2,
      "items": [{"symbol": "KRW-BTC", "name": "BTC", "quantity": 0.5}]
    },
    "errors": []
  }
}
```

### 3. 보유 자산 목록 조회
```
GET /holdings/api/list?instrument_type={type}
```
저장된 보유 자산 목록 조회

**파라미터:**
- `instrument_type` (string, optional): 상품 타입 필터 ("equity_kr", "equity_us", "crypto")

**응답 예시:**
```json
{
  "success": true,
  "count": 10,
  "data": [
    {
      "id": 1,
      "symbol": "005930",
      "name": "삼성전자",
      "instrument_type": "equity_kr",
      "exchange": "한국거래소",
      "exchange_code": "KRX",
      "quantity": 10,
      "desired_buy_px": 70000,
      "target_sell_px": 80000,
      "stop_loss_px": 65000,
      "note": null,
      "created_at": "2025-10-22T...",
      "updated_at": "2025-10-22T..."
    }
  ]
}
```

### 4. 통계 조회
```
GET /holdings/api/statistics
```
보유 자산 통계 정보

**응답 예시:**
```json
{
  "success": true,
  "data": {
    "total_count": 10,
    "kr_stocks_count": 5,
    "us_stocks_count": 3,
    "crypto_count": 2,
    "last_updated": "2025-10-22T..."
  }
}
```

### 5. 필터 옵션 조회
```
GET /holdings/api/filters
```
사용 가능한 필터 옵션

## 사용 방법

### 0. 초기 설정 (최초 1회만)
```bash
# 기본 사용자 생성
poetry run python create_default_user.py
```

### 1. 테스트 스크립트 실행
```bash
# 보유 자산 수집 및 저장 테스트
poetry run python test_holdings.py
```

### 2. 웹 대시보드 사용
```bash
# 개발 서버 시작
make dev
# 또는
poetry run uvicorn app.main:app --reload
```

브라우저에서 접속:
```
http://127.0.0.1:8000/holdings/
```

**대시보드 기능:**
1. **조회 버튼**: 저장된 보유 자산 목록 표시
2. **갱신 버튼**: KIS/Upbit에서 최신 데이터 가져와 업데이트
3. **상품 타입 필터**: 국내주식/미국주식/암호화폐별 필터링
4. **투자 모드 선택**: KIS 실전투자/모의투자 전환

### 3. 주기적 갱신 설정 (선택사항)

Celery나 APScheduler를 사용하여 주기적으로 보유 자산을 갱신할 수 있습니다:

```python
# app/core/scheduler.py에 추가 예시
from app.services.holdings_service import HoldingsService
from app.services.kis import KISClient
from app.core.db import AsyncSessionLocal

async def update_holdings_job():
    """보유 자산 주기적 갱신"""
    async with AsyncSessionLocal() as db:
        kis_client = KISClient(...)
        holdings_service = HoldingsService(kis_client)
        await holdings_service.fetch_and_update_all_holdings(db, user_id=1)

# 매일 오전 9시에 실행
scheduler.add_job(update_holdings_job, 'cron', hour=9, minute=0)
```

## 데이터베이스 스키마

### user_watch_items 테이블
| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | BigInteger | PK |
| user_id | BigInteger | 사용자 ID (FK to users) |
| instrument_id | BigInteger | 종목 ID (FK to instruments) |
| quantity | Numeric(18,6) | 보유 수량 |
| desired_buy_px | Numeric(18,8) | 희망 매수가 |
| target_sell_px | Numeric(18,8) | 목표 매도가 |
| stop_loss_px | Numeric(18,8) | 손절가 |
| note | Text | 메모 |
| use_trailing | Boolean | 추적 매도 사용 여부 |
| trailing_gap_pct | Numeric(9,4) | 추적 갭 퍼센트 |
| notify_cooldown | Interval | 알림 쿨다운 |
| is_active | Boolean | 활성 여부 |
| created_at | Timestamp | 생성일시 |
| updated_at | Timestamp | 수정일시 |

### instruments 테이블
| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | BigInteger | PK |
| exchange_id | BigInteger | 거래소 ID (FK to exchanges) |
| symbol | Text | 종목 코드 |
| name | Text | 종목명 |
| type | InstrumentType | 상품 타입 (equity_kr, equity_us, crypto) |
| base_currency | Text | 기준 통화 (KRW, USD) |
| tick_size | Numeric(18,8) | 호가 단위 |
| is_active | Boolean | 활성 여부 |

### exchanges 테이블
| 컬럼 | 타입 | 설명 |
|------|------|------|
| id | BigInteger | PK |
| code | Text | 거래소 코드 (KRX, NASDAQ, NYSE, UPBIT) |
| name | Text | 거래소명 |
| country | Text | 국가 코드 |
| tz | Text | 타임존 |

## 주의사항

1. **API 키 설정 필수**
   - `.env` 파일에 KIS_APP_KEY, KIS_APP_SECRET, UPBIT_ACCESS_KEY, UPBIT_SECRET_KEY 설정 필요
   - 실전투자와 모의투자는 별도 API 키 사용

2. **데이터 갱신 주기**
   - KIS API는 호출 제한이 있으므로 너무 자주 갱신하지 않는 것을 권장
   - 하루 1-2회 정도가 적절

3. **보유 수량 정확도**
   - KIS: `hldg_qty` (보유수량) 또는 `ord_psbl_qty` (주문가능수량)
   - Upbit: `balance` (보유수량, locked 제외)

4. **거래소 매핑**
   - KIS 해외주식: `ovrs_excg_cd` 필드로 거래소 구분 (NASD, NYSE, AMEX)
   - Upbit: 모든 암호화폐는 UPBIT 거래소로 저장

## 확장 가능성

### 1. 실시간 가격 추적
현재 보유 자산의 실시간 가격을 가져와 평가금액 계산:
```python
from app.services.upbit import fetch_multiple_current_prices
from app.services.kis import KISClient

# 실시간 가격 조회 후 평가금액 계산
prices = await fetch_multiple_current_prices([holding["symbol"] for holding in holdings])
total_value = sum(holding["quantity"] * prices[holding["symbol"]] for holding in holdings)
```

### 2. 알림 기능
목표 매도가 도달 시 Telegram 알림:
```python
if current_price >= holding["target_sell_px"]:
    await send_telegram_notification(f"{holding['name']} 목표가 도달!")
```

### 3. 자동 매매 연동
보유 자산 정보를 바탕으로 자동 매매 전략 실행

### 4. 포트폴리오 분석
보유 자산 비중, 수익률, 리스크 분석 리포트 생성

## 문제 해결

### 1. 데이터베이스 연결 실패
```bash
# Docker 서비스 확인
docker-compose ps

# PostgreSQL 재시작
docker-compose restart db
```

### 2. API 인증 실패
- `.env` 파일의 API 키 확인
- KIS 모의투자/실전투자 API 키 구분 확인
- Upbit API 키 권한 확인 (조회 권한 필요)

### 3. 데이터 갱신 실패
- `test_holdings.py` 실행하여 상세 에러 확인
- API 호출 제한 확인 (KIS는 초당 20회 제한)

## 참고 문서

- [KIS API 문서](https://apiportal.koreainvestment.com/)
- [Upbit API 문서](https://docs.upbit.com/)
- [프로젝트 CLAUDE.md](CLAUDE.md) - 프로젝트 전체 개요
