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
- MCP tool call arguments are attached as structured Sentry context (`mcp_tool_call`) via `McpToolCallSentryMiddleware`:
  - Context fields: `tool_name` (string), `arguments` (dict, sanitized and truncated)
  - Tag: `mcp.tool.name` for issue-level filtering
  - Sensitive values (`token`, `secret`, `password`, `authorization`, `cookie`) are masked to `[Filtered]`
  - Large arguments are truncated (strings: 1024 chars, lists/dicts: 25 items) with a visible `[truncated]` marker
  - The middleware never calls `capture_exception` directly; exception capture is handled by Sentry's `MCPIntegration`

## Tools

### News Tools (Pre-Market Briefing Pipeline)

- `get_market_news(market=None, hours=24, feed_source=None, source=None, keyword=None, limit=20, briefing_filter=False)`
  - Fetch recent market news for OpenClaw pre-market briefing
  - `market`: Optional market scope (`kr`, `us`, `crypto`) for market-separated briefing inputs
  - `feed_source`: Collection path key (e.g., `browser_naver_mainnews`, `browser_naver_research`, `rss_cointelegraph`)
  - `source`: Publisher label (e.g., `연합뉴스`, `매일경제`, `Cointelegraph`)
  - `briefing_filter`: When `market="crypto"`, rank crypto-relevant articles and separate broad-tech/AI noise into `excluded_news`; raw storage is not affected
  - Returns: `count`, `total`, `news` (list), `sources` (unique publishers), `feed_sources` (unique collection paths), `briefing_filter`, `briefing_summary`, `excluded_news`
  - Each article includes `stock_symbol` and `stock_name` for holdings impact analysis; crypto articles also include `crypto_relevance` metadata

- `search_news(query, days=7, limit=20)`
  - Search news by keyword in title and keywords field
  - Returns matching articles with relevance based on title/keyword match

### Market Data Tools

- `search_symbol(query, limit=20)`
- `get_quote(symbol, market=None)`
- `get_orderbook(symbol, market="kr")`
- US equity quote price resolution uses Yahoo directly via `app.services.brokers.yahoo.client`
  - US quote response keeps `source: "yahoo"` and includes `previous_close/open/high/low/volume` from Yahoo `fast_info`
  - US equity Yahoo lookup failures are propagated as tool-level errors (exceptions), not returned as in-band error payload dicts
- `get_holdings(account=None, market=None, include_current_price=True, minimum_value=None, account_mode=None)`
  - Crypto positions may include optional `strategy_signal` field when Phase 2 exit logic triggers (4.5% stop-loss or RSI > 46 mean-reversion on profitable positions)
- `get_position(symbol, market=None, account_mode=None)`
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
- `get_investment_opinions(symbol, limit=10, market=None)`
- `get_short_interest(symbol, days=20)`
  - 6자리 KR 종목코드만 지원 (예: `005930`)
  - US ticker (`AAPL`, `SMCI`) 와 crypto symbol (`KRW-BTC`) 은 지원하지 않음
  - `days` 는 1~60 범위로 cap 됨
- `get_volume_profile(symbol, market=None, period=60, bins=20)`
- `get_order_history(symbol=None, status="all", order_id=None, limit=50, account_mode=None)`
  - `status="pending"` 만 symbol 없이 호출 가능
  - `status in {"all", "filled", "cancelled"}` 는 symbol 필요
  - filled/cancelled 조회는 시장별 historical endpoint 제약 때문에 symbol fan-out을 자동 수행하지 않음
- `save_trade_journal(symbol, thesis, ..., paperclip_issue_id=None)` - Save the thesis, strategy, account context, and optional Paperclip issue link for a trade.
- `get_trade_journal(symbol=None, status=None, ..., paperclip_issue_id=None)` - Query active journal entries by symbol/account or reverse-lookup a journal from a Paperclip issue ID.
- `update_trade_journal(journal_id=None, symbol=None, ...)` - Activate, close, stop, or adjust the latest matching journal entry.
- `format_execution_comment(stage, symbol, side, filled_qty, filled_price, ...)` - Format Discord/Paperclip-ready Markdown for `fill` and `follow_up` execution stages.
- `get_latest_market_brief(symbols=None, market=None, limit=10)` - Return concise latest AI analysis context for recent or selected symbols.
- `get_market_reports(symbol, days=7, limit=10)` - Return detailed AI analysis report history and decision trend for one symbol.
- `place_order(symbol, side, order_type="limit", quantity=None, price=None, amount=None, dry_run=True, reason="", exit_reason=None, thesis=None, strategy=None, target_price=None, stop_loss=None, min_hold_days=None, notes=None, indicators_snapshot=None, defensive_trim=False, approval_issue_id=None, account_mode=None)`
  - `side="buy"` 이고 `dry_run=False` 인 경우 `thesis` 와 `strategy` 가 필수
  - 실매수 성공 시 trade journal draft를 자동 생성하고 fill 저장 후 active로 연결 시도
  - 실매도 성공 시 동일 symbol의 active journal을 FIFO 기준으로 auto-close 시도
  - 부분 매도는 quantity를 수정하지 않고, fully-consumed journal만 close한다
  - journal close 실패는 주문 성공을 되돌리지 않고 `journal_warning` 으로 응답한다
  - `defensive_trim=True` 는 ROB-164/ROB-166 승인 기반 제한 경로이며 `(a) side="sell"`, `(b) order_type="limit"`, `(c) `approval_issue_id` 가 Paperclip `done` 상태, `(d) middleware-extracted caller identity 가 Trader agent 와 일치할 때만 평균단가 1% 매도 floor 를 우회한다
- `modify_order(order_id, symbol, market=None, new_price=None, new_quantity=None, dry_run=True, account_mode=None)`
- `cancel_order(order_id, symbol=None, market=None, account_mode=None)`
  - US equities: resolves exchange from symbol DB, open orders, and recent history before cancel
  - When symbol is omitted, KR/US auto-lookup is best effort and may fail if the order cannot be reconstructed
  - Discord button flows: `cancel_order(order_id="...", market="...")` — symbol auto-lookup enabled
- `modify_order` Discord button flow example:
  - `modify_order(order_id="...", symbol="...", market="...", new_price=123.45, dry_run=false)`
- `manage_watch_alerts(action, market=None, target_kind=None, symbol=None, metric=None, operator=None, threshold=None)`
- `screen_stocks(...)` - Screen stocks across different markets (KR/US/Crypto) with various filters.
- `recommend_stocks(...)` - Recommend stocks based on budget and strategy.
- `analyze_stock_batch(symbols, market=None, include_peers=False, quick=True)`
  - Analyze multiple symbols in parallel and return compact per-symbol summaries
  - Default `quick=True` returns compact summary with: symbol, current_price, rsi_14, consensus, recommendation, supports (top 3), resistances (top 3)

### Alpaca paper read-only smoke tools

ROB-69 exposes Alpaca paper broker inspection via explicit read-only MCP tool
names only:

- `alpaca_paper_get_account()`
- `alpaca_paper_get_cash()`
- `alpaca_paper_list_positions()`
- `alpaca_paper_list_orders(status="open", limit=50)`
- `alpaca_paper_get_order(order_id)`
- `alpaca_paper_list_assets(status="active", asset_class="us_equity")`
- `alpaca_paper_list_fills(after=None, until=None, limit=50)`

These tools instantiate `AlpacaPaperBrokerService`, so they inherit the
service-level endpoint guard: the trading base URL must be exactly
`https://paper-api.alpaca.markets`. The Alpaca dashboard may display
`https://paper-api.alpaca.markets/v2`, but runtime env should **not** include
`/v2`; service methods append `/v2/...` paths internally, and setting the env to
`.../v2` would produce duplicated `/v2/v2/...` requests.

Safety boundary: there are no Alpaca live MCP tools. ROB-73 adds explicit
paper-only, confirm-gated `alpaca_paper_submit_order` and
`alpaca_paper_cancel_order` tools for dev-owned smoke, with no runtime live
switch and no bulk/by-symbol cancel. There is still no Alpaca paper
`place_order`, `replace_order`, `modify_order`, `cancel_all`, or generic
Alpaca order-routing surface.

Read-only operator runbook: [`docs/runbooks/alpaca-paper-readonly-smoke.md`](../../docs/runbooks/alpaca-paper-readonly-smoke.md)
Read-only smoke helper: `scripts/smoke/alpaca_paper_readonly_smoke.py` (argumentless, read-only, exits non-zero on failure)
Dev submit/cancel smoke runbook: [`docs/runbooks/alpaca-paper-dev-smoke.md`](../../docs/runbooks/alpaca-paper-dev-smoke.md)
Dev submit/cancel smoke helper: `scripts/smoke/alpaca_paper_dev_smoke.py` (preview-only by default, side effects require dual explicit gates)

### Alpaca paper order preview

ROB-70 adds `alpaca_paper_preview_order`: a side-effect-free validator + echo tool.

**Signature:**
```
alpaca_paper_preview_order(
    symbol,          # US equity ticker (1-10 chars, uppercased)
    side,            # "buy" | "sell"
    type,            # "market" | "limit"  (stop/stop_limit deferred)
    qty=None,        # Decimal quantity (xor notional)
    notional=None,   # Decimal notional USD (xor qty; market orders only)
    time_in_force="day",   # "day" | "gtc" | "ioc" | "fok"
    limit_price=None,      # required for limit orders, forbidden for market
    stop_price=None,       # always rejected (deferred)
    client_order_id=None,  # optional, 1-48 chars
    asset_class="us_equity",  # only "us_equity" supported
)
```

**Validation rules (enforced before any service call):**
- `symbol`: non-empty after strip; uppercased; 1–10 chars
- `side`: `"buy"` or `"sell"`; case-insensitive
- `type`: `"market"` or `"limit"`; stop/stop_limit deferred
- `qty` xor `notional`: exactly one required
- `notional` + `type="limit"`: rejected (Alpaca only supports notional for market orders)
- `limit_price`: required for `type="limit"`, forbidden for `type="market"`, must be > 0
- `stop_price`: always rejected with explicit error
- `asset_class`: only `"us_equity"`; `"crypto"` and others rejected
- `time_in_force`: one of `"day"`, `"gtc"`, `"ioc"`, `"fok"`

**Return shape:**
```json
{
  "success": true,
  "account_mode": "alpaca_paper",
  "source": "alpaca_paper",
  "preview": true,
  "submitted": false,
  "order_request": { "symbol": "AAPL", "side": "buy", "type": "market", ... },
  "estimated_cost": "360" | null,
  "account_context": { "cash": "...", "buying_power": "..." } | null,
  "would_exceed_buying_power": false | null,
  "warnings": []
}
```

**Safety boundary:** Preview is a pure validator + echo. It does NOT call
POST `/v2/orders`. There is no `alpaca_paper_submit_order` / `place_order` /
`cancel_order` / `modify_order` / `replace_order` tool.

Account context (cash/buying_power) is fetched via read-only `GET /v2/account`
and fails soft: if unavailable, `account_context` is `null` and
`"context_unavailable"` is added to `warnings`. The preview still returns
`success: true` with the normalized `order_request` echo.

The endpoint guard applies: if `ALPACA_PAPER_BASE_URL` is ever set to the live
endpoint, the service constructor rejects it and the tool raises
`AlpacaPaperEndpointError` (fail closed).

### Account Routing

MCP account-facing tools use `account_mode` to avoid mixing DB simulation,
official KIS mock, and KIS live account paths:

- `account_mode="db_simulated"`: DB-backed paper trading only. No KIS broker
  calls. Existing `account_type="paper"`, `account_mode="paper"`, and
  `account_mode="simulated"` remain aliases and return warnings.
- `account_mode="kis_mock"`: official KIS mock/sandbox account. Uses KIS mock
  credentials only, passes `is_mock=True` to KIS broker methods, and fails
  closed if `KIS_MOCK_ENABLED=true`, `KIS_MOCK_APP_KEY`,
  `KIS_MOCK_APP_SECRET`, or `KIS_MOCK_ACCOUNT_NO` are missing. HTTP requests
  use `KIS_MOCK_BASE_URL`, which defaults to the official KIS mock host
  `https://openapivts.koreainvestment.com:29443`.
- `account_mode="kis_live"` or omitted: existing live KIS behavior. For
  `place_order`, `dry_run=True` remains the default.

Do not use `account_type="paper"` for official KIS mock. It is always DB
simulation. Responses from updated surfaces include `account_mode`; deprecated
aliases include `warnings`.
  - Set `quick=False` for full analysis payload (like `analyze_portfolio`)
  - Example: `analyze_stock_batch(symbols=["NVDA", "AMZN", "MSFT", "GOOGL"], market="us")`

#### KIS mock unsupported endpoints

> See [`docs/kis-mock-tr-routing-matrix.md`](../../docs/kis-mock-tr-routing-matrix.md)
> for the full live ↔ mock TR routing matrix.

`account_mode="kis_mock"` returns explicit "mock unsupported" errors instead
of silently degrading for the following KIS endpoints, which are live-only on
the official KIS mock account:

- `inquire_integrated_margin` (`TTTC0869R`) — returns `OPSQ0002 없는 서비스 코드 입니다`
  on mock. Mock cash routes via `inquire_domestic_cash_balance` (`VTTC8434R`)
  instead.
- `inquire_overseas_orders` (`TTTS3018R`) — KIS does not publish a mock TR.
  Pending US history under `account_mode="kis_mock"` returns
  `errors: [{market: "equity_us", error: "kis_mock: overseas pending-orders
  inquiry ..."}]` and an empty orders list.
- `inquire_korea_orders` (`TTTC8036R`) — confirmed live-only on the mock
  account (returns `EGW02006 모의투자 TR 이 아닙니다`). Pending KR history
  and KR cancel/modify lookup under `account_mode="kis_mock"` return
  `mock_unsupported=true` errors instead of attempting the call.
- KIS overseas margin (`TTTC2101R` / `VTTS2101R`) — treated as
  mock-unsupported; the USD account row is omitted under
  `account_mode="kis_mock"` and the failure is reported in `errors[]`.

#### Operator runtime config

`account_mode="kis_mock"` reads only `KIS_MOCK_*` settings. To enable the
mock account in production, the operator should source a separate env file
(for example `~/services/auto_trader/shared/.env.kis-mock`) into the launchd
plist environment for the MCP / API processes — **never** merge mock
secrets into the live `.env.prod.native` file. When any of
`KIS_MOCK_ENABLED=true`, `KIS_MOCK_APP_KEY`, `KIS_MOCK_APP_SECRET`, or
`KIS_MOCK_ACCOUNT_NO` are missing, every mock surface returns:

```
{
  "success": false,
  "error": "KIS mock account is disabled or missing required configuration: KIS_MOCK_ENABLED, ...",
  "source": "kis",
  "account_mode": "kis_mock"
}
```

The error names variables only — never values.

### `get_orderbook` spec
Parameters:
- `symbol`: KR equity symbol/code or Upbit market code (required)
- `market`: defaults to `"kr"`; supports KR aliases (`"kr"`, `"kospi"`, `"kosdaq"`, `"korea"`, `"kis"`, `"equity_kr"`) plus crypto aliases (`"crypto"`, `"upbit"`)

Behavior:
- KR requests follow the existing KR quote normalization path, including zero-padding numeric codes such as `5930 -> 005930`
- Crypto orderbook requests require explicit `market="crypto"` (or `"upbit"`) and a raw `KRW-*` symbol such as `KRW-BTC`; plain coins (`BTC`) and non-KRW crypto pairs (`USDT-BTC`) raise an argument error
- Valid KR requests use KIS `inquire-asking-price-exp-ccn` and return 10-level asks/bids, total residual quantities, expected match metadata, and integer-valued `price`, `quantity`, `total_ask_qty`, `total_bid_qty`, and `spread`
- Valid crypto requests use Upbit orderbook data and return the same shared snapshot fields, but `price`, `quantity`, `total_ask_qty`, `total_bid_qty`, and `spread` can be fractional numbers
- `expected_qty` keeps the public `int | null` contract; when KIS leaves `output2.antc_cnqn` blank or omits it, the response serializes `expected_qty` as `null` instead of inventing a fallback quantity
- During the NXT session (`16:00`-`20:00` KST), KIS may return `expected_price` while leaving `expected_qty` blank or absent; this is treated as a valid upstream state, not an MCP error
- Successful responses always include MCP-only derived fields: `pressure`, `pressure_desc`, `spread`, `spread_pct`, `bid_walls`, and `ask_walls`
- Successful KR responses use `source: "kis"`, `instrument_type: "equity_kr"`, and return `bid_walls: []`, `ask_walls: []`
- Successful crypto responses use `source: "upbit"`, `instrument_type: "crypto"`, and may return non-empty wall arrays
- Invalid input raises; upstream failures for otherwise valid requests return an in-band error payload via the shared MCP error contract. When the underlying exception is a `DomainServiceError`, the payload may also include `error_type`

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
  "expected_qty": null,
  "bid_walls": [],
  "ask_walls": []
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
- `spread` is `asks[0].price - bids[0].price` when both best levels exist; otherwise it is `null` (integer-valued for KR, fractional-capable for crypto)
- `spread_pct` is `(spread / bids[0].price) * 100`, rounded to 3 decimal places, and becomes `null` when the best bid is missing or `<= 0`
- `bid_walls` / `ask_walls` are MCP-only convenience fields. They are calculated only for crypto orderbooks by taking each side's `value_krw = round(price * quantity)`, using the side median as the baseline, selecting levels where `value_krw >= baseline * 2`, sorting by `value_krw` descending, and returning up to 3 entries shaped as `{price, size, value_krw}`

### Crypto sell orderable validation
- Crypto `place_order(..., side="sell")` uses **orderable balance only** (`balance` from Upbit `/v1/accounts`), not total holdings (`balance + locked`).
- `locked` coins are already committed to pending orders and cannot be sold.
- `quantity=None` (full sell) defaults to the orderable balance.
- If `quantity > orderable balance`, the tool returns `success: false` with an error containing `requested`, `orderable`, and `locked` values instead of forwarding to Upbit.

### Crypto stop-loss cooldown (Phase 2 strategy)
- `place_order(..., side="buy", market="crypto")` may reject buys while a stop-loss cooldown is active; returns `success: false` with cooldown message
- `place_order(..., side="sell", market="crypto")` automatically records an 8-day stop-loss cooldown after a non-dry-run sell when `current_price <= avg_buy_price * (1 - 0.045)` (4.5% stop-loss)
- Dry-run sells do not record cooldown; profitable sells (above stop-loss threshold) do not record cooldown

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

### `analyze_stock` spec

Parameters:
- `symbol`: Asset symbol/ticker/code (required; string or int accepted)
- `market`: Market - `"kr"`, `"us"`, `"crypto"` (optional, inferred from symbol if omitted)
- `include_peers`: Whether to include sector peer analysis for KR/US equities (default: false; ignored for crypto)

Response notes:
- Equity responses (KR/US markets) include `recommendation.rsi14` when RSI(14) is available from the indicator payload
- This field provides a convenient summary; callers should continue to use `get_indicators` when they need the full indicator set rather than the summarized recommendation field

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

### `get_investment_opinions` spec
Parameters:
- `symbol`: Asset ticker/code input (required)
- `limit`: Maximum detailed opinion rows to return (default: 10)
- `market`: Optional explicit market (`kr`, `us`)

Behavior:
- KR requests keep the existing Naver Finance path and return recent analyst opinions plus consensus statistics.
- US requests use yfinance and keep the public top-level shape: `symbol`, `count`, `opinions`, `consensus`, plus optional `warning`.
- US `opinions` remains the recent Yahoo `upgrades_downgrades` event list (firm/rating/date plus row-level `target_price` when Yahoo provides one).
- US top-level `count` remains `len(opinions)`; it does **not** represent aggregate analyst coverage.
- US `consensus.total_count` is the aggregate analyst coverage count from the current Yahoo `recommendationTrend` / `ticker.recommendations` row (`period="0m"` preferred).
- US aggregate count mapping is:
  - `buy_count = strongBuy + buy`
  - `hold_count = hold`
  - `sell_count = sell + strongSell`
  - `strong_buy_count = strongBuy`
  - `total_count = strongBuy + buy + hold + sell + strongSell`
- US target statistics (`avg_target_price`, `median_target_price`, `min_target_price`, `max_target_price`, `current_price`, `upside_pct`) come from Yahoo `analyst_price_targets` after numeric normalization.
- US target normalization accepts Yahoo raw dicts such as `{raw, fmt}`, plain numbers, and pandas/numpy scalars; `0`, negative, empty, and non-numeric placeholders are treated as unavailable.
- When Yahoo analyst counts or target statistics are unavailable, the corresponding US `consensus` fields are returned as `null` instead of fabricated zeroes.
- When Yahoo provides neither usable aggregate counts nor usable analyst target data, the US response includes a top-level `warning`.

### `manage_watch_alerts` spec
Parameters:
- `action`: Required action - `"add"`, `"remove"`, `"list"`
- `market`: Market - `"crypto"`, `"kr"`, `"us"` (required for `add`/`remove`, optional for `list`)
- `target_kind`: Watched target type - `"asset"` (default), `"index"`, or `"fx"`
- `symbol`: Asset ticker, index symbol, or FX symbol (required for `add`/`remove`)
- `metric`: Condition metric - `"price"`, `"rsi"`, or `"trade_value"` (required for `add`/`remove`)
- `operator`: Condition operator - `"above"` or `"below"` (required for `add`/`remove`)
- `threshold`: Numeric threshold value (required for `add`/`remove`)

Behavior:
- `action="add"`: Creates a watch condition in Redis; repeated same condition is idempotent.
- `action="remove"`: Removes one matching watch condition.
- `action="list"`: Returns all watches, optionally filtered by market.
- Triggered watches are removed only after successful outbound alert delivery by the scheduler path.
- Legacy asset watches stored before `target_kind` are listed as `target_kind="asset"` and can still be removed with the same tool arguments.

Supported target/metric combinations:
- `target_kind="asset"`: `price` and `rsi` for `crypto`, `kr`, `us`; `trade_value` for `kr` only.
- `target_kind="index"`: `price` for `market="kr"` and `symbol="KOSPI"` or `"KOSDAQ"`.
- `target_kind="fx"`: `price` for `market="kr"` and `symbol="USDKRW"`.

Example calls:
```text
manage_watch_alerts(action="add", market="kr", target_kind="index", symbol="KOSPI", metric="price", operator="below", threshold=6176.75)
manage_watch_alerts(action="add", market="kr", target_kind="index", symbol="KOSDAQ", metric="price", operator="below", threshold=1161.00)
manage_watch_alerts(action="add", market="kr", target_kind="fx", symbol="USDKRW", metric="price", operator="above", threshold=1478)
manage_watch_alerts(action="add", market="kr", symbol="005930", metric="trade_value", operator="above", threshold=1000000000)
```

Response examples:
```json
{
  "success": true,
  "action": "add",
  "market": "crypto",
  "target_kind": "asset",
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
        "target_kind": "asset",
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
- `market`: Market to screen - "kr", "kospi", "kosdaq", "konex", "all", "us", "crypto" (default: "kr")
- `asset_type`: Asset type - "stock", "etf", "etn" (only applicable to KR, default: None)
- `category`: Category filter - ETF categories for KR, sector for US (default: None)
- `sector`: Sector filter for KR/US stocks (default: None). Not supported for crypto or KR ETF/ETN requests
- `exclude_sectors`: Sector exclusion list for KR/US stocks (default: None). Values are de-duplicated case-insensitively for ASCII labels
- `instrument_types`: Instrument taxonomy filter list - "common", "preferred", "etf", "reit", "spac", "unknown" (default: None)
- `adv_krw_min`: Minimum 30-day average daily value in KRW. Use 1,000,000,000 for a conservative liquidity floor or 5,000,000,000 for an aggressive liquidity floor
- `market_cap_min_krw`: Minimum market capitalization in KRW (default: None)
- `market_cap_max_krw`: Maximum market capitalization in KRW (default: None)
- `sort_by`: Sort criteria - "volume", "trade_amount", "market_cap", "change_rate", "dividend_yield", "rsi" (default: crypto="rsi", KR/US="volume")
- `sort_order`: Sort order - "asc" or "desc" (default: "desc")
- `min_market_cap`: Minimum market cap (억원 for KR, USD for US; not supported for crypto)
- `max_per`: Maximum P/E ratio filter (not applicable to crypto)
- `min_dividend_yield`: Minimum dividend yield filter (accepts both decimal, e.g., 0.03, and percentage, e.g., 3.0; values > 1 are treated as percentages) (not applicable to crypto)
- `min_dividend`: Alias for `min_dividend_yield`. Accepts same format. If both specified, they must be equal
- `min_analyst_buy`: Minimum analyst buy count filter (default: None). Only supported for KR/US stocks (not ETF/ETN)
- `max_rsi`: Maximum RSI filter 0-100 (not applicable to sorting by dividend_yield in crypto)
- `limit`: Maximum results 1-100 (default: 50)

Market-specific behavior:
- **KR market**:
  - `market="konex"` screens KONEX only; `market="all"` screens KOSPI, KOSDAQ, and KONEX
  - Default `asset_type in {None, "stock"}` + `category=None` requests use tvscreener only when verified KR stock-query capabilities cover the request; otherwise they fall back to the legacy KRX/Naver path before entering tvscreener
  - Successful stock responses expose `meta.source = "tvscreener"` and include `adx`, `instrument_type`, and 30-day ADV fields when TradingView provides them
  - `adv_krw_min` uses TradingView 30-day average volume multiplied by price; responses set `meta.adv_window_days = 30` when this filter is requested
  - Legacy KRX fallback cannot compute `adv_krw_min`; it returns a warning and skips only that filter
  - `sort_by="rsi"` is supported via tvscreener RSI data; legacy path falls back to OHLCV-based RSI enrichment
  - ETF/category requests stay on the legacy KRX/Naver path
  - KRX data cached with 300s TTL (Redis) + in-memory fallback
  - Trading date auto-fallback (up to 10 days back)
  - Category filter auto-limits to ETFs if `asset_type=None`
  - ETN (`asset_type="etn"`) not supported - returns error

- **US market**:
  - Default `asset_type in {None, "stock"}` requests use tvscreener only when verified US stock-query capabilities cover the request
  - US `category`/`sector` alias requests stay on the tvscreener path only when the TradingView sector filter capability is verified; otherwise they fall back to legacy before running the tv query
  - `sort_by="rsi"` is supported via tvscreener RSI data; legacy yfinance path falls back to OHLCV-based RSI enrichment
  - Successful stock responses expose `meta.source = "tvscreener"`, include `adx`, `instrument_type`, 30-day ADV fields, and preserve public enrichment fields (`sector`, `analyst_buy`, `analyst_hold`, `analyst_sell`, `avg_target`, `upside_pct`) from tvscreener when available
  - `adv_krw_min` uses TradingView 30-day average volume multiplied by price; responses set `meta.adv_window_days = 30` when this filter is requested
  - Legacy yfinance fallback cannot compute `adv_krw_min`; it returns a warning and skips only that filter
  - Post-screen enrichment skips per-row Finnhub/yfinance fan-out when those public fields are already populated; missing fields fall back to lightweight yfinance/Finnhub enrichment
  - Unsupported or unverified tvscreener request-critical capabilities fall back to the legacy yfinance path
  - Legacy yfinance maps: `min_market_cap` → `intradaymarketcap`, `max_per` → `peratio.lasttwelvemonths`, `min_dividend_yield` → `forward_dividend_yield`
  - Legacy yfinance sort maps: `volume` → `dayvolume`, `market_cap` → `intradaymarketcap`, `change_rate` → `percentchange`
  - Legacy yfinance screen enrichment reuses a request-scoped session for repeated analyst-target lookups
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
  - Stop-loss cooldown filter: symbols in an 8-day stop-loss cooldown window (after a stop-loss sell) are excluded from results; count available in `meta.filtered_by_stop_loss_cooldown`
  - `sort_by="volume"` is not supported for crypto and returns an error
  - Crypto response payload does not include `volume`; use `trade_amount_24h`
  - `market_cap` sorting is supported; public `market_cap` prefers CoinGecko cache values and falls back to TradingView `MARKET_CAP`, and final ordering uses that public value without silently falling back to `trade_amount_24h`
  - `max_per`, `min_dividend_yield`, `sort_by="dividend_yield"` not supported - returns error
  - `min_market_cap` filter is not supported; crypto responses return a warning that it was ignored
  - `sector`, `exclude_sectors`, `instrument_types`, `adv_krw_min`, `market_cap_min_krw`, `market_cap_max_krw`, and `min_analyst_buy` filters are not supported for crypto - returns error

Filter compatibility and error semantics:
- `sector` filter: Supported for KR/US stocks only. Returns error for crypto or KR ETF/ETN requests
- `exclude_sectors`: Supported for KR/US stocks only. Cannot overlap with `sector`
- `instrument_types`: Supported for KR/US only. `asset_type="etf"` conflicts with `instrument_types=["common"]`
- `adv_krw_min`, `market_cap_min_krw`, `market_cap_max_krw`: Non-negative integers only. `market_cap_min_krw` must be less than or equal to `market_cap_max_krw`
- `min_analyst_buy` filter: Supported for KR/US stocks only (not ETF/ETN). Returns error for crypto or non-stock asset types
- `min_dividend` / `min_dividend_yield`: These are aliases. Accepts decimal (0.03) or percentage (3.0) formats. If both are specified with different values, returns error. Not supported for crypto
- `category` and `sector`: These are aliases for US market. If both are specified with different values, returns error
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
      "sector": "Technology",  // Industry sector (can be null for some stocks)
      "analyst_buy": 15,  // Number of analyst buy ratings (default: 0)
      "analyst_hold": 3,  // Number of analyst hold ratings (default: 0)
      "analyst_sell": 2,  // Number of analyst sell ratings (default: 0)
      "avg_target": 85000.0,  // Average analyst target price (can be null)
      "upside_pct": 6.25,  // Upside percentage based on analyst targets (can be null)
      "market": "kr"
      "market": "kr"
    }
  ],
  "total_count": 2400,  // Total stocks that passed all filters (before sort/limit). If data source provides total, uses that; otherwise uses fetched candidates count.
  "returned_count": 20,  // Actual number of results returned (after limit)
  "filters_applied": {
    "market": "kr",
    "asset_type": "stock",
    "sector": "Technology",  // Applied sector filter (if specified)
    "min_market_cap": 100000,
    "max_per": 20,
    "min_dividend_yield": 0.03,
    "min_dividend_yield_input": 3.0,
    "min_dividend_yield_normalized": 0.03,
    "min_dividend_input": 3.0,  // Original min_dividend value if specified
    "min_analyst_buy": 5,  // Applied minimum analyst buy count filter
    "max_rsi": 70
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
  - `orderable`: domestic integrated-margin orderable minus pending KR buy-order notional; if pending-order lookup fails, raw KIS orderable is returned; result is clamped at `0.0`
- **KIS overseas (`account="kis_overseas"`)**
  - `balance`: USD cash balance (`frcr_dncl_amt1` fallback `frcr_dncl_amt_2`)
  - `orderable`: USD orderable cash minus pending US buy-order notional; if pending-order lookup fails, raw KIS orderable is returned; result is clamped at `0.0`

Response shape:
- `accounts`: per-account cash entries
- `summary.total_krw`: sum of KRW `balance` fields
- `summary.total_usd`: sum of USD `balance` fields
- `errors`: per-source partial failures in non-strict mode

### `get_available_capital` spec
Parameters:
- `account`: optional account filter (`upbit`, `kis`, `kis_domestic`, `kis_overseas`, `toss`)
- `include_manual`: whether to include manual cash in aggregation (default: `true`)

Behavior:
- Aggregates orderable cash across all broker accounts (Upbit, KIS domestic, KIS overseas)
- Converts USD orderable amounts to KRW equivalents using current exchange rate
- Includes manual cash (Toss/non-API cash) when `include_manual=True`
- Marks manual cash as stale when older than 3 days

Response shape:
- `accounts`: per-account cash entries with `krw_equivalent` added for USD accounts
- `manual_cash`: manual cash details with `amount`, `updated_at`, and `stale_warning`
- `summary.total_orderable_krw`: total orderable amount in KRW across all sources
- `summary.exchange_rate_usd_krw`: USD to KRW exchange rate used for conversion
- `summary.as_of`: ISO timestamp of when the data was retrieved
- `errors`: per-source partial failures

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
- KIS US holdings keep KIS-provided snapshot values when the KIS snapshot is numerically valid: `current_price > 0`, `evaluation_amount > 0`, and `profit_loss` / `profit_rate` are parseable numbers
- KIS US holdings fall back to Yahoo only when that KIS snapshot is missing or invalid; Yahoo is a fallback refresh path, not the default for valid KIS US holdings
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

### User Settings Tools

- `get_user_setting(key)` - Get a user setting value by key. Returns the JSON value or None if not found.
- `set_user_setting(key, value)` - Set a user setting value by key (upsert). Returns the serialized setting with key, value, and updated_at.

These tools provide a generic key-value storage for user preferences and settings. Values are stored as JSON and can be any valid JSON-serializable data structure.

Common settings:
- `manual_cash`: Stores manually-managed cash amounts (e.g., `{"amount": 15000000}`) for accounts not backed by APIs (Toss, etc.)

### `update_manual_holdings` spec

Parameters:
- `holdings`: List of holding objects to upsert/remove (required)
- `broker`: Broker identifier - `"toss"`, `"samsung"`, `"kis"` (required)
- `account_name`: Account name - `"기본 계좌"`, `"퇴직연금"`, `"ISA"` (default: `"기본 계좌"`)
- `dry_run`: Preview mode without DB changes (default: `true`)

Holding object fields:
- `symbol`: Ticker/symbol (e.g., `"AAPL"`, `"005930"`, `"KRW-BTC"`). Takes precedence over `stock_name`.
- `stock_name`: Company name or alias (e.g., `"삼성전자"`, `"애플"`). Used for symbol resolution when `symbol` is not provided.
- `quantity`: Number of shares/coins (required for upsert)
- `avg_buy_price`: Average purchase price (optional). If not provided, calculated from `eval_amount`, `profit_loss`, and `quantity`.
- `eval_amount`: Current evaluation amount (optional, used for avg_price calculation)
- `profit_loss`: Unrealized profit/loss (optional, used for avg_price calculation)
- `profit_rate`: Profit rate percentage (optional, informational)
- `market_section`: Market type - `"kr"`, `"us"`, `"crypto"` (required)
- `action`: Operation - `"upsert"` or `"remove"` (default: `"upsert"`)

Validation rules:
- **US ticker resolution**: US holdings must use a real ticker or a pre-registered alias in `stock_alias` table.
- **US name-like input**: If a US name-like string fails lookup, the tool raises an error asking to add `stock_alias` mapping or supply the ticker directly.
- **US avg_buy_price**: Must be in USD. Values above `1000` are rejected with error: `"USD 단위로 입력해주세요 (현재 값: {value}, KRW로 의심됩니다)"`.
- **Quantity zero/negative**: `qty <= 0` payloads are treated as delete/cleanup intent:
  - If a matching holding exists, it is removed (same as `action="remove"`)
  - If no matching holding exists, a warning is generated
- **dry_run behavior**: When `dry_run=True`, no DB mutations occur; only preview data and warnings are returned.

Response format:
```json
{
  "success": true,
  "dry_run": false,
  "message": "Holdings updated successfully",
  "broker": "samsung",
  "account_name": "기본 계좌",
  "parsed_count": 3,
  "holdings": [...],
  "warnings": [],
  "added_count": 1,
  "updated_count": 1,
  "removed_count": 1,
  "unchanged_count": 0,
  "diff": [...]
}
```

Error response format:
```json
{
  "success": false,
  "error": "USD 단위로 입력해주세요 (현재 값: 14966.0, KRW로 의심됩니다)"
}
```

### `get_user_setting` spec
Parameters:
- `key`: Setting key string (required)

Returns:
- The JSON value stored for the key, or `None` if the key doesn't exist

### `set_user_setting` spec
Parameters:
- `key`: Setting key string (required)
- `value`: Any JSON-serializable value (required)

Returns:
```json
{
  "key": "manual_cash",
  "value": {"amount": 15000000},
  "updated_at": "2026-04-01T08:00:00+00:00"
}
```

Behavior:
- Creates the setting if it doesn't exist, updates it if it does (upsert)
- `updated_at` is automatically set to the current timestamp
- The (user_id, key) pair is unique; attempting to create a duplicate key for the same user will update the existing entry

## Caller Identity Header (required)

All MCP callers (Scout, Trader, CIO bridges, and any future client) MUST send
`x-paperclip-agent-id: <calling agent's Paperclip agent id>` on every
`tools/call` request. The value is the caller's Paperclip agent id, not the
target trader agent id.

- The `CallerIdentityMiddleware` added in ROB-214 (ST-3.1) reads this header,
  stores it in a request-scoped contextvar, and records the extraction source
  (`http_header` | `env_fallback` | `none`) on each call.
- Caller-identity-gated tools (e.g. `place_order(..., defensive_trim=True)`
  after ST-3.2) reject calls where the contextvar is `None`, so a missing
  header in a production path is an outage, not a soft warning.
- Local dev / stdio transports that cannot send HTTP headers may export
  `MCP_CALLER_AGENT_ID` as an env fallback. This is a dev convenience only —
  production callers must send the header explicitly. `MCP_CALLER_AGENT_ID`
  MUST NOT be set in production HTTP deployments because it re-opens a caller
  spoofing vector for requests that omit `x-paperclip-agent-id`.

### Scout / Trader curl bridge

When an agent runs under a harness that does not register the auto_trader MCP
server in-process (current state for Scout and Trader on `claude_local`),
they use a JSON-RPC curl bridge at `/tmp/mcp_call.sh`. The canonical template
lives at `scripts/templates/mcp_call.sh.tmpl`; both agents MUST regenerate
their local `/tmp/mcp_call.sh` from that template so the header is present.

```bash
# From the repo root, per operator host/session:
export MCP_ENDPOINT="http://127.0.0.1:8765/mcp"
export MCP_AUTH_TOKEN="<value from env.MCP_AUTH_TOKEN>"
export MCP_SESSION_ID="<MCP session id>"
export PAPERCLIP_AGENT_ID="<calling agent's Paperclip agent id>"
envsubst '$MCP_ENDPOINT $MCP_AUTH_TOKEN $MCP_SESSION_ID $PAPERCLIP_AGENT_ID' \
  < scripts/templates/mcp_call.sh.tmpl > /tmp/mcp_call.sh
# 0700 — owner-only. The rendered script bakes MCP_AUTH_TOKEN in plaintext,
# so group/other read bits must be stripped.
chmod 700 /tmp/mcp_call.sh

# Smoke test — should return a tool payload, not 401/403/reject:
/tmp/mcp_call.sh get_quote '{"symbol":"005930","market":"kr"}'
```

The rendered bridge intentionally calls curl with `-N --max-time 15` and sends
`Connection: close`. It only consumes the first SSE `data:` line, so no-buffer
mode and the timeout keep the helper from holding a completed Paperclip run open
if the server keeps the stream alive.

If the Trader adapter is later migrated to an in-process MCP client (for
example a Claude Code `.mcp.json` entry or an SDK-level `default_headers`
config), that client must also set `x-paperclip-agent-id`; do not rely on
the shell bridge as the long-term header injection point.

## Run (docker-compose.prod)
Environment variables:
- `MCP_TYPE` : `streamable-http` (default) | `sse` | `stdio`
- `MCP_HOST` : `0.0.0.0`
- `MCP_PORT` : `8765`
- `MCP_PATH` : `/mcp`
- `MCP_GRACEFUL_SHUTDOWN_TIMEOUT` : `10` (seconds, HTTP transports only: `sse` / `streamable-http`)
- `MCP_USER_ID` : `1` (manual holdings 조회에 사용할 기본 사용자 ID)
- `MCP_CALLER_AGENT_ID` : DEV/stdio only — MUST NOT be set in production HTTP deployments (re-opens caller spoofing vector)

Example:
```bash
docker compose -f docker-compose.prod.yml up -d mcp
```

> Note: current prod compose uses `network_mode: host`, so port publishing is handled by the host network.

---

## MCP Profiles (ROB-56)

### Overview

The `MCP_PROFILE` env var selects which tool subset is registered at startup.

| Profile | Value | Order surface |
|---|---|---|
| Default (unchanged) | `default` (or unset) | Legacy `place_order`/`cancel_order`/`modify_order`/`get_order_history` + typed `kis_live_*` + typed `kis_mock_*` |
| Paper/mock-only | `hermes-paper-kis` | Typed `kis_mock_*` only — live surface **physically absent** |

### Profile: `hermes-paper-kis`

Set `MCP_PROFILE=hermes-paper-kis` on paper-only deployments (e.g., where `KIS_MOCK_ENABLED=true`).

- `kis_live_place_order`, `kis_live_cancel_order`, `kis_live_modify_order`, `kis_live_get_order_history` are **not registered**.
- The legacy ambiguous `place_order`, `cancel_order`, `modify_order`, `get_order_history` are **not registered**.
- Only `kis_mock_*` typed order tools are registered.
- All read-only research and portfolio tools remain available.

**Operator validation:** after deploying with `hermes-paper-kis`, check the MCP `/mcp` listing and confirm that none of `kis_live_*` or the legacy ambiguous order tools appear.

### Typed KIS order tools

Both profiles (including `default`) provide explicitly-named typed variants:

**Mock (KIS official mock / paper):**
- `kis_mock_place_order` — hard-pinned `is_mock=True`; fails closed if KIS mock config missing
- `kis_mock_cancel_order`
- `kis_mock_modify_order`
- `kis_mock_get_order_history`

**Live (real-money):**
- `kis_live_place_order` — hard-pinned `is_mock=False`
- `kis_live_cancel_order`
- `kis_live_modify_order`
- `kis_live_get_order_history`

Each typed tool rejects any `account_mode` value other than its own pinned mode.

### Fail-closed behavior

`kis_mock_*` tools return a structured error (without delegating) when KIS mock config is incomplete:

```json
{
  "success": false,
  "error": "KIS mock account is disabled or missing required configuration: KIS_MOCK_ENABLED, KIS_MOCK_APP_KEY",
  "source": "kis",
  "account_mode": "kis_mock"
}
```

With all mock vars missing, the `hermes-paper-kis` profile is effectively read-only KIS — the safe state for a misconfigured paper deployment.
