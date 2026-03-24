# GEMINI.md

## Project Overview
**Auto Trader** is a comprehensive automatic trading system designed for Korean (KR) stocks, US stocks, and Cryptocurrencies. It integrates multiple data sources (Upbit, KIS, Yahoo Finance, KRX, Naver Finance) and utilizes AI (Google Gemini) for technical and news analysis.

### Main Technologies
- **Language:** Python 3.13+
- **Framework:** FastAPI (Web API), TaskIQ (Distributed Task Queue)
- **Data Management:** PostgreSQL (SQLAlchemy + Alembic), Redis (Caching & Task Broker)
- **Package Manager:** [UV](https://github.com/astral-sh/uv)
- **Integrations:**
  - **Crypto:** Upbit API
  - **KR Stocks:** Korea Investment & Securities (KIS) API, KRX Information Data System
  - **US Stocks:** Yahoo Finance, TVScreener, Finnhub
  - **AI:** Google Gemini API
  - **Monitoring:** Sentry, Discord/Telegram Webhooks
  - **MCP:** FastMCP for Model Context Protocol exposure

### Architecture
- **`app/`**: Core application logic.
  - `auth/` & `middleware/`: Authentication (JWT) and request gating.
  - `routers/`: FastAPI endpoint definitions.
  - `services/`: Business logic and external provider integrations (KIS, Upbit, etc.).
  - `jobs/` & `tasks/`: Background job orchestration and TaskIQ task declarations.
  - `models/` & `schemas/`: SQLAlchemy DB models vs Pydantic API schemas.
  - `mcp_server/`: MCP tool implementations.
- **`scripts/`**: Maintenance scripts for symbol syncing, candle backfilling, and report ingestion.
- **`tests/`**: Comprehensive test suite with unit, integration, and live API tests.
- **`alembic/`**: Database migration scripts.

---

## Building and Running

### Prerequisites
- Python 3.13+
- [UV](https://github.com/astral-sh/uv) installed
- PostgreSQL and Redis instances

### Setup
1. **Install Dependencies:**
   ```bash
   uv sync --all-groups
   ```
2. **Environment Variables:**
   Copy `env.example` to `.env` and fill in the required API keys and database URLs.
3. **Database Migrations:**
   ```bash
   uv run alembic upgrade head
   ```

### Running the Application
- **Development Server:**
  ```bash
  make dev
  # or
  uv run uvicorn app.main:api --reload
  ```
- **TaskIQ Worker:**
  ```bash
  make taskiq-worker
  ```
- **TaskIQ Scheduler:**
  ```bash
  make taskiq-scheduler
  ```

---

## Development Conventions

### Coding Standards
- **Formatting & Linting:** Strictly follow [Ruff](https://github.com/astral-sh/ruff).
  - Run `make format` to format code.
  - Run `make lint` to check for linting errors.
- **Type Checking:** Use `ty` (wrapper for pyright/basedpyright) via `make typecheck`.
- **Logic Separation:** Keep routers thin. Delegate business logic to services and orchestration to jobs.
- **Database:** Always use Alembic for schema changes. Entities go in `app/models/`.

### Testing Practices
- **Test Discovery:** `pytest` is used. All tests reside in `tests/`.
- **Markers:**
  - `@pytest.mark.unit`: Fast, isolated tests.
  - `@pytest.mark.integration`: Tests requiring DB/Redis but not external APIs.
  - `@pytest.mark.live`: Tests that call external APIs (require `--run-live` flag).
- **Execution:**
  - Fast Gate (No Live): `make test`
  - Unit Tests Only: `make test-unit`
  - Live API Tests: `make test-live`
  - Coverage: `make test-cov`

### Symbols & Markets
- **KR Market:** Use `scripts/sync_kr_symbol_universe.py` to update the local symbol database.
- **US Market:** Use `scripts/sync_us_symbol_universe.py`.
- **Crypto:** Use `scripts/sync_upbit_symbol_universe.py`.
- Symbols are generally normalized to consistent formats (e.g., `KRW-BTC`, `AAPL`, `005930`).

---

## Key Files & Directories
- `app/main.py`: Application entry point and lifecycle management.
- `app/core/config.py`: Centralized configuration management using Pydantic Settings.
- `app/AGENTS.md`: Detailed guidance for AI agents working on the `app/` directory.
- `Makefile`: Convenient shortcuts for common development tasks.
- `pyproject.toml`: Project metadata, dependencies, and tool configurations.
