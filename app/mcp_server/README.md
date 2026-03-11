# auto_trader MCP server

MCP tools (market data, portfolio, order execution) exposed via `fastmcp`.

## Observability (Sentry MCP)
- MCP tracing uses `sentry_sdk.integrations.mcp.MCPIntegration` when enabled.
- Recommended trace filter:
  - `service:auto-trader-mcp op:mcp.server`
- yfinance outbound HTTP is custom-instrumented via `SentryTracingCurlSession` and injected with `session=` at yfinance entrypoints.
- yfinance child span format:
  - `op:http.client`
  - span name/description: `METHOD /path` (query string excluded)
- Example trace filters for yfinance spans:
  - `service:auto-trader-mcp op:http.client transaction:"tools/call screen_stocks"`
  - `service:auto-trader-api op:http.client span.description:"GET /v1/finance/screener"`
- `profile` flamegraph and `trace` spans are different datasets, so some frames may appear only in profiling.
- It is normal to see only high-level spans when a tool does not execute DB/HTTP operations.

## Tools
- `search_symbol(query, limit=20)`
- `get_quote(symbol, market=None)`
- `get_orderbook(symbol, market="kr")`
- US equity quote price resolution uses Yahoo directly via `app.services.brokers.yahoo.client`
  - US quote response keeps `source: "yahoo"` and includes `previous_close/open/high/low/volume` from Yahoo `fast_info`
  - US equity Yahoo lookup failures are propagated as tool-level errors (exceptions), not returned as in-band error payload dicts
- `get_holdings(account=None, market=None, include_current_price=True, minimum_value=None)`
- `get_position(symbol, market=None)`
- `get_ohlcv(symbol, count=100, period="day", end_date=None, market=None, include_indicators=False)`
  - period: `day`, `week`, `month`, `1m`, `5m`, `15m`, `30m`, `4h`, `1h`
  - `include_indicators=True` adds `indicators_included` at the payload top level and appends `rsi_14`, `ema_20`, `bb_upper`, `bb_mid`, `bb_lower`, `vwap` to each row
  - `vwap` is populated for intraday periods and `null` for `day/week/month`
  - `1m` / `5m` / `15m` / `30m`: KR/US equity + crypto
  - `4h`: crypto only
  - `1h`: KR/US equity + crypto
  - Crypto `1m` / `5m` / `15m` / `30m` rows expose `timestamp`, `date`, `time`, `open`, `high`, `low`, `close`, `volume`, `value`, `trade_amount` and do not expose raw `datetime`
- US OHLCV behavior:
  - US `day`/`week`/`month` uses Yahoo Finance (`app.services.brokers.yahoo.client.fetch_ohlcv`)
  - US intraday (`1m`/`5m`/`15m`/`30m`/`1h`) uses KIS via DB-first reader (`read_us_intraday_candles`) with ET-naive timestamps
  - US intraday rows include `session` field (`PRE_MARKET`, `REGULAR`, `POST_MARKET`)
  - US intraday `end_date="YYYY-MM-DD"` is interpreted as ET `20:00:00` for that market date; timestamp inputs use the exact provided instant
- KR OHLCV behavior:
  - KR `day` keeps the existing Redis-backed `kis_ohlcv_cache` path when `end_date` is omitted
  - KR `1m` reads DB-first from raw `public.kr_candles_1m` with venue merge (`KRX` price priority, `volume/value` sum)
  - KR `5m/15m/30m/1h` read DB-first from Timescale continuous aggregates (`public.kr_candles_5m`, `public.kr_candles_15m`, `public.kr_candles_30m`, `public.kr_candles_1h`)
  - KR intraday (`1m/5m/15m/30m/1h`) overlays the most recent 30 minutes from `public.kr_candles_1m` + KIS minute API to cover the unchanged 10-minute sync cadence
  - KR intraday includes the current partial bucket when minute data is available
  - KIS minute venues are merged with strict dedup to prevent double-counting (API overwrites DB per minute+venue)
  - KIS minute API call plan (KST):
    - `09:00 <= now < 15:35`: call KRX (`J`) + NTX (`NX`) in parallel when `nxt_eligible=true` (15:35 delay defense)
    - `08:00 <= now < 09:00`: call NTX (`NX`) only when `nxt_eligible=true`
    - `15:35 <= now < 20:00`: call NTX (`NX`) only when `nxt_eligible=true`
    - When `end_date` is in the past: DB-only (0 API calls)
  - KR intraday degrades to an empty result when symbol is missing/inactive in `kr_symbol_universe` (used for `nxt_eligible`)
  - KR intraday does not use Redis OHLCV cache (`kis_ohlcv_cache`)
  - KR intraday degrades to DB-backed partial data when recent KIS minute overlay calls fail
  - KR intraday response rows add `session` and `venues` fields
- `get_indicators(symbol, indicators, market=None)`
- `get_volume_profile(symbol, market=None, period=60, bins=20)`
- `get_order_history(symbol=None, status="all", order_id=None, limit=50)`
- `place_order(symbol, side, order_type="limit", quantity=None, price=None, amount=None)`
- `modify_order(order_id, symbol, new_price=None, new_quantity=None)`
- `cancel_order(order_id)`
- `manage_watch_alerts(action, market=None, symbol=None, metric=None, operator=None, threshold=None)`
- `screen_stocks(...)` - Screen stocks across different markets (KR/US/Crypto) with various filters.
- `recommend_stocks(...)` - Recommend stocks based on budget and strategy.

### `get_orderbook` spec
Parameters:
- `symbol`: KR equity symbol/code (required)
- `market`: KR market alias only (`"kr"`, `"kospi"`, `"kosdaq"`, `"korea"`, `"kis"`, `"equity_kr"`); default `"kr"`

Behavior:
- Only KR equity orderbook is supported in v1; `market="us"` or `market="crypto"` raises an argument error
- Symbol normalization follows the KR quote path, including zero-padding numeric codes such as `5930 -> 005930`
- Valid KR requests use KIS `inquire-asking-price-exp-ccn` and return 10-level asks/bids, total residual quantities, and expected match metadata
- `expected_qty` keeps the public `int | null` contract; when KIS leaves `output2.antc_cnqn` blank or omits it, the response serializes `expected_qty` as `null` instead of inventing a fallback quantity
- During the NXT session (`16:00`-`20:00` KST), KIS may return `expected_price` while leaving `expected_qty` blank or absent; this is treated as a valid upstream state, not an MCP error
- Successful responses also include MCP-only derived fields: `pressure`, `pressure_desc`, `spread`, and `spread_pct`
- Successful responses include `source: "kis"` and `instrument_type: "equity_kr"`
- Invalid input raises; upstream KIS failures for otherwise valid KR requests return in-band error payloads via the shared MCP error contract

Response format:
```json
{
  "symbol": "005930",
  "instrument_type": "equity_kr",
  "source": "kis",
  "asks": [{"price": 70100, "quantity": 123}],
  "bids": [{"price": 70000, "quantity": 321}],
  "total_ask_qty": 1000,
  "total_bid_qty": 1500,
  "bid_ask_ratio": 1.5,
  "pressure": "buy",
  "pressure_desc": "매수잔량이 매도잔량의 1.5배 - 매수 압력",
  "spread": 100,
  "spread_pct": 0.143,
  "expected_price": 70050,
  "expected_qty": null
}
```

`expected_qty: null` means KIS did not provide `antc_cnqn`; it does not by itself indicate a tool failure.

Derived fields:
- `pressure` is derived from `bid_ask_ratio` using fixed inclusive boundaries:
  - `ratio > 2.0` -> `strong_buy`
  - `ratio > 1.3` (i.e. 1.3 excluded) -> `buy`
  - `ratio >= 0.7` (i.e. 0.7 included, up to 1.3 inclusive) -> `neutral`
  - `ratio >= 0.5` (i.e. 0.5 included, below 0.7) -> `sell`
  - `ratio < 0.5` -> `strong_sell`
- `pressure_desc` is a Korean interpretation string. `strong_buy`/`buy` use `total_bid_qty / total_ask_qty`, `strong_sell`/`sell` use `total_ask_qty / total_bid_qty`, and `neutral` is always `"매수/매도 잔량이 균형권 - 중립"`
- If `bid_ask_ratio` is `null`, both `pressure` and `pressure_desc` are `null`
- `spread` is `asks[0].price - bids[0].price` when both best levels exist; otherwise it is `null`
- `spread_pct` is `(spread / bids[0].price) * 100`, rounded to 3 decimal places, and becomes `null` when the best bid is missing or `<= 0`

### KR order routing
- Domestic order tools (`place_order`, `modify_order`, `cancel_order` with `market="kr"`) use the new KIS TR IDs (`TTTC0012U/TTTC0011U/TTTC0013U`, mock: `VTTC0012U/VTTC0011U/VTTC0013U`).
- Domestic order requests (`order-cash`, `order-rvsecncl`) route with `EXCG_ID_DVSN_CD="SOR"`.

### US symbol/exchange resolution
- US symbol search and order routing resolve from DB table `us_symbol_universe` only.
- Runtime does not use in-memory/file-cache fallback for US symbol/exchange lookups.
- If symbol/name is missing, inactive, or ambiguous in `us_symbol_universe`, tools return explicit lookup errors with sync hint.
- US prerequisite: run `make sync-us-symbol-universe` (or `uv run python scripts/sync_us_symbol_universe.py`) right after migrations.

### KR symbol resolution
- KR symbol search resolves from DB table `kr_symbol_universe` only.
- Runtime does not use in-memory/file-cache fallback for KR name/symbol lookups.
- If symbol/name is missing, inactive, or ambiguous in `kr_symbol_universe`, tools return explicit lookup errors with sync hint.
- KR prerequisite: run `make sync-kr-symbol-universe` (or `uv run python scripts/sync_kr_symbol_universe.py`) right after migrations.

### Upbit symbol resolution
- Upbit crypto symbol/market resolution uses DB table `upbit_symbol_universe` only.
- Runtime does not call Upbit `/v1/market/all`; that endpoint is sync-path only.
- If `upbit_symbol_universe` is empty/unavailable, tools fail fast with explicit sync hint.
- If a coin/market lookup is missing or inactive in `upbit_symbol_universe`, MCP tools generally propagate explicit lookup errors (no silent fallback/default ticker).
- `get_holdings` and `get_position` are exceptions: missing/inactive Upbit holdings coins are silently skipped at collection time, while universe-level fatal states (for example empty/unavailable) still fail fast.
- `search_symbol` (crypto) uses DB-backed `search_upbit_symbols` only; in-memory map-based search is removed.
- Upbit prerequisite: run `make sync-upbit-symbol-universe` (or `uv run python scripts/sync_upbit_symbol_universe.py`) right after migrations.
- Scheduled sync task `symbols.upbit.universe.sync` runs daily at `06:15` KST (`cron: 15 6 * * *`, `cron_offset: Asia/Seoul`).

### `get_indicators` spec
Parameters:
- `symbol`: Asset symbol/ticker
- `indicators`: Indicator list (e.g. `rsi`, `sma`, `obv`)
- `market`: Optional explicit market (`crypto`, `kr`, `us`)

Symbol/market contract:
- `market` is required when `symbol` is a plain alphabetic token (for example `AAPL`, `ETC`).
- If omitted for plain alphabetic symbols, `get_indicators` raises:
  - `"market is required for plain alphabetic symbols. Use market='us' for US equities, or provide KRW-/USDT- prefixed symbol for crypto."`
- Crypto symbols continue to support prefix-based routing (`KRW-` / `USDT-`) and can omit `market`.
- This requirement is specific to `get_indicators`; other tools keep their existing routing behavior.

Examples:
- Allowed: `symbol="AAPL", market="us"`
- Allowed: `symbol="KRW-ETC", market="crypto"`
- Allowed: `symbol="KRW-ETC"` (market omitted)
- Rejected: `symbol="ETC"` (market omitted)

### `get_correlation` spec
Parameters:
- `symbols`: List of asset ticker/code inputs (required, 2-10 entries)
- `period`: Lookback window in days (default: 60, minimum effective value: 30, maximum: 365)

Symbol contract:
- `get_correlation` has no `market` parameter and therefore accepts ticker/code inputs only.
- Mixed-market ticker/code inputs continue to work, including KR codes such as `005930`, US tickers such as `AAPL`, and crypto symbols such as `KRW-BTC`.
- Company-name inputs such as `삼성전자` or `Apple Inc.` are rejected with:
  - `"get_correlation does not support company-name inputs because it has no market parameter. Use ticker/code inputs directly."`
- When at least 2 ticker/code inputs resolve and fetch successfully, the tool still returns a correlation matrix and includes failed symbols in `errors`.

### `get_disclosures` spec
Parameters:
- `symbol`: Korean corporation lookup input (required)
- `days`: Lookback window in days (default: 30)
- `limit`: Maximum filings to return (default: 20)
- `report_type`: Optional Korean disclosure group (`정기`, `주요사항`, `발행`, `지분`, `기타`)

Symbol contract:
- Direct 6-digit KR stock codes such as `005930` are passed through to OpenDartReader as-is.
- Korean company names such as `삼성전자` are supported on a best-effort basis through OpenDartReader's exact-name corp lookup.
- Blank or whitespace-only `symbol` inputs are rejected with an explicit in-band error payload (`success: false`, `error: "symbol is required"`, `filings: []`, `symbol: ""`).
- Company-name inputs that OpenDartReader cannot resolve return an explicit in-band error payload with `success: false`; they do not silently degrade to an empty `filings` list.

Behavior:
- `report_type` maps internally to DART disclosure kinds: `정기 -> A`, `주요사항 -> B`, `발행 -> C`, `지분 -> D`, `기타 -> E`.
- Unsupported `report_type` inputs return `success: false` instead of silently broadening the query.
- Successful responses return the existing `filings` list shape with `date`, `report_nm`, `rcp_no`, and `corp_name`.
- An empty DataFrame from OpenDartReader is treated as a successful lookup with `filings: []`.
- The first process-local client initialization still downloads the OpenDART corp-code cache, so cold-start latency can be higher than warm calls.

Error payload:
- Failure responses include `success`, `error`, `filings`, and `symbol`.

### `manage_watch_alerts` spec
Parameters:
- `action`: Required action - `"add"`, `"remove"`, `"list"`
- `market`: Market - `"crypto"`, `"kr"`, `"us"` (required for `add`/`remove`, optional for `list`)
- `symbol`: Asset symbol/ticker (required for `add`/`remove`)
- `metric`: Condition metric - `"price"` or `"rsi"` (required for `add`/`remove`)
- `operator`: Condition operator - `"above"` or `"below"` (required for `add`/`remove`)
- `threshold`: Numeric threshold value (required for `add`/`remove`)

Behavior:
- `action="add"`: Creates a watch condition in Redis; repeated same condition is idempotent.
- `action="remove"`: Removes one matching watch condition.
- `action="list"`: Returns all watches, optionally filtered by market.
- Triggered watches are removed only after successful outbound alert delivery by the scheduler path.

Response examples:
```json
{
  "success": true,
  "action": "add",
  "market": "crypto",
  "symbol": "BTC",
  "condition_type": "price_below",
  "threshold": 90000000.0,
  "created": true,
  "already_exists": false
}
```

```json
{
  "success": true,
  "action": "list",
  "watches": {
    "crypto": [
      {
        "symbol": "BTC",
        "condition_type": "price_below",
        "threshold": 90000000.0,
        "created_at": "2026-02-17T13:40:00+09:00"
      }
    ]
  }
}
```

Error examples:
```json
{
  "success": false,
  "error": "Unknown action: foo"
}
```

### `screen_stocks` spec
Parameters:
- `market`: Market to screen - "kr", "us", "crypto" (default: "kr")
- `asset_type`: Asset type - "stock", "etf", "etn" (only applicable to KR, default: None)
- `category`: Category filter - ETF categories for KR, sector for US (default: None)
- `sort_by`: Sort criteria - "volume", "trade_amount", "market_cap", "change_rate", "dividend_yield", "rsi" (default: crypto="rsi", KR/US="volume")
- `sort_order`: Sort order - "asc" or "desc" (default: "desc")
- `min_market_cap`: Minimum market cap (억원 for KR, USD for US; not supported for crypto)
- `max_per`: Maximum P/E ratio filter (not applicable to crypto)
- `min_dividend_yield`: Minimum dividend yield filter (accepts both decimal, e.g., 0.03, and percentage, e.g., 3.0; values > 1 are treated as percentages) (not applicable to crypto)
- `max_rsi`: Maximum RSI filter 0-100 (not applicable to sorting by dividend_yield in crypto)
- `limit`: Maximum results 1-50 (default: 20)

Market-specific behavior:
- **KR market**:
  - Default `asset_type in {None, "stock"}` + `category=None` requests use tvscreener first
  - Successful stock responses expose `meta.source = "tvscreener"` and include `adx` in each result row
  - ETF/category requests stay on the legacy KRX/Naver path
  - KRX data cached with 300s TTL (Redis) + in-memory fallback
  - Trading date auto-fallback (up to 10 days back)
  - Category filter auto-limits to ETFs if `asset_type=None`
  - ETN (`asset_type="etn"`) not supported - returns error

- **US market**:
  - Default `asset_type in {None, "stock"}` + `category=None` requests use tvscreener first
  - Successful stock responses expose `meta.source = "tvscreener"` and include `adx` in each result row
  - Category/unsupported requests fall back to the legacy yfinance path
  - Legacy yfinance maps: `min_market_cap` → `intradaymarketcap`, `max_per` → `peratio.lasttwelvemonths`, `min_dividend_yield` → `forward_dividend_yield`
  - Legacy yfinance sort maps: `volume` → `dayvolume`, `market_cap` → `intradaymarketcap`, `change_rate` → `percentchange`
  - Yahoo OHLCV (`day/week/month`) requests use Redis closed-candle cache at the service boundary
  - Closed-bucket cutoff uses NYSE session close via `exchange_calendars` (`XNYS`), including DST/holidays/early close

- **Crypto market**:
  - Default success path uses tvscreener `CryptoScreener` filtered by `EXCHANGE == "UPBIT"`
  - Default sort remains `sort_by="rsi"`, `sort_order="asc"`; a requested crypto `sort_by="rsi", sort_order="desc"` is coerced to ascending and reported in `warnings` plus `filters_applied.sort_order`
  - `trade_amount_24h` maps to TradingView `CryptoField.VALUE_TRADED` and keeps the public KRW traded-value contract
  - `volume_24h` keeps the legacy Upbit 24h volume meaning (`acc_trade_volume_24h`); `VOLUME_24H_IN_USD` is never used as a public replacement for either `trade_amount_24h` or `volume_24h`
  - Result symbols are normalized back to Upbit format such as `KRW-BTC`
  - Successful tvscreener responses still restore legacy public crypto fields including `rsi_bucket`, `market_cap_rank`, `market_warning`, `volume_ratio`, `candle_type`, `plus_di`, and `minus_di`
  - Warning/crash metadata (`filtered_by_warning`, `filtered_by_crash`) and CoinGecko cache metadata are preserved on the tvscreener success path
  - `sort_by="volume"` is not supported for crypto and returns an error
  - Crypto response payload does not include `volume`; use `trade_amount_24h`
  - `market_cap` sorting is supported; public `market_cap` prefers CoinGecko cache values and falls back to TradingView `MARKET_CAP`, and final ordering uses that public value without silently falling back to `trade_amount_24h`
  - `max_per`, `min_dividend_yield`, `sort_by="dividend_yield"` not supported - returns error
  - `min_market_cap` filter is not supported; crypto responses return a warning that it was ignored

#### Crypto Composite Score Formula (`recommend_stocks`)

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
- Top 30 candidates pre-filtered by 24h traded value (`trade_amount`)
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
      "adx": 23.1,
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
  "meta": {
    "source": "tvscreener",
    "rsi_enrichment": {
      "attempted": 0,
      "succeeded": 0,
      "failed": 0,
      "rate_limited": 0,
      "timeout": 0,
      "error_samples": []
    }
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
      "rsi": 45.0,
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
      "rsi": 39.2,
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

### `get_cash_balance` spec
Parameters:
- `account`: optional account filter (`upbit`, `kis`, `kis_domestic`, `kis_overseas`)

Broker-specific contract:
- **Upbit (`account="upbit"`)**
  - `balance`: total KRW (`balance + locked`)
  - `orderable`: orderable KRW (`balance`)
  - `formatted`: formatted total KRW string (e.g. `"700,000 KRW"`)
- **KIS domestic (`account="kis_domestic"`)**
  - `balance`: `stck_cash_objt_amt` (`intgr-margin`)
  - `orderable`: first usable positive domestic integrated-margin orderable in this priority: `stck_cash100_max_ord_psbl_amt` -> `stck_itgr_cash100_ord_psbl_amt` -> `stck_cash_ord_psbl_amt` -> `stck_cash_objt_amt`; if all candidates are zero/missing, `0.0` is returned
- **KIS overseas (`account="kis_overseas"`)**
  - `balance`: USD cash balance (`frcr_dncl_amt1` fallback `frcr_dncl_amt_2`)
  - `orderable`: USD orderable cash (`frcr_gnrl_ord_psbl_amt`)

Response shape:
- `accounts`: per-account cash entries
- `summary.total_krw`: sum of KRW `balance` fields
- `summary.total_usd`: sum of USD `balance` fields
- `errors`: per-source partial failures in non-strict mode

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
- During Upbit holdings collection, coins that raise `UpbitSymbolNotRegisteredError` or `UpbitSymbolInactiveError` on name lookup are silently skipped (not added to `errors`).
- Before batch ticker request, tradable markets are loaded from `upbit_symbol_universe` and only valid holdings symbols are included in the batch
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
