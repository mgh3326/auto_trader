# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

Auto Trader is an automated trading system that collects and analyzes financial data from Korean stocks (KIS), US stocks (Yahoo Finance), and cryptocurrencies (Upbit). It performs multi-timeframe technical analysis using Google Gemini AI models and provides trading signals through a FastAPI web application.

## Key Commands

### Development
```bash
# Start development server
make dev
# or
poetry run uvicorn app.main:app --reload --host 0.0.0.0 --port 8000

# Install dependencies
poetry install              # Production only
poetry install --with test  # With test dependencies
```

### Testing
```bash
make test                    # Run all tests
make test-unit              # Unit tests only (skip integration)
make test-integration       # Integration tests only
make test-cov               # With coverage report
poetry run pytest tests/ -v # Direct pytest invocation

# Run a single test file
poetry run pytest tests/test_services.py -v

# Run a specific test function
poetry run pytest tests/test_services.py::test_function_name -v
```

### Code Quality
```bash
make lint      # Run flake8, black --check, isort --check, mypy
make format    # Format code with black and isort
make security  # Run bandit and safety checks
```

### Database
```bash
# Run migrations
poetry run alembic upgrade head

# Create new migration
poetry run alembic revision --autogenerate -m "description"

# Rollback one migration
poetry run alembic downgrade -1
```

### Docker
```bash
# Start infrastructure (PostgreSQL + Redis + Adminer)
docker-compose up -d

# Stop infrastructure
docker-compose down

# View logs
docker logs auto_trader_pg
docker logs auto_trader_redis
```

## Architecture

### Service Layer Pattern
The codebase uses a service-oriented architecture where external APIs are abstracted into service modules:

- **app/services/kis.py**: Korea Investment & Securities API client with Redis-based token management
- **app/services/upbit.py**: Upbit cryptocurrency exchange API client
- **app/services/yahoo.py**: Yahoo Finance API wrapper using yfinance
- **app/services/redis_token_manager.py**: Centralized Redis-based token management with distributed locking

### Analyzer Pattern
Three analyzer classes (UpbitAnalyzer, YahooAnalyzer, KISAnalyzer) in `app/analysis/service_analyzers.py` inherit from a base `Analyzer` class in `app/analysis/analyzer.py`. Each analyzer:

1. Collects data from its respective service (OHLCV + fundamentals + minute candles)
2. Generates AI prompts using `app/analysis/prompt.py`
3. Calls Google Gemini API with smart retry logic (tries gemini-2.5-pro → gemini-2.5-flash → gemini-2.0-flash)
4. Saves results to PostgreSQL via SQLAlchemy async models

### Multi-Timeframe Analysis
The system collects and analyzes data across multiple timeframes:

- **Daily candles (200)**: Long-term trend analysis
- **60-min candles (12)**: Medium-term directional analysis
- **5-min candles (12)**: Short-term momentum analysis
- **1-min candles (10)**: Ultra-short-term volatility detection

Note: KIS minute chart API has known limitations (time_unit parameter doesn't work correctly). The code works around this by collecting 1-minute data and aggregating it into 5-min and 60-min candles.

### Redis Token Management
KIS API tokens are managed centrally via Redis with:
- Distributed locking to prevent concurrent token refresh
- Automatic token expiration handling (EGW00123, EGW00121 error codes)
- Retry logic with token refresh on authentication failures

### Rate Limiting
Google Gemini API calls use a sophisticated rate limiter (`app/core/model_rate_limiter.py`) that:
- Tracks rate limits per model per API key in Redis
- Automatically switches to the next API key on 429 errors
- Falls back to lower-tier models on quota exhaustion
- Parses retry_delay from Google API error responses

### Database Models
Two primary analysis result storage patterns:

1. **PromptResult** (`app/models/prompt.py`): Legacy text-based analysis results
2. **StockAnalysisResult** (`app/models/analysis.py`): Structured JSON analysis with decision/confidence/price ranges, linked to **StockInfo** table

## Important Configuration

### Environment Variables
Key variables in `.env` (see `env.example`):

- `DATABASE_URL`: PostgreSQL connection string (required for async: postgresql+asyncpg://...)
- `REDIS_URL`: Redis connection URL (or use individual redis_host/redis_port settings)
- `KIS_APP_KEY`, `KIS_APP_SECRET`: Korea Investment & Securities API credentials
- `UPBIT_ACCESS_KEY`, `UPBIT_SECRET_KEY`: Upbit API credentials
- `GOOGLE_API_KEY`: Single Gemini API key
- `GOOGLE_API_KEYS`: Comma-separated list of Gemini API keys for rotation
- `TELEGRAM_TOKEN`, `TELEGRAM_CHAT_IDS_STR`: Telegram bot configuration

### Settings Behavior
`app/core/config.py` Settings class:
- Uses pydantic-settings with `.env` file loading
- `get_random_key()`: Returns random API key from pool
- `get_next_key()`: Returns next API key in rotation (circular)
- API keys validator splits comma-separated string into list

## Common Patterns

### Adding a New Analyzer
1. Create service client in `app/services/` (follow kis.py pattern with async methods)
2. Create analyzer class in `app/analysis/service_analyzers.py` inheriting from `Analyzer`
3. Implement `_collect_*_data()` method for data collection
4. Use `analyze_and_save()` or `analyze_and_save_json()` from base class

### Working with KIS API
- Always use `await kis.kis._ensure_token()` before API calls (already built into methods)
- Handle token expiration errors (EGW00123, EGW00121) with retry after clearing Redis token
- Use `inquire_daily_itemchartprice()` for daily/weekly/monthly candles
- Use `fetch_minute_candles()` for minute-level data (returns dict with 60min/5min/1min keys)

### JSON Analysis vs Text Analysis
- JSON analysis: Use `analyze_and_save_json()` → returns `StockAnalysisResponse` → saves to `StockAnalysisResult` table
- Text analysis: Use `analyze_and_save()` → returns string → saves to `PromptResult` table
- JSON schema is defined in `app/analysis/models.py` using Pydantic

### Working with Stock Info
Static data stored in `data/stocks_info/` and `data/coins_info/`:
- `KRX_NAME_TO_CODE`: Maps Korean stock names to codes
- `upbit_pairs.NAME_TO_PAIR_KR`: Maps Korean coin names to trading pairs
- Use `await upbit_pairs.prime_upbit_constants()` before accessing Upbit coin lists

## Testing Notes

- Tests use pytest-asyncio for async test functions
- Markers: `@pytest.mark.unit`, `@pytest.mark.integration`, `@pytest.mark.slow`
- Fixtures in `tests/conftest.py` provide mocked services and database sessions
- Integration tests may require running infrastructure: `docker-compose up -d`

## API Limitations

### KIS (Korea Investment & Securities)
- Minute chart API `time_unit` parameter is currently non-functional (API bug)
- Workaround: Collect 1-minute data and aggregate to other timeframes
- Token expires after ~24 hours (tracked in Redis)

### Upbit
- WebSocket support available (see `UPBIT_WEBSOCKET_README.md`)
- Order placing requires sufficient KRW balance (min: `upbit_buy_amount + 5000`)

### Google Gemini
- Model priority: gemini-2.5-pro → gemini-2.5-flash → gemini-2.0-flash
- Rate limit handling via Redis prevents excessive API calls
- JSON mode requires response_schema configuration
