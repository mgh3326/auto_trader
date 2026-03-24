# GEMINI.md

This file provides guidance for Gemini CLI when working with the `auto-trader` repository.

## Project Overview

`auto-trader` is an AI-powered automated trading analysis system that collects financial data from various markets and provides investment analysis using Google Gemini AI.

**Key Features:**
- **Multi-market Support:** KR Stocks (KIS), US Stocks (KIS/Yahoo Finance), Crypto (Upbit).
- **Multi-timeframe Analysis:** 200 daily candles + minute candles (60m/5m/1m).
- **AI Analysis:** Structured JSON analysis via Google Gemini API.
- **Smart Rate Limiting:** Redis-based model limiting per API key to handle 429 errors.

## Development Setup

### Prerequisites
- Python 3.13+
- UV (Package Manager)
- PostgreSQL (Database)
- Redis (Caching & Rate Limiting)

### Initial Configuration
```bash
# Install dependencies
uv sync --all-groups

# Environment Variables
cp env.example .env
# Edit .env with your API keys (GOOGLE_API_KEY, KIS_*, UPBIT_*, etc.)

# Database Migrations
uv run alembic upgrade head

# Start Development Server
make dev
```

### Docker Environment
```bash
docker compose up -d              # Start PostgreSQL, Redis, Adminer
docker compose ps                 # Check status
docker compose down               # Stop services
```

## Key Commands

### Testing
- `make test`: Run all tests (excluding live API tests).
- `make test-unit`: Run unit tests only.
- `make test-integration`: Run integration tests.
- `make test-cov`: Run tests with coverage report.
- `uv run pytest tests/test_*.py -v -k "test_name"`: Run specific test.

### Code Quality
- `make lint`: Run Ruff (linting/formatting check) and `ty` (type check).
- `make format`: Format code using Ruff.
- `make typecheck`: Run `ty` type checking.
- `make security`: Run security scans (bandit, safety).

### Database
- `uv run alembic revision --autogenerate -m "message"`: Create new migration.
- `uv run alembic upgrade head`: Apply migrations.
- `uv run alembic downgrade -1`: Rollback last migration.

### Data Sync
- `make sync-kr-symbol-universe`: Sync KR stock symbols.
- `make sync-us-symbol-universe`: Sync US stock symbols.
- `make sync-upbit-symbol-universe`: Sync Upbit coin symbols.

## Architecture

### Analysis System
Located in `app/analysis/`:
- `analyzer.py`: Base `Analyzer` class containing common logic (prompting, AI calls, DB storage, retries).
- `service_analyzers.py`: Service-specific analyzers (Upbit, Yahoo, KIS) that inherit from `Analyzer`.

### Model Rate Limiter
Located in `app/core/model_rate_limiter.py`:
- Uses Redis to track 429 errors per API key/model.
- Automatically switches to alternative models or waits for TTL expiration.

### Symbol Normalization
Located in `app/core/symbol.py`:
- **Standard format:** `SYMBOL.SUFFIX` (e.g., `BRK.B`).
- **KIS format:** `SYMBOL/SUFFIX` (e.g., `BRK/B`).
- **Yahoo format:** `SYMBOL-SUFFIX` (e.g., `BRK-B`).
- Always use `to_db_symbol()` when saving to DB and service-specific converters when calling APIs.

## Important Conventions

- **Async First:** Use `async/await` for I/O operations (API calls, DB queries).
- **Type Hints:** Required for all new code. Use `ty` for validation.
- **Testing:** Add `pytest` markers (`@pytest.mark.unit`, `@pytest.mark.integration`).
- **Database:** Use `app/models/` for SQLAlchemy models and `alembic` for all schema changes.
- **Large Files:** `app/services/kis.py` is very large (~30k lines). Use surgical reads/edits.

## Files NOT to Modify

The following files are part of the core backtesting engine and should not be modified unless explicitly requested:
- `backtest/prepare.py`
- `backtest/backtest.py`

## Directory Structure
- `app/`: Main application code.
- `alembic/`: Database migration scripts.
- `tests/`: Test suite.
- `scripts/`: Utility scripts for data syncing and management.
- `n8n/`: Workflows for automation.
- `docs/`: Technical documentation and plans.
