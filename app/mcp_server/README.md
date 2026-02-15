# auto_trader MCP server

MCP tools (market data, portfolio, order execution) exposed via `fastmcp`.

## Tools
- `search_symbol(query, limit=20)`
- `get_quote(symbol, market=None)`
- `get_holdings(account=None, market=None, include_current_price=True, minimum_value=None)`
- `get_position(symbol, market=None)`
- `get_ohlcv(symbol, count=100, period="day", end_date=None, market=None)`
- `get_volume_profile(symbol, market=None, period=60, bins=20)`
- `get_order_history(symbol=None, status="all", order_id=None, limit=50)`
- `place_order(symbol, side, order_type="limit", quantity=None, price=None, amount=None)`
- `modify_order(order_id, symbol, new_price=None, new_quantity=None)`
- `cancel_order(order_id)`
- `screen_stocks(...)` - Screen stocks across different markets (KR/US/Crypto) with various filters.
- `recommend_stocks(...)` - Recommend stocks based on budget and strategy.

### `screen_stocks` spec
Parameters:
- `market`: Market to screen - "kr", "us", "crypto" (default: "kr")
- `asset_type`: Asset type - "stock", "etf", "etn" (only applicable to KR, default: None)
- `category`: Category filter - ETF categories for KR, sector for US (default: None)
- `sort_by`: Sort criteria - "volume", "market_cap", "change_rate", "dividend_yield" (default: "volume")
- `sort_order`: Sort order - "asc" or "desc" (default: "desc")
- `min_market_cap`: Minimum market cap (억원 for KR, USD for US; not supported for crypto)
- `max_per`: Maximum P/E ratio filter (not applicable to crypto)
- `min_dividend_yield`: Minimum dividend yield filter (accepts both decimal, e.g., 0.03, and percentage, e.g., 3.0; values > 1 are treated as percentages) (not applicable to crypto)
- `max_rsi`: Maximum RSI filter 0-100 (not applicable to sorting by dividend_yield in crypto)
- `limit`: Maximum results 1-50 (default: 20)

Market-specific behavior:
- **KR market**: Uses KRX API for stocks/ETFs + Naver Finance for PER/dividend yield + RSI calculation
  - KRX data cached with 300s TTL (Redis) + in-memory fallback
  - Trading date auto-fallback (up to 10 days back)
  - Category filter auto-limits to ETFs if `asset_type=None`
  - ETN (`asset_type="etn"`) not supported - returns error

- **US market**: Uses yfinance screener with EquityQuery
  - Maps: `min_market_cap` → `intradaymarketcap`, `max_per` → `peratio.lasttwelvemonths`, `min_dividend_yield` → `forward_dividend_yield`
  - Sort maps: `volume` → `dayvolume`, `market_cap` → `intradaymarketcap`, `change_rate` → `percentchange`

- **Crypto market**: Uses Upbit `fetch_top_traded_coins`
  - `trade_amount_24h` uses `acc_trade_price_24h` (24h traded value in KRW)
  - `market_cap` is not available and returned as `null`
  - `max_per`, `min_dividend_yield`, `sort_by="dividend_yield"` not supported - returns error
  - `min_market_cap` filter is not supported; warning added and filter ignored
  - RSI/composite enrichment uses OHLCV fetch for subset `min(max(limit*3, 30), 60)`
  - Composite score calculated using dedicated crypto formula (see below)
  - Each result includes: `score`, `rsi`, `volume_24h`, `volume_ratio`, `candle_type`, `adx`, `plus_di`, `minus_di`

#### Crypto Composite Score Formula

Crypto market uses a dedicated composite score formula instead of strategy-weighted scoring:

```
Total Score = (100 - RSI) * 0.4 + (Vol_Score * Candle_Coef) * 0.3 + Trend_Score * 0.3
```

**Components:**
- **RSI Score** (40%): `100 - RSI` - Lower RSI (oversold) gives higher score
- **Volume Score** (30%): `min(vol_ratio * 33.3, 100)` where `vol_ratio = today_volume / avg_volume_20d`
- **Trend Score** (30%): Based on ADX/DI indicators
  - `plus_di > minus_di` → 90 (uptrend)
  - `adx < 35` → 60 (weak trend)
  - `35 <= adx <= 50` → 30 (moderate trend)
  - `adx > 50` → 10 (strong trend, possibly exhausted)

**Candle Coefficient** (applied to volume score):
- Uses completed candle (index -2, fallback to -1)
- `total_range == 0` → coef=0.5, type=flat
- Bullish (close > open) → coef=1.0, type=bullish
- Lower shadow > body*2 → coef=0.8, type=hammer
- Body > range*0.7 and bearish → coef=0.0, type=bearish_strong
- Other bearish → coef=0.5, type=bearish_normal

**Default values for missing data:**
- RSI missing → rsi_score = 50
- ADX/DI missing → trend_score = 30 (conservative)
- Volume missing → vol_score = 0
- Final score is clamped to 0-100

**Crypto recommend_stocks behavior:**
- Top 30 candidates pre-filtered by 24h volume
- Enriched with composite metrics (RSI, ADX/DI, volume ratio, candle type)
- Sorted by composite score (descending)
- Equal-weight budget allocation
- `score` field is always numeric (0-100)
- Timeout/429 errors return partial results with warnings instead of failing

Advanced filters subset behavior (KR/US):
- **Note**: `min_market_cap` is NOT an advanced filter for KR/US - it uses already available KRX/yfinance fields and does not trigger extra fetches.
- Advanced filters (PER, dividend yield, RSI) require external enrichment in KR market.
- KR/US RSI enrichment subset limit: `min(len(candidates), limit*3, 150)`.
- Parallel fetch with `asyncio.Semaphore(10)`.
- Timeout: 30 seconds.
- Individual failures don't stop overall operation.

Crypto enrichment behavior:
- Uses a dedicated crypto composite enrichment subset: `min(max(limit*3, 30), 60)`.
- `min_market_cap` is not applied as a filter in crypto; it is returned as a warning only.

Response format:
```json
{
  "results": [
    {
      "code": "005930",
      "name": "삼성전자",
      "close": 80000.0,
      "change_rate": 0.05,
      "volume": 10000000,
      "market_cap": 480000000000000,
      "per": 15.0,
      "dividend_yield": 0.03,
      "rsi": 45.5,
      "market": "kr"
    }
  ],
  "total_count": 2400,  // Total stocks that passed all filters (before sort/limit). If data source provides total, uses that; otherwise uses fetched candidates count.
  "returned_count": 20,  // Actual number of results returned (after limit)
  "filters_applied": {
    "market": "kr",
    "asset_type": "stock",
    "min_market_cap": 100000,
    "max_per": 20,
    "min_dividend_yield": 0.03,
    "min_dividend_yield_input": 3.0,
    "min_dividend_yield_normalized": 0.03,
    "max_rsi": 70
  },
  "timestamp": "2026-02-10T14:20:59.123456"
}
```

### `recommend_stocks` spec
Parameters:
- `budget`: Total budget to allocate (required, must be positive)
- `market`: Market to screen - "kr", "us", "crypto" (default: "kr")
- `strategy`: Scoring strategy - "balanced", "growth", "value", "dividend", "momentum" (default: "balanced")
- `exclude_symbols`: List of symbols to exclude from recommendations (optional)
- `sectors`: List of sectors/categories to filter (uses first value only)
- `max_positions`: Maximum number of positions to recommend 1-20 (default: 5)

> Breaking change: `account` parameter is removed from `recommend_stocks`.

Strategy descriptions:
- **balanced**: 균형 잡힌 포트폴리오. RSI, 밸류에이션, 모멘텀, 배당을 균등하게 고려
- **growth**: 성장주 중심. 높은 모멘텀과 거래량 가중
- **value**: 가치투자 중심. 낮은 PER/PBR, 적정 RSI 가중 (max_per=20, max_pbr=1.5, min_market_cap=300억)
- **dividend**: 배당주 중심. 높은 배당수익률 가중 (min_dividend_yield=1.5%, min_market_cap=300억)
- **momentum**: 모멘텀 중심. 강한 상승 모멘텀과 거래량 가중

Strategy default thresholds (KR market):
- **value**: `max_per=20`, `max_pbr=1.5`, `min_market_cap=300` (억원)
- **dividend**: `min_dividend_yield=1.5` (percent), `min_market_cap=300` (억원)

2-stage relaxation (value/dividend only):
- When strict screening yields fewer candidates than `max_positions`, a fallback screening is triggered with relaxed thresholds:
  - **value**: `max_per=25`, `max_pbr=2.0`, `min_market_cap=200`
  - **dividend**: `min_dividend_yield=1.0`, `min_market_cap=200`
- Fallback candidates are added to fill remaining positions (deduped by symbol)
- For value strategy: candidates with missing PER/PBR receive score penalties (-12 for PER, -8 for PBR)
- For dividend strategy: candidates with missing or zero dividend_yield are excluded from fallback
- The `fallback_applied` field indicates whether fallback was used

Scoring weight factors:
- `rsi_weight`: RSI 기반 기술적 과매수/과매도 점수 비중
- `valuation_weight`: PER/PBR 기반 밸류에이션 점수 비중
- `momentum_weight`: 등락률 기반 모멘텀 점수 비중
- `volume_weight`: 거래량 기반 유동성 점수 비중
- `dividend_weight`: 배당수익률 기반 인컴 점수 비중

Behavior:
- Invalid `market` values raise `ValueError` (no silent fallback)
- Strategy-specific `screen_params` are applied per market and unsupported filters are ignored with warnings
- KR screens candidates using internal screener (max 100 candidates)
- Crypto prefilters top 30 candidates by 24h traded value, then enriches with RSI/composite metrics
- US uses `get_top_stocks(market="us", ranking_type="volume")` for candidate collection (max 50 candidates)
- Dividend threshold input is normalized as percent when `>= 1` (e.g., `1.0 -> 0.01`, `3.0 -> 0.03`)
- Excludes user holdings from all accounts (internal `account=None` query)
- Applies strategy-weighted composite scoring for KR/US (0-100); crypto uses dedicated composite score
- Sorts by score and allocates budget with integer quantities
- Remaining budget is added to top recommendation if possible

Response format:
```json
{
  "recommendations": [
    {
      "symbol": "005930",
      "name": "삼성전자",
      "price": 80000.0,
      "quantity": 10,
      "amount": 800000.0,
      "score": 75.5,
      "reason": "[balanced] RSI 45.0 (저평가 구간) | PER 12.0 (적정)",
      "rsi_14": 45.0,
      "per": 12.0,
      "change_rate": 2.5
    }
  ],
  "total_amount": 950000.0,
  "remaining_budget": 50000.0,
  "strategy": "balanced",
  "strategy_description": "균형 잡힌 포트폴리오 구성을 위한 전략...",
  "candidates_screened": 100,
  "diagnostics": {
    "raw_candidates": 100,
    "post_filter_candidates": 95,
    "per_none_count": 5,
    "pbr_none_count": 3,
    "dividend_none_count": 10,
    "dividend_zero_count": 2,
    "strict_candidates": 80,
    "fallback_candidates_added": 0,
    "fallback_applied": false,
    "active_thresholds": {
      "min_market_cap": 500,
      "max_per": null,
      "max_pbr": null,
      "min_dividend_yield": null
    }
  },
  "fallback_applied": false,
  "warnings": [],
  "timestamp": "2026-02-13T02:11:52.950534+00:00"
}
```

Crypto recommendation example (`market="crypto"`):
```json
{
  "recommendations": [
    {
      "symbol": "KRW-BTC",
      "name": "비트코인",
      "price": 142000000.0,
      "quantity": 1,
      "amount": 142000000.0,
      "score": 78.4,
      "reason": "Composite Score 78.4 | RSI 39.2(저평가) | 캔들 bullish | 거래량 1.3배",
      "rsi_14": 39.2,
      "per": null,
      "change_rate": 1.8,
      "volume_24h": 12543.21,
      "volume_ratio": 1.32,
      "candle_type": "bullish",
      "adx": 27.41,
      "plus_di": 31.52,
      "minus_di": 18.07
    }
  ],
  "total_amount": 142000000.0,
  "remaining_budget": 8000000.0,
  "strategy": "balanced",
  "warnings": [],
  "timestamp": "2026-02-15T00:00:00+00:00"
}
```

Diagnostics fields:
- `raw_candidates`: Number of candidates from screener
- `post_filter_candidates`: After normalization
- `per_none_count`: Candidates with missing PER
- `pbr_none_count`: Candidates with missing PBR
- `dividend_none_count`: Candidates with missing dividend_yield
- `dividend_zero_count`: Candidates with zero dividend_yield
- `strict_candidates`: After exclusion/dedup
- `fallback_candidates_added`: Additional candidates from 2-stage relaxation
- `fallback_applied`: Whether fallback screening was triggered
- `active_thresholds`: The strict stage thresholds used
- `fallback_thresholds`: (optional) Fallback thresholds if fallback was applied

Error response format (unexpected internal failure):
```json
{
  "error": "recommend_stocks failed: RuntimeError",
  "source": "recommend_stocks",
  "query": "market=kr,strategy=balanced,budget=5000000,max_positions=5",
  "details": "Traceback (most recent call last): ..."
}
```

### `get_holdings` spec
Parameters:
- `account`: optional account filter (`kis`, `upbit`, `toss`, `samsung_pension`, `isa`)
- `market`: optional market filter (`kr`, `us`, `crypto`)
- `include_current_price`: if `True`, tries to fetch latest prices and calculate PnL fields
- `minimum_value`: optional numeric threshold. When `None` (default), per-currency thresholds apply: KRW=5000, USD=10. Explicit number uses uniform threshold. Positions below threshold are excluded only when `include_current_price=True`

Filtering rules:
- If `include_current_price=False`, `minimum_value` filtering is skipped
- When `minimum_value=None`, per-currency thresholds are automatically applied based on `instrument_type`: `equity_kr` and `crypto` use 5000, `equity_us` uses 10
- When `minimum_value` is a number, that uniform threshold is applied to all positions
- Upbit crypto current prices are fetched via batch ticker request (`/v1/ticker?markets=...`)
- Before batch ticker request, tradable markets are loaded from `/v1/market/all` and only valid holdings symbols are included in the batch
- Non-tradable symbols (delisted/unsupported) are excluded from ticker request and treated as 0 value for `minimum_value` filtering (counted in `filtered_count`)
- Value is primarily based on `evaluation_amount`
- If current price lookup fails (`current_price=null`), value is treated as `0` for minimum filtering

Response contract additions:
- `filtered_count`: number of positions excluded by `minimum_value` filter
- `filter_reason`: filter status string, e.g. `minimum_value < 1000` or `equity_kr < 5000, equity_us < 10, crypto < 5000`
- `errors`: includes per-symbol price lookup failures for holdings price refresh (example fields: `source`, `market`, `symbol`, `stage`, `error`)
- `filters.minimum_value`: when `minimum_value=None` in the request, this field contains the per-currency threshold dict that was applied

Market routing:
- `market` can override routing: `crypto|upbit`, `kr|kis|krx|kospi|kosdaq`, `us|yahoo|nasdaq|nyse`
- If `market` is omitted, routing is heuristic: KRW-/USDT- prefix -> crypto, 6-digit code -> KR equity, otherwise -> US equity
- Crypto symbols must include `KRW-` or `USDT-` prefix

## Run (docker-compose.prod)
Environment variables:
- `MCP_TYPE` : `streamable-http` (default) | `sse` | `stdio`
- `MCP_HOST` : `0.0.0.0`
- `MCP_PORT` : `8765`
- `MCP_PATH` : `/mcp`
- `MCP_USER_ID` : `1` (manual holdings 조회에 사용할 기본 사용자 ID)

Example:
```bash
docker compose -f docker-compose.prod.yml up -d mcp
```

> Note: current prod compose uses `network_mode: host`, so port publishing is handled by the host network.
