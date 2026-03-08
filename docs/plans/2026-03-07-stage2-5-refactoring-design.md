# Stage 2-5 Refactoring Design

**Generated:** 2026-03-07
**Status:** Implementation Ready
**Prerequisite:** Stage 1 Complete (KIS Trading Service Exception Handling)

---

## Overview

Multi-stage refactoring to reduce operational risk, improve maintainability, and consolidate duplicated logic across the auto_trader codebase.

| Stage | Target | LOC | Priority | Status |
|-------|--------|-----|----------|--------|
| Stage 1 | KIS Trading Service Exception Handling | 690 | High | **COMPLETE** |
| Stage 2 | KISClient Decomposition | 3,626 | High | Pending |
| Stage 3 | MCP Pipeline Normalization | 2,490 | Medium | Pending |
| Stage 4 | Notification Layer Separation | 1,832 | Medium | Pending |
| Stage 5 | Fundamentals + Naver Cleanup | 908+ | Low | Pending |

---

## Stage 2: KISClient Decomposition

### Goal
Decompose 3,626 LOC monolithic `KISClient` into domain-specific sub-clients while maintaining backward-compatible public API.

### Current Structure
```
app/services/brokers/kis/
├── client.py          # 3,626 LOC, 30 public methods
├── constants.py       # URL/TR constants
├── types.py           # Type definitions
└── __init__.py
```

### Target Structure
```
app/services/brokers/kis/
├── base.py            # BaseKISClient (token, rate-limit, HTTP)
├── market_data.py     # MarketDataClient (12 methods)
├── account.py         # AccountClient (6 methods)
├── domestic_orders.py # DomesticOrderClient (6 methods)
├── overseas_orders.py # OverseasOrderClient (7 methods)
├── client.py          # KISClient facade (backward compat)
├── constants.py       # URL/TR constants (unchanged)
├── types.py           # Type definitions (unchanged)
└── __init__.py        # Export all clients
```

### Sub-Client Design

#### BaseKISClient (Shared Infrastructure)
```python
class BaseKISClient:
    """Shared infrastructure for all KIS sub-clients."""
    
    def __init__(self, settings: Settings):
        self._settings = settings
        self._token_manager = TokenManager(settings)
        self._rate_limiter = AsyncRateLimiter()
    
    async def _ensure_token(self) -> str:
        """Ensures valid access token."""
    
    async def _request_with_rate_limit(
        self, method: str, url: str, tr_id: str, ...
    ) -> dict[str, Any]:
        """HTTP request with rate limiting and retry logic."""
    
    def _get_rate_limit_for_api(self, api_key: str) -> tuple[int, int]:
        """Resolves rate limit for specific API."""
```

#### MarketDataClient (12 methods)
- `volume_rank()` - Volume ranking
- `market_cap_rank()` - Market cap ranking
- `fluctuation_rank()` - Price change ranking
- `foreign_buying_rank()` - Foreign buying ranking
- `inquire_price()` - Single stock price
- `inquire_orderbook()` - 10-level orderbook
- `fetch_fundamental_info()` - Fundamental data
- `inquire_daily_itemchartprice()` - Daily candles
- `inquire_time_dailychartprice()` - Intraday candles
- `inquire_minute_chart()` - Minute candles
- `inquire_overseas_daily_price()` - Overseas daily chart
- `fetch_minute_candles()` - Aggregated minute candles

#### AccountClient (6 methods)
- `fetch_my_stocks()` - Domestic/overseas holdings
- `inquire_domestic_cash_balance()` - KR cash balance
- `inquire_overseas_margin()` - Overseas margin by currency
- `inquire_integrated_margin()` - Integrated margin
- `fetch_my_overseas_stocks()` - Overseas holdings wrapper
- `fetch_my_us_stocks()` - US holdings wrapper

#### DomesticOrderClient (6 methods)
- `inquire_korea_orders()` - Open orders inquiry
- `order_korea_stock()` - Place buy/sell order
- `sell_korea_stock()` - Sell wrapper
- `cancel_korea_order()` - Cancel order
- `modify_korea_order()` - Modify order
- `inquire_daily_order_domestic()` - Order history

#### OverseasOrderClient (7 methods)
- `order_overseas_stock()` - Place buy/sell order
- `buy_overseas_stock()` - Buy wrapper
- `sell_overseas_stock()` - Sell wrapper
- `inquire_overseas_orders()` - Open orders inquiry
- `cancel_overseas_order()` - Cancel order
- `modify_overseas_order()` - Modify order
- `inquire_daily_order_overseas()` - Order history

### Backward Compatibility

```python
# client.py - Facade maintaining existing API
class KISClient(BaseKISClient):
    """Facade providing backward-compatible API."""
    
    def __init__(self, settings: Settings):
        super().__init__(settings)
        self._market_data = MarketDataClient(settings, self)
        self._account = AccountClient(settings, self)
        self._domestic_orders = DomesticOrderClient(settings, self)
        self._overseas_orders = OverseasOrderClient(settings, self)
    
    # Delegate all 30 methods to sub-clients
    def volume_rank(self, *args, **kwargs):
        return self._market_data.volume_rank(*args, **kwargs)
    
    # ... (29 more delegations)
```

### Implementation Steps

1. **Create `base.py`** with `BaseKISClient`
   - Extract `_ensure_token`, `_request_with_rate_limit`, `_get_rate_limit_for_api`
   - Extract `_fetch_token`

2. **Create `market_data.py`** with `MarketDataClient`
   - Move 12 market data methods
   - Move chart aggregation helpers (`_aggregate_minute_candles`)

3. **Create `account.py`** with `AccountClient`
   - Move 6 account methods

4. **Create `domestic_orders.py`** with `DomesticOrderClient`
   - Move 6 domestic order methods
   - Move `_resolve_korea_order_orgno`, `_extract_korea_order_orgno`

5. **Create `overseas_orders.py`** with `OverseasOrderClient`
   - Move 7 overseas order methods

6. **Update `client.py`** as facade
   - Inherit from `BaseKISClient`
   - Compose sub-clients
   - Delegate all 30 methods

7. **Update imports** across codebase
   - Search for `from app.services.brokers.kis import KISClient`
   - No changes needed (facade maintains API)

8. **Add tests** for sub-clients
   - Test each sub-client in isolation
   - Test facade delegation

---

## Stage 3: MCP Pipeline Normalization

### Goal
Consolidate legacy vs tvscreener code paths in `analysis_screen_core.py` (2,490 LOC).

### Current Structure
- 3 legacy paths: `_screen_kr()`, `_screen_us()`, `_screen_crypto()`
- 4 tvscreener paths: `_screen_kr_via_tvscreener()`, `_screen_us_via_tvscreener()`, `_enrich_crypto_indicators()`, `_screen_crypto_via_tvscreener()`

### Consolidation Plan

#### Phase 1: Extract Shared Logic
1. `_filter_crypto_candidates()` - Warning + crash filtering
2. `_enrich_coingecko_market_cap()` - CoinGecko enrichment
3. `_sort_crypto_results()` - RSI bucket sorting
4. `_map_crypto_symbols_to_tradingview()` - Symbol mapping

#### Phase 2: Unify Crypto Screening
```python
async def _screen_crypto_unified(
    use_tvscreener: bool = True, ...
) -> dict[str, Any]:
    """Unified crypto screening with fallback."""
    if use_tvscreener:
        try:
            return await _screen_crypto_via_tvscreener(...)
        except (TvScreenerError, TvScreenerTimeoutError):
            logger.warning("tvscreener failed, falling back to legacy")
    return await _screen_crypto_legacy(...)
```

#### Phase 3: Unify KR/US Screening
- `_screen_kr_unified(use_tvscreener=True)`
- `_screen_us_unified(use_tvscreener=True)`

### Implementation Steps
1. Extract crypto filtering helpers
2. Create unified crypto screening function
3. Create unified KR screening function
4. Create unified US screening function
5. Update callers to use unified functions
6. Remove legacy functions after validation

---

## Stage 4: Notification Layer Separation

### Goal
Separate formatter and transport layers in `trade_notifier.py` (1,832 LOC).

### Current Structure
- `TradeNotifier` class contains both formatting and sending logic
- 16 formatter methods mixed with 5 transport methods
- Tight coupling between layers

### Target Structure
```
app/monitoring/
├── formatters/
│   ├── __init__.py
│   ├── discord.py      # DiscordEmbedFormatter
│   ├── telegram.py     # TelegramMarkdownFormatter
│   └── templates.py    # TemplateRegistry (colors, emoji, titles)
├── transports/
│   ├── __init__.py
│   ├── telegram.py     # TelegramTransport
│   ├── discord.py      # DiscordTransport
│   └── router.py       # TransportRouter
├── trade_notifier.py   # TradeNotifier (orchestration only)
└── __init__.py
```

### Formatter Interface
```python
class MessageFormatter(Protocol):
    def format_buy_notification(self, data: BuyNotificationData) -> FormattedMessage: ...
    def format_sell_notification(self, data: SellNotificationData) -> FormattedMessage: ...
    # ... 8 more format methods
```

### Transport Interface
```python
class MessageTransport(Protocol):
    async def send(self, message: FormattedMessage) -> bool: ...
    async def test_connection(self) -> bool: ...
```

### Implementation Steps
1. Create `formatters/templates.py` with `TemplateRegistry`
2. Create `formatters/discord.py` with `DiscordEmbedFormatter`
3. Create `formatters/telegram.py` with `TelegramMarkdownFormatter`
4. Create `transports/telegram.py` with `TelegramTransport`
5. Create `transports/discord.py` with `DiscordTransport`
6. Create `transports/router.py` with `TransportRouter`
7. Update `TradeNotifier` to use injected dependencies
8. Add tests for formatters and transports

---

## Stage 5: Fundamentals + Naver Cleanup

### Goal
Eliminate duplicates and extract shared utilities in fundamentals sources.

### Current Issues
- 310 LOC of duplicated Finnhub functions in `naver.py`
- Inconsistent parsing helpers across files
- Hardcoded values requiring configuration

### Consolidation Plan

#### Phase 1: Eliminate Duplicates (310 LOC saved)
- Delete `_get_finnhub_client()`, `_fetch_news_finnhub()`, etc. from `naver.py`
- Import from `fundamentals_sources_finnhub.py`

#### Phase 2: Extract Shared Utilities
- `get_base_ticker()` - Extract to `shared.py`
- `dedupe_tickers_by_base()` - Extract to `shared.py`
- Response builders - `build_news_response()`, etc.
- Async helpers - `async_call_sync()`, `async_http_get()`
- Calculation helpers - `calculate_52w_position()`, `calculate_metric_rank()`

#### Phase 3: Configuration Migration
- Move hardcoded values to `settings`
- HTTP timeouts, API URLs, date ranges, limits

### Implementation Steps
1. Delete Finnhub duplicates from `naver.py`
2. Add imports from `finnhub.py`
3. Extract `get_base_ticker()` to `shared.py`
4. Extract `dedupe_tickers_by_base()` to `shared.py`
5. Create response builder functions
6. Create async helper functions
7. Migrate hardcoded values to settings

---

## Risk Mitigation

### Stage 2 Risks
- **Import breakage**: Use facade pattern to maintain backward compatibility
- **Test coverage**: Add tests for each sub-client before integration

### Stage 3 Risks
- **Behavior changes**: Run parallel comparison tests before removing legacy
- **tvscreener availability**: Keep fallback logic

### Stage 4 Risks
- **Message format changes**: Use exact same formatting logic initially
- **Delivery failures**: Maintain existing fallback patterns

### Stage 5 Risks
- **API changes**: No functional changes, only code organization
- **Import breakage**: Update all imports systematically

---

## Success Criteria

### Stage 2
- [ ] All 30 public methods available via facade
- [ ] Each sub-client testable in isolation
- [ ] No changes to caller code required
- [ ] All existing tests pass

### Stage 3
- [ ] Unified functions produce identical results to separate paths
- [ ] Fallback logic works when tvscreener unavailable
- [ ] Reduced LOC by ~500

### Stage 4
- [ ] Formatters and transports independently testable
- [ ] Message formats unchanged
- [ ] Delivery success rate maintained

### Stage 5
- [ ] No duplicate functions across files
- [ ] All hardcoded values in settings
- [ ] Reduced `naver.py` from 908 to ~320 LOC

---

## Timeline

| Stage | Estimated Effort | Dependencies |
|-------|------------------|--------------|
| Stage 2 | 2-3 days | Stage 1 complete |
| Stage 3 | 1-2 days | None |
| Stage 4 | 1-2 days | None |
| Stage 5 | 1 day | None |

**Total Estimated Effort:** 5-8 days
