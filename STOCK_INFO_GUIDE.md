# StockInfo + StockAnalysisResult 정규화 구조 가이드

## 🎯 개요

주식 정보와 분석 결과를 정규화된 구조로 분리하여 데이터 일관성을 보장하고 중복을 방지합니다.

## 📊 테이블 구조

### 1. **stock_info (마스터 테이블)**
```sql
CREATE TABLE stock_info (
    id SERIAL PRIMARY KEY,
    symbol VARCHAR(50) UNIQUE NOT NULL,
    name VARCHAR(100) NOT NULL,
    instrument_type VARCHAR(50) NOT NULL,
    exchange VARCHAR(50),
    sector VARCHAR(100),
    market_cap FLOAT,
    is_active BOOLEAN DEFAULT TRUE,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE
);
```

### 2. **stock_analysis_results (분석 결과 테이블)**
```sql
CREATE TABLE stock_analysis_results (
    id SERIAL PRIMARY KEY,
    stock_info_id INTEGER REFERENCES stock_info(id) NOT NULL,
    model_name VARCHAR(100) NOT NULL,
    decision VARCHAR(20) NOT NULL,
    confidence INTEGER NOT NULL,
    -- 가격 분석 필드들
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
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE
);
```

## 🔍 주요 조회 패턴

### **1. 종목별 최신 분석 결과 조회**
```sql
-- 방법 1: Correlated Subquery (간단함)
SELECT 
    si.symbol, si.name, si.instrument_type,
    sar.model_name, sar.decision, sar.confidence,
    sar.appropriate_buy_min, sar.appropriate_buy_max,
    sar.appropriate_sell_min, sar.appropriate_sell_max,
    sar.buy_hope_min, sar.buy_hope_max,
    sar.sell_target_min, sar.sell_target_max,
    sar.reasons, sar.created_at
FROM stock_analysis_results sar
JOIN stock_info si ON sar.stock_info_id = si.id
WHERE si.is_active = true
  AND sar.id = (
      SELECT id FROM stock_analysis_results sar2 
      WHERE sar2.stock_info_id = sar.stock_info_id 
      ORDER BY created_at DESC LIMIT 1
  )
ORDER BY sar.created_at DESC;

-- 방법 2: Window Function (성능이 좋음)
WITH latest_analysis AS (
    SELECT 
        si.symbol, si.name, si.instrument_type,
        sar.model_name, sar.decision, sar.confidence,
        sar.appropriate_buy_min, sar.appropriate_buy_max,
        sar.appropriate_sell_min, sar.appropriate_sell_max,
        sar.buy_hope_min, sar.buy_hope_max,
        sar.sell_target_min, sar.sell_target_max,
        sar.reasons, sar.created_at,
        ROW_NUMBER() OVER (PARTITION BY si.symbol ORDER BY sar.created_at DESC) as rn
    FROM stock_analysis_results sar
    JOIN stock_info si ON sar.stock_info_id = si.id
    WHERE si.is_active = true
)
SELECT * FROM latest_analysis WHERE rn = 1
ORDER BY created_at DESC;
```

### **2. 특정 종목의 분석 히스토리 조회**
```sql
SELECT 
    si.symbol, si.name,
    sar.model_name, sar.decision, sar.confidence,
    sar.created_at, sar.updated_at
FROM stock_analysis_results sar
JOIN stock_info si ON sar.stock_info_id = si.id
WHERE si.symbol = :symbol
  AND si.is_active = true
ORDER BY sar.created_at DESC;
```

### **3. 상품 타입별 최신 분석 결과**
```sql
SELECT 
    si.instrument_type,
    COUNT(*) as total_stocks,
    COUNT(CASE WHEN sar.decision = 'buy' THEN 1 END) as buy_count,
    COUNT(CASE WHEN sar.decision = 'hold' THEN 1 END) as hold_count,
    COUNT(CASE WHEN sar.decision = 'sell' THEN 1 END) as sell_count,
    AVG(sar.confidence) as avg_confidence
FROM stock_info si
LEFT JOIN LATERAL (
    SELECT * FROM stock_analysis_results sar2
    WHERE sar2.stock_info_id = si.id
    ORDER BY created_at DESC LIMIT 1
) sar ON true
WHERE si.is_active = true
GROUP BY si.instrument_type;
```

## 🚀 Python/SQLAlchemy 사용법

### **1. 종목별 최신 분석 결과 조회**
```python
from sqlalchemy import select, desc
from sqlalchemy.orm import selectinload

# 방법 1: Raw SQL 사용
async def get_latest_analysis_results(db: AsyncSession, limit: int = 20):
    query = text("""
        SELECT 
            si.symbol, si.name, si.instrument_type,
            sar.model_name, sar.decision, sar.confidence,
            sar.created_at
        FROM stock_analysis_results sar
        JOIN stock_info si ON sar.stock_info_id = si.id
        WHERE si.is_active = true
          AND sar.id = (
              SELECT id FROM stock_analysis_results sar2 
              WHERE sar2.stock_info_id = sar.stock_info_id 
              ORDER BY created_at DESC LIMIT 1
          )
        ORDER BY sar.created_at DESC
        LIMIT :limit
    """)
    result = await db.execute(query, {"limit": limit})
    return result.fetchall()

# 방법 2: SQLAlchemy ORM 사용
async def get_latest_analysis_by_symbol(db: AsyncSession, symbol: str):
    # 해당 종목의 최신 분석 결과 조회
    subq = select(StockAnalysisResult.id).where(
        StockAnalysisResult.stock_info_id == StockInfo.id
    ).order_by(desc(StockAnalysisResult.created_at)).limit(1)
    
    query = select(StockInfo, StockAnalysisResult).join(
        StockAnalysisResult, StockAnalysisResult.stock_info_id == StockInfo.id
    ).where(
        StockInfo.symbol == symbol,
        StockInfo.is_active == True,
        StockAnalysisResult.id == subq
    )
    
    result = await db.execute(query)
    return result.first()
```

### **2. 주식 정보 생성/조회**
```python
from app.services.stock_info_service import create_stock_if_not_exists

# 주식 정보 자동 생성/조회
stock_info = await create_stock_if_not_exists(
    symbol="AAPL",
    name="Apple Inc.",
    instrument_type="equity_us",
    exchange="NASDAQ",
    sector="Technology"
)

# 분석 결과 저장
analysis_result = StockAnalysisResult(
    stock_info_id=stock_info.id,
    model_name="gemini-1.5-flash",
    decision="buy",
    confidence=85,
    # ... 기타 필드들
)
```

## ⚡ 성능 최적화 팁

### **1. 인덱스 최적화**
```sql
-- 필수 인덱스들
CREATE INDEX idx_stock_info_symbol ON stock_info(symbol);
CREATE INDEX idx_stock_info_active ON stock_info(is_active);
CREATE INDEX idx_stock_analysis_stock_info_id ON stock_analysis_results(stock_info_id);
CREATE INDEX idx_stock_analysis_created_at ON stock_analysis_results(created_at DESC);

-- 복합 인덱스 (자주 함께 조회되는 컬럼들)
CREATE INDEX idx_stock_analysis_stock_created ON stock_analysis_results(stock_info_id, created_at DESC);
```

### **2. 파티셔닝 (대용량 데이터시)**
```sql
-- 월별 파티셔닝 예시 (PostgreSQL 10+)
CREATE TABLE stock_analysis_results (
    -- 기존 컬럼들...
    created_at TIMESTAMP WITH TIME ZONE DEFAULT NOW()
) PARTITION BY RANGE (created_at);

-- 월별 파티션 생성
CREATE TABLE stock_analysis_results_2025_01 
PARTITION OF stock_analysis_results
FOR VALUES FROM ('2025-01-01') TO ('2025-02-01');
```

### **3. 구체화된 뷰 (Materialized View)**
```sql
-- 성능이 중요한 경우에만 사용
CREATE MATERIALIZED VIEW stock_latest_analysis_mv AS
WITH latest_analysis AS (
    SELECT 
        si.symbol, si.name, si.instrument_type,
        sar.model_name, sar.decision, sar.confidence,
        sar.created_at,
        ROW_NUMBER() OVER (PARTITION BY si.symbol ORDER BY sar.created_at DESC) as rn
    FROM stock_analysis_results sar
    JOIN stock_info si ON sar.stock_info_id = si.id
    WHERE si.is_active = true
)
SELECT * FROM latest_analysis WHERE rn = 1;

-- 인덱스 생성
CREATE INDEX idx_stock_latest_mv_symbol ON stock_latest_analysis_mv(symbol);
CREATE INDEX idx_stock_latest_mv_created_at ON stock_latest_analysis_mv(created_at DESC);

-- 정기적으로 리프레시 (cron job 또는 트리거로)
REFRESH MATERIALIZED VIEW CONCURRENTLY stock_latest_analysis_mv;
```

## 💡 권장사항

### **현재 단계 (데이터가 적을 때)**
- 직접 JOIN + Correlated Subquery 사용
- 간단하고 투명한 쿼리 작성
- 필요한 인덱스만 생성

### **확장 단계 (성능 이슈가 발생할 때)**
1. **Window Function** 사용으로 성능 개선
2. **복합 인덱스** 추가
3. **구체화된 뷰** 도입 검토
4. **파티셔닝** 적용

### **대규모 단계 (매우 큰 데이터일 때)**
1. **읽기 전용 복제본** 분리
2. **캐싱 레이어** (Redis) 도입
3. **데이터 아카이빙** 전략 수립

---

**정규화된 구조로 시작하여 필요에 따라 성능 최적화를 점진적으로 적용하는 것이 가장 좋은 접근법입니다! 🎯**
