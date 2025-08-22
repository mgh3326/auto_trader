# JSON 형식 주식 분석 기능

이 프로젝트는 Google AI의 Gemini 모델을 사용하여 주식 분석을 수행하고, 구조화된 JSON 응답을 받을 수 있는 기능을 제공합니다.

## 주요 기능

### 1. 구조화된 분석 결과
- **투자 결정**: buy, hold, sell 중 하나
- **근거**: 최대 3개의 구체적인 분석 근거
- **가격 분석**: 4가지 가격 범위 제공
- **신뢰도**: 0-100 사이의 분석 신뢰도
- **상세 분석**: 기존 형태의 마크다운 텍스트

### 2. 가격 분석 구조
```json
{
  "price_analysis": {
    "appropriate_buy_range": {"min": 75000.0, "max": 78000.0},
    "appropriate_sell_range": {"min": 82000.0, "max": 85000.0},
    "buy_hope_range": {"min": 72000.0, "max": 75000.0},
    "sell_target_range": {"min": 85000.0, "max": 90000.0}
  }
}
```

**가격 용어 정의:**
- **적절한 매수 범위**: 현재 시점에서 매수하기에 적정한 가격 범위
- **적절한 매도 범위**: 보유중일 때 매도하기에 적정한 가격 범위 (단기 목표)
- **매수 희망 범위**: 조금 더 저렴하게 사고 싶은 이상적인 매수 가격 범위
- **매도 목표 범위**: 최종적으로 도달하기를 기대하는 매도 가격 범위 (장기 목표)

## JSON 분석 대시보드

### 1. 대시보드 접속
```
http://localhost:8000/analysis-json/
```

### 2. 주요 기능
- **통계 카드**: 전체 분석 수, 평균 신뢰도, 매수/관망/매도 추천 수
- **필터링**: 상품 타입, 종목 코드, 모델명별 필터링
- **결과 테이블**: 구조화된 분석 결과를 테이블 형태로 표시
- **상세 모달**: 각 분석 결과의 상세 정보를 모달로 표시
- **페이지네이션**: 대량의 결과를 페이지별로 분할 표시

### 3. 테이블 컬럼
- **종목**: 종목 코드/심볼
- **종목명**: 종목의 이름
- **상품타입**: 국내주식, 해외주식, 암호화폐 등
- **모델명**: 사용된 AI 모델 (gemini-2.5-pro 등)
- **투자결정**: 매수/관망/매도 (색상 구분)
- **신뢰도**: 0-100% 시각적 표시
- **가격분석**: 매수/매도 가격 범위 요약
- **생성일시**: 분석이 수행된 시간

### 4. 상세 모달 내용
- **기본 정보**: 종목코드, 종목명, 상품타입, 모델명, 생성일시
- **투자 결정**: 결정과 신뢰도 시각적 표시
- **분석 근거**: 3가지 구체적인 분석 근거
- **가격 분석**: 4가지 가격 범위를 카드 형태로 표시
- **상세 분석**: 마크다운 형태의 전체 분석 텍스트

## 서비스별 JSON 분석 메서드

### 1. Upbit (암호화폐)
```python
from app.analysis.service_analyzers import UpbitAnalyzer

analyzer = UpbitAnalyzer()

# JSON 형식으로 코인 분석
await analyzer.analyze_coins_json(["비트코인", "이더리움", "리플"])
```

**실행 파일**: `debug_upbit_json.py`

### 2. Yahoo Finance (미국 주식)
```python
from app.analysis.service_analyzers import YahooAnalyzer

analyzer = YahooAnalyzer()

# JSON 형식으로 미국 주식 분석
await analyzer.analyze_stocks_json(["AAPL", "MSFT", "GOOGL"])
```

**실행 파일**: `debug_yahoo_json.py`

### 3. KIS (국내 주식)
```python
from app.analysis.service_analyzers import KISAnalyzer

analyzer = KISAnalyzer()

# JSON 형식으로 국내 주식 분석
await analyzer.analyze_stocks_json(["삼성전자", "SK하이닉스", "NAVER"])
```

**실행 파일**: `debug_kis_json.py`

## 데이터베이스 구조

### 1. 기존 테이블 (PromptResult)
- 텍스트 기반 분석 결과 저장
- 기존 호환성 유지

### 2. 새로운 테이블 (StockAnalysisResult)
- 구조화된 JSON 분석 결과 저장
- 가격 범위를 개별 컬럼으로 저장
- 분석 근거와 신뢰도 저장

```sql
CREATE TABLE stock_analysis_results (
    id INTEGER PRIMARY KEY,
    symbol VARCHAR(50) NOT NULL,
    name VARCHAR(100) NOT NULL,
    instrument_type VARCHAR(50) NOT NULL,
    model_name VARCHAR(100) NOT NULL,
    decision VARCHAR(20) NOT NULL,
    confidence INTEGER NOT NULL,
    appropriate_buy_min FLOAT,
    appropriate_buy_max FLOAT,
    appropriate_sell_min FLOAT,
    appropriate_sell_max FLOAT,
    buy_hope_min FLOAT,
    buy_hope_max FLOAT,
    sell_target_min FLOAT,
    sell_target_max FLOAT,
    reasons TEXT,
    detailed_text TEXT,
    prompt TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT NOW(),
    updated_at TIMESTAMP
);
```

## API 엔드포인트

### 1. 대시보드 페이지
- `GET /analysis-json/` - JSON 분석 결과 대시보드

### 2. 데이터 조회
- `GET /analysis-json/api/results` - 분석 결과 목록 조회 (페이지네이션, 필터링)
- `GET /analysis-json/api/detail/{result_id}` - 특정 분석 결과 상세 조회
- `GET /analysis-json/api/filters` - 필터 옵션 조회
- `GET /analysis-json/api/statistics` - 통계 정보 조회

### 3. 쿼리 파라미터
- `instrument_type`: 상품 타입 필터
- `symbol`: 종목 코드 필터
- `model_name`: 모델명 필터
- `page`: 페이지 번호 (기본값: 1)
- `page_size`: 페이지 크기 (기본값: 20, 최대: 100)

## 사용법

### 기본 사용법

```python
from app.analysis import Analyzer

# 분석기 생성
analyzer = Analyzer()

# JSON 형식으로 분석 실행
result, model_name = await analyzer.analyze_and_save_json(
    df=stock_data,
    symbol="005930",
    name="삼성전자",
    instrument_type="stock",
    currency="₩",
    unit_shares="주",
    fundamental_info={
        "시가총액": "500조원",
        "PER": 15.2,
        "PBR": 1.8
    },
    position_info={
        "quantity": 100,
        "avg_price": 75000,
        "total_value": 7500000
    }
)
```

### 결과 접근

```python
# 투자 결정
decision = result.decision  # "buy", "hold", "sell"

# 근거
reasons = result.reasons  # ["근거1", "근거2", "근거3"]

# 가격 분석
buy_range = result.price_analysis.appropriate_buy_range
print(f"매수 범위: {buy_range.min:,.2f}원 ~ {buy_range.max:,.2f}원")

# 신뢰도
confidence = result.confidence  # 0-100

# 상세 분석 텍스트
detailed_text = result.detailed_text
```

## Pydantic 모델

### PriceRange
```python
class PriceRange(BaseModel):
    min: float = Field(description="가격 범위의 최소값")
    max: float = Field(description="가격 범위의 최대값")
```

### PriceAnalysis
```python
class PriceAnalysis(BaseModel):
    appropriate_buy_range: PriceRange
    appropriate_sell_range: PriceRange
    buy_hope_range: PriceRange
    sell_target_range: PriceRange
```

### StockAnalysisResponse
```python
class StockAnalysisResponse(BaseModel):
    decision: str = Field(enum=["buy", "hold", "sell"])
    reasons: List[str] = Field(max_items=3)
    price_analysis: PriceAnalysis
    detailed_text: str
    confidence: int = Field(ge=0, le=100)
```

## 기존 기능과의 호환성

기존의 텍스트 기반 분석도 계속 사용할 수 있습니다:

```python
# 기존 방식 (텍스트 응답) - PromptResult 테이블에 저장
result, model_name = await analyzer.analyze_and_save(
    df=stock_data,
    symbol="005930",
    name="삼성전자",
    instrument_type="stock"
)

# JSON 방식 - StockAnalysisResult 테이블에 저장
result, model_name = await analyzer.analyze_and_save_json(
    df=stock_data,
    symbol="005930",
    name="삼성전자",
    instrument_type="stock"
)
```

## 데이터 저장 방식

### 1. 텍스트 분석 결과
- `PromptResult` 테이블에 저장
- `result` 컬럼에 전체 텍스트 저장

### 2. JSON 분석 결과
- `StockAnalysisResult` 테이블에 저장
- 가격 범위를 개별 컬럼으로 분리 저장
- `reasons` 컬럼에 JSON 형태로 근거 저장
- `detailed_text` 컬럼에 마크다운 텍스트 저장

## 에러 처리

JSON 파싱에 실패할 경우 자동으로 텍스트 응답으로 fallback됩니다:

```python
try:
    result, model_name = await analyzer.analyze_and_save_json(...)
    if isinstance(result, StockAnalysisResponse):
        # JSON 응답 성공 - StockAnalysisResult 테이블에 저장
        print(f"결정: {result.decision}")
    else:
        # fallback된 텍스트 응답 - PromptResult 테이블에 저장
        print(f"텍스트 응답: {result}")
except Exception as e:
    print(f"분석 실패: {e}")
```

## 실행 예시

### 1. Upbit 암호화폐 분석
```bash
python debug_upbit_json.py
```

### 2. Yahoo Finance 미국 주식 분석
```bash
python debug_yahoo_json.py
```

### 3. KIS 국내 주식 분석
```bash
python debug_kis_json.py
```

### 4. 웹 대시보드 접속
```bash
# 서버 실행 후
open http://localhost:8000/analysis-json/
```

## 마이그레이션

새로운 테이블을 생성하려면 Alembic을 사용하세요:

```bash
# 마이그레이션 실행
alembic upgrade head

# 롤백 (필요시)
alembic downgrade -1
```

## 요구사항

- Python 3.8+
- Google AI Python SDK
- Pydantic
- pandas
- asyncio
- SQLAlchemy
- Alembic
- FastAPI
- Jinja2Templates

## 라이센스

이 프로젝트의 라이센스 정보는 프로젝트 루트의 LICENSE 파일을 참조하세요.
