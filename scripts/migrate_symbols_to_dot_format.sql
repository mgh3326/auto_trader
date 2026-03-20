-- 해외주식 심볼 형식 정규화 마이그레이션
-- 하이픈(-) 또는 슬래시(/) 형식을 점(.) 형식으로 변환
--
-- 실행 전 백업 권장:
-- pg_dump -t stock_info -t manual_holdings -t stock_aliases -t symbol_trade_settings your_db > backup.sql

-- 1. stock_info 테이블 (해외주식만 - instrument_type이 equity_us인 경우)
UPDATE stock_info
SET symbol = REPLACE(REPLACE(symbol, '-', '.'), '/', '.')
WHERE instrument_type = 'equity_us'
  AND (symbol LIKE '%-%' OR symbol LIKE '%/%');

-- 2. manual_holdings 테이블 (해외주식만 - market_type이 US인 경우)
UPDATE manual_holdings
SET ticker = REPLACE(REPLACE(ticker, '-', '.'), '/', '.')
WHERE market_type = 'US'
  AND (ticker LIKE '%-%' OR ticker LIKE '%/%');

-- 3. stock_aliases 테이블 (해외주식만 - market_type이 US인 경우)
UPDATE stock_aliases
SET ticker = REPLACE(REPLACE(ticker, '-', '.'), '/', '.')
WHERE market_type = 'US'
  AND (ticker LIKE '%-%' OR ticker LIKE '%/%');

-- 4. symbol_trade_settings 테이블 (해외주식만 - instrument_type이 equity_us인 경우)
UPDATE symbol_trade_settings
SET symbol = REPLACE(REPLACE(symbol, '-', '.'), '/', '.')
WHERE instrument_type = 'equity_us'
  AND (symbol LIKE '%-%' OR symbol LIKE '%/%');

-- 변경된 레코드 확인
SELECT 'stock_info' as table_name, COUNT(*) as count
FROM stock_info
WHERE instrument_type = 'equity_us' AND symbol LIKE '%.%'
UNION ALL
SELECT 'manual_holdings', COUNT(*)
FROM manual_holdings
WHERE market_type = 'US' AND ticker LIKE '%.%'
UNION ALL
SELECT 'stock_aliases', COUNT(*)
FROM stock_aliases
WHERE market_type = 'US' AND ticker LIKE '%.%'
UNION ALL
SELECT 'symbol_trade_settings', COUNT(*)
FROM symbol_trade_settings
WHERE instrument_type = 'equity_us' AND symbol LIKE '%.%';
