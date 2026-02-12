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

### `screen_stocks` spec
Parameters:
- `market`: Market to screen - "kr", "us", "crypto" (default: "kr")
- `asset_type`: Asset type - "stock", "etf", "etn" (only applicable to KR, default: None)
- `category`: Category filter - ETF categories for KR, sector for US (default: None)
- `sort_by`: Sort criteria - "volume", "market_cap", "change_rate", "dividend_yield" (default: "volume")
- `sort_order`: Sort order - "asc" or "desc" (default: "desc")
- `min_market_cap`: Minimum market cap (억원 for KR, USD for US, KRW 24h volume for crypto)
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
  - `market_cap` uses `acc_trade_price_24h` (24h trading volume in KRW)
  - `max_per`, `min_dividend_yield`, `sort_by="dividend_yield"` not supported - returns error
  - RSI calculated using OHLCV fetch for subset (max limit*3 or 150)

Advanced filters (PER/dividend/RSI) apply to subset:
- **Note**: `min_market_cap` is NOT an advanced filter - it uses data already available from KRX/yfinance, so it doesn't trigger external API calls
- Advanced filters (PER, dividend yield, RSI) require external data fetch for KR market
- Limit: `min(len(candidates), limit*3, 150)`
- Parallel fetch with `asyncio.Semaphore(10)`
- Timeout: 30 seconds
- Individual failures don't stop overall operation

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
