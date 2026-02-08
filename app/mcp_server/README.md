# auto_trader MCP server

Read-only MCP tools (market data) exposed via `fastmcp`.

## Tools
- `search_symbol(query, limit=20)`
- `get_quote(symbol, market=None)`
- `get_holdings(account=None, market=None, include_current_price=True, minimum_value=1000)`
- `get_position(symbol, market=None)`
- `get_ohlcv(symbol, count=100, period="day", end_date=None, market=None)`
- `get_volume_profile(symbol, market=None, period=60, bins=20)`

### `get_holdings` spec
Parameters:
- `account`: optional account filter (`kis`, `upbit`, `toss`, `samsung_pension`, `isa`)
- `market`: optional market filter (`kr`, `us`, `crypto`)
- `include_current_price`: if `True`, tries to fetch latest prices and calculate PnL fields
- `minimum_value`: optional numeric threshold (default `1000`). Positions below this value are excluded only when `include_current_price=True`

Filtering rules:
- If `include_current_price=False`, `minimum_value` filtering is skipped
- Upbit crypto current prices are fetched via batch ticker request (`/v1/ticker?markets=...`)
- Before batch ticker request, tradable markets are loaded from `/v1/market/all` and only valid holdings symbols are included in the batch
- Non-tradable symbols (delisted/unsupported) are excluded from ticker request and treated as 0 value for `minimum_value` filtering (counted in `filtered_count`)
- Value is primarily based on `evaluation_amount`
- If current price lookup fails (`current_price=null`), value is treated as `0` for minimum filtering

Response contract additions:
- `filtered_count`: number of positions excluded by `minimum_value` filter
- `filter_reason`: filter status string, e.g. `minimum_value < 1000`
- `errors`: includes per-symbol price lookup failures for holdings price refresh (example fields: `source`, `market`, `symbol`, `stage`, `error`)

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
